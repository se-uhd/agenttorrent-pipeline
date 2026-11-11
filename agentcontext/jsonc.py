"""Reading a settings file the way the tool that owns it reads it.

Most of the tools document their settings file as strict JSON. Claude Code's documentation
states it explicitly: "a `//` comment or a trailing comma is a syntax error, and Claude Code
reports the file as a Settings Error at the next start". Gemini CLI does the opposite, in its
source rather than in its documentation. `packages/cli/src/config/settings.ts` reads
`.gemini/settings.json` with `JSON.parse(stripJsonComments(content))`.

The format is therefore a property of the file, and `heuristics/config_path_patterns.csv`
records it in `content_format`. Reading a file more permissively than its own tool does would
record a configuration that the tool never applies, and reading it more strictly would lose a
configuration that the tool applies. A strict reading, for example, records no MCP for a
`.gemini/settings.json` that declares `mcpServers` beside a comment line.
"""

import json

__all__ = ["strip_comments", "loads"]


def strip_comments(text: str) -> str:
    """`text` with each `//` and `/* */` comment blanked out, and every string literal untouched.

    The function scans the text rather than matching a regular expression, because a URL
    contains `//` too. Over `{"url": "https://example.com"}`, a pattern that deletes to the end
    of the line leaves `{"url": "https:`, and the file stops parsing for a new reason. The scan
    tracks whether it is inside a string and whether the preceding character was a backslash,
    so that an escaped quote does not end the string.

    A comment becomes spaces, with its line breaks kept, rather than nothing.
    `strip-json-comments`, the package that Gemini CLI calls, does the same by default
    ("replace single-line comments `//` and multi-line comments `/**/` with whitespace").
    Removing the comment would change the result: `[1/**/2]` would read as `[12]`, where the
    tool reports a syntax error.

    An unterminated comment or string runs to the end of the text, and neither is valid JSON
    either way. The function gives `json.loads` the text that the tool would parse and
    validates nothing.
    """
    out = []
    i, end = 0, len(text)
    in_string = False
    while i < end:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < end:
                out.append(text[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < end and text[i + 1] in "/*":
            if text[i + 1] == "/":
                stop = text.find("\n", i)
            else:
                stop = text.find("*/", i + 2)
                stop = stop + 2 if stop != -1 else -1
            stop = end if stop == -1 else stop
            out.append("".join(c if c in "\r\n" else " " for c in text[i:stop]))
            i = stop
            continue
        out.append(char)
        i += 1
    return "".join(out)


def strip_trailing_commas(text: str) -> str:
    """Blank commas before closing brackets outside string literals."""
    out = list(text)
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == ",":
            next_index = index + 1
            while next_index < len(text) and text[next_index] in " \t\r\n":
                next_index += 1
            previous = index - 1
            while previous >= 0 and text[previous] in " \t\r\n":
                previous -= 1
            if (next_index < len(text) and text[next_index] in "}]"
                    and previous >= 0 and text[previous] not in "{[,:"):
                out[index] = " "
    return "".join(out)


def loads(text: str, content_format: str = ""):
    """Parse JSON using the comment and trailing-comma policy of its format.

    Raises what `json.loads` raises, so that a caller that treats a parse failure as no
    detection does the same for a file that is malformed for its own tool.
    """
    if content_format in ("jsonc", "jsonc_trailing"):
        text = strip_comments(text)
    if content_format == "jsonc_trailing":
        text = strip_trailing_commas(text)
    return json.loads(text)
