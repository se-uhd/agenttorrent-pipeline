# The bridge between the MCP server and the model

## Context

`mcp_bridge.py` lets the classification model call the tools of `classification_tools.py` (`classification-tools-spec.md`). The Responses API can reach an MCP server on its own only over the network. The bridge keeps the loop of tool calls local, so that nothing needs a public address and the server stays a plain MCP server.

## Key decisions

- **Language:** Python 3.14, in the single module `mcp_bridge.py`, with `mcp` for the client and `readmes.RECHECK_WAITS` for the time that a call may take
- The bridge, an async context manager, starts the server as a subprocess over stdio and keeps it running for as long as the bridge is open. The server is this repository's own script, and the bridge passes it the whole environment, because it needs the GitHub access tokens and `AGENTCONTEXT_CREDENTIALS`. By default, `stdio_client` passes on only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`
- The bridge offers each tool as a flat, strict function tool in the format that the Responses API takes. Every object of its schema lists all its properties as required and allows no other properties, and the bridge drops the titles that Pydantic writes
- The model is offered only the tools that the caller names (`offered`), every tool of the server by default, and the caller can call every tool. The classification offers `read_linked_page` alone and calls the other three tools itself before the first request (`classify-repos-spec.md`)
- The bridge keeps the output schema of every tool that the server lists, offered or not (`output_schemas`), because the classification parameters record the output schemas of the tools with results that the classification puts in the input
- The model is not shown the parameters that the caller supplies (`supplied`, per tool). The classification fills in the repository, the commit and the outline's `max_chars`, and a model that saw these parameters could set values that it must not set. The server still requires them, and another MCP client therefore sees the whole schema
- A tool's output is the structured content that its output schema describes, or else its text blocks
- For a call to a tool that the server lacks, and for a call that fails, the bridge returns to the model an error that the model can continue from, rather than ending the run. The error for a tool that the server lacks (`NO_SUCH_TOOL`) names only the offered tools, the ones that the model may call, with the tools that the caller offers itself (`own_tools`), such as the classification's tool of the checklist, `submit_classification`
- A call without a result after `CALL_SECONDS` raises `ToolTimeout`, and the client sends the server a cancellation of the call. Otherwise, a call that hangs would keep the worker that labels the repository, and with it the run, waiting without end. After the timeout, the result of the call is unknown. If the bridge returned the timeout to the model as an error, the model could decide a label without the page or the outline, and the cache of labels would keep that label. The classification therefore fails the attempt instead (`classify-repos-spec.md`). `CALL_SECONDS` is longer than the longest wait that a call may need. A call may wait for the server's checks of its GitHub access tokens once GitHub has rejected every one, which take the sum of `readmes.RECHECK_WAITS`. It may also wait for a rate limit of GitHub, which ends within the hour that GitHub's limits count, and for the summary request with the OpenAI client's retries. A shorter wait would end a call while the server still checks, and the attempt to label the repository would fail although GitHub may accept the GitHub access tokens again
- The bridge passes on a tool error that the server marks as a failure (`classification_tools.failure`) as it passes on any tool error, and the classification raises it (`classification-tools-spec.md`)
- A closed connection to the server, as when the server's process ended, raises `ServerGone`, and the classification stops the run on this exception. Every later call would fail in the same way. If the bridge returned the failure to the model as an error, the cache of labels would keep the labels that the model decided without the tools

## Module: `mcp_bridge.py`

### Function structure

**`strict_schema(schema)`**, **`function_tool(tool, supplied)`**, **`tool_output(result)`**: the translation of the server's tools and results for the Responses API

**`Bridge(server, arguments, python, supplied, errlog, call_seconds, offered)`**: the connection, with `function_tools`, `names`, `output_schemas` and `call(name, arguments)`. The server's stderr goes to `errlog`, the caller's stderr by default. A call may take `call_seconds`, `CALL_SECONDS` by default

**`ServerGone`**, **`ToolTimeout`**: the connection to the server is closed, and a call returned no result within `call_seconds`

## Files

| File | Action |
|------|--------|
| `mcp_bridge.py` | The module |
| `classification_tools.py` | Started |
| `readmes.py` | Imported for `RECHECK_WAITS` |
| `test_mcp_bridge.py` | Tests |

## Tests

See `test_mcp_bridge.py`, which uses the standard library's `unittest`. One test starts the real server over stdio and calls it only in ways that reach neither GitHub nor the web. Another starts a server that ends its process at its first tool call, and a third starts a server with a tool that never returns.

- **`TestTranslation`** (4): a schema is made strict and loses its titles; a tool is a flat function tool; the parameters that the caller supplies are not shown; structured content comes first
- **`TestTheServer`** (4): the bridge offers the server's tools and returns errors to the model, a failure that the server marks reaches the caller with its kind, and the server writes nothing to its stderr for a refused call; a server that ended stops the caller; a call without a result in time is raised for the caller, and the server stays usable; the model is offered only the tools that the caller names, the caller can still call the others, the output for a call of a tool that the server lacks names only the offered tools, and the bridge keeps the output schema of every tool as the server lists it

Run with:

```shell
python3 -m unittest test_mcp_bridge -v
```
