"""Connect the classification's MCP server to OpenAI's Responses API.

The Responses API can reach an MCP server on its own only over the network. The bridge keeps the
loop of tool calls local: it starts `classification_tools.py` as a subprocess over stdio, offers
its tools to the model as function tools, and runs the model's tool calls against the server.
Nothing needs a public address, and the server stays a plain MCP server that any MCP client can
use.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from readmes import RECHECK_WAITS  # noqa: E402

SERVER = HERE / "classification_tools.py"
# How long the bridge waits for the result of one tool call. Without the limit, a call that hangs
# would keep the worker that labels the repository, and with it the run, waiting without end. The
# limit is longer than the longest wait that a call may need. A call may wait for the server's
# checks of its GitHub access tokens once GitHub has rejected every one (`readmes.RECHECK_WAITS`),
# for a rate limit of GitHub to end within the hour that GitHub's limits count, and for the
# summary request with the OpenAI client's retries. A shorter limit would end a call while the
# server still checks, and the attempt to label the repository would fail although GitHub may
# accept the GitHub access tokens again.
CALL_SECONDS = sum(RECHECK_WAITS) + 7200
# The output for a call of a tool that the server lacks. It names only the tools that the model is
# offered, the caller's own tools among them, because the model may call only those.
NO_SUCH_TOOL = "There is no tool {name}. The tools are {tools}."


class ServerGone(Exception):
    """The connection to the server is closed, as when its process ended. The caller stops rather
    than return an error to the model, because every later call would fail the same way."""


class ToolTimeout(Exception):
    """A call returned no result within the bridge's time limit. The tool's result is unknown and
    is no evidence about the repository. The caller therefore fails its attempt rather than
    return an error to the model, which would then decide a label without the page or the
    outline."""


def strict_schema(schema: dict) -> dict:
    """A JSON Schema that the Responses API accepts in strict mode: every object lists all of
    its properties as required and allows no others, and the titles that Pydantic writes are
    dropped."""
    def strict(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: strict(v) for k, v in node.items() if k != "title"}
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [strict(v) for v in node]
        return node
    return strict(copy.deepcopy(schema))


def function_tool(tool: Any, supplied: tuple = ()) -> dict:
    """One MCP tool as a Responses API function tool, which is flat: name, description and
    parameters at the top level. The parameters in `supplied` are left out, because the caller
    fills them in, and a model shown them could set values that it must not set."""
    schema = copy.deepcopy(tool.input_schema or {"type": "object", "properties": {}})
    for name in supplied:
        schema.get("properties", {}).pop(name, None)
        if name in schema.get("required", []):
            schema["required"].remove(name)
    return {"type": "function", "name": tool.name, "description": tool.description or "",
            "parameters": strict_schema(schema), "strict": True}


def tool_output(result: Any) -> str:
    """A CallToolResult as the text of a function_call_output: the structured content that
    the server's output schema describes, else the text blocks."""
    structured = getattr(result, "structured_content", None)
    if structured:
        return json.dumps(structured, ensure_ascii=False)
    texts = [block.text for block in getattr(result, "content", None) or []
             if getattr(block, "text", None)]
    return "\n".join(texts) if texts else "(the tool returned nothing)"


class Bridge:
    """A connection to the MCP server, its tools offered as Responses API function tools.

    An async context manager: the server runs as long as the bridge is open.
    """

    def __init__(self, server: Path = SERVER, arguments: list | None = None,
                 python: str | None = None, supplied: dict | None = None, errlog=None,
                 call_seconds: float = CALL_SECONDS, offered: tuple | None = None,
                 own_tools: tuple = ()):
        """`supplied` names, per tool, the parameters that the caller fills in, which the model is
        not shown. `offered` names the tools that the model is offered, every tool of the server by
        default. The caller can call every tool of the server, offered or not. `own_tools` names
        the tools that the caller offers the model beside the server's, such as the tool of the
        checklist. The output for a call of a tool that the server lacks names them too. `errlog`
        is the file that receives the server's stderr, the caller's stderr by default.
        `call_seconds` is how long a call may take."""
        self.server, self.arguments = Path(server), list(arguments or [])
        self.supplied = dict(supplied or {})
        self.offered = offered
        self.own_tools = tuple(own_tools)
        self.errlog = errlog
        self.call_seconds = call_seconds
        self.python = python or sys.executable
        self.stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.function_tools: list[dict] = []
        self.names: set[str] = set()
        # The output schema of every tool of the server, offered or not, as the server lists it.
        # The classification calls some of these tools itself, puts their results in the model's
        # input and records their output schemas.
        self.output_schemas: dict[str, dict | None] = {}

    async def __aenter__(self) -> "Bridge":
        # stdio_client passes on only HOME, LOGNAME, PATH, SHELL, TERM and USER by default. The
        # server, this repository's own script, also needs the GitHub access tokens and
        # AGENTCONTEXT_CREDENTIALS.
        parameters = StdioServerParameters(command=self.python,
                                           args=[str(self.server), *self.arguments],
                                           env=dict(os.environ))
        self.stack = AsyncExitStack()
        read, write = await self.stack.enter_async_context(
            stdio_client(parameters, errlog=self.errlog or sys.stderr))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.function_tools = [function_tool(tool, tuple(self.supplied.get(tool.name, ())))
                               for tool in listed.tools
                               if self.offered is None or tool.name in self.offered]
        self.names = {tool.name for tool in listed.tools}
        self.output_schemas = {tool.name: tool.output_schema for tool in listed.tools}
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self.stack is not None:
            await self.stack.aclose()
        self.stack = self.session = None

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Run one tool call against the server, and return its output and whether it is an
        error.

        For a tool that the server does not have and for a call that fails, the model receives
        an error that it can continue from, and the run goes on. A call without a result after
        `call_seconds` raises `ToolTimeout`, and the client sends the server a cancellation of
        the call. A closed connection raises `ServerGone`. Returned to the model as an error, it
        would make every later tool call an error, and the cache of labels would keep the labels
        decided without the tools.
        """
        if self.session is None:
            raise RuntimeError("the bridge is not open")
        if name not in self.names:
            offered = sorted([*(tool["name"] for tool in self.function_tools), *self.own_tools])
            return NO_SUCH_TOOL.format(name=name, tools=offered), True
        try:
            result = await self.session.call_tool(name, arguments,
                                                  read_timeout_seconds=self.call_seconds)
        except MCPError as error:
            if error.code == CONNECTION_CLOSED:
                raise ServerGone(f"the connection to the MCP server {self.server.name} is closed "
                                 f"({error})") from error
            if error.code == REQUEST_TIMEOUT:
                raise ToolTimeout(f"the tool {name} returned no result within "
                                  f"{self.call_seconds:g} seconds") from error
            return f"The tool failed: {type(error).__name__}: {error}", True
        except Exception as error:  # noqa: BLE001 - returned to the model
            return f"The tool failed: {type(error).__name__}: {error}", True
        return tool_output(result), bool(getattr(result, "is_error", False))
