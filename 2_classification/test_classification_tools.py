"""
Tests for classification_tools.py

Strategy:
  - GitHub is a fake client that returns a response for each URL from a map and records each
    request with its media type. The outline is built from git trees written out by hand.
  - The tool reads a web page from a fake session. A replaced name resolution makes an address
    public or private as the case specifies.
  - The summarizing model is a fake OpenAI client that records the request and returns a fixed
    summary and token counts.
  - The server is called in process, through the SDK's own list_tools and call_tool.

Requirements: Python 3.14, mcp, pydantic, requests, html2text (requirements.txt)
Run with:  python3 -m unittest test_classification_tools -v
"""

import asyncio
import base64
import contextlib
import gzip
import io
import json
import os
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classification_tools as tools  # noqa: E402
import endpoints  # noqa: E402
import openai  # noqa: E402
import readmes  # noqa: E402
import urllib3  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from agentcontext.github_api import NoTokens  # noqa: E402

REVISION = "c" * 40
API = "https://api.github.com/repos/o/r"


class _Answer:

    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self.payload, self.content = status, payload, content
        self.headers = {}

    def json(self):
        return self.payload


class _GitHub:
    """GitHub's API, returning a response for each URL from a map, and for a GraphQL query from
    the map's entry ("graphql", "owner/repo"). An unknown URL returns 404, and an unknown
    repository GraphQL's null."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, url, accept=readmes.JSON, etag=""):
        self.asked.append((url, accept))
        answer = self.answers.get((url, accept), self.answers.get(url))
        if isinstance(answer, Exception):
            raise answer
        return answer or _Answer(404, {"message": "Not Found"})

    def query(self, query, variables):
        name = f"{variables['owner']}/{variables['name']}"
        self.asked.append(("graphql", name))
        answer = self.answers.get(("graphql", name))
        if isinstance(answer, Exception):
            raise answer
        return answer or _Answer(200, {"data": {"repository": None}})


class _Page:
    """A response that streams its body as urllib3's response does, in the content coding that
    the headers name."""

    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.closed = False
        self.raw = SimpleNamespace(stream=self.stream)

    def stream(self, size, decode_content=None):
        if decode_content is not False:
            raise AssertionError("the body is decoded by the tool, not by urllib3")
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        self.closed = True


class _Web:

    def __init__(self, pages):
        self.pages, self.asked, self.headers = pages, [], []

    def get(self, url, **kwargs):
        self.asked.append(url)
        self.headers.append(kwargs.get("headers", {}))
        return self.pages[url]


class _Summarizer:
    """A summarizer that records what it was given."""

    def __init__(self):
        self.given = []

    async def __call__(self, repo_name, url, text):
        self.given.append((repo_name, url, text))
        return {"summary": f"A summary of {url}.", "cut": False, "input_tokens": 120,
                "output_tokens": 30, "model": "gpt-6-luna-2026-09-01", "cached_tokens": 40,
                "cache_write_tokens": 60, "service_tier": "default"}


def _resolving(addresses):
    """getaddrinfo returning for each host the address that the case names."""
    def getaddrinfo(host, port):
        return [(2, 1, 6, "", (addresses[host], 0))]
    return mock.patch.object(tools.socket, "getaddrinfo", getaddrinfo)


def _tree(paths, truncated=False):
    """A git tree as GitHub's trees API lists it recursively: every file of `paths`, every
    directory above one, and the paths ending in "@" as submodules."""
    entries, directories = [], set()
    for path in paths:
        name = path.rstrip("@")
        parts = name.split("/")
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]))
        entries.append({"path": name, "type": "commit" if path.endswith("@") else "blob"})
    entries += [{"path": d, "type": "tree"} for d in sorted(directories)]
    return {"tree": entries, "truncated": truncated}


SMALL = _tree(["src/app.py", "src/util.py", "src/data.json", "tests/test_app.py",
               ".github/workflows/ci.yml", "README.md", "pyproject.toml", "vendor/lib@"])


class TestTheCommandLine(unittest.TestCase):

    def test_an_abbreviated_option_is_a_usage_error(self):
        """The server takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with mock.patch.object(tools, "build_server"), \
                mock.patch.object(tools.logs, "capture_libraries"), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            tools.main(["--summary-m", "gpt-6-luna"])
        self.assertEqual(stop.exception.code, 2)


class TestTheServerEndsWithTheClassification(unittest.TestCase):
    """The MCP SDK starts the server in a session of its own, and a stop of the classification
    does not reach the server. The server also holds open the pipe of the step's output, and the
    runner reads that pipe to its end."""

    # STEP starts the server as the SDK does, with the step's output and in a session of its
    # own, and waits. The server's `main` runs a stand-in server that spends a minute in a tool
    # call, as a wait for GitHub's rate limit does.
    STEP = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1]], start_new_session=True)\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n")
    SERVER = ("import time\n"
              "import classification_tools as tools\n"
              "class Server:\n"
              "    def run(self, transport):\n"
              "        time.sleep(60)\n"
              "tools.build_server = lambda **kwargs: Server()\n"
              "tools.Summarizer = lambda **kwargs: None\n"
              "tools.main([])\n")

    def test_a_stop_of_the_classification_ends_the_server(self):
        here = Path(__file__).resolve().parent
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(here), str(here.parent)])}
        step = subprocess.Popen([sys.executable, "-c", self.STEP, self.SERVER],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.addCleanup(step.kill)
        self.assertEqual(step.stdout.readline(), b"started\n")
        time.sleep(1)
        step.terminate()
        step.wait(timeout=10)
        ended = threading.Event()
        threading.Thread(target=lambda: (step.stdout.read(), ended.set()), daemon=True).start()
        self.assertTrue(ended.wait(timeout=15), "the server still holds the step's output")


class TestInput(unittest.TestCase):

    def test_a_repository_name_is_owner_and_repo(self):
        """A repository's name may begin with a hyphen, as Hackerprod/-SKYNET-Steam-Emulator in
        the sampling frame does. A refused name would leave the model without the outline that it
        requested. An owner's name may not begin with a hyphen."""
        self.assertEqual(tools.checked_repo_name(" o/r.js "), "o/r.js")
        self.assertEqual(tools.checked_repo_name("Hackerprod/-SKYNET-Steam-Emulator"),
                         "Hackerprod/-SKYNET-Steam-Emulator")
        for bad in ("o", "o/r/x", "../etc", "o/..", "-o/r", "o/r;rm", "o§r", "", "o/ r"):
            with self.subTest(bad=bad), self.assertRaises(tools.InvalidInput):
                tools.checked_repo_name(bad)

    def test_the_server_refuses_bad_input_with_a_message_for_the_model(self):
        server = tools.build_server(github=_GitHub({}))
        for arguments in ({"repo_name": "../etc", "revision": ""},
                          {"repo_name": "o/r", "revision": "HEAD; rm -rf"},
                          {"repo_name": "o/r", "revision": "", "max_chars": -1}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                asyncio.run(server.call_tool("get_file_tree", arguments))

    def test_the_server_lists_the_four_tools_with_their_descriptions_and_schemas(self):
        """The server lists every tool to any MCP client. The classification offers the model
        `read_linked_page` alone, with its description. It reads the results of the other three
        itself and puts them in the input, and the classification parameters record their
        output schemas."""
        listed = asyncio.run(tools.build_server().list_tools())
        self.assertEqual({t.name: t.description for t in listed},
                         {"get_file_tree": tools.FILE_TREE_DESCRIPTION,
                          "read_linked_page": tools.LINKED_PAGE_DESCRIPTION,
                          "get_repository_metadata": tools.METADATA_DESCRIPTION,
                          "get_issues_and_pull_requests": tools.ACTIVITY_DESCRIPTION})
        self.assertEqual({t.name: sorted(t.input_schema["properties"]) for t in listed},
                         {"get_file_tree": ["max_chars", "repo_name", "revision"],
                          "read_linked_page": ["repo_name", "url"],
                          "get_repository_metadata": ["repo_name"],
                          "get_issues_and_pull_requests": ["repo_name"]})
        self.assertEqual({t.name: sorted(t.output_schema["properties"]) for t in listed}, {
            "get_file_tree": ["available", "file_tree", "note", "repo_name", "revision"],
            "read_linked_page": ["available", "cache_write_tokens", "cached_tokens",
                                 "input_tokens", "model", "note", "other_repository",
                                 "output_tokens", "purpose_sentence", "service_tier", "summary",
                                 "url"],
            "get_repository_metadata": ["available", "created_at", "description", "has_issues",
                                        "homepage", "is_template", "license", "note",
                                        "owner_description", "pushed_at", "repo_name", "topics"],
            "get_issues_and_pull_requests": ["available", "closed_issues",
                                             "closed_pull_requests", "has_issues",
                                             "merged_pull_requests", "note", "open_issues",
                                             "open_pull_requests", "pinned_issues",
                                             "recent_issues", "recent_pull_requests",
                                             "repo_name"]})


class TestOutline(unittest.TestCase):

    def test_the_outline_of_a_small_repository(self):
        text, note = tools.outline(SMALL, {"Python": 900, "Shell": 100})
        self.assertEqual(text.splitlines(), [
            "Files: 7, directories: 5, deepest path: 3 levels, submodules: 1",
            "Languages: Python 90.0%, Shell 10.0%",
            "Top level:",
            "  src/ (3 files: .py 2, .json 1)",
            "  .github/ (1 file: .yml 1)",
            "  tests/ (1 file: .py 1)",
            "  README.md, pyproject.toml",
            "Paths that show engineering practice:",
            "  tests: 1 file (tests/test_app.py)",
            "  ci: 1 file (.github/workflows/ci.yml)",
            "  build and packaging: 1 file (pyproject.toml)"])
        self.assertEqual(note, "")

    def test_nothing_is_left_out_without_a_limit(self):
        """Rule 12: every directory, every root file, every language and every file of a
        practice."""
        paths = [f"tests/test_{i:03}.py" for i in range(300)] + \
                [f"d{i:02}/f.c" for i in range(80)] + [f"root{i:02}.txt" for i in range(70)]
        languages = {f"L{i}": 100 - i for i in range(15)}
        text, note = tools.outline(_tree(paths), languages)
        self.assertEqual(note, "")
        self.assertIn("tests: 300 files (", text)
        self.assertIn("tests/test_299.py", text)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("  d")), 80)
        self.assertIn("root69.txt", text)
        self.assertIn("L14 ", text)

    def test_only_documents_show_contribution_guidelines(self):
        paths = ["CONTRIBUTING.md", "docs/SECURITY.rst", "GOVERNANCE", "app/middleware/security.py",
                 "src/pages/Governance.tsx", "src/assets/svgs/story/security.svg",
                 "shared/services/security.go", "Security.java"]
        text, _ = tools.outline(_tree(paths), {})
        self.assertIn("contribution: 3 files (CONTRIBUTING.md, GOVERNANCE, docs/SECURITY.rst)", text)

    def test_a_truncated_list_and_no_language_are_stated_in_the_outline(self):
        text, _ = tools.outline(_tree(["a.txt"], truncated=True), {})
        self.assertIn("(GitHub listed only part of the tree)", text.splitlines()[0])
        self.assertIn("Languages: none detected", text)
        self.assertIn("Paths that show engineering practice: none found", text)

    def test_over_the_limit_the_lists_of_files_are_shortened_first(self):
        """Rule 1: the lists keep the same number of files each, as many as fit."""
        paths = [f"tests/test_{i:02}.py" for i in range(30)] + [".github/workflows/ci.yml"]
        whole, _ = tools.outline(_tree(paths), {"Python": 1})
        # Dropping one file from the list of tests removes "tests/test_29.py, " (18 characters)
        # and adds ", and 1 more" (12 characters). The outline gets 6 characters shorter and fits
        # in a limit one less than the whole outline.
        text, note = tools.outline(_tree(paths), {"Python": 1}, len(whole) - 1)
        self.assertLessEqual(len(text), len(whole) - 1)
        self.assertIn("tests: 30 files (tests/test_00.py", text)
        self.assertIn("tests/test_28.py, and 1 more)", text)
        self.assertIn("ci: 1 file (.github/workflows/ci.yml)", text)
        self.assertIn("at most 29 files each", note)

    def test_when_the_lists_do_not_suffice_the_outline_is_cut(self):
        text, note = tools.outline(SMALL, {"Python": 1}, 50)
        whole, _ = tools.outline(SMALL, {"Python": 1})
        self.assertEqual(text, whole[:50])
        self.assertIn("cut after 50 characters", note)


class TestFileTree(unittest.TestCase):

    def test_the_outline_is_read_at_the_revision(self):
        github = _GitHub({f"{API}/git/trees/{REVISION}?recursive=1": _Answer(200, SMALL),
                          f"{API}/languages": _Answer(200, {"Python": 3})})
        result = tools.file_tree(github, "o/r", REVISION)
        self.assertTrue(result.available, result.note)
        self.assertIn("Languages: Python 100.0%", result.file_tree)
        self.assertEqual([url for url, _ in github.asked],
                         [f"{API}/git/trees/{REVISION}?recursive=1", f"{API}/languages"])

    def test_without_a_revision_the_default_branch(self):
        github = _GitHub({f"{API}/git/trees/HEAD?recursive=1": _Answer(200, SMALL)})
        self.assertTrue(tools.file_tree(github, "o/r").available)

    def test_a_status_states_its_cause_and_a_failure_of_github_is_raised_for_the_classification(
            self):
        """A 404 concerns the repository, and the result reports the 404 to the model. A missing
        response of GitHub says nothing about the repository. Reported to the model as an
        unavailable outline, the missing response would let a label be made and cached without
        the outline."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIn("HTTP 404", tools.file_tree(_GitHub({}), "o/r", REVISION).note)
        self.assertEqual(err.getvalue(), "")
        github = _GitHub({f"{API}/git/trees/{REVISION}?recursive=1":
                          readmes.NoResponse("GitHub returned HTTP 502")})
        server = tools.build_server(github=github)
        with contextlib.redirect_stderr(err), self.assertRaises(ToolError) as raised:
            asyncio.run(server.call_tool("get_file_tree", {"repo_name": "o/r",
                                                           "revision": REVISION}))
        self.assertEqual(tools.failure_of(str(raised.exception)),
                         (tools.GITHUB_FAILED, "NoResponse (GitHub returned HTTP 502)"))
        self.assertEqual(err.getvalue(), "")

    def test_github_refusing_every_github_access_token_is_raised_for_the_classification(self):
        """With no GitHub access token left, every tool call of the server fails, and the labels
        that the model made without the tools would be cached."""
        class Refusing(_GitHub):
            def get(self, url, accept=readmes.JSON, etag=""):
                raise NoTokens("GitHub accepts none of the configured GitHub access tokens")
        server = tools.build_server(github=Refusing({}))
        for name, arguments in (("get_file_tree", {"repo_name": "o/r", "revision": REVISION}),
                                ("get_repository_metadata", {"repo_name": "o/r"}),
                                ("read_linked_page", {"url": "https://github.com/o/r",
                                                      "repo_name": "o/r"})):
            with self.subTest(tool=name), self.assertRaises(ToolError) as raised:
                asyncio.run(server.call_tool(name, arguments))
            self.assertEqual(tools.failure_of(str(raised.exception))[0],
                             tools.NO_GITHUB_ACCESS_TOKEN)

    def test_the_server_reads_the_credentials_file_that_its_command_line_names(self):
        """The classification passes its credentials file to the server, so that the server
        sends the GitHub access tokens of a file outside its checkout with its requests to
        GitHub."""
        sent, served = [], {}

        def get(url, headers=None, timeout=None):
            sent.append((headers or {}).get("Authorization", ""))
            return _Answer(200, {"full_name": "o/r", "description": "A tool."})

        def run(server, transport):
            served.update(asyncio.run(server.call_tool(
                "get_repository_metadata", {"repo_name": "o/r"})).structured_content)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "elsewhere.csv"
            path.write_text("service,account,credential\ngithub,someone,ghp_fromfile\n",
                            encoding="utf-8")
            environment = {"GITHUB_TOKENS": "", "GITHUB_TOKEN": "",
                           "AGENTCONTEXT_CREDENTIALS": os.devnull}
            with mock.patch.dict(os.environ, environment), \
                    mock.patch.object(tools, "_github_client", None), \
                    mock.patch.object(tools, "exit_with_parent", lambda pid: None), \
                    mock.patch.object(tools.MCPServer, "run", run), \
                    mock.patch.object(readmes.requests, "get", get):
                self.assertEqual(tools.main(["--credentials", str(path)]), 0)
        self.assertEqual((served["available"], served["description"]), (True, "A tool."))
        self.assertEqual(set(sent), {"bearer ghp_fromfile"})

    def test_a_spell_in_which_github_rejects_every_github_access_token_is_waited_out(self):
        """GitHub can reject valid GitHub access tokens for a while, also for their rate limit.
        Once GitHub has rejected every GitHub access token, the server's client checks the
        configured GitHub access tokens again after each wait, and the call returns its result.
        Reported as no GitHub access token left, the rejections would stop the run."""
        sent = []

        def get(url, headers=None, timeout=None):
            sent.append(url)
            if len(sent) <= 4:
                return _Answer(401, {"message": "Bad credentials"})
            return _Answer(200, {"full_name": "o/r", "description": "A tool."})
        environment = {"GITHUB_TOKENS": "t1,t2", "GITHUB_TOKEN": "",
                       "AGENTCONTEXT_CREDENTIALS": os.devnull}
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(tools, "_github_client", None), \
                mock.patch.object(readmes.requests, "get", get), \
                mock.patch.object(readmes, "RECHECK_WAITS", (0, 0, 0, 0, 0)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = asyncio.run(tools.build_server().call_tool(
                "get_repository_metadata", {"repo_name": "o/r"})).structured_content
        self.assertEqual((result["available"], result["description"]), (True, "A tool."))
        self.assertEqual(sent, [API, readmes.RATE_LIMIT_URL] * 2 + [readmes.RATE_LIMIT_URL] * 2
                         + [API])
        self.assertIn("GitHub rejects every GitHub access token (HTTP 401)", err.getvalue())


class TestReadWeb(unittest.TestCase):

    def test_html_is_read_as_text_and_text_is_requested(self):
        web = _Web({"https://docs.example/": _Page(200, b"<html><body><h1>Guide</h1>"
                                                         b"<p>Install with <a href='x'>pip</a>."
                                                         b"</p><script>x()</script></body></html>",
                                                   {"Content-Type": "text/html; charset=utf-8"})})
        with _resolving({"docs.example": "93.184.216.34"}):
            text, read_at, note = tools.read_web("https://docs.example/", web)
        self.assertEqual((read_at, note), ("https://docs.example/", ""))
        self.assertIn("# Guide", text)
        self.assertIn("Install with pip.", text)
        self.assertNotIn("](x)", text)
        self.assertEqual(web.headers[0]["Accept"], tools.ACCEPT)

    def test_an_address_off_the_public_internet_is_not_read(self):
        """A README can name any address, and the stage runs on a network with other
        machines."""
        web = _Web({})
        for host, address in (("nas.local", "192.168.1.10"), ("loop", "127.0.0.1"),
                              ("meta", "169.254.169.254"), ("six", "::1")):
            with self.subTest(host=host), _resolving({host: address}):
                text, _, note = tools.read_web(f"http://{host}/", web)
                self.assertIsNone(text)
                self.assertIn("not on the public internet", note)
        self.assertEqual(web.asked, [])

    def test_a_redirect_is_followed_and_checked_like_the_first_address(self):
        web = _Web({"https://a.example/": _Page(302, headers={"Location": "http://inner/"}),
                    "http://inner/": _Page(200, b"secret", {"Content-Type": "text/plain"})})
        with _resolving({"a.example": "93.184.216.34", "inner": "10.0.0.5"}):
            text, read_at, _ = tools.read_web("https://a.example/", web)
        self.assertIsNone(text)
        self.assertEqual(read_at, "http://inner/")
        self.assertEqual(web.asked, ["https://a.example/"])

    def test_the_check_reads_the_host_that_the_request_connects_to(self):
        """GitHub renders a Markdown link to "http://10.0.0.5\\@docs.example/" with the host
        docs.example, and urlsplit reads that host too, while the request connects to 10.0.0.5.
        The request also removes the dot segment of the second address, so that such a link
        chooses the path on the host that it connects to."""
        page = _Page(200, b"secret", {"Content-Type": "text/plain"})
        hosts = {"docs.example": "93.184.216.34", "10.0.0.5": "10.0.0.5",
                 "a.example": "93.184.216.34"}
        for url in ("http://10.0.0.5\\@docs.example/",
                    "http://10.0.0.5:8080\\@docs.example/../latest/meta-data/"):
            web = _Web({url: page})
            with self.subTest(url=url), _resolving(hosts):
                text, read_at, note = tools.read_web(url, web)
                self.assertIsNone(text)
                self.assertEqual(read_at, url)
                self.assertIn("not on the public internet", note)
                self.assertEqual(web.asked, [])
        inner = "http://10.0.0.5\\@docs.example/second"
        web = _Web({"https://a.example/": _Page(302, headers={"Location": inner}), inner: page})
        with _resolving(hosts):
            text, read_at, note = tools.read_web("https://a.example/", web)
        self.assertIsNone(text)
        self.assertEqual(read_at, inner)
        self.assertIn("not on the public internet", note)
        self.assertEqual(web.asked, ["https://a.example/"])

    def test_a_meta_refresh_is_followed(self):
        """Rule 6: GitHub Pages sites move a page with a meta tag rather than HTTP."""
        web = _Web({"https://a.example/old": _Page(200, b'<html><head><meta http-equiv="refresh"'
                                                         b' content="0; url=/new"></head></html>',
                                                    {"Content-Type": "text/html"}),
                    "https://a.example/new": _Page(200, b"The new page.",
                                                   {"Content-Type": "text/plain"})})
        with _resolving({"a.example": "93.184.216.34"}):
            text, read_at, _ = tools.read_web("https://a.example/old", web)
        self.assertEqual((text, read_at), ("The new page.", "https://a.example/new"))

    def test_five_redirects_and_refreshes_are_followed_and_a_sixth_is_not(self):
        """Rule 7: HTTP redirects and meta refreshes count together."""
        def chain(hops):
            pages = {}
            for i in range(hops):
                if i % 2:
                    pages[f"https://a.example/{i}"] = _Page(
                        200, f'<meta http-equiv="refresh" content="0;url=/{i + 1}">'.encode(),
                        {"Content-Type": "text/html"})
                else:
                    pages[f"https://a.example/{i}"] = _Page(
                        301, headers={"Location": f"/{i + 1}"})
            pages[f"https://a.example/{hops}"] = _Page(200, b"end", {"Content-Type": "text/plain"})
            return _Web(pages)
        with _resolving({"a.example": "93.184.216.34"}):
            self.assertEqual(tools.read_web("https://a.example/0", chain(5))[0], "end")
            text, _, note = tools.read_web("https://a.example/0", chain(6))
        self.assertIsNone(text)
        self.assertEqual(note, "more than 5 redirects")

    def test_only_text_and_only_http(self):
        """Rule 3: the tool reads only a body of a text type, or of a response that names no
        type."""
        web = _Web({"https://a.example/f.zip": _Page(200, b"PK",
                                                      {"Content-Type": "application/zip"}),
                    "https://a.example/plain": _Page(200, b"no type")})
        with _resolving({"a.example": "93.184.216.34"}):
            self.assertIn("not text", tools.read_web("https://a.example/f.zip", web)[2])
            self.assertEqual(tools.read_web("https://a.example/plain", web)[0], "no type")
            self.assertIsNone(tools.read_web("file:///etc/passwd", web)[0])

    def test_a_download_stops_at_two_megabytes_and_its_note_states_the_limit(self):
        """Rule 9."""
        body = b"word " * (tools.MAX_PAGE_BYTES // 5 + 1000)
        web = _Web({"https://a.example/": _Page(200, body, {"Content-Type": "text/plain"})})
        with _resolving({"a.example": "93.184.216.34"}):
            text, _, note = tools.read_web("https://a.example/", web)
        self.assertEqual(len(text), tools.MAX_PAGE_BYTES - 1)  # the trailing space is stripped
        self.assertIn("cut at 2,000,000 bytes", note)

    def test_a_page_that_trickles_ends_at_the_deadline(self):
        """A server that sends a few bytes within every read keeps each read under its timeout.
        Without a deadline on the whole reading, such a server would hold the tool and the
        worker that labels the repository."""
        clock = SimpleNamespace(now=0.0)

        class Trickling(_Page):
            def stream(self, size, decode_content=None):
                for _ in range(tools.PAGE_DEADLINE * 2):
                    clock.now += 1.0
                    yield b"w"
        page = Trickling(200, headers={"Content-Type": "text/plain"})
        web = _Web({"https://a.example/": page})
        with _resolving({"a.example": "93.184.216.34"}):
            text, _, note = tools.read_web("https://a.example/", web, clock=lambda: clock.now)
        self.assertIsNone(text)
        self.assertEqual(note, f"reading the page took more than {tools.PAGE_DEADLINE} seconds")
        self.assertEqual(clock.now, tools.PAGE_DEADLINE)
        self.assertTrue(page.closed)

    def test_a_server_that_trickles_is_cut_off_at_the_deadline(self):
        """A real server on the loopback address that sends a byte every fifth of a second keeps
        every read under its timeout. Between the checks of the deadline, the client waits for a
        whole chunk of the body, or for the whole status line and headers. Only a shutdown of the
        connection at the deadline therefore ends the reading in time."""
        stop = threading.Event()
        self.addCleanup(stop.set)
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100\r\n"
                b"Connection: close\r\n\r\n")

        class Trickling(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                if self.server.slow_head:
                    sent, slow = b"", head
                else:
                    sent, slow = head, b"x" * 100
                try:
                    self.request.sendall(sent)
                    for byte in slow:
                        if stop.wait(0.2):
                            return
                        self.request.sendall(bytes([byte]))
                except OSError:
                    return

        for slow_head in (False, True):
            with self.subTest(slow_head=slow_head):
                server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Trickling)
                server.daemon_threads, server.slow_head = True, slow_head
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                url = f"http://127.0.0.1:{server.server_address[1]}/"
                started = time.monotonic()
                with mock.patch.object(tools, "PAGE_DEADLINE", 1), \
                        mock.patch.object(tools, "public_address", lambda host: ""):
                    text, read_at, note = tools.read_web(url)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual((text, read_at, note),
                                 (None, url, "reading the page took more than 1 seconds"))

    def test_a_connection_made_after_the_deadline_is_cut_off(self):
        """The timer shuts down the connections that it finds at the deadline. A connection
        that the connect timeout allowed to finish after the deadline would wait for the status
        line and every header on a server that sends a byte within every read timeout."""
        stop = threading.Event()
        self.addCleanup(stop.set)
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100\r\n"
                b"Connection: close\r\n\r\n")

        class Trickling(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                try:
                    for byte in head:
                        if stop.wait(0.2):
                            return
                        self.request.sendall(bytes([byte]))
                except OSError:
                    return

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Trickling)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        new_conn = urllib3.connection.HTTPConnection._new_conn

        def slow_connect(connection):
            time.sleep(1.5)
            return new_conn(connection)

        started = time.monotonic()
        with mock.patch.object(tools, "PAGE_DEADLINE", 1), \
                mock.patch.object(tools, "public_address", lambda host: ""), \
                mock.patch.object(urllib3.connection.HTTPConnection, "_new_conn", slow_connect):
            text, read_at, note = tools.read_web(url)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((text, read_at, note),
                         (None, url, "reading the page took more than 1 seconds"))

    def test_a_body_without_a_length_that_the_deadline_cuts_is_late(self):
        """A plain-http body without a length ends where the server closes the connection, and
        the timer's shutdown at the deadline ends the body the same way. Without a check of the
        deadline, the cut page would be summarized as the whole page."""
        stop = threading.Event()
        self.addCleanup(stop.set)

        class Unfinished(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                try:
                    # Send one whole chunk of the reader before the deadline, so that the
                    # read that the shutdown ends returns no byte.
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                         b"Connection: close\r\n\r\n" + b"x" * 65536)
                    stop.wait(10)
                except OSError:
                    return

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Unfinished)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with mock.patch.object(tools, "PAGE_DEADLINE", 1), \
                mock.patch.object(tools, "public_address", lambda host: ""):
            self.assertEqual(tools.read_web(url),
                             (None, url, "reading the page took more than 1 seconds"))

    def test_a_body_that_the_timer_ends_before_the_clock_reaches_the_deadline_is_late(self):
        """The timer starts before `_read_web` reads the clock for its deadline. A delay
        between the two, such as a wait for the interpreter lock, lets the timer's shutdown end
        a body without a length while the clock is still before the deadline. Here the clock is
        late by such a delay at its first reading."""
        stop = threading.Event()
        self.addCleanup(stop.set)

        class Trickling(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                try:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                         b"Connection: close\r\n\r\n")
                    while not stop.wait(0.01):
                        self.request.sendall(b"x")
                except OSError:
                    return

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Trickling)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        readings = []

        def clock():
            readings.append(None)
            return time.monotonic() + (0.3 if len(readings) == 1 else 0)

        with mock.patch.object(tools, "PAGE_DEADLINE", 0.5), \
                mock.patch.object(tools, "public_address", lambda host: ""):
            self.assertEqual(tools.read_web(url, clock=clock),
                             (None, url, "reading the page took more than 0.5 seconds"))

    def test_an_address_that_is_no_url_is_not_read(self):
        """A link, a redirect or a refresh can name an IPv6 address without its closing
        bracket. Parsing such an address raises ValueError, and an unhandled ValueError would
        give the model an error without the page."""
        web = _Web({"https://a.example/moved": _Page(301, headers={
                        "Location": "http://[2001:db8::1/next"}),
                    "https://a.example/refresh": _Page(200, (
                        b'<meta http-equiv="refresh" content="0; url=http://[2001:db8::1/">'),
                        {"Content-Type": "text/html"})})
        with _resolving({"a.example": "93.184.216.34"}):
            for url in ("http://[2001:db8::1/page", "https://a.example/moved",
                        "https://a.example/refresh"):
                with self.subTest(url=url):
                    self.assertEqual(tools.read_web(url, web), (None, url, tools.NOT_A_URL))

    def test_a_charset_that_cannot_decode_reads_as_utf_8(self):
        for charset in ("undefined", "idna", "no-such-charset"):
            with self.subTest(charset=charset):
                self.assertEqual(tools.page_text(b"Install with pip.",
                                                 f"text/plain; charset={charset}"),
                                 "Install with pip.")

    def test_a_compressed_page_reads_as_the_same_page_uncompressed(self):
        """The tool decodes the content coding itself, as urllib3 decodes it: gzip with further
        members and with data after a member ignored, and deflate with or without the zlib
        header."""
        plain = b"Install with pip.\n"
        deflater = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw_deflate = deflater.compress(plain) + deflater.flush()
        bodies = {"identity": plain, "gzip": gzip.compress(plain),
                  "x-gzip": gzip.compress(plain), "deflate": zlib.compress(plain),
                  "Deflate": raw_deflate,
                  "GZIP": gzip.compress(plain[:8]) + gzip.compress(plain[8:]) + b"\x00 trailing"}
        for coding, body in bodies.items():
            with self.subTest(coding=coding):
                web = _Web({"https://a.example/": _Page(200, body,
                                                        {"Content-Type": "text/plain",
                                                         "Content-Encoding": coding})})
                with _resolving({"a.example": "93.184.216.34"}):
                    self.assertEqual(tools.read_web("https://a.example/", web),
                                     ("Install with pip.", "https://a.example/", ""))
                self.assertEqual(web.headers[0]["Accept-Encoding"], "gzip, deflate")

    def test_a_coding_that_is_not_offered_or_more_than_one_is_not_read(self):
        unreadable = "the page could not be read: ContentDecodingError"
        for coding, body in (("br", b"\x0b\x02\x80text"), ("zstd", b"(\xb5/\xfd text"),
                             ("gzip, gzip", gzip.compress(gzip.compress(b"text"))),
                             ("gzip", b"not gzip"), ("deflate", b"not deflate")):
            with self.subTest(coding=coding):
                page = _Page(200, body, {"Content-Type": "text/plain",
                                         "Content-Encoding": coding})
                with _resolving({"a.example": "93.184.216.34"}):
                    self.assertEqual(tools.read_web("https://a.example/",
                                                    _Web({"https://a.example/": page})),
                                     (None, "https://a.example/", unreadable))
                self.assertTrue(page.closed)

    def _serve(self, head, pieces):
        """The address of a server on the loopback address that responds to each request with
        the header lines `head` and the pieces of the body, until the client closes."""
        class Server(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(65536)
                try:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\n" + head
                                         + b"\r\nConnection: close\r\n\r\n")
                    for piece in pieces:
                        self.request.sendall(piece)
                except OSError:
                    return

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Server)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    def _read_traced(self, url):
        """read_web of `url` from a real server, and the largest memory that it held."""
        with mock.patch.object(tools, "public_address", lambda host: ""):
            tracemalloc.start()
            try:
                result = tools.read_web(url)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        return result, peak

    def test_a_body_that_decodes_to_far_more_than_the_limit_is_never_held_whole(self):
        """urllib3 decodes each chunk that it reads in full. A small compressed body would
        therefore fill the tool server's memory before the limit of rule 9 is checked. The tool
        does not read a stacked coding at all and decodes a single coding up to the limit."""
        words = b"word " * (1 << 20)
        compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        pieces = [compressor.compress(words) for _ in range(16)] + [compressor.flush()]
        inner = b"".join(pieces)
        outer = gzip.compress(inner)
        stacked = [outer[start:start + 64] for start in range(0, len(outer), 64)]
        cases = (("gzip", pieces, "the download was cut at 2,000,000 bytes"),
                 ("gzip, gzip", stacked, "the page could not be read: ContentDecodingError"))
        for coding, body, note in cases:
            with self.subTest(coding=coding):
                url = self._serve(b"Content-Type: text/plain\r\nContent-Encoding: "
                                  + coding.encode(), body)
                (text, read_at, said), peak = self._read_traced(url)
                self.assertLess(peak, 5 * tools.MAX_PAGE_BYTES)
                self.assertEqual((read_at, said), (url, note))
                if coding == "gzip":
                    self.assertEqual(text, words[:tools.MAX_PAGE_BYTES].decode().strip())

    def test_an_ordinary_compressed_page_reads_from_a_server(self):
        html = (b"<html><body><h1>Guide</h1><p>Install with pip.</p>" * 2000
                + b"</body></html>")
        url = self._serve(b"Content-Type: text/html; charset=utf-8\r\nContent-Encoding: gzip",
                          [gzip.compress(html)])
        (text, read_at, note), _ = self._read_traced(url)
        self.assertEqual((read_at, note), (url, ""))
        self.assertEqual(text, tools.page_text(html, "text/html; charset=utf-8"))
        self.assertEqual(text.count("Install with pip."), 2000)

    def test_the_conversion_of_deeply_nested_lists_stops_at_the_limit(self):
        """html2text's output grows with the square of a nested list's depth, because it
        indents each level further."""
        started = time.monotonic()
        text = tools.page_text(("<html>" + "<ul><li>" * 16000).encode(), "text/html")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(text), tools.PAGE_TEXT_CHARS)

    def test_a_conversion_that_stops_gives_the_summary_the_text_that_the_whole_gives(self):
        """The summary reads the first MAX_SUMMARY_INPUT_CHARS characters and states that the
        page was cut when the text is longer. A conversion that stops must give the summary the
        same first characters and the same statement as the whole conversion."""
        import html2text
        for parts in (600, 1200):
            with self.subTest(parts=parts):
                html = ("<html><body>" + "".join(
                    f"<h2>Part {n}</h2><p>One&nbsp;line\n\n\n\nof {n}</p>"
                    + "<ul><li>item" * 40 + "</li></ul>" * 40 for n in range(parts))
                        + "</body></html>")
                converter = html2text.HTML2Text()
                converter.ignore_links = converter.ignore_images = True
                converter.body_width = 0
                whole = re.sub(r"\n{3,}", "\n\n", converter.handle(html)).strip()
                self.assertGreater(len(whole), tools.PAGE_TEXT_CHARS + tools.CONVERSION_MARGIN)
                text = tools.page_text(html.encode(), "text/html")
                self.assertEqual(text, whole[:tools.PAGE_TEXT_CHARS])
                self.assertGreater(len(text), tools.MAX_SUMMARY_INPUT_CHARS)


class TestGitHubLinks(unittest.TestCase):

    def test_what_a_link_names(self):
        self.assertEqual(tools.github_link("https://github.com/o/r"), ("repository", "o/r", "", ""))
        self.assertEqual(tools.github_link("https://github.com/o/r.git"),
                         ("repository", "o/r", "", ""))
        self.assertEqual(tools.github_link("https://github.com/o/r/blob/main/docs/a%20b.md"),
                         ("blob", "o/r", "main", "docs/a b.md"))
        self.assertEqual(tools.github_link("https://www.github.com/o/r/tree/v1"),
                         ("tree", "o/r", "v1", ""))
        self.assertEqual(tools.github_link("https://github.com/o"), ("account", "", "", ""))
        self.assertEqual(tools.github_link("https://github.com/o/r/issues/3")[0], "issues")
        self.assertIsNone(tools.github_link("https://raw.githubusercontent.com/o/r/HEAD/a.md"))

    def test_each_kind_is_read_through_the_api(self):
        """Rule 4: a repository as its README, a file as itself, a directory as its listing,
        releases as their list."""
        github = _GitHub({
            (f"{API}/readme", readmes.RAW): _Answer(200, content=b"# R"),
            f"{API}/contents/docs/guide.md?ref=main": _Answer(200, {
                "type": "file", "encoding": "base64",
                "content": base64.b64encode(b"Guide").decode()}),
            f"{API}/contents/docs?ref=main": _Answer(200, [
                {"name": "guide.md", "type": "file", "size": 5},
                {"name": "img", "type": "dir", "size": 0}]),
            f"{API}/releases?per_page=100": _Answer(200, [
                {"tag_name": "v1", "published_at": "2026-01-01T00:00:00Z", "name": "One"}])})
        cases = {"https://github.com/o/r": "# R",
                 "https://github.com/o/r/blob/main/docs/guide.md": "Guide",
                 "https://github.com/o/r/raw/main/docs/guide.md": "Guide",
                 "https://github.com/o/r/tree/main/docs":
                     "guide.md (file, 5 bytes)\nimg (dir, 0 bytes)",
                 "https://github.com/o/r/releases": "v1 2026-01-01T00:00:00Z One"}
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(tools.read_github(github, url), (expected, ""))

    def test_a_large_file_is_read_raw(self):
        """GitHub's contents API leaves out the content of a file over 1 MB."""
        github = _GitHub({
            f"{API}/contents/big.md?ref=main": _Answer(200, {"type": "file", "encoding": "none",
                                                             "content": ""}),
            (f"{API}/contents/big.md?ref=main", readmes.RAW): _Answer(200, content=b"Big")})
        self.assertEqual(tools.read_github(github, "https://github.com/o/r/blob/main/big.md"),
                         ("Big", ""))

    def test_a_file_at_a_renamed_branch_is_read_at_the_branch_that_github_names(self):
        """android/android-test renamed master to main. GitHub returns 404 for a file at master
        and responds to the branch master with a redirect to main, which the tool follows once.
        When the HTTP client follows the redirect itself, the request for the branch master
        returns the branch main with its name. A file that a branch lacks stays unavailable, and
        a commit is not requested as a branch."""
        guide = _Answer(200, {"type": "file", "encoding": "base64",
                              "content": base64.b64encode(b"Guide").decode()})
        moved = _Answer(301, {"message": "Moved Permanently"})
        moved.headers = {"Location": f"{API}/branches/main"}
        for branch_answer in (moved, _Answer(200, {"name": "main"})):
            with self.subTest(status=branch_answer.status_code):
                github = _GitHub({f"{API}/branches/master": branch_answer,
                                  f"{API}/contents/CONTRIBUTING.md?ref=main": guide})
                self.assertEqual(tools.read_github(
                    github, "https://github.com/o/r/blob/master/CONTRIBUTING.md"), ("Guide", ""))
                self.assertEqual([url for url, _ in github.asked], [
                    f"{API}/contents/CONTRIBUTING.md?ref=master", f"{API}/branches/master",
                    f"{API}/contents/CONTRIBUTING.md?ref=main"])
        github = _GitHub({f"{API}/branches/main": _Answer(200, {"name": "main"})})
        self.assertEqual(tools.read_github(github, "https://github.com/o/r/blob/main/GONE.md"),
                         (None, "GitHub returned HTTP 404 for GONE.md"))
        self.assertEqual(len(github.asked), 2)
        github = _GitHub({})
        commit = "0123456789abcdef0123456789abcdef01234567"
        self.assertIn("HTTP 404", tools.read_github(
            github, f"https://github.com/o/r/blob/{commit}/GONE.md")[1])
        self.assertEqual(github.asked, [(f"{API}/contents/GONE.md?ref={commit}", readmes.JSON)])

    def test_other_kinds_binaries_and_failures_are_not_offered(self):
        github = _GitHub({f"{API}/contents/logo.png?ref=main": _Answer(200, {
            "type": "file", "encoding": "base64",
            "content": base64.b64encode(b"\x89PNG\x00\x00").decode()})})
        text, note = tools.read_github(github, "https://github.com/o/r/wiki/Home")
        self.assertIsNone(text)
        self.assertIn("does not read GitHub's wiki pages", note)
        self.assertEqual(github.asked, [])
        self.assertEqual(tools.read_github(github, "https://github.com/o/r/blob/main/logo.png"),
                         (None, "logo.png is not text"))
        self.assertIn("HTTP 404", tools.read_github(github, "https://github.com/o/gone")[1])

    def test_an_issue_is_read_with_its_maintainers_comments(self):
        """streamlink-twitch-gui's README points to issue #1045 for the project's development
        status, which a member of the project states in that issue. The tool reads the issue
        through the API, because scripts render the issue's web page."""
        github = _GitHub({
            f"{API}/issues/1045": _Answer(200, {
                "title": "Maintenance note", "state": "open", "user": {"login": "lead"},
                "author_association": "MEMBER", "created_at": "2023-01-02T00:00:00Z",
                "body": "Active development has ceased."}),
            f"{API}/issues/1045/comments?per_page={tools.ISSUE_COMMENTS}": _Answer(200, [
                {"user": {"login": "visitor"}, "author_association": "NONE",
                 "created_at": "2023-01-03T00:00:00Z", "body": "Please keep it going!"},
                {"user": {"login": "lead"}, "author_association": "OWNER",
                 "created_at": "2023-01-04T00:00:00Z", "body": "Only critical fixes."}])})
        text, note = tools.read_github(github, "https://github.com/o/r/issues/1045")
        self.assertEqual(note, "")
        self.assertEqual(text, "Issue #1045 of o/r: Maintenance note (open), opened by lead "
                               "(MEMBER) on 2023-01-02T00:00:00Z\n\nActive development has "
                               "ceased.\n\nComment by lead (OWNER) on 2023-01-04T00:00:00Z:\n"
                               "Only critical fixes.")
        self.assertEqual(tools.read_github(github, "https://github.com/o/r/pull/1045")[0], text)
        self.assertEqual(tools.read_github(github, "https://github.com/o/r/issues/new"),
                         (None, "'new' is not the number of an issue or a pull request"))

    def test_an_issue_of_another_repository_names_that_repository(self):
        """A README can link to an issue of its upstream. The OWNER and MEMBER of that issue
        are the upstream's maintainers, and the summary can tell them apart from this
        repository's maintainers only when the text names the issue's repository."""
        github = _GitHub({
            "https://api.github.com/repos/up/stream/issues/7": _Answer(200, {
                "title": "Fork policy", "state": "closed", "user": {"login": "chief"},
                "author_association": "OWNER", "created_at": "2024-05-06T00:00:00Z",
                "body": "We do not maintain forks."})})
        text, note = tools.read_github(github, "https://github.com/up/stream/issues/7")
        self.assertEqual(note, "")
        self.assertTrue(text.startswith("Issue #7 of up/stream: Fork policy (closed), opened by "
                                        "chief (OWNER)"), text)


def _openrouter_client(body):
    """The SDK's client for OpenRouter, whose requests all get the status 200 and the JSON `body`
    through a transport of httpx2, the HTTP client that the SDK installs and uses, so that the SDK
    parses the body as it parses OpenRouter's."""
    import httpx2
    return openai.AsyncOpenAI(api_key="sk-or-v1-test", base_url=endpoints.OPENROUTER_API,
                              max_retries=0, http_client=httpx2.AsyncClient(
                                  transport=httpx2.MockTransport(
                                      lambda request: httpx2.Response(200, json=body))))


# A completed Response of OpenRouter's Responses API with a summary.
COMPLETED_SUMMARY = {
    "id": "resp_1", "object": "response", "created_at": 0,
    "model": "deepseek/deepseek-v4.1-flash", "status": "completed",
    "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "A parser's documentation.",
                             "annotations": []}]}],
    "usage": {"input_tokens": 50, "input_tokens_details": {"cached_tokens": 0},
              "output_tokens": 7, "output_tokens_details": {"reasoning_tokens": 2},
              "total_tokens": 57}}
# A Response of OpenRouter's Responses API with the status "failed", the code of its error and
# the `error_type` at the top level, as in the example of the section of the Responses API
# (openrouter.ai/docs/api_reference/errors-and-debugging).
FAILED_RESPONSE = {"id": "resp_abc123", "object": "response", "status": "failed",
                   "error": {"code": "server_error", "message": "Provider returned error"},
                   "error_type": "provider_unavailable"}


class TestSummaries(unittest.TestCase):

    def _client(self, recorded):
        async def create(**kwargs):
            recorded.append(kwargs)
            return SimpleNamespace(
                output_text="A parser's documentation.", model="m-1", service_tier="default",
                status="completed", output=[SimpleNamespace(type="message")],
                usage=SimpleNamespace(input_tokens=50, output_tokens=7,
                                      input_tokens_details=SimpleNamespace(
                                          cached_tokens=20, cache_write_tokens=25)))
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    def test_a_summary_that_the_service_ended_fails_the_summary(self):
        """OpenRouter reports a failure after the status 200 in a Response with the status
        failed, an error and the `error_type`, without output or with an empty output, and the
        `output_text` of such a Response is empty. Through the real client of the SDK, both forms
        are a failure of the summary, after which the classification makes the call again and
        then fails the repository. Read as the summary, the empty text would make an available
        page say nothing, and the cache would keep the label decided from it. A completed
        Response through the same client is the summary."""
        completed = {
            "id": "resp_1", "object": "response", "created_at": 0,
            "model": "deepseek/deepseek-v4.1-flash", "status": "completed",
            "output": [{"type": "message", "id": "msg_1", "status": "completed",
                        "role": "assistant", "content": [
                            {"type": "output_text", "text": "A parser's documentation.",
                             "annotations": []}]}],
            "usage": {"input_tokens": 50, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 7, "output_tokens_details": {"reasoning_tokens": 2},
                      "total_tokens": 57}}
        web = _Web({"https://docs.example/": _Page(200, b"Docs.", {"Content-Type": "text/plain"})})
        for form, body in (("completed", completed), ("without output", FAILED_RESPONSE),
                           ("with an empty output",
                            {**FAILED_RESPONSE, "output": [], "usage": None})):
            server = tools.build_server(github=_GitHub({}), session=web,
                                        summarizer=tools.Summarizer(_openrouter_client(body)))
            err = io.StringIO()
            with self.subTest(form=form), _resolving({"docs.example": "93.184.216.34"}), \
                    contextlib.redirect_stderr(err):
                call = server.call_tool("read_linked_page", {"url": "https://docs.example/",
                                                             "repo_name": "o/r"})
                if form == "completed":
                    result = asyncio.run(call).structured_content
                    self.assertEqual((result["available"], result["summary"],
                                      result["input_tokens"], result["output_tokens"]),
                                     (True, "A parser's documentation.", 50, 7))
                    continue
                with self.assertRaises(ToolError) as raised:
                    asyncio.run(call)
                self.assertEqual(tools.failure_of(str(raised.exception)),
                                 (tools.SUMMARY_FAILED,
                                  "summarizing https://docs.example/ failed with SummaryEnded "
                                  "(the API ended the summary with the error server_error "
                                  "(Provider returned error) with the error_type "
                                  "provider_unavailable)"))
                self.assertEqual(err.getvalue(), "")

    def test_the_prompt_is_the_instructions_and_the_page_the_input(self):
        """Rule 10, and the review of the prompts: the page is data in its own tags. The
        summarizer returns what the response reports about the prompt cache and the service
        tier, which decide what OpenAI bills for the request, and the request names no tier."""
        recorded = []
        summarizer = tools.Summarizer(self._client(recorded), model="m", effort="low")
        made = asyncio.run(summarizer("o/r", "https://a.example/", "Page text."))
        self.assertEqual(made, {"summary": "A parser's documentation.", "cut": False,
                                "input_tokens": 50, "output_tokens": 7, "model": "m-1",
                                "cached_tokens": 20, "cache_write_tokens": 25,
                                "service_tier": "default"})
        self.assertNotIn("service_tier", recorded[0])
        request = recorded[0]
        self.assertEqual((request["model"], request["reasoning"]), ("m", {"effort": "low"}))
        self.assertIn('"o/r"', request["instructions"])
        self.assertNotIn("$repo_name", request["instructions"])
        self.assertEqual(request["input"], '<page url="https://a.example/">\nPage text.\n</page>')

    def test_a_summary_model_of_openrouter_is_called_on_its_pinned_endpoint(self):
        """The summarizer sends the request to the pinned endpoint without fallbacks and reports
        the endpoint as the service tier, which sets the price of the summary in the cost
        record."""
        recorded = []
        endpoint = endpoints.Endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8",
                                      "deepseek-ai/DeepSeek-V4.1-Flash")
        summarizer = tools.Summarizer(self._client(recorded), effort="low", endpoint=endpoint)
        made = asyncio.run(summarizer("o/r", "https://a.example/", "Page text."))
        self.assertEqual(recorded[0]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(recorded[0]["extra_body"], endpoint.routing())
        self.assertEqual(made["service_tier"], "morph/fp8")

    def test_the_summary_model_by_default_is_deepseek_on_its_pinned_endpoint(self):
        """The maintainer chose DeepSeek V4.1 Flash on Morph's fp8 endpoint as the summary model
        on 2026-10-31. A request without the endpoint would go to whatever provider and
        quantization OpenRouter picks, and so would the requests of a server started from its
        own command line without the options of the endpoint."""
        recorded = []
        made = asyncio.run(tools.Summarizer(self._client(recorded))(
            "o/r", "https://a.example/", "Page text."))
        pinned = endpoints.Endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8",
                                    "deepseek-ai/DeepSeek-V4.1-Flash")
        self.assertEqual((recorded[0]["model"], recorded[0]["extra_body"],
                          recorded[0]["reasoning"], made["service_tier"]),
                         (pinned.model, pinned.routing(), {"effort": "low"}, "morph/fp8"))

    def test_a_page_over_the_summary_input_is_cut(self):
        recorded = []
        summarizer = tools.Summarizer(self._client(recorded))
        made = asyncio.run(summarizer("o/r", "u", "x" * (tools.MAX_SUMMARY_INPUT_CHARS + 10)))
        self.assertTrue(made["cut"])
        self.assertEqual(recorded[0]["input"].count("x"), tools.MAX_SUMMARY_INPUT_CHARS)

    def test_the_backup_provider_serves_the_summaries_while_the_pinned_provider_fails(self):
        """Morph's endpoint rate-limits the summary model. The same request goes to the backup
        provider at once, which serves every summary from then on, the result names it as the
        service tier, and once a request that tests Morph's endpoint after
        PINNED_CHECK_SECONDS gets a response, Morph's endpoint serves again. Without the backup
        provider, the failure fails the summary."""
        clock = SimpleNamespace(now=0.0)
        recorded, down = [], {"morph/fp8": True}
        served = self._client([])

        async def create(**kwargs):
            recorded.append(kwargs)
            provider = kwargs["extra_body"]["provider"]["order"][0]
            if down.get(provider):
                response = SimpleNamespace(request=SimpleNamespace(), status_code=429,
                                           headers={})
                raise openai.RateLimitError("rate-limited", response=response,
                                            body={"code": 429, "message": "rate-limited"})
            return await served.responses.create(**kwargs)
        # The fake makes no retries of the SDK, which a test of the pinned provider turns off.
        client = SimpleNamespace(responses=SimpleNamespace(create=create),
                                 with_options=lambda **options: client)
        pinned = endpoints.PRODUCTION
        summarizer = tools.Summarizer(client, effort="low", endpoint=pinned,
                                      backup=endpoints.backup_of(pinned),
                                      clock=lambda: clock.now)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            made = [asyncio.run(summarizer("o/r", "https://a.example/", "Page."))["service_tier"]
                    for _ in range(2)]
            clock.now = endpoints.PINNED_CHECK_SECONDS
            down["morph/fp8"] = False
            made += [asyncio.run(summarizer("o/r", "https://a.example/", "Page."))["service_tier"]
                     for _ in range(2)]
        self.assertEqual(made, ["deepinfra/fp8", "deepinfra/fp8", "morph/fp8", "morph/fp8"])
        self.assertEqual([request["extra_body"]["provider"]["order"][0] for request in recorded],
                         ["morph/fp8", "deepinfra/fp8", "deepinfra/fp8", "morph/fp8",
                          "morph/fp8"])
        self.assertEqual({key: value for key, value in recorded[1].items() if key != "extra_body"},
                         {key: value for key, value in recorded[0].items()
                          if key != "extra_body"})
        self.assertIn("WARNING repository_labeling: The pinned provider morph/fp8 of the summary "
                      "model, deepseek/deepseek-v4.1-flash, failed a summary with RateLimitError",
                      err.getvalue())
        self.assertIn("INFO repository_labeling: The pinned provider morph/fp8 of the summary "
                      "model, deepseek/deepseek-v4.1-flash, responds again", err.getvalue())
        without = tools.Summarizer(client, effort="low", endpoint=pinned)
        down["morph/fp8"] = True
        with self.assertRaises(openai.RateLimitError):
            asyncio.run(without("o/r", "https://a.example/", "Page."))

    def test_a_summary_request_that_tests_the_pinned_provider_goes_without_the_retries(self):
        """During a switch, the summary request that tests the pinned provider goes once,
        without the retries that the client of the SDK makes for an HTTP 500 otherwise, and the
        same request goes on to the backup provider."""
        import httpx2
        sent = []

        def respond(request):
            provider = json.loads(request.content)["provider"]["order"][0]
            sent.append(provider)
            if provider == "morph/fp8":
                return httpx2.Response(500, headers={"retry-after-ms": "1"},
                                       json={"error": {"code": 500, "message": "Upstream "
                                                       "error from Morph"}})
            return httpx2.Response(200, json=COMPLETED_SUMMARY)
        client = openai.AsyncOpenAI(api_key="sk-or-v1-test", base_url=endpoints.OPENROUTER_API,
                                    max_retries=2, http_client=httpx2.AsyncClient(
                                        transport=httpx2.MockTransport(respond)))
        clock = SimpleNamespace(now=0.0)
        pinned = endpoints.PRODUCTION
        summarizer = tools.Summarizer(client, effort="low", endpoint=pinned,
                                      backup=endpoints.backup_of(pinned),
                                      clock=lambda: clock.now)
        with contextlib.redirect_stderr(io.StringIO()):
            summarizer.providers.switch("failed a summary")
            clock.now = endpoints.PINNED_CHECK_SECONDS
            made = asyncio.run(summarizer("o/r", "https://a.example/", "Page."))
        self.assertEqual(sent, ["morph/fp8", "deepinfra/fp8"])
        self.assertEqual((made["summary"], made["service_tier"]),
                         ("A parser's documentation.", "deepinfra/fp8"))

    def test_a_refusal_for_the_budget_of_the_requests_in_flight_goes_to_no_backup(self):
        """OpenRouter refuses a summary request with HTTP 402 while the requests in flight fill
        the budget of the API key, which is no failure of the provider. The refusal fails the
        summary, as without a backup provider, and no request goes to the backup provider."""
        sent = []

        async def create(**kwargs):
            sent.append(kwargs["extra_body"]["provider"]["order"][0])
            response = SimpleNamespace(request=SimpleNamespace(), status_code=402, headers={})
            raise openai.APIStatusError(
                "in flight", response=response,
                body={"code": 402, "message": "Too many requests in flight",
                      "metadata": {"limit_source": "openrouter_in_flight_budget"}})
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        pinned = endpoints.PRODUCTION
        summarizer = tools.Summarizer(client, effort="low", endpoint=pinned,
                                      backup=endpoints.backup_of(pinned))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(openai.APIStatusError):
            asyncio.run(summarizer("o/r", "https://a.example/", "Page."))
        self.assertEqual(sent, ["morph/fp8"])
        self.assertFalse(summarizer.providers.switched)
        self.assertEqual(err.getvalue(), "")

    def test_a_failure_of_the_backup_fails_the_summary(self):
        """Both providers fail. The failure of the backup provider fails the summary as a
        failure of the pinned provider did without it, and the classification makes the call
        again after its waits."""
        async def create(**kwargs):
            response = SimpleNamespace(request=SimpleNamespace(), status_code=503, headers={})
            raise openai.InternalServerError("unavailable", response=response,
                                             body={"code": 503, "message": "unavailable"})
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        pinned = endpoints.PRODUCTION
        summarizer = tools.Summarizer(client, effort="low", endpoint=pinned,
                                      backup=endpoints.backup_of(pinned))
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(openai.InternalServerError):
            asyncio.run(summarizer("o/r", "https://a.example/", "Page."))

    def test_the_command_line_names_the_backup_provider_of_the_summary_model(self):
        """The classification starts the server with the backup provider of the summary model,
        which serves the weights of the summary model in its quantization."""
        built = {}

        def build_server(summarizer=None, credentials_path=None):
            built["summarizer"] = summarizer
            return SimpleNamespace(run=lambda transport: None)
        with mock.patch.object(tools, "build_server", build_server), \
                mock.patch.object(tools, "exit_with_parent", lambda parent: None), \
                mock.patch.object(tools.logs, "capture_libraries", lambda: None):
            tools.main(["--summary-model", "deepseek/deepseek-v4.1-flash",
                        "--summary-provider", "morph/fp8", "--summary-quantization", "fp8",
                        "--summary-hugging-face-id", "deepseek-ai/DeepSeek-V4.1-Flash",
                        "--summary-backup-provider", "deepinfra/fp8"])
        providers = built["summarizer"].providers
        self.assertEqual((providers.pinned.provider, providers.backup.provider,
                          providers.backup.quantization, providers.backup.hugging_face_id),
                         ("morph/fp8", "deepinfra/fp8", "fp8",
                          "deepseek-ai/DeepSeek-V4.1-Flash"))


class TestRepositoryMetadata(unittest.TestCase):

    def call(self, github, repo_name="o/r"):
        server = tools.build_server(github=github)
        return asyncio.run(server.call_tool("get_repository_metadata",
                                            {"repo_name": repo_name})).structured_content

    def test_what_github_records_reaches_the_model(self):
        """The description, which the classification also sends with every README, reaches the
        model, and so does what a criterion can depend on: GitHub's template flag, whether issues
        are enabled and the dates. The stars and the forks that GitHub returns do not reach the
        model."""
        github = _GitHub({API: _Answer(200, {
            "full_name": "o/r", "description": "A tool.", "homepage": "https://r.example",
            "topics": ["cli", "yaml"], "license": {"spdx_id": "MIT", "name": "MIT License"},
            "is_template": True, "has_issues": False, "created_at": "2020-01-01T00:00:00Z",
            "pushed_at": "2026-09-01T00:00:00Z", "stargazers_count": 40, "forks_count": 3,
            "private": False})})
        self.assertEqual(self.call(github), {
            "repo_name": "o/r", "available": True, "description": "A tool.",
            "homepage": "https://r.example", "topics": ["cli", "yaml"], "license": "MIT",
            "is_template": True, "has_issues": False, "created_at": "2020-01-01T00:00:00Z",
            "pushed_at": "2026-09-01T00:00:00Z", "owner_description": "", "note": ""})

    def test_the_owners_description_is_read_once_per_owner(self):
        """KDE's organization describes all of its repositories on GitHub: "Official read-only
        mirror of the KDE project. Please don't send pull requests", while KDE/liquidshell's own
        description and README do not. The metadata gives an organization's description and a
        user's bio, and the repositories of one owner cost one request for it in a run. A
        failure other than 404 gives an empty description, which is not kept, so the server
        requests the description again for the next repository of the owner."""
        kde = "Official read-only mirror of the KDE project. Please don't send pull requests"
        github = _GitHub({
            "https://api.github.com/repos/KDE/liquidshell": _Answer(200, {
                "full_name": "KDE/liquidshell", "owner": {"login": "KDE",
                                                          "type": "Organization"}}),
            "https://api.github.com/repos/KDE/elisa": _Answer(200, {
                "full_name": "KDE/elisa", "owner": {"login": "KDE", "type": "Organization"}}),
            "https://api.github.com/orgs/KDE": _Answer(200, {"description": kde}),
            "https://api.github.com/repos/ana/tool": _Answer(200, {
                "full_name": "ana/tool", "owner": {"login": "ana", "type": "User"}}),
            "https://api.github.com/users/ana": _Answer(200, {"bio": "Maintainer of tools."}),
            "https://api.github.com/repos/bo/tool": _Answer(200, {
                "full_name": "bo/tool", "owner": {"login": "bo", "type": "User"}}),
            "https://api.github.com/users/bo": _Answer(502, {"message": "Bad Gateway"})})
        server = tools.build_server(github=github)
        found = {name: asyncio.run(server.call_tool("get_repository_metadata", {
                     "repo_name": name})).structured_content["owner_description"]
                 for name in ("KDE/liquidshell", "KDE/elisa", "ana/tool", "bo/tool")}
        self.assertEqual(found, {"KDE/liquidshell": kde, "KDE/elisa": kde,
                                 "ana/tool": "Maintainer of tools.", "bo/tool": ""})
        self.assertEqual([url for url, _ in github.asked].count(
            "https://api.github.com/orgs/KDE"), 1)
        asyncio.run(server.call_tool("get_repository_metadata", {"repo_name": "bo/tool"}))
        self.assertEqual([url for url, _ in github.asked].count(
            "https://api.github.com/users/bo"), 2)

    def test_a_repository_without_a_description_or_license_returns_empty_fields(self):
        github = _GitHub({API: _Answer(200, {"full_name": "o/r", "description": None,
                                             "homepage": None, "license": None})})
        result = self.call(github)
        self.assertEqual((result["available"], result["description"], result["license"],
                          result["topics"]), (True, "", "", []))

    def test_a_failure_states_its_cause_and_bad_input_is_refused(self):
        self.assertEqual(self.call(_GitHub({}))["note"], "GitHub returned HTTP 404")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(ToolError) as raised:
            self.call(_GitHub({API: readmes.NoResponse("GitHub returned HTTP 502")}))
        self.assertEqual(tools.failure_of(str(raised.exception)),
                         (tools.GITHUB_FAILED, "NoResponse (GitHub returned HTTP 502)"))
        self.assertEqual(err.getvalue(), "")
        with self.assertRaises(ToolError) as raised:
            self.call(_GitHub({}), "../etc")
        self.assertIsNone(tools.failure_of(str(raised.exception)))


class TestIssuesAndPullRequests(unittest.TestCase):

    def call(self, github, repo_name="o/r"):
        server = tools.build_server(github=github)
        return asyncio.run(server.call_tool("get_issues_and_pull_requests",
                                            {"repo_name": repo_name})).structured_content

    def test_the_activity_reaches_the_model_from_one_query(self):
        """mlichvar/chrony, a mirror, closes pull requests with "This was merged with some fixes"
        and merges none. openssl/openssl closes most of its pull requests without a merge
        (classification-tools-spec.md gives the counts) and reviews the open pull requests. The
        response contains the counts, the pinned issues and the latest pull requests and issues,
        newest first, with the beginning of their last comment."""
        comment = "This was merged   with\nsome fixes. " + "x" * tools.COMMENT_CHARS
        github = _GitHub({("graphql", "o/r"): _Answer(200, {"data": {"repository": {
            "hasIssuesEnabled": True, "openPullRequests": {"totalCount": 0},
            "mergedPullRequests": {"totalCount": 0}, "closedPullRequests": {"totalCount": 3},
            "openIssues": {"totalCount": 1}, "closedIssues": {"totalCount": 4},
            "pinnedIssues": {"nodes": [{"issue": {"number": 9, "title": "Status",
                                                  "url": "https://github.com/o/r/issues/9"}}]},
            "pullRequests": {"nodes": [
                {"number": 6, "title": "Old", "url": "https://github.com/o/r/pull/6",
                 "state": "CLOSED", "createdAt": "2025-01-01T00:00:00Z",
                 "closedAt": "2025-01-02T00:00:00Z", "mergedAt": None, "mergedBy": None,
                 "authorAssociation": "NONE", "comments": {"nodes": []}},
                {"number": 7, "title": "Fix", "url": "https://github.com/o/r/pull/7",
                 "state": "CLOSED", "createdAt": "2026-01-01T00:00:00Z",
                 "closedAt": "2026-01-03T00:00:00Z", "mergedAt": None, "mergedBy": None,
                 "authorAssociation": "CONTRIBUTOR",
                 "comments": {"nodes": [{"authorAssociation": "OWNER", "body": comment}]}}]},
            "issues": {"nodes": [{"number": 9, "title": "Status",
                                  "url": "https://github.com/o/r/issues/9", "state": "OPEN",
                                  "createdAt": "2026-02-01T00:00:00Z", "closedAt": None,
                                  "authorAssociation": "OWNER", "comments": {"nodes": []}}]}}}})})
        result = self.call(github)
        self.assertEqual({key: result[key] for key in (
            "available", "has_issues", "open_pull_requests", "merged_pull_requests",
            "closed_pull_requests", "open_issues", "closed_issues", "pinned_issues")},
            {"available": True, "has_issues": True, "open_pull_requests": 0,
             "merged_pull_requests": 0, "closed_pull_requests": 3, "open_issues": 1,
             "closed_issues": 4, "pinned_issues": [{"number": 9, "title": "Status",
                                                    "url": "https://github.com/o/r/issues/9"}]})
        newest = result["recent_pull_requests"][0]
        self.assertEqual([pr["number"] for pr in result["recent_pull_requests"]], [7, 6])
        self.assertEqual((newest["state"], newest["author"], newest["closed"], newest["merged_by"]),
                         ("CLOSED", "CONTRIBUTOR", "2026-01-03T00:00:00Z", ""))
        self.assertEqual(newest["last_comment"],
                         ("OWNER: This was merged with some fixes. " + "x" * tools.COMMENT_CHARS)
                         [:tools.COMMENT_CHARS])
        self.assertEqual(result["recent_pull_requests"][1]["last_comment"], "")
        self.assertEqual(result["recent_issues"][0]["author"], "OWNER")
        self.assertEqual(github.asked, [("graphql", "o/r")])

    def test_a_failure_states_its_cause_and_bad_input_is_refused(self):
        self.assertEqual(self.call(_GitHub({}))["note"],
                         "GitHub's GraphQL API did not find the repository")
        missing = _Answer(200, {"data": {"repository": None}, "errors": [
            {"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]})
        self.assertEqual(self.call(_GitHub({("graphql", "o/r"): missing}))["note"],
                         "GitHub's GraphQL API did not find the repository")
        self.assertEqual(self.call(_GitHub({("graphql", "o/r"): _Answer(502)}))["note"],
                         "GitHub returned HTTP 502")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(ToolError) as raised:
            self.call(_GitHub({("graphql", "o/r"): readmes.NoResponse("HTTP 502")}))
        self.assertEqual(tools.failure_of(str(raised.exception)),
                         (tools.GITHUB_FAILED, "NoResponse (HTTP 502)"))
        self.assertEqual(err.getvalue(), "")
        with self.assertRaises(ToolError):
            self.call(_GitHub({}), "../etc")

    def test_a_graphql_response_with_another_error_is_no_fact_about_the_repository(self):
        """GitHub reports its own failure with HTTP 200, errors and no data. Read as a
        repository that GitHub did not find, the failure would reach the model as a fact about
        the repository."""
        broken = _Answer(200, {"data": None, "errors": [
            {"type": "SERVICE_UNAVAILABLE", "message": "Something went wrong"}]})
        with self.assertRaises(ToolError) as raised:
            self.call(_GitHub({("graphql", "o/r"): broken}))
        kind, message = tools.failure_of(str(raised.exception))
        self.assertEqual(kind, tools.GITHUB_FAILED)
        self.assertIn("Something went wrong", message)

    def test_a_graphql_response_with_errors_in_place_of_a_field_is_no_fact_about_the_repository(
            self):
        """GitHub returns the repository with an error and with null for a count or a list of
        pull requests or issues, as for a part of a query that timed out. Read as a value, the null
        would reach the model as no merged pull requests or no recent issues. Without errors, a
        null is GitHub's value, such as for a repository without pinned issues."""
        repository = {"hasIssuesEnabled": True, "openPullRequests": {"totalCount": 2},
                      "mergedPullRequests": {"totalCount": 151},
                      "closedPullRequests": {"totalCount": 18}, "openIssues": {"totalCount": 1},
                      "closedIssues": {"totalCount": 4}, "pinnedIssues": {"nodes": []},
                      "pullRequests": {"nodes": []}, "issues": {"nodes": []}}
        for field, value in (("mergedPullRequests", None), ("pullRequests", {"nodes": None}),
                             ("issues", {"nodes": [None]})):
            with self.subTest(field=field):
                body = {"data": {"repository": {**repository, field: value}},
                        "errors": [{"type": "SERVICE_UNAVAILABLE", "message": "timedout",
                                    "path": ["repository", field]}]}
                with self.assertRaises(ToolError) as raised:
                    self.call(_GitHub({("graphql", "o/r"): _Answer(200, body)}))
                kind, message = tools.failure_of(str(raised.exception))
                self.assertEqual(kind, tools.GITHUB_FAILED)
                self.assertIn(f"has errors in place of {field} (timedout)", message)
        result = self.call(_GitHub({("graphql", "o/r"): _Answer(200, {"data": {"repository": {
            **repository, "pinnedIssues": None}}})}))
        self.assertEqual((result["available"], result["merged_pull_requests"],
                          result["pinned_issues"]), (True, 151, []))

    def test_an_error_concerns_the_field_that_its_path_names(self):
        """GitHub's schema declares `pinnedIssues` nullable, and GitHub returns null for it for
        a repository with issues disabled. An error deep in another field, such as the body of a
        comment of a recent issue, says nothing about `pinnedIssues`, and the activity reaches
        the model. An error with a path that names `pinnedIssues` is a failure of GitHub, and so
        is an error without a path, which could have left any field null."""
        repository = {"hasIssuesEnabled": False, "openPullRequests": {"totalCount": 2},
                      "mergedPullRequests": {"totalCount": 151},
                      "closedPullRequests": {"totalCount": 18}, "openIssues": {"totalCount": 0},
                      "closedIssues": {"totalCount": 0}, "pinnedIssues": None,
                      "pullRequests": {"nodes": []}, "issues": {"nodes": []}}
        deep = {"type": "INTERNAL", "message": "timedout",
                "path": ["repository", "issues", "nodes", 0, "comments", "nodes", 0, "body"]}
        result = self.call(_GitHub({("graphql", "o/r"): _Answer(200, {
            "data": {"repository": repository}, "errors": [deep]})}))
        self.assertEqual((result["available"], result["has_issues"], result["merged_pull_requests"],
                          result["pinned_issues"]), (True, False, 151, []))
        for error in ({"type": "INTERNAL", "message": "timedout",
                       "path": ["repository", "pinnedIssues"]},
                      {"type": "INTERNAL", "message": "timedout"}):
            with self.subTest(error=error):
                with self.assertRaises(ToolError) as raised:
                    self.call(_GitHub({("graphql", "o/r"): _Answer(200, {
                        "data": {"repository": repository}, "errors": [error]})}))
                kind, message = tools.failure_of(str(raised.exception))
                self.assertEqual(kind, tools.GITHUB_FAILED)
                self.assertIn("has errors in place of pinnedIssues (timedout)", message)
                self.assertTrue(tools.field_errors(message))

    def test_a_malformed_graphql_response_is_no_fact_about_the_repository(self):
        """A proxy can return HTTP 200 with its text in `errors`, or a body or a `data` that is
        no object. Such a body is a failure of GitHub, named by the text. The body does not
        become an AttributeError, which the tool would pass to the model as an error, nor a
        repository that GitHub did not find, nor one error per character of the text."""
        for body, said in (({"data": None, "errors": ["502 Bad Gateway"]}, "502 Bad Gateway"),
                           ({"data": None, "errors": "502 Bad Gateway"}, "502 Bad Gateway"),
                           ({"data": None, "errors": [None, 502]}, "502"),
                           ({"data": "502 Bad Gateway"}, "not the JSON object"),
                           ({"data": {"repository": ["502"]}}, "not the JSON object"),
                           (["502 Bad Gateway"], "not the JSON object")):
            with self.subTest(body=body):
                with self.assertRaises(ToolError) as raised:
                    self.call(_GitHub({("graphql", "o/r"): _Answer(200, body)}))
                kind, message = tools.failure_of(str(raised.exception))
                self.assertEqual(kind, tools.GITHUB_FAILED)
                self.assertIn(said, message)


class TestReadLinkedPage(unittest.TestCase):

    def call(self, url, github=None, web=None, addresses=None):
        summarizer = _Summarizer()
        server = tools.build_server(github=github or _GitHub({}), session=web or _Web({}),
                                    summarizer=summarizer)
        with _resolving(addresses or {}):
            result = asyncio.run(server.call_tool("read_linked_page",
                                                  {"url": url, "repo_name": "o/r"}))
        return result.structured_content, summarizer

    def test_a_page_whose_server_is_unreachable_while_this_machine_reaches_none_fails(self):
        """A failure of this machine's network or name resolution says nothing about the page.
        Passed to the model as an unavailable page, the failure would let a label be decided and
        cached without the page. A page stays unavailable when only its host cannot be
        reached."""
        class Unreachable(_Web):
            def get(self, url, **kwargs):
                raise tools.requests.exceptions.ConnectionError("Network is unreachable")

        for down in (True, False):
            with self.subTest(down=down), \
                    mock.patch.object(tools, "network_down", return_value=down):
                if not down:
                    result, _ = self.call("https://docs.example/", web=Unreachable({}),
                                          addresses={"docs.example": "93.184.216.34"})
                    self.assertEqual((result["available"], result["note"]),
                                     (False, "the request failed: ConnectionError"))
                    continue
                with self.assertRaises(ToolError) as raised:
                    self.call("https://docs.example/", web=Unreachable({}),
                              addresses={"docs.example": "93.184.216.34"})
                kind, message = tools.failure_of(str(raised.exception))
                self.assertEqual(kind, tools.NETWORK_FAILED)
                self.assertIn("the request failed: ConnectionError", message)

    def test_a_page_whose_connection_fails_in_its_body_while_this_machine_reaches_none_fails(self):
        """A connection that this machine's network drops after the headers ends the body with
        a reset or a read timeout, which says nothing about the page either."""
        class Dropped(_Page):
            def __init__(self, error):
                super().__init__(200, b"", {"Content-Type": "text/plain",
                                            "Content-Length": "100000"})
                self.error = error

            def stream(self, size, decode_content=None):
                yield b"x" * 1000
                raise self.error

        errors = {"ChunkedEncodingError": urllib3.exceptions.ProtocolError(
                      "Connection broken: ConnectionResetError(54, 'Connection reset by peer')"),
                  "ConnectionError": urllib3.exceptions.ReadTimeoutError(
                      None, "/", "Read timed out.")}
        for name, error in errors.items():
            note = f"the page could not be read: {name}"
            web = _Web({"https://docs.example/": Dropped(error)})
            with self.subTest(error=name):
                with mock.patch.object(tools, "network_down", return_value=True), \
                        self.assertRaises(ToolError) as raised:
                    self.call("https://docs.example/", web=web,
                              addresses={"docs.example": "93.184.216.34"})
                kind, message = tools.failure_of(str(raised.exception))
                self.assertEqual(kind, tools.NETWORK_FAILED)
                self.assertIn(note, message)
            web = _Web({"https://docs.example/": Dropped(error)})
            with self.subTest(error=name, down=False), \
                    mock.patch.object(tools, "network_down", return_value=False):
                result, _ = self.call("https://docs.example/", web=web,
                                      addresses={"docs.example": "93.184.216.34"})
                self.assertEqual((result["available"], result["note"]), (False, note))

    def test_a_host_that_does_not_resolve_while_this_machine_reaches_none_is_a_failure(self):
        def no_name(host, port):
            raise tools.socket.gaierror(-3, "Temporary failure in name resolution")

        with mock.patch.object(tools.socket, "getaddrinfo", no_name), \
                mock.patch.object(tools, "network_down", return_value=True), \
                self.assertRaises(ToolError) as raised:
            server = tools.build_server(github=_GitHub({}), session=_Web({}),
                                        summarizer=_Summarizer())
            asyncio.run(server.call_tool("read_linked_page", {"url": "https://docs.example/",
                                                              "repo_name": "o/r"}))
        self.assertEqual(tools.failure_of(str(raised.exception))[0], tools.NETWORK_FAILED)

    def test_a_link_that_is_no_url_reaches_the_model_as_unavailable(self):
        """An IPv6 address without its closing bracket is no URL, also for the test whether
        the link is GitHub's. The model receives the note rather than an error."""
        result, summarizer = self.call("http://[::1/docs")
        self.assertEqual((result["available"], result["note"]), (False, tools.NOT_A_URL))
        self.assertEqual(summarizer.given, [])

    def test_a_page_reaches_the_model_as_a_summary_with_its_cost(self):
        """The result carries what the service of the summary model bills for the summary: its
        tokens, those that the API read from and wrote to its prompt cache, and the tier that
        served the request. The classification counts them and passes the rest of the result to
        the model."""
        web = _Web({"https://docs.example/": _Page(200, b"Docs.", {"Content-Type": "text/plain"})})
        result, summarizer = self.call("https://docs.example/", web=web,
                                       addresses={"docs.example": "93.184.216.34"})
        self.assertEqual(result, {"url": "https://docs.example/", "available": True,
                                  "summary": "A summary of https://docs.example/.",
                                  "purpose_sentence": "", "other_repository": "", "note": "",
                                  "input_tokens": 120, "output_tokens": 30,
                                  "model": "gpt-6-luna-2026-09-01", "cached_tokens": 40,
                                  "cache_write_tokens": 60, "service_tier": "default"})
        self.assertEqual(summarizer.given, [("o/r", "https://docs.example/", "Docs.")])

    def test_the_pages_purpose_sentence_is_returned_only_when_the_page_contains_it(self):
        """The description of openjdk/lilliput is the address of the project's page, and only
        that page says what Lilliput is. The summary model copies the purpose sentence of the
        page onto the last line of the summary, and the server returns the sentence apart from
        the summary when the page contains its words in their order, whatever the markup. A
        sentence that the summary model changed or made up, or a phrase of fewer than three
        words, is left out, because a purpose quoted from the sentence counts as stated."""
        page = (b"<html><body><h1>Project Lilliput</h1><p>The goal of the <b>Lilliput</b> "
                b"Project is to explore techniques to downsize Java object headers in the "
                b"Hotspot JVM.</p></body></html>")
        sentence = ("The goal of the Lilliput Project is to explore techniques to downsize Java "
                    "object headers in the Hotspot JVM.")
        dropped = ("the summary's purpose sentence is not in the page, and the result leaves "
                   "it out")
        cases = [(f"Lilliput's project page.\nPurpose sentence: {sentence}", sentence, ""),
                 (f'Lilliput\'s project page.\n**Purpose sentence:** "{sentence}"', sentence, ""),
                 ("Lilliput's project page.\nPurpose sentence: Lilliput makes Java objects "
                  "smaller in every JVM.", "", dropped),
                 ("Lilliput's project page.\nPurpose sentence: Project Lilliput", "", dropped),
                 ("Lilliput's project page.\nPurpose sentence: none", "", ""),
                 ("Lilliput's project page.", "", "")]
        for written, expected, note in cases:
            with self.subTest(written=written):
                class Writing(_Summarizer):
                    async def __call__(self, repo_name, url, text, written=written):
                        made = await super().__call__(repo_name, url, text)
                        return {**made, "summary": written}
                web = _Web({"https://openjdk.example/projects/lilliput": _Page(
                    200, page, {"Content-Type": "text/html"})})
                server = tools.build_server(github=_GitHub({}), session=web, summarizer=Writing())
                with _resolving({"openjdk.example": "93.184.216.34"}):
                    result = asyncio.run(server.call_tool(
                        "read_linked_page", {"url": "https://openjdk.example/projects/lilliput",
                                             "repo_name": "openjdk/lilliput"})).structured_content
                self.assertEqual((result["summary"], result["purpose_sentence"], result["note"]),
                                 ("Lilliput's project page.", expected, note))

    def test_a_purpose_sentence_is_found_whatever_the_markup_around_its_words(self):
        """html2text writes an emphasized word as `_word_`, a README writes a link with its target
        inside a sentence or as raw HTML, and a page can break a word with a soft hyphen. The
        words of the sentence are found in the page all the same, and a sentence with a word
        that the page lacks is not. The summary model can also write the label of the sentence
        at the end of the summary's last sentence."""
        sentence = ("The goal of the Lilliput Project is to explore techniques to downsize Java "
                    "object headers in the Hotspot JVM.")
        pages = {"emphasis": "The goal of the _Lilliput_ Project is to explore techniques to "
                             "downsize Java object headers in the Hotspot JVM.",
                 "a Markdown link": "The goal of the [Lilliput](https://openjdk.org/projects/"
                                    "lilliput \"Lilliput\") Project is to explore techniques to "
                                    "downsize Java object headers in the Hotspot JVM.",
                 "raw HTML": "<p align=\"center\">The goal of the <a href=\"https://openjdk.org/"
                             "projects/lilliput\">Lilliput</a> Project is to explore techniques "
                             "to downsize Java object headers in the <b>Hotspot</b> JVM.</p>",
                 "a soft hyphen": "The goal of the Lilliput Project is to ex\u00adplore "
                                  "techniques to downsize Java object headers in the Hotspot JVM.",
                 "a bare address": "The goal of the Lilliput Project (https://openjdk.org/) is to "
                                   "explore techniques to downsize Java object headers in the "
                                   "Hotspot JVM."}
        for name, page in pages.items():
            with self.subTest(page=name):
                self.assertTrue(tools.in_page(sentence, page))
        self.assertFalse(tools.in_page(sentence.replace("Hotspot", "OpenJ9"), pages["emphasis"]))
        self.assertEqual(tools.purpose_of(f"Lilliput's project page. Purpose sentence: {sentence}"),
                         ("Lilliput's project page.", sentence))
        self.assertEqual(tools.purpose_of(f"Lilliput's project page. **Purpose sentence:** "
                                          f"{sentence}"), ("Lilliput's project page.", sentence))

    def test_a_page_of_another_repository_is_named_by_githubs_ids(self):
        """GitHub resolves hyperledger/firefly-evmconnect, the earlier name of
        hyperledger-firefly/evmconnect, to the repository and returns the same id for both
        names. A page under the earlier name is the repository's own, and `read_linked_page`
        names another repository, under the name that GitHub gives it, only for a page of a
        repository with another id. Without an id for one of the names, the comparison of the
        names decides."""
        readme = _Answer(200, content=b"# EVM Connect\n")
        own = _Answer(200, {"id": 495595431, "full_name": "hyperledger-firefly/evmconnect"})
        cases = [("https://github.com/hyperledger/firefly-evmconnect",
                  {f"https://api.github.com/repos/hyperledger/firefly-evmconnect": own}, ""),
                 ("https://github.com/up/stream",
                  {"https://api.github.com/repos/up/stream": _Answer(200, {
                      "id": 7, "full_name": "Up/Stream"})}, "Up/Stream"),
                 ("https://github.com/gone/away", {}, "gone/away"),
                 ("https://github.com/hyperledger-firefly/evmconnect", {}, "")]
        for url, answers, expected in cases:
            with self.subTest(url=url):
                owner_repo = tools.github_link(url)[1]
                github = _GitHub({(f"https://api.github.com/repos/{owner_repo}/readme",
                                   readmes.RAW): readme,
                                  "https://api.github.com/repos/hyperledger-firefly/evmconnect":
                                      own, **answers})
                server = tools.build_server(github=github, session=_Web({}),
                                            summarizer=_Summarizer())
                result = asyncio.run(server.call_tool(
                    "read_linked_page", {"url": url,
                                         "repo_name": "hyperledger-firefly/evmconnect"}))
                self.assertEqual(result.structured_content["other_repository"], expected)
        # For openjdk/lilliput, the classification read
        # git.openjdk.org/jdk/blob/master/doc/building.md, a web link that redirects to
        # github.com/openjdk/jdk, a page of another repository.
        web = _Web({"https://git.openjdk.example/jdk/blob/master/doc/building.md": _Page(
                        301, headers={"Location": "https://github.com/openjdk/jdk/blob/master/"
                                                  "doc/building.md"}),
                    "https://github.com/openjdk/jdk/blob/master/doc/building.md": _Page(
                        200, b"<html><body><p>Building the JDK.</p></body></html>",
                        {"Content-Type": "text/html"})})
        github = _GitHub({"https://api.github.com/repos/openjdk/jdk": _Answer(200, {
                              "id": 3, "full_name": "openjdk/jdk"}),
                          "https://api.github.com/repos/openjdk/lilliput": _Answer(200, {
                              "id": 4, "full_name": "openjdk/lilliput"})})
        server = tools.build_server(github=github, session=web, summarizer=_Summarizer())
        with _resolving({"git.openjdk.example": "93.184.216.34", "github.com": "140.82.121.4"}):
            result = asyncio.run(server.call_tool(
                "read_linked_page", {"url": "https://git.openjdk.example/jdk/blob/master/doc/"
                                            "building.md", "repo_name": "openjdk/lilliput"}))
        self.assertEqual((result.structured_content["url"],
                          result.structured_content["other_repository"]),
                         ("https://github.com/openjdk/jdk/blob/master/doc/building.md",
                          "openjdk/jdk"))

    def test_a_github_link_is_read_through_github_and_not_the_web(self):
        github = _GitHub({(f"{API}/readme", readmes.RAW): _Answer(200, content=b"# R")})
        web = _Web({})
        result, summarizer = self.call("https://github.com/o/r", github=github, web=web)
        self.assertTrue(result["available"])
        self.assertEqual(summarizer.given, [("o/r", "https://github.com/o/r", "# R")])
        self.assertEqual(web.asked, [])

    def test_a_github_link_with_a_dot_segment_is_not_read(self):
        """The client resolves a `..` segment against the segments before it. The link of a
        file could then request `https://api.github.com/user`, the account of the server's GitHub
        access token, and pass the account's data to the summary model and the cache of labels.
        The tool reads such a link neither through GitHub nor from the web. The same holds for a
        `.` segment, for dots percent-encoded once or twice, and for an encoded slash or
        backslashes. A file under `.github/` is read."""
        guide = f"{API}/contents/.github/CONTRIBUTING.md?ref=main"
        github = _GitHub({guide: _Answer(200, {"type": "file", "encoding": "base64",
                                               "content": base64.b64encode(b"Guide").decode()}),
                          "https://api.github.com/user": _Answer(200, {"login": "owner"})})
        for url in ("https://github.com/o/r/blob/main/../../../../user",
                    "https://github.com/o/r/blob/main/%2e%2e/%2E%2E/%2e%2e/%2e%2e/user",
                    "https://github.com/o/r/tree/main/docs/%252e%252e/%252E%252E/%252e%252e/"
                    "%252e%252e/%252e%252e/user",
                    "https://github.com/o/r/blob/main/docs%2F..%2F..%2F..%2F..%2F..%2Fuser",
                    "https://github.com/o/r/blob/main/..\\..\\..\\..\\user",
                    "https://github.com/o/r/blob/main/./CONTRIBUTING.md"):
            with self.subTest(url=url):
                web = _Web({})
                result, summarizer = self.call(url, github=github, web=web)
                self.assertEqual((result["available"], result["note"]),
                                 (False, "The tool does not read a link with a . or .. segment "
                                         "in its path"))
                self.assertEqual((github.asked, web.asked, summarizer.given), ([], [], []))
        result, summarizer = self.call("https://github.com/o/r/blob/main/.github/CONTRIBUTING.md",
                                       github=github)
        self.assertTrue(result["available"])
        self.assertEqual(github.asked, [(guide, readmes.JSON)])

    def test_a_summary_that_fails_is_raised_for_the_classification(self):
        """A failure of the summary model says nothing about the page. Reported to the model as
        an unavailable page, a retired or refused summary model would make every page unavailable
        and leave labels cached without the pages. The raised failure marks a refusal of every
        request of the summary model as a refusal, a refusal for spent credit (which a top-up
        ends) as spent credit, and any other failure as a failure of the summary."""
        def status(kind, code, http, metadata=None):
            response = SimpleNamespace(request=SimpleNamespace(), status_code=http, headers={})
            return kind("refused", response=response,
                        body={"code": code, "message": "refused", "metadata": metadata})
        web = _Web({"https://docs.example/": _Page(200, b"Docs.", {"Content-Type": "text/plain"})})
        for error, kind in ((RuntimeError("a defect"), tools.SUMMARY_FAILED),
                            (status(openai.InternalServerError, None, 500), tools.SUMMARY_FAILED),
                            (status(openai.NotFoundError, "model_not_found", 404),
                             tools.SUMMARY_REFUSED),
                            (status(openai.AuthenticationError, 401, 401), tools.SUMMARY_REFUSED),
                            (status(openai.RateLimitError, "insufficient_quota", 429),
                             tools.SUMMARY_NO_CREDIT),
                            (status(openai.APIStatusError, 402, 402), tools.SUMMARY_NO_CREDIT),
                            (status(openai.APIStatusError, 402, 402,
                                    {"limit_source": "openrouter_in_flight_budget"}),
                             tools.SUMMARY_FAILED)):
            class Failing(_Summarizer):
                async def __call__(self, repo_name, url, text, error=error):
                    raise error
            server = tools.build_server(github=_GitHub({}), session=web, summarizer=Failing())
            err = io.StringIO()
            with self.subTest(error=type(error).__name__), \
                    _resolving({"docs.example": "93.184.216.34"}), \
                    contextlib.redirect_stderr(err), self.assertRaises(ToolError) as raised:
                asyncio.run(server.call_tool("read_linked_page", {"url": "https://docs.example/",
                                                                  "repo_name": "o/r"}))
            self.assertEqual(tools.failure_of(str(raised.exception))[0], kind)
            self.assertEqual(err.getvalue(), "")

    def test_a_page_that_the_summary_model_refuses_is_unavailable(self):
        """The API refuses a summary request with HTTP 400 for the page that the request
        contains, such as a page past the summary model's context window or a page that its usage
        policy refuses, and it refuses the same request the same way every time. Raised as a
        failure of the summary, the refusal would fail every attempt at the label. The model is
        therefore told that the page is unavailable."""
        response = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})
        web = _Web({"https://docs.example/": _Page(200, b"Docs.", {"Content-Type": "text/plain"})})
        for code in ("context_length_exceeded", "invalid_prompt"):
            class Refusing(_Summarizer):
                async def __call__(self, repo_name, url, text, code=code):
                    raise openai.BadRequestError("refused", response=response,
                                                 body={"code": code, "message": "refused"})
            server = tools.build_server(github=_GitHub({}), session=web, summarizer=Refusing())
            with self.subTest(code=code), _resolving({"docs.example": "93.184.216.34"}):
                result = asyncio.run(server.call_tool("read_linked_page", {
                    "url": "https://docs.example/", "repo_name": "o/r"})).structured_content
                self.assertEqual((result["available"], result["summary"], result["note"]),
                                 (False, "", f"the summary model refused the page ({code})"))

    def test_a_page_that_openrouter_blocks_is_unavailable(self):
        """OpenRouter responds with HTTP 403 to a page that its moderation flags, and blocks the
        page again in every attempt. Read as a key without access, the block would stop the
        run."""
        response = SimpleNamespace(request=SimpleNamespace(), status_code=403, headers={})
        web = _Web({"https://docs.example/": _Page(200, b"Docs.", {"Content-Type": "text/plain"})})

        class Blocking(_Summarizer):
            async def __call__(self, repo_name, url, text):
                raise openai.PermissionDeniedError("blocked", response=response, body={
                    "code": 403, "message": "Your input was flagged",
                    "metadata": {"reasons": ["harassment"], "flagged_input": "..."}})
        server = tools.build_server(github=_GitHub({}), session=web, summarizer=Blocking())
        with _resolving({"docs.example": "93.184.216.34"}):
            result = asyncio.run(server.call_tool("read_linked_page", {
                "url": "https://docs.example/", "repo_name": "o/r"})).structured_content
        self.assertEqual((result["available"], result["summary"], result["note"]),
                         (False, "", "the summary model's service blocked the page "
                                     "(OpenRouter's moderation flagged the input (harassment))"))

    def test_a_page_that_cannot_be_read_is_not_summarized(self):
        web = _Web({"https://a.example/": _Page(404)})
        result, summarizer = self.call("https://a.example/", web=web,
                                       addresses={"a.example": "93.184.216.34"})
        self.assertEqual((result["available"], result["note"]),
                         (False, "the page returned HTTP 404"))
        self.assertEqual(summarizer.given, [])


if __name__ == "__main__":
    unittest.main()
