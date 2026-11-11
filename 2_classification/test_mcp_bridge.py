"""
Tests for mcp_bridge.py

Strategy:
  - The schema translation is checked on Pydantic's own output.
  - One case starts the real server, classification_tools.py, as a subprocess over stdio and
    calls it through the bridge. The tools offered to the model in that case are the tools that
    the server lists. The case reads nothing from GitHub or the web.

Requirements: Python 3.14, mcp, pydantic (requirements.txt)
Run with:  python3 -m unittest test_mcp_bridge -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classification_tools  # noqa: E402
import mcp_bridge  # noqa: E402


class TestTranslation(unittest.TestCase):

    def test_a_schema_is_made_strict_and_loses_its_titles(self):
        schema = {"title": "Arguments", "type": "object",
                  "properties": {"repo_name": {"title": "Repo Name", "type": "string"},
                                 "nested": {"type": "object",
                                            "properties": {"a": {"type": "string"}}}},
                  "required": ["repo_name"]}
        self.assertEqual(mcp_bridge.strict_schema(schema), {
            "type": "object", "additionalProperties": False, "required": ["repo_name", "nested"],
            "properties": {"repo_name": {"type": "string"},
                           "nested": {"type": "object", "properties": {"a": {"type": "string"}},
                                      "required": ["a"], "additionalProperties": False}}})

    def test_a_tool_is_a_flat_function_tool(self):
        tool = SimpleNamespace(name="t", description="d",
                               input_schema={"type": "object", "properties": {}})
        self.assertEqual(mcp_bridge.function_tool(tool),
                         {"type": "function", "name": "t", "description": "d",
                          "parameters": {"type": "object", "properties": {}, "required": [],
                                         "additionalProperties": False},
                          "strict": True})

    def test_the_parameters_the_caller_supplies_are_not_shown(self):
        """The model does not set the repository, the commit or the outline's room, because the
        classification fills them in."""
        tool = SimpleNamespace(name="t", description="d", input_schema={
            "type": "object", "required": ["url", "repo_name"],
            "properties": {"url": {"type": "string"}, "repo_name": {"type": "string"}}})
        parameters = mcp_bridge.function_tool(tool, ("repo_name",))["parameters"]
        self.assertEqual(parameters, {"type": "object", "properties": {"url": {"type": "string"}},
                                      "required": ["url"], "additionalProperties": False})
        self.assertIn("repo_name", tool.input_schema["properties"])

    def test_structured_content_comes_first(self):
        result = SimpleNamespace(structured_content={"available": False},
                                 content=[SimpleNamespace(text="ignored")])
        self.assertEqual(mcp_bridge.tool_output(result), '{"available": false}')
        self.assertEqual(mcp_bridge.tool_output(SimpleNamespace(
            structured_content=None, content=[SimpleNamespace(text="a"),
                                              SimpleNamespace(text="b")])), "a\nb")


class TestTheServer(unittest.TestCase):

    def test_the_bridge_offers_the_servers_tools_and_returns_errors_to_the_model(self):
        """The server's stderr is the classification's log, and the SDK's INFO line for the
        refused call is not written there."""
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8")
        self.addCleanup(errlog.close)

        async def session():
            async with mcp_bridge.Bridge(supplied={"get_file_tree": ("repo_name", "revision",
                                                                     "max_chars")},
                                         errlog=errlog) as bridge:
                unknown = await bridge.call("delete_everything", {})
                refused = await bridge.call("get_file_tree", {"repo_name": "../etc",
                                                              "revision": ""})
                # Without a GitHub access token in the environment, the server's pool is empty,
                # and the call fails before any request.
                tokenless = await bridge.call("get_file_tree", {"repo_name": "o/r",
                                                                "revision": ""})
                return bridge.function_tools, unknown, refused, tokenless

        with mock.patch.dict(os.environ, {"AGENTCONTEXT_CREDENTIALS": os.devnull,
                                          "GITHUB_TOKEN": "", "GITHUB_TOKENS": ""}):
            tools, unknown, refused, tokenless = asyncio.run(session())
        # The failure reaches the caller with its kind, which the classification raises.
        self.assertTrue(tokenless[1])
        self.assertEqual(classification_tools.failure_of(tokenless[0]),
                         (classification_tools.NO_GITHUB_ACCESS_TOKEN,
                          "GitHub accepts none of the configured GitHub access tokens"))
        self.assertIsNone(classification_tools.failure_of(refused[0]))
        self.assertEqual({t["name"]: sorted(t["parameters"]["properties"]) for t in tools},
                         {"get_file_tree": [], "read_linked_page": ["repo_name", "url"],
                          "get_repository_metadata": ["repo_name"],
                          "get_issues_and_pull_requests": ["repo_name"]})
        self.assertTrue(all(t["strict"] for t in tools))
        self.assertTrue(unknown[1])
        self.assertIn("There is no tool delete_everything", unknown[0])
        self.assertTrue(refused[1])
        self.assertIn("repo_name must be owner/repo", refused[0])
        errlog.seek(0)
        self.assertEqual(errlog.read(), "")

    def test_the_model_is_offered_only_the_tools_that_the_caller_names(self):
        """The classification reads the outline, the metadata and the issues and pull requests
        itself and offers the model `read_linked_page` alone, and it can still call the other
        tools. The output for a call of a tool that the server lacks names only the tools that the
        model may call, namely the offered tool and the tool of the checklist. The bridge keeps
        the output schema of every tool as the server lists it, because the classification
        parameters record the schemas of the three results that the input holds."""
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8")
        self.addCleanup(errlog.close)

        async def session():
            async with mcp_bridge.Bridge(offered=("read_linked_page",), errlog=errlog,
                                         own_tools=("submit_classification",)) as bridge:
                refused = await bridge.call("get_file_tree", {"repo_name": "../etc",
                                                              "revision": ""})
                unknown = await bridge.call("delete_everything", {})
                return ([tool["name"] for tool in bridge.function_tools], refused, unknown,
                        bridge.output_schemas)

        with mock.patch.dict(os.environ, {"AGENTCONTEXT_CREDENTIALS": os.devnull,
                                          "GITHUB_TOKEN": "", "GITHUB_TOKENS": ""}):
            names, refused, unknown, schemas = asyncio.run(session())
        self.assertEqual(names, ["read_linked_page"])
        self.assertTrue(refused[1])
        self.assertIn("repo_name must be owner/repo", refused[0])
        # The reply names the tool of the checklist, which the classification offers beside the
        # server's tools, so that a model that misnames it learns its name.
        self.assertEqual(unknown, ("There is no tool delete_everything. The tools are "
                                   "['read_linked_page', 'submit_classification'].", True))
        listed = asyncio.run(classification_tools.build_server().list_tools())
        self.assertEqual(schemas, {tool.name: tool.output_schema for tool in listed})
        self.assertEqual(sorted(schemas["get_repository_metadata"]["properties"]),
                         ["available", "created_at", "description", "has_issues", "homepage",
                          "is_template", "license", "note", "owner_description", "pushed_at",
                          "repo_name", "topics"])

    def test_a_server_that_ended_stops_the_caller(self):
        """After the server's process has ended, as after an out-of-memory kill, every later
        call raises `ServerGone`. Returned to the model as errors, the failed calls would let
        the classification keep labels that the model decided without the tools."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        server = directory / "ending_server.py"
        server.write_text(textwrap.dedent('''
            import os
            from mcp.server import MCPServer

            server = MCPServer(name="ending", log_level="WARNING")


            @server.tool(name="get_file_tree")
            async def get_file_tree(repo_name: str) -> str:
                """Ends the server's process."""
                os._exit(1)


            if __name__ == "__main__":
                server.run(transport="stdio")
            '''), encoding="utf-8")
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8")
        self.addCleanup(errlog.close)
        raised = []

        async def session():
            async with mcp_bridge.Bridge(server=server, errlog=errlog) as bridge:
                for _ in range(2):
                    try:
                        await asyncio.wait_for(bridge.call("get_file_tree", {"repo_name": "o/r"}),
                                               60)
                    except mcp_bridge.ServerGone as error:
                        raised.append(str(error))

        asyncio.run(session())
        self.assertEqual(len(raised), 2)
        self.assertIn("ending_server.py is closed", raised[0])

    def test_a_call_without_a_result_in_time_is_raised_for_the_caller(self):
        """A tool that never returns would keep the worker that labels the repository, and with
        it the run, waiting without end. The call's result is unknown. Returned to the model as
        an error, the unknown result would let a label be decided without the page. The bridge
        therefore raises `ToolTimeout` for the caller, which fails its attempt. The server stays
        usable for the next call."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        server = directory / "hanging_server.py"
        server.write_text(textwrap.dedent('''
            import asyncio
            from mcp.server import MCPServer

            server = MCPServer(name="hanging", log_level="WARNING")


            @server.tool(name="read_linked_page")
            async def read_linked_page(url: str) -> str:
                """Returns for one page and never for any other."""
                if url != "https://quick.example/":
                    await asyncio.sleep(3600)
                return "read"


            if __name__ == "__main__":
                server.run(transport="stdio")
            '''), encoding="utf-8")
        errlog = tempfile.TemporaryFile("w+", encoding="utf-8")
        self.addCleanup(errlog.close)

        async def session():
            async with mcp_bridge.Bridge(server=server, errlog=errlog,
                                         call_seconds=1) as bridge:
                try:
                    await asyncio.wait_for(
                        bridge.call("read_linked_page", {"url": "https://slow.example/"}), 60)
                except mcp_bridge.ToolTimeout as error:
                    hanging = str(error)
                else:
                    hanging = None
                quick = await asyncio.wait_for(
                    bridge.call("read_linked_page", {"url": "https://quick.example/"}), 60)
                return hanging, quick

        hanging, quick = asyncio.run(session())
        self.assertEqual(hanging, "the tool read_linked_page returned no result within 1 seconds")
        self.assertEqual(quick, ('{"result": "read"}', False))


if __name__ == "__main__":
    unittest.main()
