"""Tests for agentcontext.imports: the import heuristics and their grounding, one parser per
syntax against what each tool's own code or a run of the tool read, the chain of imports with
its depth, its limits and what each import reaches, the importing files of each row, and the
copy of the files that the imports reach out of a real clone.

The expected imports of Claude Code and Gemini CLI are what the vendors' own code returned for each
text: the import finder of Claude Code 2.1.303, run over the text with the Markdown lexer that it
bundles, and `findImports` of Gemini CLI at the commit that the section "Imports" of
docs/heuristics.md names. The expectations of Copilot CLI and Antigravity are what Copilot CLI
1.0.107 and Antigravity CLI 1.4.2 read in a workspace with the case's files, and those of Cursor
Bugbot are the examples of the posts of Cursor's staff that the row cites. The expectations of
`MAIL` are the reading of the dataset and not of a tool: The dataset starts no import at the `@` of
a mail link or a mail address, which the tools read as any other `@`. The expectations of
`NO_MAIL_LINK` are what Copilot CLI 1.0.101 read in a workspace with the case's files.

Requirements: Python 3.14, git on PATH (stdlib only)
Run with:  python3 -m unittest tests.test_imports -v
"""

import csv
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agentcontext import heuristics, imports
from agentcontext.collector import CollectionConfig, process_repo
from agentcontext.paths import COLLECTED_CSV_HEADER
from tests import _ambient_env
from tests._fixtures import _init_git_repo, _row_field

ROOT = Path(__file__).resolve().parents[1]
ROWS = imports.load_import_patterns()
NOT_IMPORTS = imports.load_not_imports()
BY_ID = {row["id"]: row for row in ROWS}


def _paths(found: list) -> list:
    return [each.path for each in found]


def _case_sensitive_filesystem() -> bool:
    """Whether two names that differ only in case can coexist where the tests build trees."""
    probe = Path(tempfile.mkdtemp())
    try:
        (probe / "aFile").write_text("x", encoding="utf-8")
        return not (probe / "afile").exists()
    finally:
        shutil.rmtree(probe, ignore_errors=True)


_CASE_SENSITIVE = _case_sensitive_filesystem()


# What Claude Code 2.1.303 imports from each text, as its own import finder returned it,
# resolved against a repository at `/repo` and written relative to it.
CLAUDE_CODE = {
    '@a.md': ['a.md'],
    'See @docs/x.md for details.': ['docs/x.md'],
    '`@a.md`': [],
    '```\n@a.md\n```': [],
    'text\n\n    @a.md\n': [],
    '- `@a.md`': [],
    '- item\n  ```\n  @a.md\n  ```': [],
    '<!-- @a.md -->': [],
    'user@example.com': [],
    '@scope/pkg': ['scope/pkg'],
    '@./a.md#section': ['a.md'],
    '@a\\ b.md': ['a b.md'],
    '@"a b.md"': [],
    '**@a.md**': ['a.md'],
    '(@a.md)': [],
    '@~/x.md': ['~/x.md'],
    '@/abs/x.md': ['../abs/x.md'],
    '@../x.md': ['../x.md'],
    '---\ntitle: @a.md\n---\nbody @b.md': ['b.md'],
    '<div>@a.md</div>': [],
    '@a.md,': ['a.md,'],
    '~~@a.md~~ @b.md': ['b.md'],
    '@a.md @a.md': ['a.md'],
    '1. @a.md': ['a.md'],
    '> @a.md': ['a.md'],
    '| @a.md | x |': ['a.md'],
    '@@a.md': [],
    'x@a.md': [],
    '\t@a.md': [],
    '@a.md\r\n@b.md': ['a.md', 'b.md'],
    '```js\n@a.md': [],
    '``@a.md``': [],
    '`` ` @a.md ``': [],
    '~~~\n@a.md\n~~~': [],
    '# Title @a.md': ['a.md'],
    '@.': ['.'],
    '@': [],
    '@a.md:12': ['a.md:12'],
    '@-a.md': ['-a.md'],
    '@_a.md': ['_a.md'],
    ' @a.md': ['a.md'],
    '*@a.md*': ['a.md'],
    '[x](@a.md)': [],
    '- `x @a.md`': ['a.md`'],
    '- see ` @a.md`': ['a.md`'],
    '- [ ] @a.md': ['a.md'],
    'Hi!@x.md': [],
    'a <b>@x.md</b>': ['x.md'],
    '\\@a.md': [],
    '@a.md\\': ['a.md'],
    '<!-- c -->@a.md': ['a.md'],
    '<!-- c --> @a.md': ['a.md'],
    '@#x.md': [],
    '@%x.md': [],
    '@(x).md': [],
    '@a.md\xa0b': ['a.md'],
    '@a.md\u3000@b.md': ['a.md', 'b.md'],
    '  - nested\n    - @deep.md': ['deep.md'],
    'Para\n@next.md': ['next.md'],
    '**bold** @a.md': ['a.md'],
    '_@a.md_': ['a.md'],
    '@a.MD': ['a.MD'],
    '@a.png': ['a.png'],
    '@dir/': ['dir'],
    '> - `x @q.md`': ['q.md`'],
    '- a\n\n  `x @loose.md`': ['loose.md`'],
    # Texts of the October collection that test where a masker can differ from Claude Code.
    '- a `b\nc` d\n  from `@x/y`.': [],
    '1. a\n\n   b\n\n   `npm i @angular/cli`': ['angular/cli`'],
    '*   x\n    *   y:\n        ```c\n        @import Foo;\n        ```': [],
    '4. **Build**: Combine:\n   - one\n   - Do NOT (e.g., "bump @microsoft/xxx from A to B")':
        ['microsoft/xxx'],
    '- x `@claude` mention, so\n  `p` picks it up\n\n  ```bash\n  > gh pr comment --body '
    '"Assigned to @claude"\n  ```': [],
    '> ```\n> @q.md\n> ```\n@r.md': ['r.md'],
    '[x][ref] @a.md\n\n[ref]: https://x': ['a.md'],
    '*   a\n    *   b\n    *   c\n        ```\n        @import y;': [],
}

# What `findImports` of Gemini CLI returns for each text, inside and outside the repository.
GEMINI_CLI = {
    '@a.md': ['a.md'],
    'See @docs/x.md for details.': ['docs/x.md'],
    '`@a.md`': [],
    '```\n@a.md\n```': [],
    'text\n\n    @a.md\n': ['a.md'],
    '- `@a.md`': [],
    '- item\n  ```\n  @a.md\n  ```': [],
    '<!-- @a.md -->': ['a.md'],
    'user@example.com': [],
    '@scope/pkg': ['scope/pkg'],
    '@./a.md#section': ['./a.md#section'],
    '@a\\ b.md': ['a\\'],
    '@"a b.md"': [],
    '**@a.md**': [],
    '(@a.md)': [],
    '@~/x.md': [],
    '@/abs/x.md': ['/abs/x.md'],
    '@../x.md': ['../x.md'],
    '---\ntitle: @a.md\n---\nbody @b.md': ['a.md', 'b.md'],
    '<div>@a.md</div>': [],
    '@a.md,': ['a.md,'],
    '~~@a.md~~ @b.md': ['b.md'],
    '@a.md @a.md': ['a.md', 'a.md'],
    '1. @a.md': ['a.md'],
    '> @a.md': ['a.md'],
    '| @a.md | x |': ['a.md'],
    '@@a.md': [],
    'x@a.md': [],
    '\t@a.md': ['a.md'],
    '@a.md\r\n@b.md': ['a.md', 'b.md'],
    '```js\n@a.md': ['a.md'],
    '``@a.md``': [],
    '`` ` @a.md ``': [],
    '~~~\n@a.md\n~~~': ['a.md'],
    '# Title @a.md': ['a.md'],
    '@.': ['.'],
    '@': [],
    '@a.md:12': ['a.md:12'],
    '@-a.md': [],
    '@_a.md': [],
    ' @a.md': ['a.md'],
    '*@a.md*': [],
    '[x](@a.md)': [],
    '- `x @a.md`': [],
    '- see ` @a.md`': [],
    '- [ ] @a.md': ['a.md'],
    'Hi!@x.md': [],
    'a <b>@x.md</b>': [],
    '\\@a.md': [],
    '@a.md\\': ['a.md\\'],
    '<!-- c -->@a.md': [],
    '<!-- c --> @a.md': ['a.md'],
    '@#x.md': [],
    '@%x.md': [],
    '@(x).md': [],
    '@a.md\xa0b': ['a.md\xa0b'],
    '@a.md\u3000@b.md': ['a.md\u3000@b.md'],
    '  - nested\n    - @deep.md': ['deep.md'],
    'Para\n@next.md': ['next.md'],
    '**bold** @a.md': ['a.md'],
    '_@a.md_': [],
    '@a.MD': ['a.MD'],
    '@a.png': ['a.png'],
    '@dir/': ['dir/'],
    '> - `x @q.md`': [],
    '- a\n\n  `x @loose.md`': [],
}

# What Copilot CLI 1.0.107 looked up for each text: the path before it is resolved. An `@` inside
# the path of another import, which contains no `/`, follows it, and the CLI looks it up only
# where that path names no file (`TestTheChain`).
COPILOT_CLI = {
    # The character before the `@`.
    'x@t06.md': [],
    'user@t19.md': [],
    'A@v01.md 1@s20.md _@s02.md .@s04.md a+@s13.md a%@s19.md a-@m01.md': [],
    '(@a.md [@b.md {@c.md "@d.md *@e.md |@f.md': ['a.md', 'b.md', 'c.md', 'd.md', 'e.md', 'f.md'],
    'é@x.md §@y.md': ['x.md', 'y.md'],
    'https://example.com/@user/a.md': ['user/a.md'],
    '\\@s31.md': ['s31.md'],
    '\t@a.md': ['a.md'],
    # The end of the path, and the characters that are removed from it.
    '@t54.md`x': ['t54.md'],
    '@t07.md.': ['t07.md'],
    '@x1.md). @x2.md.) @x3.md!? @x7.md,, @x9.md]} @t63.md...': [
        'x1.md', 'x2.md', 'x3.md', 'x7.md', 'x9.md', 't63.md'],
    '@s38.md/ @d/s.md/ @a.md)/': ['s38.md/', 'd/s.md/', 'a.md)/'],
    '@a,b.md @a)b.md @a:b.md': ['a,b.md', 'a)b.md', 'a:b.md'],
    '@s05.md* @s32.md\\ @s39.md-': ['s05.md*', 's32.md\\', 's39.md-'],
    # Whitespace that ends a path, and separators that are part of it.
    '@a.md\x0b @b.md\x85 @c.md\xa0 @d.md\u3000': ['a.md', 'b.md', 'c.md', 'd.md'],
    '@a.md\x1cb @c.md\x1f': ['a.md\x1cb', 'c.md\x1f'],
    '@s08.md#section @s09.md:12': ['s08.md#section', 's09.md:12'],
    # Mentions, which name neither a directory nor an extension.
    '@noext @Makefile @octocat @noext.': [],
    '@./noext @dir/noext2 @scope/pkg @a.b @.env': ['./noext', 'dir/noext2', 'scope/pkg', 'a.b',
                                                   '.env'],
    '@"t14 x.md" @t15\\ x.md': [],
    '@ @./': ['./'],
    '@@t39.md': ['t39.md'],
    '@@x/a.md': ['@x/a.md'],
    '@t37.md@t38.md': ['t37.md@t38.md'],
    '@\\~/x.md @\\a.md @a\\b.md': ['\\~/x.md', '\\a.md', 'a\\b.md'],
    # An `@` inside the path of an import.
    '@x/@b.md @./nm/@scope/pkg/AGENTS.md https://x.org/@b.md/@c.md': [
        'x/@b.md', './nm/@scope/pkg/AGENTS.md', 'b.md/@c.md'],
    '@x(@y/@b.md @x/y(@b.md @@x/@b.md': ['x(@y/@b.md', 'x/y(@b.md', '@x/@b.md'],
    '@x(@b.md @x,@b.md @x:@b.md': ['x(@b.md', 'b.md', 'x,@b.md', 'b.md', 'x:@b.md', 'b.md'],
    '@x(@b.md(@c.md': ['x(@b.md(@c.md', 'b.md(@c.md', 'c.md'],
    'x/y/@b.md z@q/@c.md': ['b.md', 'c.md'],
    # A fence: three or more of a character after any whitespace.
    '```\n@a.md\n```\n@b.md': ['b.md'],
    '~~~\n@a.md\n~~~\n@b.md': ['b.md'],
    '``\n@a.md\n``\n@b.md': ['a.md', 'b.md'],
    '~~\n@a.md\n~~\n@b.md': ['a.md', 'b.md'],
    '   ```\n@a.md\n```\n@b.md': ['b.md'],
    '    ```\n@a.md\n    ```\n@b.md': ['b.md'],
    '        ~~~\n@a.md\n        ~~~\n@b.md': ['b.md'],
    '```\n@a.md\n    ```\n@b.md': ['b.md'],
    '\t```\n@a.md\n \t```\n@b.md': ['b.md'],
    '\xa0```\n@a.md\n\u3000```\n@b.md': ['b.md'],
    '\x0b```\n@a.md\n\x0c```\n@b.md': ['b.md'],
    '\x1c```\n@a.md\n```\n@b.md': ['a.md'],
    '```\n@a.md\n\x1f```\n@b.md': [],
    '\ufeff```\n@a.md\n```\n@b.md': ['a.md'],
    '\u200b```\n@a.md\n```\n@b.md': ['a.md'],
    '- item\n\t```\n@a.md\n\t```': [],
    'text\n```\n@a.md\n```': [],
    # A block quote or a list item starts no fence, and its indented text opens no other.
    '> ```\n@a.md': ['a.md'],
    '> ```\n> @a.md\n> ```\n@b.md': ['a.md', 'b.md'],
    '- ```\n  @a.md\n  ```\n@b.md': ['a.md'],
    # The closing fence: the character of the opening fence.
    '~~~\n@a.md\n```\n@b.md': [],
    '```\n@a.md\n~~~\n@b.md': [],
    '```\n@a.md\n~~~\n@b.md\n```\n@c.md': ['c.md'],
    '~~~\n@a.md\n```\n@b.md\n~~~\n@c.md': ['c.md'],
    '~~~\n```\n@a.md\n```\n~~~\n@b.md': ['b.md'],
    # The closing fence: at least the length of the opening fence.
    '````\n@a.md\n```\n@b.md': [],
    '```\n@a.md\n````\n@b.md': ['b.md'],
    '````\n@a.md\n```\n@b.md\n````\n@c.md': ['c.md'],
    '~~~~\n@a.md\n~~~\n@b.md': [],
    '~~~\n@a.md\n~~~~\n@b.md': ['b.md'],
    '````markdown\n```\n@a.md\n```\n@b.md\n````\n@c.md': ['c.md'],
    '````\n```\n@a.md\n```\n````\n@b.md': ['b.md'],
    '``````````\n@a.md\n`````````\n@b.md\n``````````\n@c.md': ['c.md'],
    # The closing fence: only whitespace after it.
    '```\n@a.md\n``` \n@b.md': ['b.md'],
    '```\n@a.md\n```\t\n@b.md': ['b.md'],
    '```\n@a.md\n```\xa0\n@b.md': ['b.md'],
    '```\n@a.md\n```\x0c\n@b.md': ['b.md'],
    '~~~\n@a.md\n~~~~~~ \t \n@b.md': ['b.md'],
    '```\n@a.md\n```\x1c\n@b.md': [],
    '```\n@a.md\n```\u200b\n@b.md': [],
    '```js\n@a.md\n```js\n@b.md': [],
    '```js\n@a.md\n```js\n@b.md\n```\n@c.md': ['c.md'],
    '```\n@a.md\n``` js\n@b.md': [],
    '```\n@a.md\n``` @b.md\n@c.md\n```\n@d.md': ['d.md'],
    '~~~\n@a.md\n~~~ x\n@b.md\n~~~\n@c.md': ['c.md'],
    '```\n@a.md\n``` ```\n@b.md': [],
    '```\n@a.md\n```~\n@b.md': [],
    # The end of the file closes a fence.
    '```\n@a.md': [],
    '````\n@a.md\n```': [],
    '~~~\n@a.md': [],
    '@a.md\n```': ['a.md'],
    # A fence of backticks has no other backtick on its line, which is then text with a code span.
    # A fence of tildes can have either character.
    '``` a`b\n@a.md\n```\n@b.md': ['a.md'],
    '```a`\n@a.md\n```\n@b.md': ['a.md'],
    '````a`b\n@a.md\n````\n@b.md': ['a.md'],
    '``` ```\n@a.md\n```\n@b.md': ['a.md'],
    '``` x`y ``` @a.md\n@b.md': ['a.md', 'b.md'],
    '``` a`b @a.md\n@b.md': ['b.md'],
    '~~~ a`b\n@a.md\n~~~\n@b.md': ['b.md'],
    '~~~ a~b\n@a.md\n~~~\n@b.md': ['b.md'],
    # The rest of the line of an opening fence is no text.
    '``` @a.md\n@b.md\n```\n@c.md': ['c.md'],
    '~~~ @a.md\n@b.md\n~~~\n@c.md': ['c.md'],
    # Only a line feed ends a line.
    '```\r\n@a.md\r\n```\r\n@b.md\r': ['b.md'],
    '````\r\n@a.md\r\n```\r\n@b.md\r\n````\r\n@c.md\r\n': ['c.md'],
    '```\r@a.md\r```\r@b.md\r': ['b.md'],
    '```\n@a.md\n```\r@b.md\n@c.md': [],
    '```\r@a.md\n```\r@b.md': [],
    # Code spans.
    '``\n@a.md': ['a.md'],
    '`@c06.md` ``@c07.md`` `x` @c09.md': ['c09.md'],
    'a `b @in.md': [],
    'a `b\n@c.md` x': ['c.md'],
    '\\`@k26.md`': [],
    # Read as text.
    '    @a.md': ['a.md'],
    '<!-- @a.md -->': ['a.md'],
    '---\nx: @a.md\n---': ['a.md'],
    '<div>@a.md</div>': ['a.md'],
}

# What Antigravity CLI 1.4.2 inlined into a rule for each text.
ANTIGRAVITY = {
    '@[a](../inc/a.md)': ['../inc/a.md'],
    '```\n@[a](inc.md)\n```': ['inc.md'],
    '`@[a](inc.md)`': ['inc.md'],
    '~~~\n@[a](inc.md)\n~~~': ['inc.md'],
    '    @[a](inc.md)': ['inc.md'],
    'x@[a](inc.md)y': ['inc.md'],
    '@[](inc.md)': ['inc.md'],
    '@[two\nlines](inc.md)': ['inc.md'],
    '\\@[a](inc.md)': ['inc.md'],
    '@[a](inc.md) @[b](inc.md)': ['inc.md', 'inc.md'],
    # Taken as written, so that the tool finds no file there.
    '@[a]( inc.md )': [' inc.md '],
    '@[a](inc.md#x)': ['inc.md#x'],
    '@[a](<inc.md>)': ['<inc.md>'],
    # No include.
    '[a](inc.md)': [],
    '@inc.md': [],
}

# The links that the posts of Cursor's staff describe, and links that they rule out.
BUGBOT = {
    '[Security Rules](../docs/security-rules.md) [API Guidelines](../docs/api-guidelines.md)': [
        '../docs/security-rules.md', '../docs/api-guidelines.md'],
    'See [standards](../../docs/coding-standards.md).': ['../../docs/coding-standards.md'],
    '[r](rules/a.mdc) [r](b.MD)': ['rules/a.mdc', 'b.MD'],
    '[x](a.md#part) [y](b.md?raw=1)': ['a.md', 'b.md'],
    '[x](<a b.md>) [y](c.md "title")': ['a b.md', 'c.md'],
    '[x](a%20b.md) [y](a%ff.md)': ['a b.md', 'a%ff.md'],
    # Links that Bugbot does not follow, which have a row.
    '[y](/abs/a.md) [z](~/a.md) [x](a.txt) [y](a)': ['/abs/a.md', '~/a.md', 'a.txt', 'a'],
    # Not a link to a file.
    '[x](https://example.com/a.md) [w](#a.md) [m](mailto:a@b.md)': [],
    '![image](a.md)': [],
    # Not a link.
    '```\n[x](a.md)\n```': [],
    '`[x](a.md)`': [],
    '<!-- [x](a.md) -->': [],
}

# The includes of a root REVIEW.md as the page of Claude Code Review describes them: "a line ...
# with only @ and the file name on it", no "path into a subdirectory such as
# @docs/review-rules.md", "a name without spaces", and no "Markdown link to another file". The
# page's example comes first. A line in code, a carriage return before a line feed and a byte
# order mark are the readings of the section "Imports" of docs/heuristics.md.
CODE_REVIEW = {
    '# Review instructions\n\nReport at most five Nits per review.\n\n@review-security.md\n'
    '@review-api.md': ['review-security.md', 'review-api.md'],
    '@Review-API.md': ['Review-API.md'],
    # Not the whole line.
    'See @a.md': [],
    '@a.md for the API': [],
    ' @a.md': [],
    '@a.md ': [],
    '- @a.md': [],
    '`@a.md`': [],
    # A path, a name with a space, and no name.
    '@docs/review-rules.md': [],
    '@./a.md': [],
    '@my rules.md': [],
    '@a\tb.md': [],
    '@': [],
    # A Markdown link is no include.
    '[rules](a.md)': [],
    '- [rules](a.md)': [],
    # Readings.
    '```\n@a.md\n```': ['a.md'],
    '<!--\n@a.md\n-->': ['a.md'],
    '@a.md\r\n@b.md\r\n': ['a.md', 'b.md'],
    '\ufeff@a.md': ['a.md'],
}

# The skills that a root REVIEW.md includes: "Add @ and the path to SKILL.md, in the form
# .claude/skills/<folder>/SKILL.md, on a line of its own", "with the skill's folder directly under
# skills/", "Use a folder name without spaces", "keep the @ line out of code fences. Close every
# HTML comment and every <pre>, <script>, <style>, or <textarea> block above it", and "You can't
# include" "any other file under .claude/". The page's example comes first.
CODE_REVIEW_SKILLS = {
    '# Review instructions\n\nReport at most five Nits per review.\n\n'
    '@.claude/skills/api-review/SKILL.md\n\nTreat any change under `migrations/` as Important.\n':
        ['.claude/skills/api-review/SKILL.md'],
    '@review-api.md\n@.claude/skills/a/SKILL.md\r\n':
        ['review-api.md', '.claude/skills/a/SKILL.md'],
    # Not of the form.
    '@.claude/skills/a/b/SKILL.md': [],
    '@.claude/skills/a b/SKILL.md': [],
    '@.claude/skills/../SKILL.md': [],
    '@.claude/skills/a/README.md': [],
    '@.claude/skills/a/skill.md': [],
    '@.claude/agents/reviewer.md': [],
    '@.agents/skills/a/SKILL.md': [],
    '@./.claude/skills/a/SKILL.md': [],
    ' @.claude/skills/a/SKILL.md': [],
    '@.claude/skills/a/SKILL.md for the API': [],
    # In a code fence or an HTML block that a line above opened.
    '```\n@.claude/skills/a/SKILL.md\n```\n@.claude/skills/b/SKILL.md':
        ['.claude/skills/b/SKILL.md'],
    '~~~~\n```\n@.claude/skills/a/SKILL.md\n~~~~\n@.claude/skills/b/SKILL.md':
        ['.claude/skills/b/SKILL.md'],
    '```\n@.claude/skills/a/SKILL.md': [],
    '<!--\n@.claude/skills/a/SKILL.md\n-->\n@.claude/skills/b/SKILL.md':
        ['.claude/skills/b/SKILL.md'],
    '<!-- A note. -->\n@.claude/skills/a/SKILL.md': ['.claude/skills/a/SKILL.md'],
    '<PRE>\n@.claude/skills/a/SKILL.md\n</pre>\n@.claude/skills/b/SKILL.md':
        ['.claude/skills/b/SKILL.md'],
    '<textarea>\n@.claude/skills/a/SKILL.md': [],
    '<!--\n```\n-->\n@.claude/skills/a/SKILL.md': ['.claude/skills/a/SKILL.md'],
}

# Texts with the `@` of a mail link or a mail address, and the paths that `claude_at`,
# `copilot_at` and `gemini_at` read in each, in this order. The expectations are the reading of
# the dataset, in which such an `@` starts no import. The tools read it as any other `@`: Copilot
# CLI 1.0.101, run on test workspaces with a file at its path, read that file, such as
# `octocat](mailto:octocat@example.com` for the first text and `a.md` for `mailto:josé@a.md`.
MAIL = {
    '- [@octocat](mailto:octocat@example.com) reviews.\n\n@b.md':
        (['b.md'], ['b.md'], ['b.md']),
    'Write to [the reviewer @a.md](mailto:x@y.md) or @b.md': (['b.md'], ['b.md'], ['b.md']),
    '[@a.md](MAILTO:x@y.md) @b.md': (['b.md'], ['b.md'], ['b.md']),
    '- Write to [ @a.md](mailto:x@y.md)': ([], [], []),
    '[Write to\n@a.md](mailto:x@y.md)': ([], [], []),
    # A link by reference, full, collapsed or shortcut, and the label of its definition.
    'Write to [the reviewer @a.md][sec] or @b.md\n\n[sec]: mailto:x@y.md':
        (['b.md'], ['b.md'], ['b.md']),
    'Write to [the reviewer @a.md][] or @b.md\n\n[The  Reviewer @a.md]: mailto:x@y.md':
        (['b.md'], ['b.md'], ['b.md']),
    'Write to [the reviewer @a.md] or @b.md\n\n> [the reviewer @a.md]:\n>   <mailto:x@y.md>':
        (['b.md'], ['b.md'], ['b.md']),
    '[ @a.md]: mailto:x@y.md\n\n@b.md': (['b.md'], ['b.md'], ['b.md']),
    # The mail address of a `mailto:` URI and of an autolink.
    'Write to mailto:josé@a.md or @b.md': (['b.md'], ['b.md'], ['b.md']),
    'Write to <first/@a.md> or @b.md': (['b.md'], ['b.md'], ['b.md']),
    # The rest of the word after the link. In `copilot_at`, its next `@` starts the first import of
    # the word, unless a `/` follows the `@` of the link anywhere in the word, because Copilot CLI
    # starts no import inside a path with a `/`. Copilot CLI 1.0.101 read the same for each text
    # in a workspace with a file at the path of the later `@` and none at the path of the `@` of
    # the link. `claude_at` reads an `@` at the start of the text token after the link, and
    # `gemini_at` reads no `@` after `)` or `/`.
    '[@x](mailto:a@b.c)@d.md': (['d.md'], ['d.md'], []),
    '[@x](mailto:a@b.c)@d/e.md': (['d/e.md'], [], []),
    '[@x](mailto:a@b.c)/@d.md': ([], [], []),
}

# Texts with an `@` in brackets that form no mail link, and the paths that Copilot CLI 1.0.101
# read in each, as the text writes them, in a test workspace with a file at each path. In the
# first text, the link leads to an `https:` URI, and a `mailto:` URI follows it. In the second, a
# blank line ends the paragraph before the brackets close.
NO_MAIL_LINK = {
    '[see @a.md](https://x.org) or mailto:x@y.md': ['a.md](https://x.org'],
    '[write\n\n@a.md](mailto:x@y.md)': ['a.md](mailto:x@y.md'],
}


def _relative_to_repository(path: str) -> str:
    """A path as Claude Code's finder returned it, resolved against `/repo` and written relative
    to it."""
    if path.startswith("~"):
        return path
    return posixpath.relpath(posixpath.normpath(posixpath.join("/repo", path)), "/repo")


class TestTheHeuristicsAreGrounded(unittest.TestCase):
    """Every row cites the page or the post that states it, and every row names files that a
    path heuristic collects."""

    def test_every_row_cites_a_page_that_the_documentation_watch_reads(self):
        recorded = json.loads((ROOT / "heuristics" / "doc_hashes.json").read_text())
        for row in [*ROWS, *NOT_IMPORTS]:
            with self.subTest(row=row["id"]):
                self.assertIn(row["doc_url"], recorded,
                              "python -m agentcontext docs --update --new-only records it")
                self.assertTrue(row["notes"].strip())

    def test_a_forum_row_cites_a_post_of_the_vendor_forum(self):
        for row in [*ROWS, *NOT_IMPORTS]:
            with self.subTest(row=row["id"]):
                on_forum = row["doc_url"].startswith("https://forum.cursor.com/")
                self.assertEqual(row["source"] == "forum", on_forum)
                if on_forum:
                    self.assertIn("staff", row["notes"].lower())

    def test_every_importing_file_is_a_configuration_artifact(self):
        """Each row names its importing files with the path and the match mode of a row of
        config_path_patterns.csv, so that an import starts only at a file that the dataset
        collects."""
        for row in ROWS:
            with self.subTest(row=row["id"]):
                found = imports.config_row(row)
                self.assertIsNotNone(found)
                self.assertIn(found["tool"], (row["tool"], "any", "claude"))

    def test_every_syntax_has_a_parser_and_every_parser_a_row(self):
        self.assertEqual({row["syntax"] for row in ROWS}, set(imports.PARSERS))

    def test_every_negative_names_files_that_the_dataset_collects(self):
        config = heuristics.load_config_path_patterns()
        for row in NOT_IMPORTS:
            with self.subTest(row=row["id"]):
                self.assertTrue(any(found["path"] == row["path"]
                                    or heuristics.heuristic_matches(found, row["path"])
                                    for found in config))

    # The triggers of the Antigravity rules with includes that reach the model only on request. The
    # file of every other negative has the trigger with which Antigravity loads a rule always.
    _TRIGGERS = {"gemini-not-import-03": ("model_decision", "manual")}

    def test_no_negative_is_read_as_an_import(self):
        """The file of each negative, in a repository of its own, starts no chain of the
        negative's tool and syntax. A negative that names a directory, such as `.agents/rules`,
        gets a file in it, as the path row with the same path reads it."""
        config = heuristics.load_config_path_patterns()
        for negative in NOT_IMPORTS:
            for trigger in self._TRIGGERS.get(negative["id"], ("always_on",)):
                with self.subTest(row=negative["id"], trigger=trigger):
                    path = negative["path"]
                    found = next((row for row in config if row["path"] == path), None) or next(
                        row for row in config if heuristics.heuristic_matches(row, path))
                    mode = found["match_mode"]
                    rel = negative["path"]
                    if mode.startswith(("tree_ext:", "tree_suffix:", "dir_ext_triggered:")):
                        rel = f"{negative['path']}/x{mode.split(':', 1)[1]}"
                    elif mode == "skill_dir_flat":
                        rel = f"{negative['path']}/x/SKILL.md"
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f"---\ntrigger: {trigger}\n---\n"
                                        "@a.md @[a](a.md) [a](a.md)\n")
                        (Path(tmp) / "a.md").write_text("A\n")
                        tree = imports.CloneTree(Path(tmp))
                        started = imports.importing_files(ROWS, Path(tmp), [rel], tree)
                    self.assertEqual([row["id"] for row, _path in started
                                      if (row["tool"], row["syntax"])
                                      == (negative["tool"], negative["syntax"])], [])


class TestTheLoaderRefusesARowWithoutGrounds(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _load(self, **changes):
        path = self.tmp / "import_patterns.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=imports.FIELDNAMES)
            writer.writeheader()
            writer.writerow({**{column: ROWS[0][column] for column in imports.FIELDNAMES},
                             **changes})
        return imports.load_import_patterns(path)

    def test_the_real_row_loads(self):
        self.assertEqual(self._load()[0]["id"], ROWS[0]["id"])

    def test_each_problem_is_refused(self):
        cases = {"doc_url": ("http://x.test", "doc_url"),
                 "doc_verified": ("19 October", "doc_verified"),
                 "notes": ("", "no note"),
                 "id": ("claude-import-x", "has the id"),
                 "tool": ("vscode", "has the tool"),
                 "source": ("blog", "has the source"),
                 "syntax": ("at_sign", "has the syntax"),
                 "resolution": ("root", "has the resolution"),
                 "max_depth": ("0", "max_depth"),
                 "max_files": ("many", "max_files"),
                 "extensions": ("md", "extensions"),
                 "condition": ("always", "names the conditions"),
                 "match_mode": ("glob", "match_mode")}
        for column, (value, message) in cases.items():
            with self.subTest(column=column):
                with self.assertRaises(ValueError) as caught:
                    self._load(**{column: value})
                self.assertIn(message, str(caught.exception))

    def test_a_row_that_requires_utf8_has_a_limit(self):
        with self.assertRaises(ValueError) as caught:
            self._load(condition="utf8", max_file_bytes="", max_bytes="")
        self.assertIn("The condition utf8 of", str(caught.exception))
        self.assertIn("needs max_file_bytes or max_bytes", str(caught.exception))

    def test_a_row_that_counts_the_importing_file_has_a_byte_limit(self):
        with self.assertRaises(ValueError) as caught:
            self._load(condition="importing_file_bytes", max_file_bytes="4194304", max_bytes="")
        self.assertIn("The condition importing_file_bytes of", str(caught.exception))
        self.assertIn("needs max_bytes", str(caught.exception))
        self.assertEqual(self._load(condition="importing_file_bytes", max_bytes="10")[0]["id"],
                         ROWS[0]["id"])

    def test_a_row_reads_the_files_of_a_root_file_row(self):
        self.assertEqual(self._load(path="REVIEW.md", match_mode="root_file")[0]["match_mode"],
                         "root_file")

    def test_a_negative_names_the_file_and_the_syntax(self):
        path = self.tmp / "not_imports.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=imports.NOT_IMPORTS_FIELDNAMES)
            writer.writeheader()
            writer.writerow({**NOT_IMPORTS[0], "syntax": ""})
        with self.assertRaises(ValueError) as caught:
            imports.load_not_imports(path)
        self.assertIn("names the file and the syntax", str(caught.exception))


class TestTheParsers(unittest.TestCase):
    """Each parser against the tool's own code or a run of the tool."""

    def test_claude_code(self):
        for text, expected in CLAUDE_CODE.items():
            with self.subTest(text=text):
                self.assertEqual([_relative_to_repository(path)
                                  for path in _paths(imports.claude_at(text))], expected)

    def test_where_the_masker_differs_from_claude_code(self):
        """The two texts of the comparison over the collected files in which the import finder
        returns another path than Claude Code. In both, neither path names a file of the
        repository. Claude Code imports `Files` from the first text and nothing from the
        second."""
        self.assertEqual(_paths(imports.claude_at("* **[**@Files**](https://x) & y**")),
                         ["Files**"])
        self.assertEqual(_paths(imports.claude_at(
            "[![@a@b.pub][@a@b.pub badge]][@a@b.pub]\n\n"
            "[@a@b.pub badge]: https://x/@a@b.pub/f.svg\n[@a@b.pub]: https://h/@a")),
            ["a@b.pub"])

    def test_gemini_cli(self):
        for text, expected in GEMINI_CLI.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.gemini_at(text)), expected)

    def test_copilot_cli(self):
        for text, expected in COPILOT_CLI.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.copilot_at(text)), expected)

    def test_an_at_of_a_mail_link_or_a_mail_address_starts_no_import(self):
        for text, expected in MAIL.items():
            for parse, paths in zip((imports.claude_at, imports.copilot_at, imports.gemini_at),
                                    expected):
                with self.subTest(text=text, parser=parse.__name__):
                    self.assertEqual(_paths(parse(text)), paths)

    def test_copilot_cli_reads_an_at_in_brackets_that_form_no_mail_link(self):
        for text, expected in NO_MAIL_LINK.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.copilot_at(text)), expected)

    def test_the_import_after_a_mail_link_is_the_first_of_its_word(self):
        """The `@` of the link names no file, and Copilot CLI reads the next import of the word
        whatever the imports of the words before it named. Copilot CLI 1.0.101 read a.md and
        d.md(@e.md for this text, and e.md in place of d.md(@e.md where no file had that path."""
        self.assertEqual([(each.path, each.inner)
                          for each in imports.copilot_at("@a.md [@x](mailto:m@n.o)@d.md(@e.md")],
                         [("a.md", False), ("d.md(@e.md", False), ("e.md", True)])

    def test_antigravity(self):
        for text, expected in ANTIGRAVITY.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.antigravity_include(text)), expected)

    def test_cursor_bugbot(self):
        for text, expected in BUGBOT.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.markdown_link(text)), expected)

    def test_claude_code_review(self):
        for text, expected in CODE_REVIEW.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.code_review_include(text)), expected)

    def test_claude_code_review_includes_a_skill(self):
        for text, expected in CODE_REVIEW_SKILLS.items():
            with self.subTest(text=text):
                self.assertEqual(_paths(imports.code_review_include(text)), expected)

    def test_an_include_of_claude_code_review_records_its_line(self):
        self.assertEqual([(each.written, each.line, each.path) for each in
                          imports.code_review_include("# Rules\n\nSee @a.md\n@b.md\n")],
                         [("@b.md", 4, "b.md")])

    def test_each_import_records_its_line_and_the_text_as_written(self):
        text = "# Rules\n\nSee @docs/a.md#part\nand @b.md.\n"
        self.assertEqual([(each.written, each.line, each.path)
                          for each in imports.claude_at(text)],
                         [("@docs/a.md#part", 3, "docs/a.md"), ("@b.md.", 4, "b.md.")])
        self.assertEqual([(each.written, each.line, each.path)
                          for each in imports.copilot_at(text)],
                         [("@docs/a.md#part", 3, "docs/a.md#part"), ("@b.md", 4, "b.md")])
        self.assertEqual([(each.line, each.path) for each in imports.antigravity_include(
            "x\n@[a\nb](c.md)")], [(2, "c.md")])

    def test_a_byte_order_mark_does_not_hide_the_first_import(self):
        self.assertEqual(_paths(imports.claude_at("\ufeff@a.md")), ["a.md"])

    def test_an_import_after_a_comment_block_records_its_own_line(self):
        """Claude Code reads an HTML block that starts with a comment without its comments,
        and the import after the comment is on the line where it is written."""
        self.assertEqual([(each.line, each.path) for each in imports.claude_at(
            "<!--\nc\n--> @a.md\n<!-- d --> @b.md")], [(3, "a.md"), (4, "b.md")])


class TestLargeTexts(unittest.TestCase):
    """A tool reads targets of any size and parses them for the next hop. A parser therefore
    has to run in time about linear in the text and give imports about linear in it."""

    def test_a_line_of_minified_css_gives_a_few_imports_at_once(self):
        """Every `@` of `@media(min-width:1px){...}` starts a word that runs to the end of the
        line. No file can have a path that long, and the parser keeps one shortened import of
        those and the imports of the last few `@`."""
        started = time.monotonic()
        found = imports.copilot_at("@media(min-width:1px){.a{b:c}}" * 3400)
        self.assertLess(time.monotonic() - started, 1)
        self.assertLessEqual(max(len(each.path) for each in found), imports.PATH_MAX)
        self.assertEqual([each.too_long for each in found].count(True), 1)
        self.assertLessEqual(len(found), 10)

    @staticmethod
    def growth(parse, small: str, large: str) -> tuple:
        """How many times as long `parse` takes on `large` as on `small`, and what it finds in
        `large`. The ratio is that of the shortest of three timings of each text on the processor
        time of this thread, which leaves out the time in which other processes run. The timings
        of the two texts alternate, so that a change in the speed of the machine affects both
        texts alike."""
        timings, found = {"small": [], "large": []}, None
        for _ in range(3):
            for name, text in (("small", small), ("large", large)):
                started = time.thread_time()
                found = parse(text)
                timings[name].append(time.thread_time() - started)
        return min(timings["large"]) / min(timings["small"]), found

    def test_a_long_changelog_is_parsed_in_time_linear_in_its_length(self):
        """Lines that each have an `@`, a code span and a path of their own. The parser must not
        search Gemini CLI's code regions for each `@` or count the line of each import from the
        start of the text, which would make four times the lines take about sixteen times as
        long. The test bounds the ratio of the two times rather than a time, so that it holds on a
        machine of any speed."""
        def changelog(lines: int) -> str:
            return "".join(f"- @user fixed `x` in v1.2 by @other/a{n}.md, see @docs/c.md\n"
                           for n in range(lines))

        small, large = changelog(10000), changelog(40000)
        for parse in (imports.gemini_at, imports.claude_at, imports.copilot_at,
                      imports.markdown_link):
            with self.subTest(parser=parse.__name__):
                ratio, found = self.growth(parse, small, large)
                self.assertLess(ratio, 8)
                if found:
                    self.assertEqual(found[-1].line, 40000)

    def test_runs_of_backticks_that_nothing_closes_are_masked_in_seconds(self):
        """A megabyte of runs of backticks of each length, none of which closes another. The
        masker must not search to the end of the text for each run."""
        text = " ".join("`" * length for length in range(1, 1400)) + " @x.md"
        for parse in (imports.claude_at, imports.copilot_at):
            with self.subTest(parser=parse.__name__):
                started = time.monotonic()
                parse(text)
                self.assertLess(time.monotonic() - started, 3)


class _Tree:
    """A repository in a temporary directory, read as a clone (`imports.CloneTree`)."""

    def __init__(self, test: unittest.TestCase, files: dict, links: dict = None):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for rel, content in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            elif content is None:
                path.mkdir()
            else:
                path.write_text(content, encoding="utf-8")
        for rel, target in (links or {}).items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, self.root / rel)
        self.tree = imports.CloneTree(self.root)

    def chain(self, row_id: str, root: str, **changes) -> list:
        return [(record.importer, record.depth, record.target, record.status)
                for record in imports.chain(dict(BY_ID[row_id], **changes), root, self.tree)]


class TestTheChain(unittest.TestCase):

    def test_claude_code_reads_four_hops(self):
        files = {"CLAUDE.md": "@a.md\n", "a.md": "@b.md\n", "b.md": "@c.md\n", "c.md": "@d.md\n",
                 "d.md": "@e.md\n", "e.md": "@f.md\n", "f.md": "F\n"}
        self.assertEqual(_Tree(self, files).chain("claude-import-01", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "a.md", "found"), ("a.md", 2, "b.md", "found"),
                          ("b.md", 3, "c.md", "found"), ("c.md", 4, "d.md", "found")])

    def test_copilot_cli_reads_eight_hops(self):
        files = {"AGENTS.md": "@r01.md\n"}
        files.update({f"r{n:02}.md": f"@r{n + 1:02}.md\n" for n in range(1, 12)})
        found = _Tree(self, files).chain("copilot-import-02", "AGENTS.md")
        self.assertEqual([target for _i, _d, target, _s in found],
                         [f"r{n:02}.md" for n in range(1, 9)])

    def test_a_file_is_read_once_per_chain_and_a_cycle_ends(self):
        tree = _Tree(self, {"CLAUDE.md": "@a.md @b.md\n", "a.md": "@CLAUDE.md @b.md\n",
                            "b.md": "@a.md\n"})
        self.assertEqual(tree.chain("claude-import-01", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "a.md", "found"), ("a.md", 2, "CLAUDE.md", "found"),
                          ("a.md", 2, "b.md", "found"), ("b.md", 3, "a.md", "found"),
                          ("CLAUDE.md", 1, "b.md", "found")])

    def test_gemini_cli_records_the_imports_of_a_file_once_per_chain(self):
        """Gemini CLI reads a file again for each import that reaches it, and the chain records
        the imports of each file once, at the smallest depth at which the tool reads it, breadth
        first: x.md twice, and the import of y.md in x.md once."""
        tree = _Tree(self, {"GEMINI.md": "@x.md\n@x.md\n", "x.md": "@y.md\n", "y.md": "Y\n"})
        self.assertEqual(tree.chain("gemini-import-01", "GEMINI.md"),
                         [("GEMINI.md", 1, "x.md", "found"), ("GEMINI.md", 1, "x.md", "found"),
                          ("x.md", 2, "y.md", "found")])
        tree = _Tree(self, {"GEMINI.md": "@x.md\n", "x.md": "@GEMINI.md\n"})
        self.assertEqual(tree.chain("gemini-import-01", "GEMINI.md"),
                         [("GEMINI.md", 1, "x.md", "found"), ("x.md", 2, "GEMINI.md", "found")])

    def test_files_that_import_each_other_give_a_chain_of_bounded_size(self):
        """Ten files that import each other, which Gemini CLI reads in a number of branches
        that grows exponentially with its five levels, give each import once and every file
        that the tool reads."""
        files = {"GEMINI.md": "".join(f"@./docs/f{i}.md\n" for i in range(10))}
        for i in range(10):
            files[f"docs/f{i}.md"] = "".join(f"@./f{j}.md\n" for j in range(10) if j != i)
        records = _Tree(self, files).chain("gemini-import-01", "GEMINI.md")
        self.assertLessEqual(len(records), 110)
        self.assertEqual({target for _i, _d, target, status in records if status == "found"},
                         {f"docs/f{i}.md" for i in range(10)})

    def test_antigravity_inlines_one_hop(self):
        tree = _Tree(self, {"AGENTS.md": "@[a](a.md) @[a](a.md)\n", "a.md": "@[b](b.md)\n",
                            "b.md": "B\n"})
        self.assertEqual(tree.chain("gemini-import-05", "AGENTS.md"),
                         [("AGENTS.md", 1, "a.md", "found"), ("AGENTS.md", 1, "a.md", "found")])

    def test_claude_code_review_includes_the_files_beside_the_root_review_md(self):
        """The page's example, with an include in an included file, which "stays as plain
        text", and therefore one level."""
        tree = _Tree(self, {"REVIEW.md": "# Review instructions\n\nReport at most five Nits per "
                                         "review.\n\n@review-security.md\n@review-api.md\n",
                            "review-security.md": "Check the input.\n@deeper.md\n",
                            "review-api.md": "Keep the API stable.\n", "deeper.md": "D\n"})
        self.assertEqual(tree.chain("claude-import-07", "REVIEW.md"),
                         [("REVIEW.md", 1, "review-security.md", "found"),
                          ("REVIEW.md", 1, "review-api.md", "found")])

    def test_claude_code_review_includes_a_name_of_the_root_in_its_case(self):
        """"Location: move the file to the repository root." "Name: match the file name
        exactly, including case, and use a name without spaces." A file system that ignores
        case finds Review-API.md for review-api.md, and Code Review does not."""
        tree = _Tree(self, {"REVIEW.md": "See @notes.md\n@docs/review-rules.md\n@my rules.md\n"
                                         "@review-api.md\n@Review-API.md\n",
                            "notes.md": "N\n", "docs/review-rules.md": "D\n",
                            "my rules.md": "M\n", "Review-API.md": "A\n"})
        self.assertEqual(tree.chain("claude-import-07", "REVIEW.md"),
                         [("REVIEW.md", 1, "review-api.md", "missing"),
                          ("REVIEW.md", 1, "Review-API.md", "found")])

    def test_claude_code_review_includes_eight_different_files(self):
        """"include at most eight different files": a line of a name that matches no file is
        no include, and a file included twice is one of the eight."""
        names = [f"r{n}.md" for n in range(1, 11)]
        text = "@missing.md\n" + "".join(f"@{name}\n" for name in names[:8]) + "@r1.md\n" + \
            "".join(f"@{name}\n" for name in names[8:])
        tree = _Tree(self, {"REVIEW.md": text, **{name: f"{name}\n" for name in names}})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-07", "REVIEW.md")],
                         [("missing.md", "missing"), *((name, "found") for name in names[:8]),
                          ("r1.md", "found"), ("r9.md", "over_limit"), ("r10.md", "over_limit")])

    def test_claude_code_review_counts_review_md_toward_its_byte_limit(self):
        """"keep REVIEW.md and the files it includes under 150,000 bytes combined. A file that
        would reach the byte limit is skipped whole, not cut short." b.md would take REVIEW.md
        and a.md to the limit, and c.md, a byte smaller, to one byte below it. The imported
        tree holds a.md and c.md whole and a stub of b.md."""
        limit = 150_000
        review = "@a.md\n@b.md\n@c.md\n" + "x" * 140_000 + "\n"
        remainder = limit - len(review) - 1000
        tree = _Tree(self, {"REVIEW.md": review, "a.md": "a" * 1000, "b.md": "b" * remainder,
                            "c.md": "c" * (remainder - 1)})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-07", "REVIEW.md")],
                         [("a.md", "found"), ("b.md", "over_limit"), ("c.md", "found")])
        imported = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, imported, ignore_errors=True)
        imports.collect_imports(tree.root, tree.root, imported / "o§r", rows=[BY_ID[
            "claude-import-07"]])
        self.assertEqual((imported / "o§r" / "a.md").read_text(), "a" * 1000)
        self.assertEqual((imported / "o§r" / "c.md").read_text(), "c" * (remainder - 1))
        self.assertNotIn(b"b", (imported / "o§r" / "b.md").read_bytes())
        self.assertEqual(imports.read_stubs(imported / "o§r"), {"b.md"})

    def test_claude_code_review_reads_a_skill_up_to_its_own_size(self):
        """"Size: keep SKILL.md at 30,000 bytes or less, frontmatter included." The imports of
        a skill are not read: "Code Review doesn't load scripts, linked files, or @file
        references". A path in another case than the skill's folder "matches no file", also
        on a file system that ignores case."""
        tree = _Tree(self, {"REVIEW.md": "@.claude/skills/a/SKILL.md\n@.claude/skills/b/SKILL.md\n"
                                         "@.claude/skills/C/SKILL.md\n",
                            ".claude/skills/a/SKILL.md": "@notes.md\n" + "a" * 29_990,
                            ".claude/skills/b/SKILL.md": "b" * 30_001,
                            ".claude/skills/c/SKILL.md": "C\n", "notes.md": "N\n"})
        self.assertEqual(tree.chain("claude-import-07", "REVIEW.md"),
                         [("REVIEW.md", 1, ".claude/skills/a/SKILL.md", "found"),
                          ("REVIEW.md", 1, ".claude/skills/b/SKILL.md", "over_limit"),
                          ("REVIEW.md", 1, ".claude/skills/C/SKILL.md", "missing")])

    def test_claude_code_review_reads_eight_skill_lines(self):
        """"Count: write at most eight skill lines. A repeated line counts again toward the
        eight." The ninth skill line repeats the second and is skipped."""
        skills = [f".claude/skills/s{n}/SKILL.md" for n in range(1, 8)]
        text = "".join(f"@{path}\n" for path in skills) + f"@{skills[0]}\n@{skills[1]}\n"
        tree = _Tree(self, {"REVIEW.md": text, **{path: "S\n" for path in skills}})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-07", "REVIEW.md")],
                         [*((path, "found") for path in skills), (skills[0], "found"),
                          (skills[1], "over_limit")])

    def test_claude_code_review_counts_a_skill_toward_the_eight_files(self):
        """"The skill file counts toward the limits of eight files and 150,000 bytes": after
        seven files beside REVIEW.md, the second skill is the ninth file."""
        names = [f"r{n}.md" for n in range(1, 8)]
        text = ("".join(f"@{name}\n" for name in names)
                + "@.claude/skills/a/SKILL.md\n@.claude/skills/b/SKILL.md\n")
        tree = _Tree(self, {"REVIEW.md": text, **{name: f"{name}\n" for name in names},
                            ".claude/skills/a/SKILL.md": "A\n", ".claude/skills/b/SKILL.md": "B\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-07", "REVIEW.md")],
                         [*((name, "found") for name in names),
                          (".claude/skills/a/SKILL.md", "found"),
                          (".claude/skills/b/SKILL.md", "over_limit")])

    def test_claude_code_review_follows_no_link_to_a_skill(self):
        """"Code Review doesn't follow a symbolic link, whether the link is the file or a
        folder in its path." The page names no such rule for a file beside REVIEW.md, which
        the row reads through its link."""
        tree = _Tree(self, {"REVIEW.md": "@.claude/skills/a/SKILL.md\n@.claude/skills/b/SKILL.md\n"
                                         "@rules.md\n",
                            "shared/a/SKILL.md": "A\n", "shared/b/SKILL.md": "B\n",
                            "docs/rules.md": "R\n"},
                     links={".claude/skills/a/SKILL.md": "../../../shared/a/SKILL.md",
                            ".claude/skills/b": "../../shared/b", "rules.md": "docs/rules.md"})
        self.assertEqual(tree.chain("claude-import-07", "REVIEW.md"),
                         [("REVIEW.md", 1, ".claude/skills/a/SKILL.md", "not_read"),
                          ("REVIEW.md", 1, ".claude/skills/b/SKILL.md", "not_read"),
                          ("REVIEW.md", 1, "rules.md", "found")])

    def test_what_an_import_reaches(self):
        tree = _Tree(self, {"CLAUDE.md": "@../x.md @~/x.md @/etc/x.md @tool.exe @missing.md "
                                         "@docs @.git/config @ok.txt\n",
                            "tool.exe": "binary\n", "docs": None, ".git/config": "[core]\n",
                            "ok.txt": "OK\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-01", "CLAUDE.md")],
                         [("../x.md", "outside"), ("~/x.md", "outside"), ("/etc/x.md", "outside"),
                          ("tool.exe", "not_read"), ("missing.md", "missing"),
                          ("docs", "missing"), (".git/config", "missing"), ("ok.txt", "found")])

    def test_a_file_that_is_not_there_is_missing_whatever_its_extension(self):
        """Claude Code skips a path that it cannot open before it compares the extension of
        the file that it read."""
        tree = _Tree(self, {"CLAUDE.md": "@x.png @a.png @AGENTS.md.\n", "a.png": b"\x89PNG\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-01", "CLAUDE.md")],
                         [("x.png", "missing"), ("a.png", "not_read"), ("AGENTS.md.", "missing")])

    def test_copilot_cli_opens_a_trailing_slash_as_a_directory(self):
        """Copilot CLI 1.0.98 read nothing for these imports, although each names a file
        without its trailing `/`."""
        tree = _Tree(self, {"AGENTS.md": "@s38.md/ @d/s.md/ @a.md)/ @./\n", "s38.md": "S\n",
                            "d/s.md": "D\n", "a.md": "A\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("copilot-import-02", "AGENTS.md")],
                         [("s38.md/", "missing"), ("d/s.md/", "missing"), ("a.md)/", "missing"),
                          (".", "missing")])

    def test_copilot_cli_takes_a_leading_backslash_as_absolute(self):
        tree = _Tree(self, {"AGENTS.md": "@\\~/x.md @\\a.md @a\\b.md\n", "\\a.md": "L\n",
                            "a.md": "A\n", "a\\b.md": "M\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("copilot-import-02", "AGENTS.md")],
                         [("\\~/x.md", "outside"), ("\\a.md", "outside"),
                          ("a\\b.md", "found")])

    def test_copilot_cli_reads_an_inner_import_where_the_outer_path_names_no_file(self):
        """Copilot CLI 1.0.98 read b.md for `@x(@b.md` only where no file `x(@b.md` was, and
        no inner file for `@x/@b.md`."""
        text = "@x(@b.md @x/@b.md @d(@c.md\n"
        found = _Tree(self, {"AGENTS.md": text, "b.md": "B\n", "c.md": "C\n",
                             "d(@c.md/k.md": "K\n"}).chain("copilot-import-02", "AGENTS.md")
        self.assertEqual([(target, status) for _i, _d, target, status in found],
                         [("x(@b.md", "missing"), ("b.md", "found"), ("x/@b.md", "missing"),
                          ("d(@c.md", "missing"), ("c.md", "found")])
        found = _Tree(self, {"AGENTS.md": text, "b.md": "B\n", "x(@b.md": b"\xff\n",
                             "x/@b.md": "X\n"}).chain("copilot-import-02", "AGENTS.md")
        self.assertEqual([(target, status) for _i, _d, target, status in found],
                         [("x(@b.md", "not_read"), ("x/@b.md", "found"), ("d(@c.md", "missing"),
                          ("c.md", "missing")])

    def test_a_mail_link_imports_no_file_even_where_its_path_names_one(self):
        """Copilot CLI 1.0.101 read a.md, d.md and the file at the path of the `@` of the
        second link. The `@` of a mail link starts no import, and d.md after it is the first
        import of its word, which the chain reads after a word that named a file."""
        link = "octocat](mailto:octocat@example.com"
        found = _Tree(self, {"AGENTS.md": f"@a.md [@x](mailto:m@n.o)@d.md\n[@{link})\n",
                             "a.md": "A\n", "d.md": "D\n", link: "J\n"}).chain(
            "copilot-import-02", "AGENTS.md")
        self.assertEqual([(target, status) for _i, _d, target, status in found],
                         [("a.md", "found"), ("d.md", "found")])

    def test_copilot_cli_reads_a_fence_of_an_imported_file_as_a_fence_of_the_importing_file(self):
        """Copilot CLI 1.0.107 read q.md and c.md for each text. A shorter fence and a fence with an
        info string close nothing, and the fence of the longer example ends before c.md."""
        for name, text in {"a shorter fence": "````\n@a.md\n```\n@b.md\n````\n@c.md\n",
                           "an info string": "```\n@a.md\n```js\n@b.md\n~~~\n```\n@c.md\n"}.items():
            for row, importing in (("copilot-import-02", "AGENTS.md"),
                                   ("copilot-import-03", "CLAUDE.md")):
                with self.subTest(text=name, importing=importing):
                    tree = _Tree(self, {importing: "```\n@p.md\n```\n@q.md\n", "p.md": "P\n",
                                        "q.md": text, "a.md": "A\n", "b.md": "B\n", "c.md": "C\n"})
                    self.assertEqual(tree.chain(row, importing),
                                     [(importing, 1, "q.md", "found"), ("q.md", 2, "c.md", "found")])

    def test_a_second_link_to_a_file_that_the_chain_read_is_not_read_again(self):
        """Copilot CLI and Claude Code keep the real paths of the files that they read.
        Two links to one file and the file itself take one file of the limit."""
        tree = _Tree(self, {"AGENTS.md": "@l1.md @l2.md @real.md @other.md\n", "real.md": "R\n",
                            "other.md": "O\n"}, {"l1.md": "real.md", "l2.md": "real.md"})
        self.assertEqual([status for *_rest, status in tree.chain(
            "copilot-import-02", "AGENTS.md", max_files="2")],
            ["found", "found", "found", "found"])
        tree = _Tree(self, {"CLAUDE.md": "@l1.md @l2.md\n", "real.md": "@CLAUDE.md\n"},
                     {"l1.md": "real.md", "l2.md": "real.md"})
        self.assertEqual(tree.chain("claude-import-01", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "l1.md", "found"), ("l1.md", 2, "CLAUDE.md", "found"),
                          ("CLAUDE.md", 1, "l2.md", "found")])

    def test_a_path_that_no_file_can_have_is_missing_and_shortened(self):
        name = "a" * 300 + ".md"
        found = _Tree(self, {"CLAUDE.md": f"@{name} @b.md\n", "b.md": "B\n"}).chain(
            "claude-import-01", "CLAUDE.md")
        self.assertEqual(found, [("CLAUDE.md", 1, name[:imports.SHOWN] + "...", "missing"),
                                 ("CLAUDE.md", 1, "b.md", "found")])

    def test_copilot_cli_does_not_read_a_file_over_its_limit_to_check_its_text(self):
        """A file larger than the bytes that Copilot CLI imports is `over_limit`, also where it
        is not valid UTF-8, which the chain does not read the file to find out."""
        tree = _Tree(self, {"AGENTS.md": "@big.md\n", "big.md": b"\xff" * 600_000})
        self.assertEqual(tree.chain("copilot-import-02", "AGENTS.md"),
                         [("AGENTS.md", 1, "big.md", "over_limit")])

    def test_a_path_that_the_archive_step_prunes_is_neither_read_nor_copied(self):
        """A session transcript of Claude Code, a log and a lock file are what the release
        leaves out of its archives, and the dataset does not copy or publish them
        when an import names one."""
        tree = _Tree(self, {"CLAUDE.md": "@.claude/projects/-Users-x/s.jsonl @debug.log @a.md\n",
                            ".claude/projects/-Users-x/s.jsonl": "{}\n", "debug.log": "x\n",
                            "a.md": "A\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("claude-import-01", "CLAUDE.md")],
                         [(".claude/projects/-Users-x/s.jsonl", "pruned"),
                          ("debug.log", "pruned"), ("a.md", "found")])
        imported = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, imported, ignore_errors=True)
        imports.collect_imports(tree.root, tree.root, imported / "o§r")
        self.assertEqual(sorted(path.relative_to(imported).as_posix()
                                for path in imported.rglob("*") if path.is_file()),
                         ["o§r/a.md"])

    def test_a_place_of_a_tools_local_state_is_neither_read_nor_copied(self):
        """A file in a place where a tool keeps its own state, such as a Codex session or a
        Gemini CLI login, is no more collected through an import than by the collector."""
        tree = _Tree(self, {"AGENTS.md": "@.codex/sessions/s.md @.gemini/oauth_creds.json\n",
                            ".codex/sessions/s.md": "S\n", ".gemini/oauth_creds.json": "{}\n"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("copilot-import-02", "AGENTS.md")],
                         [(".codex/sessions/s.md", "pruned"),
                          (".gemini/oauth_creds.json", "pruned")])
        imported = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, imported, ignore_errors=True)
        imports.collect_imports(tree.root, tree.root, imported / "o§r")
        self.assertEqual([path for path in imported.rglob("*") if path.is_file()], [])

    def test_copilot_cli_reads_utf8_alone(self):
        tree = _Tree(self, {"AGENTS.md": "@latin1.md @nul.md @image.png\n",
                            "latin1.md": "caf\xe9\n".encode("latin-1"),
                            "nul.md": b"a\x00b\n", "image.png": b"\x89PNG\r\n\x1a\n\xff"})
        self.assertEqual([(target, status) for _i, _d, target, status
                          in tree.chain("copilot-import-02", "AGENTS.md")],
                         [("latin1.md", "not_read"), ("nul.md", "found"),
                          ("image.png", "not_read")])

    def test_the_limits_of_a_chain(self):
        files = {"AGENTS.md": "@a.md @big.md @b.md @c.md\n", "a.md": "a" * 10,
                 "big.md": "x" * 100, "b.md": "b" * 10, "c.md": "c" * 10}
        tree = _Tree(self, files)
        self.assertEqual([status for *_rest, status in tree.chain(
            "copilot-import-02", "AGENTS.md", max_files="2")],
            ["found", "found", "over_limit", "over_limit"])
        self.assertEqual([status for *_rest, status in tree.chain(
            "copilot-import-02", "AGENTS.md", max_bytes="30")],
            ["found", "over_limit", "found", "found"])
        self.assertEqual([status for *_rest, status in tree.chain(
            "claude-import-05", "AGENTS.md", max_file_bytes="50")],
            ["found", "over_limit", "found", "found"])

    def test_claude_code_resolves_against_the_file_that_a_link_leads_to(self):
        tree = _Tree(self, {"docs/CLAUDE.md": "@x.md\n", "docs/x.md": "D\n", "x.md": "R\n"},
                     {"CLAUDE.md": "docs/CLAUDE.md"})
        self.assertEqual(tree.chain("claude-import-01", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "docs/x.md", "found")])

    def test_copilot_cli_resolves_a_linked_root_at_the_link(self):
        """At the root of the chain against the directory of the link, and for an imported file
        against the directory that its link leads to."""
        tree = _Tree(self, {"docs/CLAUDE.md": "@x.md @link.md\n", "docs/x.md": "D\n",
                            "x.md": "R\n", "sub/real.md": "@y.md\n", "sub/y.md": "S\n",
                            "y.md": "Y\n"},
                     {"CLAUDE.md": "docs/CLAUDE.md", "link.md": "sub/real.md"})
        self.assertEqual(tree.chain("copilot-import-03", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "x.md", "found"), ("CLAUDE.md", 1, "link.md", "found"),
                          ("link.md", 2, "sub/y.md", "found")])

    def test_gemini_cli_resolves_against_the_directory_of_the_importing_file(self):
        tree = _Tree(self, {"docs/GEMINI.md": "@x.md\n", "docs/x.md": "D\n", "x.md": "R\n"},
                     {"GEMINI.md": "docs/GEMINI.md"})
        self.assertEqual(tree.chain("gemini-import-01", "GEMINI.md"),
                         [("GEMINI.md", 1, "x.md", "found")])

    def test_a_link_that_leads_out_of_the_repository_reaches_nothing(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "secret.md").write_text("S\n")
        tree = _Tree(self, {"CLAUDE.md": "@out.md\n"}, {"out.md": str(outside / "secret.md")})
        self.assertEqual(tree.chain("claude-import-01", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "out.md", "missing")])

    def test_a_chain_from_a_root_file_in_another_case_opens_its_imports_in_any_case(self):
        """mzmine/mzmine's root claude.md, `@AGENTS.MD` beside AGENTS.md. Claude Code 2.1.303
        reads a root claude.md only on a file system that ignores case, where it opens the
        import /AGENTS.MD as written and reaches AGENTS.md. Copilot CLI reads the root
        claude.md also on Linux, where AGENTS.MD names no file."""
        tree = _Tree(self, {"claude.md": "@AGENTS.MD\n", "AGENTS.md": "A\n"})
        self.assertEqual(tree.chain("claude-import-02", "claude.md"),
                         [("claude.md", 1, "AGENTS.md", "found")])
        self.assertEqual(tree.chain("copilot-import-05", "claude.md"),
                         [("claude.md", 1, "AGENTS.MD", "missing")])

    def test_each_name_of_each_hop_is_opened_in_any_case(self):
        """Gemini CLI reads `path.resolve(basePath, importPath)` with `fs.readFile`, and it
        reads an imported file of such a chain on the same file system."""
        tree = _Tree(self, {"gemini.md": "@Docs/Guide.md\n", "docs/guide.md": "@../NOTES.md\n",
                            "notes.md": "N\n"})
        self.assertEqual(tree.chain("gemini-import-02", "gemini.md"),
                         [("gemini.md", 1, "docs/guide.md", "found"),
                          ("docs/guide.md", 2, "notes.md", "found")])

    def test_another_chain_finds_a_target_in_its_case_alone(self):
        """The cloning step runs on a volume that compares case, and a step on a file system
        that ignores case, such as the default one of macOS, finds the same files."""
        tree = _Tree(self, {"CLAUDE.md": "@Notes.md @docs/notes.md\n", "notes.md": "N\n",
                            "Docs/notes.md": "D\n"})
        self.assertEqual(tree.chain("claude-import-01", "CLAUDE.md"),
                         [("CLAUDE.md", 1, "Notes.md", "missing"),
                          ("CLAUDE.md", 1, "docs/notes.md", "missing")])

    @unittest.skipUnless(_CASE_SENSITIVE, "two spellings of one name cannot coexist here")
    def test_a_name_that_two_entries_match_ignoring_case_names_no_file(self):
        """A file system that ignores case cannot hold Notes.md and NOTES.md side by side, and
        the chain does not choose one of them."""
        tree = _Tree(self, {"claude.md": "@notes.md @NOTES.md\n", "Notes.md": "A\n",
                            "NOTES.md": "B\n"})
        self.assertEqual(tree.chain("claude-import-02", "claude.md"),
                         [("claude.md", 1, "notes.md", "missing"),
                          ("claude.md", 1, "NOTES.md", "found")])


class TestTheImportingFiles(unittest.TestCase):

    def _started(self, files: dict) -> list:
        tree = _Tree(self, files)
        rels = sorted(rel for rel, content in files.items() if content is not None)
        return sorted((row["id"], path) for row, path
                      in imports.importing_files(ROWS, tree.root, rels, tree.tree))

    def test_claude_code_reads_agents_md_without_a_claude_md_above_it(self):
        self.assertIn(("claude-import-05", "AGENTS.md"), self._started({"AGENTS.md": "A\n"}))
        started = self._started({"CLAUDE.md": "C\n", "AGENTS.md": "A\n", "sub/AGENTS.md": "S\n"})
        self.assertNotIn("claude-import-05", [row for row, _path in started])
        started = self._started({"sub/CLAUDE.md": "C\n", "AGENTS.md": "A\n"})
        self.assertIn(("claude-import-05", "AGENTS.md"), started)

    def test_a_claude_file_leaves_out_the_agents_md_of_its_own_directory_alone(self):
        """Claude Code 2.1.303, started at the root, loaded pkg/sub/AGENTS.md beside
        pkg/CLAUDE.md and left pkg/AGENTS.md out."""
        started = self._started({"pkg/CLAUDE.md": "C\n", "pkg/AGENTS.md": "A\n",
                                 "pkg/sub/AGENTS.md": "S\n", "q/CLAUDE.local.md": "L\n",
                                 "q/.claude/AGENTS.md": "D\n"})
        self.assertEqual([path for row, path in started if row == "claude-import-05"],
                         ["pkg/sub/AGENTS.md"])

    def test_claude_code_reads_an_agents_md_under_a_dot_agents_directory(self):
        started = self._started({".agents/AGENTS.md": "A\n"})
        self.assertIn(("claude-import-05", ".agents/AGENTS.md"), started)

    def test_copilot_cli_reads_the_files_of_a_claude_directory(self):
        """Copilot CLI 1.0.98, started in a `.claude` directory, read its CLAUDE.md and its
        AGENTS.md and expanded their imports."""
        started = self._started({".claude/CLAUDE.md": "C\n", "sub/.claude/AGENTS.md": "A\n"})
        self.assertIn(("copilot-import-03", ".claude/CLAUDE.md"), started)
        self.assertIn(("copilot-import-02", "sub/.claude/AGENTS.md"), started)

    def test_copilot_cli_reads_github_in_lower_case_alone(self):
        """Copilot CLI 1.0.98 read .github/Copilot-Instructions.md and no
        .GitHub/copilot-instructions.md."""
        self.assertEqual(self._started({".GitHub/copilot-instructions.md": "G\n"}), [])

    def test_claude_code_reads_a_root_agents_md_in_another_case(self):
        """tmandry/glide's root agents.md beside its root claude.md. The file system that
        ignores case, on which Claude Code opens agents.md as AGENTS.md, also opens claude.md as
        CLAUDE.md, and Claude Code 2.1.303 then reads no AGENTS.md. On a volume that compares
        case, a root claude.md does not keep Claude Code from reading AGENTS.md."""
        self.assertEqual([row for row, path in self._started({"agents.md": "A\n"})
                          if path == "agents.md"], ["claude-import-06", "copilot-import-04"])
        for claude in ("CLAUDE.local.md", "claude.md", "Claude.md", "claude.local.md",
                       ".claude/claude.md"):
            with self.subTest(claude=claude):
                self.assertEqual([row for row, path in self._started({"agents.md": "A\n",
                                                                      claude: "C\n"})
                                  if path == "agents.md"], ["copilot-import-04"])
        self.assertEqual([row for row, path in self._started({"AGENTS.md": "A\n",
                                                              "claude.md": "C\n"})
                          if path == "AGENTS.md"],
                         ["claude-import-05", "copilot-import-02", "gemini-import-05"])

    def test_a_root_file_in_the_tools_own_case_starts_the_chain_of_its_basename_row(self):
        """A `root_any_case` row that `ignores_case` starts no chain at CLAUDE.md, AGENTS.md or
        GEMINI.md, which Claude Code and Gemini CLI read on every file system."""
        started = self._started({"CLAUDE.md": "C\n", "AGENTS.md": "A\n", "GEMINI.md": "G\n"})
        self.assertFalse([row for row, _path in started if imports.ignores_case(BY_ID[row])],
                         started)
        self.assertIn(("claude-import-01", "CLAUDE.md"), started)
        self.assertIn(("gemini-import-01", "GEMINI.md"), started)

    def test_a_root_file_in_any_case(self):
        started = self._started({"claude.md": "C\n", "docs/claude.md": "D\n",
                                 ".github/Copilot-Instructions.md": "G\n",
                                 "pkg/.github/copilot-instructions.md": "P\n"})
        self.assertEqual(started, [("claude-import-02", "claude.md"),
                                   ("copilot-import-01", ".github/Copilot-Instructions.md"),
                                   ("copilot-import-05", "claude.md"),
                                   ("copilot-import-06", "pkg/.github/copilot-instructions.md")])

    def test_copilot_cli_reads_the_instructions_of_a_directory_below_the_root(self):
        """Copilot CLI 1.0.101, working in pkg/ and in pkg/sub/, expanded the imports of
        pkg/.github/copilot-instructions.md against pkg/.github/. A `.github` in a tool's
        directory is that directory's, and `.GitHub` is no `.github`."""
        started = self._started({"pkg/.github/copilot-instructions.md": "@notes.md\n",
                                 "pkg/.github/notes.md": "N\n",
                                 ".claude/x/.github/copilot-instructions.md": "C\n",
                                 "web/.GitHub/copilot-instructions.md": "W\n"})
        self.assertEqual(started, [("copilot-import-06", "pkg/.github/copilot-instructions.md")])
        tree = _Tree(self, {"pkg/.github/copilot-instructions.md": "@notes.md\n",
                            "pkg/.github/notes.md": "N\n"})
        self.assertEqual(tree.chain("copilot-import-06", "pkg/.github/copilot-instructions.md"),
                         [("pkg/.github/copilot-instructions.md", 1, "pkg/.github/notes.md",
                           "found")])

    def test_claude_code_review_reads_the_includes_of_the_root_review_md_alone(self):
        """"One level: list every file in the root REVIEW.md. An @ line in an included file or
        in a subdirectory REVIEW.md stays as plain text". The collector copies the root file in
        the case of its row."""
        self.assertEqual(self._started({"REVIEW.md": "@a.md\n"}),
                         [("claude-import-07", "REVIEW.md")])
        self.assertEqual(self._started({"docs/REVIEW.md": "@a.md\n", "Review.md": "@a.md\n"}),
                         [])

    def test_one_chain_per_tool_and_syntax(self):
        self.assertEqual(self._started({"AGENTS.md": "A\n"}),
                         [("claude-import-05", "AGENTS.md"), ("copilot-import-02", "AGENTS.md"),
                          ("gemini-import-05", "AGENTS.md")])

    def test_gemini_cli_reads_the_context_files_that_its_settings_name(self):
        """`context.fileName` adds names beside GEMINI.md (setGeminiMdFilename)."""
        started = self._started({".gemini/settings.json": '{"context": {"fileName": "AGENTS.md"}}',
                                 "GEMINI.md": "G\n", "AGENTS.md": "A\n"})
        self.assertEqual([(row, path) for row, path in started
                          if BY_ID[row]["syntax"] == "gemini_at"],
                         [("gemini-import-01", "GEMINI.md"), ("gemini-import-03", "AGENTS.md")])

    def test_antigravity_reads_a_rule_that_it_loads_without_a_request(self):
        """An always_on or a glob rule, and no rule without a trigger, with a model_decision
        trigger, which the agent reads on demand, or with a manual one."""
        rules = {"on": "always_on", "glob": "glob\nglobs: '*.py'", "model": "model_decision",
                 "manual": "manual"}
        files = {f".agents/rules/{name}.md": f"---\ntrigger: {trigger}\n---\n@[a](a.md)\n"
                 for name, trigger in rules.items()}
        files[".agents/rules/off.md"] = "@[a](a.md)\n"
        files[".agents/rules.json"] = '{"entries": [{"path": "shared"}]}'
        files["shared/model.md"] = "---\ntrigger: model_decision\n---\n@[a](a.md)\n"
        files["shared/on.md"] = "---\ntrigger: always_on\n---\n@[a](a.md)\n"
        self.assertEqual(self._started(files),
                         [("gemini-import-07", ".agents/rules/glob.md"),
                          ("gemini-import-07", ".agents/rules/on.md"),
                          ("gemini-import-12", "shared/on.md")])


class TestTheCollectorCopiesWhatTheImportsReach(unittest.TestCase):
    """End to end over a real clone: the cloning step copies the files that the imports reach
    to the imported tree and leaves everything else that it writes as it was."""

    _saved = {}

    @classmethod
    def setUpClass(cls):
        _ambient_env.take(cls._saved)

    @classmethod
    def tearDownClass(cls):
        _ambient_env.restore(cls._saved)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.addCleanup(os.environ.pop, key, None)

    def _commit_all(self) -> None:
        subprocess.run(["git", "-C", str(self.remote), "add", "-A"], capture_output=True,
                       check=True)
        subprocess.run(["git", "-C", str(self.remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)

    def _config(self, name: str, imported: bool = True) -> CollectionConfig:
        return CollectionConfig(
            collected_dir=self.tmp / name / "collected", clone_dir=self.tmp / name / "clones",
            log_dir=self.tmp / name / "logs", max_tries=1, base_delay=0,
            imported_dir=self.tmp / name / "imported" if imported else None)

    def _fixture(self) -> None:
        """CLAUDE.md imports a chain, AGENTS.md, a link inside the repository, a link that leads
        out of it, a file of a submodule and a file in `.git`."""
        files = {"CLAUDE.md": "# Claude\n\n@docs/a.md\n@AGENTS.md\n@link.md\n@docs/out.md\n"
                              "@lib/dep/x.md\n@.git/config\n@missing.md\n",
                 "docs/a.md": "@b.md\n", "docs/b.md": "B\n", "AGENTS.md": "@agents-only.md\n",
                 "agents-only.md": "A\n", "sub/real.md": "@y.md\n", "sub/y.md": "Y\n",
                 "unrelated.md": "U\n"}
        for rel, content in files.items():
            (self.remote / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.remote / rel).write_text(content)
        os.symlink("sub/real.md", self.remote / "link.md")
        os.symlink("../../../outside.md", self.remote / "docs" / "out.md")
        self._commit_all()
        subprocess.run(["git", "-C", str(self.remote), "update-index", "--add", "--cacheinfo",
                        "160000", "1" * 40, "lib/dep"], capture_output=True, check=True)
        (self.remote / ".gitmodules").write_text(
            '[submodule "d"]\n\tpath = lib/dep\n\turl = https://github.com/owner/dep.git\n')
        subprocess.run(["git", "-C", str(self.remote), "add", ".gitmodules"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.remote), "commit", "-q", "-m", "submodule"],
                       capture_output=True, check=True)

    def _tree(self, root: Path) -> dict:
        found = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[rel] = ("link", os.readlink(path))
            elif path.is_file():
                found[rel] = ("file", path.read_text())
        return found

    def test_the_files_that_the_imports_reach(self):
        self._fixture()
        # The link `docs/out.md` leads to a file beside the clone, outside the repository.
        (self.tmp / "with" / "outside.md").parent.mkdir(parents=True)
        (self.tmp / "with" / "outside.md").write_text("outside\n")
        process_repo(self._config("with"), "owner/repo")
        self.assertEqual(self._tree(self.tmp / "with" / "imported" / "owner§repo"), {
            "AGENTS.md": ("file", "@agents-only.md\n"),
            "agents-only.md": ("file", "A\n"),
            "docs/a.md": ("file", "@b.md\n"),
            "docs/b.md": ("file", "B\n"),
            "link.md": ("link", "sub/real.md"),
            "sub/real.md": ("file", "@y.md\n"),
            "sub/y.md": ("file", "Y\n"),
        })

    def test_a_linked_importing_file_is_a_link_in_the_imported_tree(self):
        (self.remote / "docs").mkdir()
        (self.remote / "docs" / "CLAUDE.md").write_text("# Claude\n\n@x.md\n")
        (self.remote / "docs" / "x.md").write_text("in docs\n")
        (self.remote / "x.md").write_text("at the root\n")
        os.symlink("docs/CLAUDE.md", self.remote / "CLAUDE.md")
        self._commit_all()
        process_repo(self._config("with"), "owner/repo")
        self.assertEqual(self._tree(self.tmp / "with" / "imported" / "owner§repo"), {
            "CLAUDE.md": ("link", "docs/CLAUDE.md"),
            "docs/CLAUDE.md": ("file", "# Claude\n\n@x.md\n"),
            "docs/x.md": ("file", "in docs\n"),
            "x.md": ("file", "at the root\n"),
        }, "Claude Code reads docs/x.md through the link, and Copilot CLI x.md")

    def test_a_linked_importing_file_without_an_import_is_not_copied(self):
        """The imported tree holds the files that the imports reach, and a linked importing
        file only where the step needs its text to read an import."""
        (self.remote / "docs").mkdir()
        (self.remote / "docs" / "CLAUDE.md").write_text("# Claude\n\nNo imports.\n")
        os.symlink("docs/CLAUDE.md", self.remote / "CLAUDE.md")
        self._commit_all()
        _repo, _row, failure, _facts, _subs, measures = process_repo(self._config("with"),
                                                                     "owner/repo")
        self.assertIsNone(failure)
        self.assertEqual(self._tree(self.tmp / "with" / "imported" / "owner§repo"), {})
        self.assertEqual(measures["imported"], 0)

    def test_every_other_output_is_unchanged(self):
        self._fixture()
        without = process_repo(self._config("without", imported=False), "owner/repo")
        with_imports = process_repo(self._config("with"), "owner/repo")
        self.assertEqual(self._tree(self.tmp / "without" / "collected"),
                         self._tree(self.tmp / "with" / "collected"))
        self.assertIsNone(with_imports[2])
        columns = [column for column in COLLECTED_CSV_HEADER.split(",")
                   if column != "scanned_at"]
        self.assertEqual([_row_field(without[1], column) for column in columns],
                         [_row_field(with_imports[1], column) for column in columns])
        self.assertEqual(without[3], with_imports[3])
        self.assertEqual(without[4], with_imports[4])
        self.assertEqual({key: value for key, value in without[5].items() if "seconds" not in key
                          and "bytes" not in key},
                         {key: value for key, value in with_imports[5].items()
                          if "seconds" not in key and "bytes" not in key and key != "imported"})
        self.assertEqual(with_imports[5]["imported"], 6)

    def test_a_failed_clone_removes_the_imported_files(self):
        self._fixture()
        config = self._config("with")
        process_repo(config, "owner/repo")
        self.assertTrue((config.imported_dir / "owner§repo").is_dir())
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.tmp / 'gone'}.insteadOf"
        _repo, _row, failure, *_rest = process_repo(config, "owner/repo")
        self.assertIsNotNone(failure)
        self.assertFalse((config.imported_dir / "owner§repo").exists())


class TestARegularFile(unittest.TestCase):

    def test_a_file_below_a_gitlink_is_none(self):
        tree = _Tree(self, {"lib/dep/x.md": "X\n", "lib/depx.md": "Y\n"})
        self.assertIsNone(imports.regular_file(tree.root, "lib/dep/x.md", frozenset({"lib/dep"})))
        self.assertIsNotNone(imports.regular_file(tree.root, "lib/depx.md",
                                                  frozenset({"lib/dep"})))

    def test_a_link_into_a_gitlink_or_git_is_none(self):
        tree = _Tree(self, {"lib/dep/x.md": "X\n", ".git/config": "C\n"},
                     {"a.md": "lib/dep/x.md", "b.md": ".git/config"})
        self.assertIsNone(imports.regular_file(tree.root, "a.md", frozenset({"lib/dep"})))
        self.assertIsNone(imports.regular_file(tree.root, "b.md"))


if __name__ == "__main__":
    unittest.main()
