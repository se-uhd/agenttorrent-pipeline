"""The commit patterns, and reading every commit of a clone.

This module compiles the rows of ai_commit_patterns.csv and other_ai_commit_patterns.csv, and
`detect_ai_tools_in_fields`, `matched_rules_in_fields`, `detect_other_ai_tools_in_fields` and
`matched_other_rules_in_fields` match them against a commit's author, trailers and body.
`read_commit_history` reads every commit of a clone once, keeps the output of `git log` as the
repository's commit log, and writes the matches and the unmatched attributions to the shards
that the cloning step joins with `assemble_ai_commits`. `ai_authorship_from` computes the four
AI-authorship columns from a path's commits. `reclassify` applies the heuristics again to a
commit log, so that a changed heuristic applies to a collection without cloning its
repositories again. `checks/commits.py` checks the assembled CSV.
"""

import bisect
import contextlib
import csv
import functools
import hashlib
import lzma
import os
import re
import random
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from agentcontext import caches, fingerprints, paths, heuristics
from agentcontext.git import bounds, history

from agentcontext.repos import dir_name_to_repo, repo_name_to_dir
from typing import List
from agentcontext import logs



# The commit heuristics are defined in `ai_commit_patterns.csv` and loaded through
# `heuristics.py`, as the path patterns are defined in `config_path_patterns.csv`. Every row has
# a `source`. The documentation watch re-reads the pages that the `doc` rows cite, and the
# ranking of `unmatched commits` is the only check that shows when a `sample` row needs a change.
AI_COMMIT_PATTERNS = heuristics.load_ai_commit_patterns()
# The tools that this dataset does not study, in the same columns. Their commits go to
# a CSV of their own, `other_ai_commits.csv`, so that they leave the six tools' columns and
# counts unchanged, and an attribution that one of their rows matches is not unmatched.
OTHER_AI_COMMIT_PATTERNS = heuristics.load_other_ai_commit_patterns()


# The columns of the two commit heuristics files that the classification reads: the id that
# `matched_heuristics` records, the tool that a match credits, the scope and the pattern that
# decide what matches, and the attribution that the `attributions` column records. The other
# columns record where a row comes from and when its page was read, and reading a page or a
# commit again changes them without changing how any commit is classified.
CLASSIFICATION_COLUMNS = ("id", "tool", "scope", "attribution", "pattern")


def _classified_by(rows: list | None = None, other_rows: list | None = None) -> str:
    """What a shard's classification depends on, as one digest: the `CLASSIFICATION_COLUMNS` of
    the rows of the two commit heuristics files, in file order, as the loaders return them, and
    the code of this module and the loader, without their comments, docstrings and log calls
    (`fingerprints.code_fingerprint`). `rows` and `other_rows` default to the rows of the two
    files. `reclassify` keeps the shards of a record with the current digest once it has
    validated the record's evidence."""
    rows = heuristics.load_ai_commit_patterns() if rows is None else rows
    other_rows = heuristics.load_other_ai_commit_patterns() if other_rows is None else other_rows
    columns = [[[row[column] for column in CLASSIFICATION_COLUMNS] for row in found]
               for found in (rows, other_rows)]
    digest = hashlib.sha256(json.dumps(columns, ensure_ascii=False).encode("utf-8"))
    digest.update(fingerprints.code_fingerprint([Path(__file__), Path(heuristics.__file__)])
                  .encode("ascii"))
    return digest.hexdigest()


CLASSIFIED_BY = _classified_by(AI_COMMIT_PATTERNS, OTHER_AI_COMMIT_PATTERNS)

# `shard_rows` and `assemble_ai_commits` below read commit messages, and a message can be
# over csv's default field limit. The limit is raised here, in the module that reads them, so
# that a caller that imports this module alone does not depend on another module having raised it.
csv.field_size_limit(sys.maxsize)

# Trailer extraction. The space after the colon is optional, as it is for `git
# interpret-trailers`, which reads `Co-authored-by:Claude <noreply@anthropic.com>` as a trailer.
# Requiring one would leave such a commit with no trailer at all and count it as a person's.
_TRAILER_LINE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9-]*:\s*\S')
# The color codes of a terminal (ANSI SGR, `ESC [ <numbers> m`), which a message pasted from a
# colored terminal keeps around every line, as SatelliteQE/broker 77d12182a7eb keeps
# `\x1b[38;2;248;248;242m` before its `Co-Authored-By:` line. They are not part of the text.
_TERMINAL_COLORS = re.compile(r"\x1b\[[0-9;]*m")
# One emoji before a trailer, as in `🤖 Co-Authored-By: Claude Code` of DataDog/dd-trace-dotnet
# 141636c73d66, with the variation selector that some emoji take: the blocks of the pictographs
# and of the symbols and dingbats.
_LEADING_EMOJI = re.compile("\\A[\U0001F300-\U0001FAFF\u2600-\u27BF]\uFE0F?[ \t]*")
# The bullet with which GitHub's squash merge lists each commit of the pull request: `* ` and the
# commit's message, at the start of the line. A commit whose whole message is a trailer then
# appears as `* Co-authored-by: ...`, as comet-ml/opik 60258b3c235b lists c66ae12cac19 of pull
# request 5179, whose message is `Co-authored-by: Cursor <cursoragent@cursor.com>`. The trailer
# scope reads every top-level `* ` item of trailer form this way, not only the items of a squash
# list, with every token of its rest. A bullet indented below another line is a nested list item
# and stays, and so does a hyphen bullet.
_SQUASH_BULLET = re.compile(r"\A\* ")
# A top-level `* ` item that ends in a co-author line with an address. GitHub's squash list repeats
# the message of each listed commit after `* ` with its line breaks, so a co-author line on a line
# of its own stays on its own line there, as artsy/metaphysics aad63ec4a6d1 lists 69904373f55f. A
# listed commit with a one-line message that ends in its co-author line appears with that line on
# the line of its subject, as allenai/open-instruct 97590011918f lists d5fc8655cf38, whose message
# is the one line `Restore 🤡 to resample warnings; use self.training_step in
# DataPreparationActor.run. Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`. The trailer
# scope reads the co-author line of such an item, as it reads the subject of the listed commit,
# which is that whole line. The line has to end the item with its address, so that an item that
# names the token in a sentence stays unread, and an item of another token, such as `Made-with:`,
# stays unread too.
_JOINED_CO_AUTHOR = re.compile(
    r"\A\* \S.*?\s(co-authored-by:[ \t]*[^<>\n]*\S[ \t]*<[^<>\s]+@[^<>\s]+>)[ \t]*\Z", re.I)


# The trailer tokens that credit someone or something with writing the commit. A line with one of
# these tokens credits a tool or a person. A commit with such a line that no heuristic matched
# therefore credits either a tool that no heuristic covers or a human co-author. `unmatched commits`
# ranks these lines so that a person can tell the two apart. A token that credits no author, such as
# `Signed-off-by` or `Reviewed-by`, would fill the ranking with commits that are not about
# authorship at all. A token can also end in `-AI` or `-agent`, which marks what it credits as a
# coding agent, as in `Co-authored-by-agent: Cursor` and `Authored-by-agent: Codex` of
# moeru-ai/airi and `Assisted-by-AI: GitHub Copilot (GPT-5.4)` of zephyrproject-rtos/zephyr
# f4e9ea557a14.
ATTRIBUTION_TOKENS = (
    "co-authored-by", "co-authored-by-ai", "co-authored-by-agent", "co-developed-by",
    "assisted-by", "assisted-by-ai", "generated-by", "made-with", "authored-by",
    "authored-by-agent", "created-by", "written-by",
)

# The forms of an automated identity that is not one of the six tools. GitHub appends `[bot]`
# to the name of every app account. The other two forms are conventions that people use for
# their own service accounts.
_BOT_IDENTITY = re.compile(r"\[bot\]|\bbot@|[-_.]bot\b", re.IGNORECASE)


def _named_identity(name: str, email: str) -> bool:
    """Whether an author or a committer looks automated or names one of the six tools in its
    name or its address (`_MENTION`), such as `opencode <person231@example.org>`, a name that a user
    gave to the identity under which the tool commits."""
    return bool(_BOT_IDENTITY.search(f"{name} <{email}>") or _MENTION.search(name)
                or _MENTION.search(email))


def unmatched_attributions(author_name: str, author_email: str, committer_name: str,
                        committer_email: str, body: str) -> list:
    """The trailers and identities of this commit that credit an author and that no pattern
    matched.

    Returns `(kind, token)` pairs, `kind` being `trailer` or `author`. The caller has
    already established that no heuristic matched, and everything returned here is
    unmatched by construction. The patterns match the attributions that the heuristics
    cover. This function returns what looks like an attribution and matched no pattern, which
    shows where the heuristics miss commits that credit a tool.
    """
    found = []
    for line in _extract_trailers(body).splitlines():
        token, _, value = line.partition(":")
        token = token.strip().lower()
        if token not in ATTRIBUTION_TOKENS:
            continue
        # The name, not the address: two commits that credit the same tool under different
        # addresses are one entry to review, and an address is more likely to identify a
        # person.
        name = value.split("<")[0].strip().lower()
        found.append(("trailer", f"{token}: {name}"[:80]))
    for name, email in ((author_name, author_email), (committer_name, committer_email)):
        identity = f"{name} <{email}>".strip()
        if _named_identity(name, email):
            found.append(("author", identity.lower()[:80]))
    return sorted(set(found))


def attribution_details(author_name: str, author_email: str, committer_name: str,
                        committer_email: str, body: str) -> list:
    """`unmatched_attributions` with the line that each entry came from.

    Returns `(kind, token, line)` triples. `(kind, token)` is the pair of the unmatched
    attributions. `line` is the whole trailer line for a trailer, rather than the value cut at
    `<`, and the identity itself for an identity. A reader needs the line to decide whether a
    token names a tool that no heuristic covers. The value cut at `<` is not enough.
    """
    found = []
    for line in _extract_trailers(body).splitlines():
        token, _, value = line.partition(":")
        token = token.strip().lower()
        if token not in ATTRIBUTION_TOKENS:
            continue
        name = value.split("<")[0].strip().lower()
        found.append(("trailer", f"{token}: {name}"[:80], line.strip()[:200]))
    for name, email in ((author_name, author_email), (committer_name, committer_email)):
        identity = f"{name} <{email}>".strip()
        if _named_identity(name, email):
            found.append(("author", identity.lower()[:80], identity[:200]))
    return sorted(set(found))


def _extract_trailers(body: str) -> str:
    """
    Return every git trailer line in a commit body.

    Git trailers, in the convention of `git interpret-trailers` after RFC 822 headers, are
    'Token: value' lines, conventionally in a block at the end of the body:

        Co-authored-by: Claude <noreply@claude.ai>
        Generated-by: claude-code

    A trailer is not always in the last block. A squash merge concatenates the messages of
    the commits that it replaces, and a trailer then ends up mid-body with another commit's
    text after it. A blank line can separate two co-authors into two blocks, and `git cherry-pick
    -x` appends a "(cherry picked from commit ...)" line below them. Scanning only the final
    block would drop the tool's trailer in all three cases and count such a commit as
    human-authored.

    This function therefore returns every line in the body that has the form of a trailer,
    wherever the line is. Prose is still excluded, because the exclusion depends on the form
    of the line (`Token: value`) rather than on its position: a sentence such as "I was
    inspired by the co-authored-by claude convention" has no trailer token and never matches.

    A line is read without the color codes of a terminal, without a `* ` bullet before a line of
    trailer form and without one emoji before its token, and of a `* ` item that ends in a
    co-author line with an address only that co-author line is read (`_trailer_line`). A line
    that closes an inline code span that the line before it opened, each with an odd number of
    backticks, quotes a trailer rather than writing it.
    Comfy-Org/ComfyUI_frontend 4a4b1e487635 quotes the trailer of another commit in a code span
    that opens in the line that ends with "reports `4afc68a7c3:" and closes after
    "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>" in the next line.
    """
    lines = _own_lines(body).splitlines()
    return "\n".join(
        trailer
        for k, trailer in enumerate(_trailer_line(line) for line in lines)
        if trailer and _TRAILER_LINE_RE.match(trailer)
        and not (k and lines[k - 1].count("`") % 2 and lines[k].count("`") % 2)
    )


def _trailer_line(line: str) -> str:
    """A line as the trailer scope reads it: stripped of white space and of the color codes of a
    terminal, and of a `* ` bullet at its start, as GitHub's squash merge writes before each
    listed commit, or of one emoji before a token when the rest of the line has the form of a
    trailer. Of a `* ` item that ends in a co-author line with an address, only that co-author
    line (`_JOINED_CO_AUTHOR`)."""
    line = _TERMINAL_COLORS.sub("", line)
    if _SQUASH_BULLET.match(line) and _TRAILER_LINE_RE.match(line[2:].strip()):
        return line[2:].strip()
    joined = _JOINED_CO_AUTHOR.match(line.rstrip())
    if joined:
        return joined.group(1)
    line = line.strip()
    rest = _LEADING_EMOJI.sub("", line, count=1)
    return rest if rest != line and _TRAILER_LINE_RE.match(rest) else line


# The upstream release notes that a dependency update of Renovate quotes, which a squash merge
# copies into the commit: the `### Release Notes` section up to the `---` line before
# `### Configuration`, or up to the separator of a squash merge with only trailers after it to the
# end of the message, in a message that names Renovate. A credit in the section belongs to a commit
# of the dependency, such as the `Co-authored-by: Claude Opus 4.6 (1M context)
# <noreply@anthropic.com>` of conventional-changelog/commitlint ac2b3f4a9476 that
# sbb-design-systems/lyne-components 0d5b1a0295f4 quotes in its update of commitlint. A trailer
# below the separator is the squash merge's own, as apify/crawlee 36375cea18b2 ends with the
# co-author lines of Renovate and Copilot's coding agent. A separator with other text after it stays
# in the section, because release notes can quote one.
_RENOVATE_RELEASE_NOTES = re.compile(
    r"^### Release Notes[ \t]*$.*?(?=^---[ \t]*$|^### Configuration[ \t]*$"
    r"|^-{9}[ \t]*\n\s*(?:^[A-Za-z][A-Za-z0-9-]*:[^\n]*(?:\n|\Z))+\s*\Z|\Z)", re.M | re.S)
_RENOVATE = re.compile(r"renovate", re.I)
# The blocks in which a dependency update of Dependabot quotes the dependency, in a message that
# names Dependabot: its vulnerabilities fixed, release notes, changelog, upgrade guide and commits.
# dependabot-core writes each as `<details>\n<summary>#{summary}</summary>\n\n` ... `</details>\n`
# (common/lib/dependabot/pull_request_creator/message_builder/metadata_presenter.rb at 667a0ec84e4a,
# `build_details_tag`). A credit there belongs to a commit or a release of the dependency, such as
# the `Claude-Session:` line of electron-playwright-helpers that Automattic/studio fd755e0eee7d
# quotes in its update of the package.
_DEPENDABOT_QUOTES = re.compile(
    r"^<details>[ \t]*\n?[ \t]*<summary>(?:Vulnerabilities fixed|Release notes|Changelog|"
    r"Upgrade guide|Commits)</summary>.*?^</details>", re.I | re.M | re.S)
_DEPENDABOT = re.compile(r"dependabot", re.I)
# The block in which Copilot's cloud agent quotes its prompt, the issue and the comments on it,
# `<details>` and `<summary>Original prompt</summary>` up to the line `</details>`, which a squash
# merge copies into the commit. A statement in a comment on the issue is no credit of the change,
# as `I asked copilot to create` in microsoft/onnxruntime 616a4dfc1434 names the issue. A block
# without a closing line stays, because a trailer can follow it directly, as in
# Azure/data-api-builder 4c26d602f520.
_ORIGINAL_PROMPT = re.compile(r"^<details>\s*<summary>Original prompt</summary>.*?^</details>",
                              re.I | re.M | re.S)
# The end of the message of a version bump of kube-HPC/hkube, whose scripts/createVersion.sh
# commits with `MESSAGE="$(git log -1 --pretty=%B) .... bump version [skip ci]"` (at
# 4a57bc616f61). The message is the whole message of the commit that the bump follows, and a
# credit in it belongs to that commit.
_QUOTED_PREVIOUS_MESSAGE = re.compile(r" \.\.\.\. bump version \[skip ci\]\s*\Z")
# The commits of Chromium that brave/brave-core quotes below a line `Chromium changes:`, or below
# `Chromium change:` above a single quoted commit as in brave/brave-core 4b67ac68ac97, in the form
# of `git log`: a line `commit <hash>`, its header lines, and the indented and blank lines under
# them. A credit there belongs to the commit of Chromium, as `gemini-cli was used to quickly
# prototype` does in brave/brave-core 6e74e53c4e22, which changes only Brave's own files. Any other
# such block stays, because `git merge --squash` writes the same form for a change that the commit
# records. The block ends at the first unindented line that is no header, so that a trailer of the
# commit of Brave below it stays read.
_CHROMIUM_CHANGES = re.compile(r"^Chromium changes?:[ \t]*$", re.M)
_GIT_LOG_BLOCK = re.compile(r"^commit [0-9a-f]{7,40}\b[^\n]*\n"
                            r"(?:(?:Merge|Author|AuthorDate|Commit|CommitDate|Date):[^\n]*\n)+"
                            r"(?:(?:[ \t\r][^\n]*)?(?:\n|\Z))*", re.M)
# A list item that names another commit by its hash, `- <hash>: <message>`, in which `_own_lines`
# keeps an escaped line break, because a release commit can list the messages of other commits on
# one line each, as meziantou/Meziantou.Framework 0700bc2ff449 lists the messages of its pull
# requests. A credit in such a line belongs to the listed commit. Any other list item reads the
# escape as a line break, because a squash merge records the change of the commits that it lists on
# such lines, as xtaci/smux 148c01bcb4ad does.
_LIST_ITEM = re.compile(r"[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+[0-9a-f]{7,40}:[ \t]")


def _blank(match: re.Match) -> str:
    """The lines of a match, left empty."""
    return "\n" * match.group(0).count("\n")


def _without_quoted_chromium_commits(body: str) -> str:
    """The body with the `git log` blocks below a line `Chromium changes:` or `Chromium change:`
    left empty."""
    below = _CHROMIUM_CHANGES.search(body)
    if below is None:
        return body
    return body[:below.end()] + _GIT_LOG_BLOCK.sub(_blank, body[below.end():])


def _own_lines(body: str) -> str:
    """The body with what it quotes from other commits or from an issue left empty, line by line,
    so that every other line keeps its place, and with an escaped line break read as a line break,
    except in a list item that names another commit by its hash (`_LIST_ITEM`).

    What a body quotes is the release notes that a dependency update of Renovate or Dependabot
    quotes, the whole message that a version bump of kube-HPC/hkube quotes, the commits of Chromium
    that brave/brave-core quotes, and the issue with its comments that the block `Original prompt`
    of Copilot's cloud agent quotes. An escaped line
    break is the two characters `\\n` that some tools write into a message in place of a line
    break, as tikv/pd 34c56d780657 has `\\n\\nCo-authored-by: Claude Sonnet 5
    <noreply@anthropic.com>` at the end of a line of its body."""
    if _QUOTED_PREVIOUS_MESSAGE.search(body):
        return "\n" * body.count("\n")
    if _RENOVATE.search(body):
        body = _RENOVATE_RELEASE_NOTES.sub(_blank, body)
    if _DEPENDABOT.search(body):
        body = _DEPENDABOT_QUOTES.sub(_blank, body)
    body = _without_quoted_chromium_commits(_ORIGINAL_PROMPT.sub(_blank, body))
    if "\\n" not in body:
        return body
    return "\n".join(line if _LIST_ITEM.match(line) else line.replace("\\n", "\n")
                     for line in body.split("\n"))


# One (tool, author, trailer, full_body) entry per tool, compiled once. Which scope a
# pattern belongs to is the heuristics' `scope` column.
_COMPILED_PATTERNS = heuristics.compiled_ai_commit_patterns(AI_COMMIT_PATTERNS)
_OTHER_COMPILED_PATTERNS = heuristics.compiled_ai_commit_patterns(OTHER_AI_COMMIT_PATTERNS)


def _with_required_literals(compiled: list) -> list:
    """The entries of `compiled`, with (pattern, the literals that a match has to contain) for
    each pattern of each scope."""
    return [(tool, *[[(pattern, heuristics.required_literals(heuristics.heuristic_pattern(pattern)))
                      for pattern in scope] for scope in scopes])
            for tool, *scopes in compiled]


_CHECKED_PATTERNS = _with_required_literals(_COMPILED_PATTERNS)
_OTHER_CHECKED_PATTERNS = _with_required_literals(_OTHER_COMPILED_PATTERNS)
_REQUIRED_LITERALS = tuple(sorted({
    literal for entries in (_CHECKED_PATTERNS, _OTHER_CHECKED_PATTERNS) for _, *scopes in entries
    for scope in scopes for _, required in scope for literals in required for literal in literals}))


@functools.lru_cache(maxsize=4)
def _literals_in(text: str) -> frozenset:
    """The required literals that `text` contains. A commit's three texts are read once for each
    of the two heuristics files, so the last few results are kept."""
    lowered = heuristics.lowered_for_literals(text)
    return frozenset(literal for literal in _REQUIRED_LITERALS if literal in lowered)


def _any_match(entries: list, text: str) -> bool:
    """Whether a pattern of `entries` matches `text`. A pattern with a set of required literals of
    which `text` contains none cannot match, and it does not run."""
    present = _literals_in(text)
    for pattern, required in entries:
        for literals in required:
            if present.isdisjoint(literals):
                break
        else:
            if pattern.search(text):
                return True
    return False


def detect_ai_tools_in_fields(
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
    subject: str,
    body: str,
    check_author: bool = True,
) -> List[str]:
    """
    Return a list of tool names detected in the commit metadata.

    Three separate scopes are checked, each against a different text:

    1. AUTHOR scope: author_name, author_email, committer_name, committer_email.
       Bare tool names such as "claude" are safe here, because a person's identity
       fields do not contain "claude" or "copilot".

    2. TRAILER scope: the subject and the git trailers extracted from the body.
       Used for patterns that start with a git trailer token such as
       "Co-authored-by:" and "Generated-by:".

    3. FULL_BODY scope: the subject and the full body text.
       Used for patterns that appear anywhere in the message, such as the
       branch that a merge commit names (from <owner>/copilot/<branch>) and
       "this commit was generated by X".
    """
    return _detect(_CHECKED_PATTERNS, author_name, author_email, committer_name,
                   committer_email, subject, body, check_author)


# The label of the rows that match a trailer that credits AI and names no tool, only a model,
# its vendor, or AI itself. A commit gets the label only when no row of either heuristics file
# gives it a tool, because a line such as `Assisted-by: GPT-5.6 via Codex` names a model and a
# tool at once.
UNNAMED_TOOL = "tool not named"


def detect_other_ai_tools_in_fields(author_name: str, author_email: str, committer_name: str,
                                    committer_email: str, subject: str, body: str,
                                    check_author: bool = True) -> List[str]:
    """`detect_ai_tools_in_fields` under `other_ai_commit_patterns.csv`: the tools outside the
    six, and `UNNAMED_TOOL` for a commit that credits AI and names no tool."""
    found = _detect(_OTHER_CHECKED_PATTERNS, author_name, author_email, committer_name,
                    committer_email, subject, body, check_author)
    if UNNAMED_TOOL in found and (len(found) > 1 or _detect(
            _CHECKED_PATTERNS, author_name, author_email, committer_name, committer_email,
            subject, body, check_author)):
        found.remove(UNNAMED_TOOL)
    return found


def _full_body(subject: str, body: str) -> str:
    """The message that the full-body heuristics match: the subject and the body on lines of
    their own.

    A heuristic that starts at the beginning of a line can then match a disclosure that opens
    the body, such as `Implemented with AI assistance.` under a subject. `\\s` matches a line
    break as it does a space.

    A quoted line indented below another line, `  > This PR was generated by Codex`, is a quote
    nested in a list item, as a release changelog quotes the description of each pull request that
    it lists. It credits that pull request rather than this commit, and is left empty. A quote at
    the start of the line stays, because a squash merge contains the pull request's own description,
    and `> [!NOTE]` with `> This PR was created with the assistance of GitHub Copilot.` below it is
    that description's disclosure. The section "The detection accuracy evaluation" of
    docs/commits.md gives the commits that ground both cases. The release notes that a
    dependency update of Renovate or Dependabot quotes, the message that a version bump of
    kube-HPC/hkube quotes, the commits of Chromium that brave/brave-core quotes and the block
    `Original prompt` of Copilot's cloud agent are left empty too, and an escaped line break is a
    line break
    (`_own_lines`), also in the subject, where a tool that writes the whole message on one line
    leaves it, as Capsize-Games/airunner 2e41c054d021 does.

    An unticked box of a task list, `- [ ] created with the help of Claude` in a pull request
    template, marks a task that was not done, and is left empty too. GitHub Flavored Markdown:
    "If the character between the brackets is a whitespace character, the checkbox is
    unchecked" (https://github.github.com/gfm/#task-list-items-extension-, read 2026-10-15).
    chainsafe/lodestar fd4f342a40f6 has such a box.

    A ticked box states what its line says, as `- [x] AI-assisted: Claude Code helped diagnose the
    install failure` of ChainSafe/lodestar 59c06807b63c does. The line is read with the bullet in
    place of the marker, so that a heuristic that reads the start of a line sees the text after
    the box. The page above defines the marker as "a left bracket (`[`), either a whitespace
    character or the letter `x` in either lowercase or uppercase, and then a right bracket (`]`)",
    and states "Otherwise, the checkbox is checked" (read 2026-11-06).
    """
    parts = [subject.replace("\\n", "\n"), _own_lines(body)]
    text = "\n".join(f for f in parts if f).replace("\r\n", "\n")
    return _TICKED_TASK.sub(r"\1", _UNCHECKED_TASK.sub("", _NESTED_QUOTE.sub("", text)))


# A quoted line indented below a list item, which `_full_body` leaves empty.
_NESTED_QUOTE = re.compile(r"^[ \t]+>[^\n]*", re.MULTILINE)
# A task list item with a box that is not ticked, which `_full_body` leaves empty.
_UNCHECKED_TASK = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+\[[ \t]\][^\n]*", re.MULTILINE)
# The bullet and the marker of a task list item with a ticked box, which `_full_body` reads as the
# bullet alone.
_TICKED_TASK = re.compile(r"^([ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+)\[[xX]\][ \t]+", re.MULTILINE)
# The subject that git gives a revert, `Revert "<the reverted commit's subject>"` (git's
# sequencer.c at v2.47.0, lines 2356-2358: `strbuf_addstr(&ctx->message, "Revert \"")`, then the
# subject and the closing quote). The quoted subject is the reverted commit's, and a credit that
# it contains belongs to that commit, as in `Revert "Co-authored-by: Copilot"` in boreq/botnet
# 9e9cbf3a52, a person's revert.
_REVERT_SUBJECT = re.compile(r'\ARevert "')


def _own_subject(subject: str, body: str) -> str:
    """The subject that the heuristics match: none for a revert, because a revert's subject
    quotes another commit's, and none for a version bump of kube-HPC/hkube, whose message
    quotes the message of another commit (`_QUOTED_PREVIOUS_MESSAGE`). Such a message ends in
    the body, or in the subject when the quoted message has no body."""
    if subject and (_REVERT_SUBJECT.match(subject) or _QUOTED_PREVIOUS_MESSAGE.search(
            body if body.strip() else subject)):
        return ""
    return subject


def scope_texts(subject: str, body: str) -> tuple[str, str]:
    """(the trailer text, the full-body text) that the heuristics match: the subject, unless it
    is a revert's or quotes another commit's, with the trailers, and with the whole body. The
    classification and the attribution check both take the texts from here."""
    subject = _own_subject(subject, body)
    return (" ".join(f for f in [subject, _extract_trailers(body)] if f),
            _full_body(subject, body))


def _detect(checked: list, author_name: str, author_email: str, committer_name: str,
            committer_email: str, subject: str, body: str, check_author: bool) -> List[str]:
    """The tools of `checked` that match, in its order: the three scopes above, per tool."""
    trailer_text, fullbody_text = scope_texts(subject, body)
    author_text = " ".join(f for f in [author_name, author_email, committer_name,
                                       committer_email] if f)
    return [tool for tool, author, trailer, full_body in checked
            if (check_author and _any_match(author, author_text))
            or _any_match(trailer, trailer_text) or _any_match(full_body, fullbody_text)]


# One (id, tool, scope, attribution, compiled) per heuristic, in file order. `matched_heuristics`
# records the id rather than a row's position, which adding or removing a row changes for every
# row after it, so that a published value read against another release's heuristics names the
# same heuristic.
_INDEXED_PATTERNS = [(row["id"], row["tool"], row["scope"], row["attribution"],
                      heuristics.compile_commit_pattern(row)) for row in AI_COMMIT_PATTERNS]
_OTHER_INDEXED_PATTERNS = [(row["id"], row["tool"], row["scope"], row["attribution"],
                            heuristics.compile_commit_pattern(row))
                           for row in OTHER_AI_COMMIT_PATTERNS]
_OTHER_TOOL_OF = {heuristic: tool for heuristic, tool, _, _, _ in _OTHER_INDEXED_PATTERNS}


def matched_rules_in_fields(author_name: str, author_email: str, committer_name: str,
                            committer_email: str, subject: str, body: str,
                            check_author: bool = True) -> List[str]:
    """Every heuristic that matches this commit, by its id, in file order.

    `detect_ai_tools_in_fields` stops at the first heuristic per tool, which is enough for the
    tool column. This function tests every heuristic, so that a published row names every
    heuristic that matches it and the next CSV can be attributed without a reclassification.
    """
    return _matched(_INDEXED_PATTERNS, author_name, author_email, committer_name,
                    committer_email, subject, body, check_author)


def matched_other_rules_in_fields(author_name: str, author_email: str, committer_name: str,
                                  committer_email: str, subject: str, body: str,
                                  check_author: bool = True) -> List[str]:
    """`matched_rules_in_fields` under `other_ai_commit_patterns.csv`, without the
    `UNNAMED_TOOL` rows when the commit names a tool."""
    found = _matched(_OTHER_INDEXED_PATTERNS, author_name, author_email, committer_name,
                     committer_email, subject, body, check_author)
    if UNNAMED_TOOL not in detect_other_ai_tools_in_fields(
            author_name, author_email, committer_name, committer_email, subject, body,
            check_author):
        found = [n for n in found if _OTHER_TOOL_OF[n] != UNNAMED_TOOL]
    return found


def _matched(indexed: list, author_name: str, author_email: str, committer_name: str,
             committer_email: str, subject: str, body: str, check_author: bool) -> List[str]:
    """The ids of the heuristics of `indexed` that match."""
    trailer_text, fullbody_text = scope_texts(subject, body)
    author_text = " ".join(f for f in [author_name, author_email, committer_name,
                                       committer_email] if f)
    found = []
    for heuristic, _, scope, _, pattern in indexed:
        if scope == "author":
            if check_author and pattern.search(author_text):
                found.append(heuristic)
        elif scope == "trailer":
            if pattern.search(trailer_text):
                found.append(heuristic)
        elif pattern.search(fullbody_text):
            found.append(heuristic)
    return found


def _texts_by_line(subject: str, body: str) -> dict:
    """The trailer and full-body texts that `_matched` reads, each with where its pieces start
    and which line of the message each piece is: {scope: (text, starts, lines)}.

    Built piece by piece rather than mapped afterwards, so that each text is the text that the
    heuristics are matched against, character for character, as `test_commits` checks. A
    trailer line is found among the message's lines by its content, in order, as
    `_extract_trailers` reads each line (`_trailer_line`). A trailer line that is not found
    counts as a line of its own. The space after the subject belongs to no line and is
    recorded as `None`.
    """
    subject = _own_subject(subject, body)
    full = _full_body(subject, body)
    lines = full.split("\n")
    full_starts, at = [], 0
    for line in lines:
        full_starts.append(at)
        at += len(line) + 1
    pieces = []
    if subject:
        pieces += [(line, i) for i, line in enumerate(subject.split("\n"))]
    extracted = _extract_trailers(body)
    if extracted:
        stripped = [_trailer_line(line) for line in lines]
        at = len(subject.split("\n")) if subject else 0
        for k, line in enumerate(extracted.split("\n")):
            found = next((j for j in range(at, len(lines)) if stripped[j] == line), None)
            if found is None:
                # `splitlines` also breaks at a separator that `_full_body` keeps, such as a
                # lone carriage return. The trailer line then counts as a line of its own, and
                # the search for the next trailer line starts at the same position.
                pieces.append((line, -1 - k))
            else:
                pieces.append((line, found))
                at = found + 1
    text, starts, owners = "", [], []
    for k, (piece, line) in enumerate(pieces):
        if k:
            # The subject's lines and the trailers' are joined with a newline, and the two
            # parts with a space, as `_matched` joins them.
            joint = " " if subject and k == len(subject.split("\n")) else "\n"
            starts.append(len(text))
            owners.append(None)
            text += joint
        starts.append(len(text))
        owners.append(line)
        text += piece
    return {"trailer": (text, starts, owners),
            "full_body": (full, full_starts, list(range(len(lines))))}


def _lines_touched(starts: list, owners: list, start: int, end: int) -> set:
    """The lines that the characters from `start` to `end` come from. An empty match touches
    the line that it is on."""
    k = bisect.bisect_right(starts, start) - 1
    touched = set()
    while k < len(starts) and starts[k] < max(end, start + 1):
        if owners[k] is not None:
            touched.add(owners[k])
        k += 1
    return touched


# GitHub ends the message of a squash merge that lists its commits with a line of nine hyphens and
# a block that repeats the co-author lines of those commits, as AllStarLink/app_rpt's 9cf6e24c
# does. The line in the list already credits its tool, as `reviewer` where it follows a suggestion
# that a person committed (`coderabbit-full-body-02`), and the repeated line credits nothing of its
# own. A squash merge can list an earlier squash merge, whose message keeps its own separator and
# repeated block inside the list, as skim-rs/skim's 7d450220 lists `tests: improve coverage to 90%
# (#1099)`. The list of the outer squash merge continues after that block, and a co-author line of
# a commit listed there is the commit's own, such as CodeRabbit's line below the listed commit
# `perf: upt contracts/reputation/src/lib.rs` in kindfi-org/kindfi's 872d5d5f.
SQUASH_SEPARATOR = "-" * 9


def _repeated_co_author_lines(full: str) -> set:
    """The indices of the co-author lines of the message `full` that repeat, in the block below a
    squash separator, a line above that separator.

    Below the last separator, the block runs to the end of the message. Below an earlier one,
    which ends the list of a squash merge that the message lists, it ends at the first line that
    is neither blank nor a trailer, where the outer list continues."""
    lines = [line.strip() for line in full.split("\n")]
    separators = [k for k, line in enumerate(lines) if line == SQUASH_SEPARATOR]
    repeated = set()
    for separator, following in zip(separators, separators[1:] + [None]):
        above = set(lines[:separator])
        for k in range(separator + 1, len(lines) if following is None else following):
            if following is not None and lines[k] and not _TRAILER_LINE_RE.match(lines[k]):
                break
            if lines[k].lower().startswith("co-authored-by:") and lines[k] in above:
                repeated.add(k)
    return repeated


def _attributions(indexed: list, author_name: str, author_email: str, committer_name: str,
                  committer_email: str, subject: str, body: str, check_author: bool) -> list:
    """(tool, attribution) for every attribution that credits a tool, in `heuristics.ATTRIBUTIONS`
    order.

    The author and the committer take `account` when an author heuristic of the tool with
    `account` matches them, and otherwise `convention` when one with `convention` matches, that
    is, a heuristic for a name or an address that names the tool and that users chose for the
    identity. Every line of the message that a heuristic of the tool matches takes the attribution
    of the first heuristic in `ATTRIBUTIONS` order that matches it. A co-author trailer of a
    review app is therefore `reviewer` even though a heuristic for the tool's name matches it too,
    and the line that the tool writes itself is not also `convention`. A match that spans lines
    covers every one of them. A co-author line that a squash merge repeats below its separator is
    read by no heuristic (`_repeated_co_author_lines`).
    """
    author_text = " ".join(f for f in [author_name, author_email, committer_name,
                                       committer_email] if f)
    texts = _texts_by_line(subject, body)
    repeated = _repeated_co_author_lines(texts["full_body"][0])
    found = []
    for tool in dict.fromkeys(tool for _, tool, _, _, _ in indexed):
        rows = [(scope, attribution, pattern) for _, t, scope, attribution, pattern in indexed
                if t == tool]
        if check_author:
            matched = {attribution for scope, attribution, pattern in rows
                       if scope == "author" and pattern.search(author_text)}
            for attribution in heuristics.AUTHOR_ATTRIBUTIONS:
                if attribution in matched:
                    found.append((tool, attribution))
                    break
        read = set(repeated)
        for attribution in heuristics.ATTRIBUTIONS[1:]:
            reading = set()
            for scope, row_attribution, pattern in rows:
                if row_attribution != attribution or scope == "author":
                    continue
                text, starts, owners = texts[scope]
                for match in pattern.finditer(text):
                    touched = _lines_touched(starts, owners, *match.span())
                    if touched - read:
                        reading |= touched
            if reading and (tool, attribution) not in found:
                found.append((tool, attribution))
            read |= reading
    return found


def author_attributions(rows: list) -> list:
    """(tool, attribution) for the author heuristics `rows` that matched a commit: `account` for a
    tool when one of its rows has `account`, and otherwise `convention`, as `_attributions` gives
    them."""
    by_tool: dict = {}
    for row in rows:
        by_tool.setdefault(row["tool"], set()).add(row["attribution"])
    return [(tool, next(a for a in heuristics.AUTHOR_ATTRIBUTIONS if a in found))
            for tool, found in by_tool.items()]


def format_attributions(pairs: list) -> str:
    """`Tool:attribution` per pair, joined with a semicolon, by tool and then in `ATTRIBUTIONS`
    order, each pair once: the `attributions` column."""
    return ";".join(f"{tool}:{attribution}" for tool, attribution in sorted(
        set(pairs), key=lambda pair: (pair[0], heuristics.ATTRIBUTIONS.index(pair[1]))))


def attributions_in_fields(author_name: str, author_email: str, committer_name: str,
                           committer_email: str, subject: str, body: str,
                           check_author: bool = True) -> str:
    """The `attributions` of a commit of the commit CSV: how the commit credits each of its
    tools."""
    return format_attributions(_attributions(
        _INDEXED_PATTERNS, author_name, author_email, committer_name, committer_email,
        subject, body, check_author))


def other_attributions_in_fields(author_name: str, author_email: str, committer_name: str,
                                 committer_email: str, subject: str, body: str,
                                 check_author: bool = True) -> str:
    """`attributions_in_fields` under `other_ai_commit_patterns.csv`, without `UNNAMED_TOOL`
    when the commit names a tool, as `matched_other_rules_in_fields` leaves its rows out."""
    pairs = _attributions(_OTHER_INDEXED_PATTERNS, author_name, author_email, committer_name,
                          committer_email, subject, body, check_author)
    if UNNAMED_TOOL not in detect_other_ai_tools_in_fields(
            author_name, author_email, committer_name, committer_email, subject, body,
            check_author):
        pairs = [pair for pair in pairs if pair[0] != UNNAMED_TOOL]
    return format_attributions(pairs)


def _tools_in(commit: dict, check_author: bool = True) -> List[str]:
    """The tools of a commit of git's log, read from its message as the commit CSV stores it
    (`stored_message`). The classification of the commit CSV reads the same text, so that the
    AI columns and the commit CSV agree on a message with a lone carriage return."""
    subject, body = message_parts(stored_message(commit))
    return detect_ai_tools_in_fields(
        commit["author_name"], commit["author_email"], commit["committer_name"],
        commit["committer_email"], subject, body, check_author=check_author)


def ai_authorship_from(commits: "history.PathCommits") -> "caches.AiAuthorship":
    """The four AI columns of one path, from the commits that touched it.

    The creation commit is `commits.first()`, the commit that `first_commit_sha` names, and
    the two columns about it describe the row's own first commit. The count and the
    tool set cover every commit that either query returned. A path with no history is blank in
    all four columns, because its history was not read, which is not the same as no AI commit.
    """
    return _ai_authorship_of(commits.first(), commits.union())


# ---------------------------------------------------------------------------
# Reading the commit history
# ---------------------------------------------------------------------------
# The published columns of the commit CSV: seven columns that describe the commit,
# `matched_heuristics`, which names the heuristics that match a row by their ids, and
# `attributions`, how the row credits each of its tools. The CSV of the tools outside the six
# has the same columns, with the ids of `other_ai_commit_patterns.csv`, so that the two CSVs can
# be concatenated as they are.
COMMIT_COLUMNS = ("repo_name", "commit_timestamp", "branch", "commit_sha", "github_link",
                  "commit_message", "ai_tool", "matched_heuristics", "attributions")
# The unmatched attributions over every repository with a commit history that was read: what looked
# like an attribution in a commit that no heuristic matched, with up to UNMATCHED_SAMPLE shas and
# lines for each.
UNMATCHED_COLUMNS = ("kind", "token", "commits", "repos", "shas", "lines")
# The lines that name one of the six tools in a commit that no heuristic matched, such as an
# attribution written in prose, "Generated with Cursor". The unmatched attributions do not record
# such a line, because no trailer or identity contains it. `pattern` is the line normalized so that
# the same phrasing groups across commits and repositories, and `line` is one example as the message
# contains it.
MENTION_COLUMNS = ("tool", "pattern", "commits", "repos", "shas", "line")
MENTIONS_NAME = "all_ai_commits-mentions.csv"
OTHER_AI_COMMITS_NAME = "other_ai_commits.csv"
# One row per repository in the run: whether its commit history was read, how many commits were
# read and how many matched, and up to NO_SIGNAL_SAMPLE shas of commits with no attribution at
# all. Checking those commits by hand, together with the ranking of unmatched attributions,
# bounds the miss rate.
AI_COMMITS_REPOS_COLUMNS = ("repo_name", "head_sha", "outcome", "commits_read", "commits_matched",
                  "commits_other_tools", "no_signal_shas")
# GROUNDING: a policy. Twenty shas and lines per unmatched token let a reader decide whether
# the token needs a heuristic, and five shas per repository of commits with no attribution are
# enough to spot-check the claim that the repository has none, without copying every commit of
# the sample into the CSV. Both are samples, and the counts beside them are the full numbers.
UNMATCHED_SAMPLE = 20
NO_SIGNAL_SAMPLE = 5
# GROUNDING: a policy, as are the two above. A name such as Cursor is also an ordinary word, and
# in a repository about editors, every commit that names it can name it in a different line. Fifty
# phrasings per tool and repository keep such a repository from outweighing the rest, and the
# commits beyond them are counted rather than kept. Five shas are enough to open an example.
MENTION_PATTERNS = 50
MENTION_SAMPLE = 5
# The six tools' names as the `tool` column of `ai_commit_patterns.csv` spells them, as whole words
# in any case. The names of the tools outside the six are left out, because Continue, Factory, Warp,
# Amp and pi are ordinary words, and their lines would outnumber the phrasings that credit a tool. A
# name that is part of a path, such as `CLAUDE.md` or `.cursor/rules`, names the file rather than
# crediting the tool, and "Add CLAUDE.md" would lead the ranking in every repository that has one. A
# hyphenated name, `claude-code`, is kept, because attributions contain it.
_MENTION = re.compile(r"(?<![./\w])(" + "|".join(sorted({re.escape(row["tool"].lower())
                                                        for row in AI_COMMIT_PATTERNS}))
                      + r")(?![\w/]|\.\w)", re.IGNORECASE)
_MENTION_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
_MENTION_URL = re.compile(r"https?://\S+")
_MENTION_DIGITS = re.compile(r"\d+")


def mention_pattern(line: str) -> str:
    """A line with what varies between commits taken out: case, links, hashes, numbers and
    runs of white space. "Generated with Cursor 1.2" and "generated with cursor 1.7" are then
    one phrasing."""
    text = _MENTION_URL.sub("<url>", line.strip().lower())
    text = _MENTION_HEX.sub("<hex>", text)
    text = _MENTION_DIGITS.sub("0", text)
    return " ".join(text.split())[:160]


def tool_mentions(subject: str, body: str) -> list:
    """(tool, pattern, line) for every line of a commit message that names one of the six
    tools, each pair once."""
    found = {}
    for line in (subject + "\n" + body).splitlines():
        for tool in {m.group(1).lower() for m in _MENTION.finditer(line)}:
            found.setdefault((tool, mention_pattern(line)), line.strip()[:200])
    return [(tool, pattern, line) for (tool, pattern), line in sorted(found.items())]
# The samples above cap each entry. The number of entries per repository is the number of
# distinct attributions that no heuristic matches, which nothing caps. `assemble_ai_commits`
# reads every record of a run at once rather than streaming them, which the small size of the
# per-repository commit records allows.
# The `rs-us-v1` format of `git log`: RS between commits, US between fields, the author date in
# UTC. The `nul-v1` format below separates every field with NUL and adds the raw message.
_LOG_FORMAT = "%x1E%H%x1F%an%x1F%ae%x1F%cn%x1F%ce%x1F%ad%x1F%s%x1F%b%x1F"
_NUL_LOG_FORMAT = "%x00%H%x00%an%x00%ae%x00%cn%x00%ce%x00%ad%x00%s%x00%b%x00%B%x00"
_LOG_FIELDS = ("hash", "author_name", "author_email", "committer_name", "committer_email",
               "timestamp", "subject", "body")
_NUL_LOG_FIELDS = (*_LOG_FIELDS, "message")


def _reservoir(sample: list, seen: int, item, size: int, rng) -> None:
    """Keep a uniform sample of `size` items in place. `seen` counts this item."""
    if len(sample) < size:
        sample.append(item)
    else:
        slot = rng.randrange(seen)
        if slot < size:
            sample[slot] = item


def _records(raw_path: Path, log_format: str = "rs-us-v1"):
    """Yield one commit at a time from the file that `git log` wrote."""
    with open(raw_path, "rb") as f:
        yield from _records_from(f, log_format)


def _records_from(f, log_format: str = "rs-us-v1"):
    """Yield one commit at a time from a binary stream in the format that `git log` wrote."""
    if log_format == "nul-v1":
        yield from _nul_records_from(f)
        return
    if log_format != "rs-us-v1":
        raise ValueError(f"The commit log format {log_format} is not supported")
    rest = b""
    while True:
        chunk = f.read(1 << 20)
        if not chunk:
            break
        rest += chunk
        parts = rest.split(b"\x1e")
        rest = parts.pop()
        for part in parts:
            if part.strip():
                yield _record(part)
    if rest.strip():
        yield _record(rest)


def _nul_records_from(stream):
    """Read NUL-delimited fields without splitting control characters in messages."""
    rest, fields = b"", []
    while chunk := stream.read(1 << 20):
        parts = (rest + chunk).split(b"\0")
        rest = parts.pop()
        for part in parts:
            if not fields:
                if part.strip():
                    raise ValueError("The commit log has an invalid record boundary")
                fields.append(part)
                continue
            fields.append(part)
            if len(fields) == len(_NUL_LOG_FIELDS) + 1:
                yield {name: raw.decode("utf-8", errors="replace")
                       for name, raw in zip(_NUL_LOG_FIELDS, fields[1:])}
                fields = []
    if fields or rest.strip():
        raise ValueError("The commit log ends with an incomplete record")


def _record(raw: bytes) -> dict:
    fields = raw.decode("utf-8", errors="replace").split("\x1f")
    while len(fields) < len(_LOG_FIELDS):
        fields.append("")
    return {name: fields[index].strip() for index, name in enumerate(_LOG_FIELDS)}


def shard_paths(commits_dir: Path, repo: str) -> tuple[Path, Path]:
    dir_name = repo_name_to_dir(repo)
    return commits_dir / f"{dir_name}.csv", commits_dir / f"{dir_name}.json"


# The shards of `other_ai_commits.csv` are in a directory of their own, under the same name as
# the repository's shard. A suffix beside the shard, `<repo>.other.csv`, would be the name of the
# shard of a repository with a name that ends in `.other`.
OTHER_SHARDS_DIR_NAME = "other"


def other_shard_path(commits_dir: Path, repo: str) -> Path:
    """Where the cloning step writes this repository's commits by the tools outside the six."""
    return commits_dir / OTHER_SHARDS_DIR_NAME / f"{repo_name_to_dir(repo)}.csv"


# The commit log: what `git log` wrote as the cloning step read the commit history, every
# commit's hash, identities, date and message, kept beside the shard so that `reclassify` can
# apply a changed commit heuristic to the collection without another clone. The log is
# compressed at xz's default preset to reduce its size on disk.
LOG_SUFFIX = ".log.xz"
LOG_PRESET = 6


def stored_log_path(commits_dir: Path, repo: str) -> Path:
    """Where the cloning step keeps this repository's commit log."""
    return commits_dir / f"{repo_name_to_dir(repo)}{LOG_SUFFIX}"


def required_log_missing(commits_dir: Path, repo: str, record: dict) -> bool:
    """Whether the commit log is missing although the repository's commit history was read and
    its commit evidence recorded."""
    if record.get("outcome") != "history_read" or not record.get("commit_evidence"):
        return False
    try:
        stored_log_path(commits_dir, repo).stat()
    except FileNotFoundError:
        return True
    # A permission or mount error raises to the caller rather than counting as a missing log.
    return False


def _store_log(raw_path: Path, target: Path) -> None:
    """Compress the output of `git log` to its stored name in one step, a temporary name and a
    rename, so that a commit log is either whole or absent."""
    partial = target.with_name(target.name + ".tmp")
    try:
        with open(raw_path, "rb") as source, lzma.open(partial, "wb", preset=LOG_PRESET) as out:
            shutil.copyfileobj(source, out, 1 << 20)
        caches.sync_file(partial)
        os.replace(partial, target)
        caches.sync_directory(target.parent)
    finally:
        partial.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    """Hash a file without loading it into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _history_evidence(repo: str, head_sha: str, clone_dir: Path, timeout: float) -> dict:
    """Record the resolved revision and refs before reading their history."""
    def git(*args):
        result = bounds.run_bounded(["git", *args], cwd=str(clone_dir), timeout=timeout,
                                    env=os.environ.copy())
        if result.returncode:
            raise ValueError(f"Could not record the commit evidence, because git {args[0]} "
                             f"exited {result.returncode} ({result.stderr.strip()})")
        return result.stdout.strip()

    actual_head = git("rev-parse", "HEAD")
    if actual_head != head_sha:
        raise ValueError("The HEAD of the fetched history differs from the collected revision")
    if git("rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("The clone has no complete commit history for the recorded refs")
    refs = {}
    for line in git("for-each-ref", "--format=%(refname) %(objectname) %(objecttype) "
                   "%(*objectname) %(*objecttype)").splitlines():
        fields = line.split()
        commit_sha = (fields[1] if fields[2] == "commit" else
                      fields[3] if len(fields) == 5 and fields[4] == "commit" else
                      git("rev-parse", fields[0] + "^{commit}")
                      if len(fields) == 5 and fields[4] == "tag" else "")
        refs[fields[0]] = {"object_sha": fields[1],
                          "commit_sha": commit_sha}
    return {"schema_version": 1, "source_url": f"https://github.com/{repo}.git",
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "head_sha": actual_head, "scope": "resolved_refs_and_head", "refs": refs,
            "shallow": False, "git_version": git("--version"), "log_format": "nul-v1"}


def _validated_records(record: dict, log: Path, wanted: set):
    """Validate the commit evidence while yielding the commits, before any classification is
    replaced."""
    evidence = record["commit_evidence"]
    if (evidence.get("schema_version") != 1
            or evidence.get("head_sha") != record.get("head_sha")
            or evidence.get("scope") != "resolved_refs_and_head"
            or evidence.get("shallow") is not False
            or evidence.get("log_format") != "nul-v1"
            or evidence.get("source_url") != f"https://github.com/{record['repo_name']}.git"):
        raise ValueError("The commit evidence has an invalid provenance or scope")
    datetime.strptime(evidence["retrieved_at"], "%Y-%m-%dT%H:%M:%SZ")
    if _file_sha256(log) != evidence.get("log_sha256"):
        raise ValueError("Commit log checksum differs from its evidence record")
    required = wanted | {record["head_sha"]} | {
        ref["commit_sha"] for ref in evidence["refs"].values() if ref["commit_sha"]}
    remaining = set(required)
    count = 0
    with lzma.open(log, "rb") as stream:
        for commit in _records_from(stream, evidence["log_format"]):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit["hash"]):
                raise ValueError("The commit log has an invalid commit hash")
            count += 1
            remaining.discard(commit["hash"])
            yield commit
    if count != evidence.get("commit_count"):
        raise ValueError("The commit count of the commit log differs from its evidence record")
    if remaining:
        raise ValueError(f"Commit log lacks {len(remaining)} required revision or path commits")


def read_commit_history(repo: str, head_sha: str, branch: str, clone_dir: Path,
                        commits_dir: Path, timeout: float = history.GIT_TIMEOUT_SECONDS) -> dict:
    """Read every commit on every ref of the clone and write this repository's shard.

    `git log` writes to a file, which is read one record at a time, so that a worker keeps one
    commit in memory rather than a repository's history. A matched commit is written to the
    shard as it is found. An unmatched commit adds to the unmatched attributions, with the sha
    and the line that each attribution came from, or to the sample of commits with no
    attribution when it has none. The unmatched attributions, the mentions and the counts go
    to the per-repository commit record, which this function returns. A read that the time
    bound stops, or that git refuses, is recorded as `history_read_failed` with no rows, and
    the file-history rows that were already read are kept.
    """
    csv_path, json_path = shard_paths(commits_dir, repo)
    commits_dir.mkdir(parents=True, exist_ok=True)
    raw_path = commits_dir / (csv_path.stem + ".log")
    record = {"repo_name": repo, "head_sha": head_sha, "branch": branch,
              "outcome": "history_read", "commits_read": 0, "commits_matched": 0,
              "commits_other_tools": 0,
              "unmatched": [], "no_signal_shas": [], "ai_rows": [], "mentions": [],
              "mentions_dropped": {}}
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "TZ": "UTC"})
    try:
        evidence = _history_evidence(repo, head_sha, clone_dir, timeout)
        revisions = sorted({head_sha} | {ref["commit_sha"] for ref in evidence["refs"].values()
                                        if ref["commit_sha"]})
        # The revisions go to `git log` on its standard input. On the command line, the commits
        # of tens of thousands of tags exceed the system's limit on the size of the arguments,
        # and the read would fail on every run as if the machine had.
        with open(raw_path, "wb") as raw, tempfile.TemporaryFile() as tips:
            tips.write("".join(f"{sha}\n" for sha in revisions).encode("ascii"))
            tips.seek(0)
            result = bounds.run_bounded(
                ["git", "-c", "i18n.logOutputEncoding=UTF-8", "log",
                 "--date=format-local:%Y-%m-%dT%H:%M:%SZ", f"--format={_NUL_LOG_FORMAT}",
                 "--stdin", "--"],
                cwd=str(clone_dir), env=env, timeout=timeout, stdout_file=raw, stdin_file=tips)
        if result.returncode != 0:
            raise RuntimeError(f"git log exited {result.returncode} "
                               f"({result.stderr.strip()[-bounds.STDERR_TAIL:]})")
    except Exception as e:
        logs.warning("commits", f"Could not read the commit history of {repo} "
                                f"({e.__class__.__name__} {e}).")
        record["outcome"] = "history_read_failed"
        record["detail"] = f"{e.__class__.__name__} ({e})"[:200]
        # An OSError is a failure of the machine, which a resumed run retries. A timeout or an
        # error of git is the repository's, and recurs. A read that the time bound stopped
        # records the bound, so that a run with a larger bound reads the history again, as it
        # fetches a history again when the time bound stopped the fetch.
        record["transient"] = isinstance(e, OSError)
        if isinstance(e, subprocess.TimeoutExpired):
            record["cause"] = "time_bound"
            record["time_bound"] = timeout
        raw_path.unlink(missing_ok=True)
        csv_path.unlink(missing_ok=True)
        other_shard_path(commits_dir, repo).unlink(missing_ok=True)
        stored_log_path(commits_dir, repo).unlink(missing_ok=True)
        write_shard_record(json_path, record)
        return record

    try:
        _store_log(raw_path, stored_log_path(commits_dir, repo))
        evidence["log_sha256"] = _file_sha256(stored_log_path(commits_dir, repo))
    except OSError as e:
        # The commits that were read still count. Only a later change to the commit heuristics
        # cannot be applied to this repository without cloning it again, and the reclassification
        # records that as `no_log`. A commit log that an earlier collection kept does not belong
        # to this collection and is removed.
        logs.warning("commits", f"Could not keep the commit log of {repo} ({e}).")
        stored_log_path(commits_dir, repo).unlink(missing_ok=True)
    classify(record, _records(raw_path, "nul-v1"), commits_dir)
    evidence["commit_count"] = record["commits_read"]
    record["commit_evidence"] = evidence
    raw_path.unlink(missing_ok=True)
    write_shard_record(json_path, record)
    return record


def classify(record: dict, records, commits_dir: Path, keep: "set | None" = None) -> dict:
    """Apply the commit heuristics to one repository's commits and write its two shards.

    `record` is the per-repository commit record. The function replaces its counts, unmatched
    attributions and mentions, and sets `classified_by` to the heuristics that classified the
    commits.
    `read_commit_history` calls this function over the log that it just wrote, and `reclassify`
    over the commit log, so that both classify a commit the same way. Returns the records of the
    commits that `keep` names, from which `reclassify` computes the AI columns of the
    configuration artifacts.
    """
    repo, branch = record["repo_name"], record["branch"]
    csv_path, _json_path = shard_paths(commits_dir, repo)
    record.update({"commits_read": 0, "commits_matched": 0, "commits_other_tools": 0,
                   "no_signal_shas": [], "mentions_dropped": {}})
    kept = {}
    rng = random.Random(repo)
    unmatched = {}
    mentions = {}
    no_signal_seen = 0
    # Each shard is written beside its final name and renamed once complete, as the
    # per-repository commit record is, so that a worker killed between two writes, at a timeout
    # or by the kernel, leaves no partial shard for `assemble_ai_commits` to read after the
    # collection or on a resumed run.
    csv_partial = csv_path.with_name(csv_path.name + ".tmp")
    other_path = other_shard_path(commits_dir, repo)
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_partial = other_path.with_name(other_path.name + ".tmp")
    try:
        with open(csv_partial, "w", newline="", encoding="utf-8") as f, \
                open(other_partial, "w", newline="", encoding="utf-8") as g:
            writer = csv.DictWriter(f, fieldnames=list(COMMIT_COLUMNS), lineterminator="\n")
            writer.writeheader()
            other_writer = csv.DictWriter(g, fieldnames=list(COMMIT_COLUMNS), lineterminator="\n")
            other_writer.writeheader()
            for c in records:
                record["commits_read"] += 1
                if keep and c["hash"] in keep:
                    kept[c["hash"]] = c
                # The message that the row stores, and the subject and body that the check
                # splits off it: a lone carriage return is a line break, and a NUL-separated log
                # keeps the whitespace around each field.
                message = stored_message(c)
                subject, body = message_parts(message)
                fields = (c["author_name"], c["author_email"], c["committer_name"],
                          c["committer_email"], subject, body)
                tools = detect_ai_tools_in_fields(*fields)
                others = detect_other_ai_tools_in_fields(*fields)
                if tools or others:
                    row = {"repo_name": repo, "commit_timestamp": c["timestamp"], "branch": branch,
                           "commit_sha": c["hash"],
                           "github_link": f"https://github.com/{repo}/commit/{c['hash']}",
                           "commit_message": message}
                if tools:
                    record["commits_matched"] += 1
                    writer.writerow({**row, "ai_tool": ",".join(sorted(tools)),
                                     "matched_heuristics": ";".join(matched_rules_in_fields(*fields)),
                                     "attributions": attributions_in_fields(*fields)})
                if others:
                    record["commits_other_tools"] += 1
                    other_writer.writerow({**row, "ai_tool": ",".join(sorted(others)),
                                           "matched_heuristics": ";".join(
                                               matched_other_rules_in_fields(*fields)),
                                           "attributions": other_attributions_in_fields(*fields)})
                # A commit that a heuristic of either file matches is an AI commit, and the
                # other credits in it are not unmatched attributions.
                if tools or others:
                    continue
                # A commit beyond the cap counts once per tool, however many of its phrasings
                # the cap excludes.
                beyond = set()
                for tool, pattern, line in tool_mentions(subject, body):
                    entry = mentions.get((tool, pattern))
                    if entry is None:
                        if sum(1 for t, _ in mentions if t == tool) >= MENTION_PATTERNS:
                            if tool not in beyond:
                                beyond.add(tool)
                                dropped = record["mentions_dropped"]
                                dropped[tool] = dropped.get(tool, 0) + 1
                            continue
                        entry = mentions[(tool, pattern)] = {"commits": 0, "shas": [], "line": line}
                    entry["commits"] += 1
                    _reservoir(entry["shas"], entry["commits"], c["hash"], MENTION_SAMPLE, rng)
                details = attribution_details(c["author_name"], c["author_email"],
                                              c["committer_name"], c["committer_email"], body)
                if not details:
                    no_signal_seen += 1
                    _reservoir(record["no_signal_shas"], no_signal_seen, c["hash"],
                               NO_SIGNAL_SAMPLE, rng)
                    continue
                # A commit counts once per (kind, token), however many lines contain the token.
                # A squash merge can credit one co-author under two addresses, which is one
                # commit with two lines.
                counted = set()
                for kind, token, line in details:
                    entry = unmatched.setdefault((kind, token), {"commits": 0, "shas": [], "lines": []})
                    if (kind, token) not in counted:
                        counted.add((kind, token))
                        entry["commits"] += 1
                        _reservoir(entry["shas"], entry["commits"], c["hash"], UNMATCHED_SAMPLE, rng)
                    if line not in entry["lines"] and len(entry["lines"]) < UNMATCHED_SAMPLE:
                        entry["lines"].append(line)
    except BaseException:
        # The evidence check at the end of the log raises here. The existing shards stay in
        # place, and the partial files are removed.
        csv_partial.unlink(missing_ok=True)
        other_partial.unlink(missing_ok=True)
        raise
    # The data before the renames, and the directories after them, so that the per-repository
    # commit record, written after this function returns, never reaches the disk before the
    # shards with the checksums that it records.
    caches.sync_file(csv_partial)
    caches.sync_file(other_partial)
    os.replace(csv_partial, csv_path)
    os.replace(other_partial, other_path)
    caches.sync_directory(csv_path.parent)
    caches.sync_directory(other_path.parent)
    record["unmatched"] = [{"kind": kind, "token": token, **entry}
                           for (kind, token), entry in sorted(unmatched.items())]
    record["mentions"] = [{"tool": tool, "pattern": pattern, **entry}
                          for (tool, pattern), entry in sorted(mentions.items())]
    record["classified_by"] = CLASSIFIED_BY
    record["classified_outputs"] = {
        "tools": _file_sha256(csv_path), "other_tools": _file_sha256(other_path)}
    return kept


def path_commits_entry(artifact: str, path_history: "history.PathCommits") -> dict:
    """What `reclassify` needs of one configuration artifact's commits: the creation commit and
    every commit that touched it, by hash, which are the commits that `ai_authorship_from`
    reads."""
    first = path_history.first()
    entry = {"artifact": artifact, "first": first["hash"] if first else "",
             "commits": [commit["hash"] for commit in path_history.union()]}
    if path_history.source_path and path_history.source_path != artifact:
        entry["source_path"] = path_history.source_path
    return entry


def recorded_commit_counts(commits_dir: Path, repos: list) -> dict:
    """`#commits` keyed by `(repo_name, head_sha, artifact)`, from the path histories that the
    per-repository commit records of `repos` contain: every commit that touched the path under
    any of its names or on any ref, which is what `history.history_from` counts."""
    counts = {}
    for repo, _csv_path, record in readable_shards(commits_dir, repos):
        for entry in record.get("path_commits") or ():
            counts[(repo, record["head_sha"], entry["artifact"])] = len(entry["commits"])
    return counts


def _ai_authorship_of(first, touching: list) -> "caches.AiAuthorship":
    """The four AI columns from a creation commit and every commit that touched the path."""
    if first is None:
        return caches.AiAuthorship()
    first_tools = _tools_in(first)
    count, tools = 0, set()
    for commit in touching:
        found = _tools_in(commit)
        if found:
            count += 1
            tools.update(found)
    return caches.AiAuthorship(
        first_commit_ai_created="TRUE" if first_tools else "FALSE",
        first_commit_ai_tool=",".join(sorted(first_tools)),
        ai_commits=str(count),
        ai_commit_tools=",".join(sorted(tools)),
    )


def reclassify(commits_dir: Path, repo: str, head_sha: str | None = None,
               artifacts: list | None = None) -> str:
    """Apply the current commit heuristics to one repository's commit log.

    Validate provenance, log integrity and path coverage before replacing classifications.
    Return `reclassified` or `current` on success, or an outcome that names the missing
    evidence. Invalid evidence raises before the original shards are replaced. The optional
    revision and path arguments tie the evidence to the collection's inputs.
    """
    _csv_path, json_path = shard_paths(commits_dir, repo)
    with open(json_path, encoding="utf-8") as f:
        record = json.load(f)
    if record.get("repo_name") != repo:
        raise ValueError("The per-repository commit record names a different repository")
    if head_sha is not None and record.get("head_sha") != head_sha:
        raise ValueError("Commit evidence differs from the collected revision")
    if record.get("outcome") != "history_read":
        return "history_not_read"
    log = stored_log_path(commits_dir, repo)
    if not log.is_file():
        return "no_log"
    if not record.get("commit_evidence"):
        return "no_scope"
    if record.get("history_failed"):
        return "history_incomplete"
    entries = record.get("path_commits")
    if entries is None or "collected_artifacts" not in record:
        return "no_path_history"
    recorded_artifacts = [entry["artifact"] for entry in entries]
    # A file that the collector does not copy, which an older record may still name and a
    # handover leaves out of the tree, is left out on both sides of the comparison.
    if (len(recorded_artifacts) != len(set(recorded_artifacts))
            or set(recorded_artifacts) != set(record["collected_artifacts"])
            or (artifacts is not None
                and {a for a in recorded_artifacts if not paths.is_uncollected(a)}
                != {a for a in artifacts if not paths.is_uncollected(a)})
            or set(recorded_artifacts) != {row["artifact"] for row in record.get("ai_rows", [])}):
        return "no_path_history"
    wanted = {sha for entry in entries or () for sha in (entry["first"], *entry["commits"]) if sha}
    for entry in entries:
        if entry["first"] and entry["first"] not in entry["commits"]:
            raise ValueError("The commit that created a path is missing from the path's history")
    records = _validated_records(record, log, wanted)
    if record.get("classified_by") == CLASSIFIED_BY:
        found = {commit["hash"]: commit for commit in records if commit["hash"] in wanted}
        expected = [
            dict(caches.AiAuthorshipKey(repo, record["head_sha"], entry["artifact"]).as_row(),
                 **_ai_authorship_of(found[entry["first"]] if entry["first"] else None,
                                     [found[sha] for sha in entry["commits"]]).as_row())
            for entry in entries]
        if record["ai_rows"] != expected:
            raise ValueError("The recorded AI authorship differs from the AI authorship that "
                             "the commit log gives")
        if _shards_intact(commits_dir, repo, record):
            return "current"
        # A shard that differs from the record, such as a shard that a power loss cut short after
        # the record reached the disk, is written again from the validated commit log, the
        # evidence that the shards are derived from.
        records = _validated_records(record, log, wanted)
    found = classify(record, records, commits_dir, keep=wanted)
    record["ai_rows"] = [
        dict(caches.AiAuthorshipKey(repo, record["head_sha"], entry["artifact"]).as_row(),
             **_ai_authorship_of(found[entry["first"]] if entry["first"] else None,
                                 [found[sha] for sha in entry["commits"]]).as_row())
        for entry in entries]
    write_shard_record(json_path, record)
    return "reclassified"


def _shards_intact(commits_dir: Path, repo: str, record: dict) -> bool:
    """Whether both shards of `repo` have the checksums that the per-repository commit record
    gives and the current header."""
    csv_path, _ = shard_paths(commits_dir, repo)
    for label, path in (("tools", csv_path), ("other_tools", other_shard_path(commits_dir, repo))):
        try:
            if _file_sha256(path) != record.get("classified_outputs", {}).get(label):
                return False
            with path.open(newline="", encoding="utf-8") as stream:
                if next(csv.reader(stream), []) != list(COMMIT_COLUMNS):
                    return False
        except (FileNotFoundError, UnicodeDecodeError, csv.Error):
            return False
    return True


def write_shard_record(json_path: Path, record: dict) -> None:
    """Write a per-repository commit record in one step, a temporary file and a rename."""
    caches.replace_file(json_path, json.dumps(record) + "\n")


def unavailable_record(repo: str, head_sha: str, branch: str, outcome: str) -> dict:
    """The per-repository commit record of a repository with a commit history that was not
    read, with the reason, in the columns of `ai_commits-repos.csv`."""
    return {"repo_name": repo, "head_sha": head_sha, "branch": branch, "outcome": outcome,
            "commits_read": "", "commits_matched": "", "unmatched": [],
            "no_signal_shas": [], "ai_rows": [],
            # A history that the fetch did not bring within the time or size bound recurs under
            # the same bounds. When git cannot be started, the outcome is `collector_error`.
            "transient": False}


def remove_shards(commits_dir: Path | None, repo: str) -> None:
    """Drop the shards and the per-repository commit record that an earlier run left for `repo`.

    Every collection of a repository rewrites or removes its shards, so that the CSV never has
    rows from a clone that a later run could not repeat. A repository with a clone that fails
    this time, or that no longer contributes a file, would otherwise keep the rows of the run
    that read its history before.
    """
    if commits_dir is None:
        return
    csv_path, json_path = shard_paths(commits_dir, repo)
    other_path = other_shard_path(commits_dir, repo)
    log = stored_log_path(commits_dir, repo)
    for path in (csv_path, json_path, csv_path.with_name(csv_path.name + ".tmp"),
                 csv_path.with_suffix(".log"), other_path,
                 other_path.with_name(other_path.name + ".tmp"),
                 log, log.with_name(log.name + ".tmp")):
        path.unlink(missing_ok=True)
    # The temporary name under which `caches.replace_file` writes the per-repository commit
    # record, left when a worker was killed between the write and the rename.
    for path in commits_dir.glob(json_path.name + ".????????.tmp"):
        path.unlink(missing_ok=True)


def readable_shards(commits_dir: Path, repos: "list | set | None" = None) -> list:
    """The shards under `commits_dir`, one entry for each per-repository commit record:
    (repo, csv_path, record).

    `csv_path` is None where the repository has no shard, or where its record does not parse or
    does not have the keys that the cloning step writes. Such a repository is recorded as
    `shard_unreadable`, so that its rows stay out of every CSV built from the shards.
    `assemble_ai_commits` and the restriction of the detection step both read the shards
    through this function. A shard with a header of another column set is logged once, and its
    rows are read into the current columns, blank where a column is new. The header is read
    from the shard itself, because a key of the record could mark the current header as a
    different one. `repos`, when given, restricts the reading to the input list.
    """
    commits_dir = Path(commits_dir)
    wanted = None if repos is None else set(repos)
    shards = []
    for json_path in sorted(commits_dir.glob("*.json")) if commits_dir.is_dir() else []:
        repo = dir_name_to_repo(json_path.stem)
        if wanted is not None and repo not in wanted:
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                record = json.load(f)
            # Every record that the cloning step writes has these three keys. A record without
            # one of them, such as a record that keeps the unmatched attributions under the key
            # `unexplained`, is refused rather than read as having no attributions, which would
            # drop them without a warning.
            if not isinstance(record, dict) or not all(
                    isinstance(record.get(key), list)
                    for key in ("unmatched", "no_signal_shas", "ai_rows")):
                raise TypeError("The record lacks the keys that the cloning step writes")
            record = {**unavailable_record(repo, "", "", "history_read"), **record}
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            logs.warning("commits", f"{json_path.name} is not a readable per-repository commit "
                                    f"record. Recorded {repo} as shard_unreadable and left its "
                                    f"rows out of the commit CSVs.")
            shards.append((repo, None, unavailable_record(repo, "", "", "shard_unreadable")))
            continue
        csv_path = json_path.with_suffix(".csv")
        if not csv_path.is_file():
            shards.append((repo, None, record))
            continue
        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                header = next(csv.reader(f), [])
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            header = []
        if not header:
            logs.warning("commits", f"{csv_path.name} is not a readable shard. Recorded {repo} "
                                    f"as shard_unreadable and left its rows out of the commit "
                                    f"CSVs.")
            shards.append((repo, None, unavailable_record(repo, "", "", "shard_unreadable")))
            continue
        if header != list(COMMIT_COLUMNS):
            logs.warning("commits", f"{repo}'s shard has the columns {header}. Read its rows "
                                    f"into the current columns, blank in each new column.")
        shards.append((repo, csv_path, record))
    return shards


@contextlib.contextmanager
def _replaced(path: Path):
    """Write `path` through a temporary sibling, which is renamed to `path` only when the block
    finishes.

    The CSVs that this module writes are replaced in one step each. A stop part way through
    would leave a truncated `all_ai_commits-unmatched.csv`, and `unmatched commits`, the
    runner's check over it, exits 0 on an unmatched-attributions CSV that contains only its
    header.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    # A temporary file left by a run that ended where the except clause below could not remove
    # it. The cloning step restores the default signal dispositions before it calls this
    # function, and a SIGTERM here ends the process at once. No inventory lists the
    # file and no cleanup removes it, and it would stop the next publication.
    Path(tmp).unlink(missing_ok=True)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            yield f
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _with_newlines(message: str) -> str:
    """The message as the commit CSV stores it: verbatim, every line break a newline.

    The CSV quotes a field that contains a line break, as RFC 4180 specifies. The message
    therefore needs no escaping of its own, and every CSV reader reads it back as it was. A carriage
    return, alone or before a newline, is a line break and becomes a newline, so that a message
    has one kind of line break.
    """
    return message.replace("\r\n", "\n").replace("\r", "\n")


def stored_message(commit: dict) -> str:
    """The message of a commit of git's log, its subject and its body, as the commit CSV stores
    it (`_with_newlines`)."""
    return _with_newlines(commit["subject"] + ("\n\n" + commit["body"] if commit["body"] else ""))


def message_parts(message: str) -> tuple[str, str]:
    """The subject and the body of a message as the commit CSV stores it: split on the first
    blank line, each stripped. The classification and the check of the commit CSV both split a
    message through this function, so that both read a row from its stored message."""
    parts = _with_newlines(message).split("\n\n", 1)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""


def shard_rows(csv_path: Path):
    """The rows of one shard, each in the current column set, blank where it lacks one."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield {column: row.get(column, "") for column in COMMIT_COLUMNS}


def assemble_ai_commits(commits_dir: Path, results: dict, ai_commits_csv: Path,
                        ai_authorship_path: Path | None, repos: list | None = None) -> dict:
    """Write the commit CSV, the commits of the tools outside the six, the unmatched
    attributions, the mentions, `ai_commits-repos.csv` and the AI cache from the shards.

    The shards of the repositories in `repos`, the input list, are read, so that the CSV
    describes the list as the collected list does. A repository dropped from the input drops
    out of the CSV, and a repository collected in an earlier run of this checkout keeps its rows
    until it is collected again. `ai_commits-repos.csv` covers the repositories in `results`,
    the list of finished repositories restricted the same way, so that a failed clone has a row
    with its outcome beside the repositories with a commit history that was read. A
    per-repository commit record that does not parse is logged and its repository recorded as
    `shard_unreadable`, rather than ending the step after the whole collection. Returns the
    counts written.
    """
    unmatched_csv = ai_commits_csv.with_name(f"{ai_commits_csv.stem}-unmatched{ai_commits_csv.suffix}")
    mentions_csv = ai_commits_csv.with_name(MENTIONS_NAME)
    repos_csv = ai_commits_csv.with_name("ai_commits-repos.csv")
    other_csv = ai_commits_csv.with_name(OTHER_AI_COMMITS_NAME)
    ai_commits_csv.parent.mkdir(parents=True, exist_ok=True)
    commits_dir.mkdir(parents=True, exist_ok=True)

    shards = readable_shards(commits_dir, repos)
    records = {repo: record for repo, _, record in shards}

    rows = 0
    with _replaced(ai_commits_csv) as out:
        writer = csv.DictWriter(out, fieldnames=list(COMMIT_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for _, csv_path, _ in shards:
            if csv_path is None:
                continue
            for row in shard_rows(csv_path):
                writer.writerow(row)
                rows += 1

    # The same repositories as the commit CSV. A repository with a per-repository commit record
    # that does not parse is left out of both, and a repository without a shard of the other
    # tools adds nothing.
    other_rows = 0
    with _replaced(other_csv) as out:
        writer = csv.DictWriter(out, fieldnames=list(COMMIT_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for repo, csv_path, _ in shards:
            other_path = other_shard_path(commits_dir, repo)
            if csv_path is None or not other_path.is_file():
                continue
            for row in shard_rows(other_path):
                writer.writerow(row)
                other_rows += 1

    aggregate = {}
    for record in records.values():
        for entry in record["unmatched"]:
            key = (entry["kind"], entry["token"])
            found = aggregate.setdefault(key, {"commits": 0, "repos": 0, "shas": [], "lines": []})
            found["commits"] += entry["commits"]
            found["repos"] += 1
            found["shas"] = sorted(set(found["shas"]) | set(entry["shas"]))[:UNMATCHED_SAMPLE]
            for line in entry["lines"]:
                if line not in found["lines"] and len(found["lines"]) < UNMATCHED_SAMPLE:
                    found["lines"].append(line)
    with _replaced(unmatched_csv) as f:
        writer = csv.DictWriter(f, fieldnames=list(UNMATCHED_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for (kind, token), found in sorted(aggregate.items(),
                                            key=lambda item: (-item[1]["repos"],
                                                              -item[1]["commits"], item[0])):
            writer.writerow({"kind": kind, "token": token, "commits": found["commits"],
                             "repos": found["repos"], "shas": ";".join(found["shas"]),
                             "lines": " | ".join(found["lines"])})

    # The mentions, grouped by phrasing across repositories and ranked the way the unmatched
    # attributions are. A per-repository commit record without the key adds nothing.
    grouped = {}
    for record in records.values():
        for entry in record.get("mentions", []):
            found = grouped.setdefault((entry["tool"], entry["pattern"]),
                                       {"commits": 0, "repos": 0, "shas": [],
                                        "line": entry["line"]})
            found["commits"] += entry["commits"]
            found["repos"] += 1
            found["shas"] = sorted(set(found["shas"]) | set(entry["shas"]))[:MENTION_SAMPLE]
    with _replaced(mentions_csv) as f:
        writer = csv.DictWriter(f, fieldnames=list(MENTION_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for (tool, pattern), found in sorted(grouped.items(),
                                             key=lambda item: (-item[1]["repos"],
                                                               -item[1]["commits"], item[0])):
            writer.writerow({"tool": tool, "pattern": pattern, "commits": found["commits"],
                             "repos": found["repos"], "shas": ";".join(found["shas"]),
                             "line": found["line"]})

    with _replaced(repos_csv) as f:
        writer = csv.DictWriter(f, fieldnames=list(AI_COMMITS_REPOS_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for repo in sorted(results):
            main_row, fail_row = results[repo][0], results[repo][1]
            record = records.get(repo)
            if fail_row is not None:
                outcome = fail_row.rsplit(",", 1)[-1]
            elif record is not None:
                outcome = record["outcome"]
            else:
                outcome = "no_files"
            writer.writerow({
                "repo_name": repo,
                "head_sha": paths.row_column(main_row, "head_sha"),
                "outcome": outcome,
                "commits_read": record["commits_read"] if record else "",
                "commits_matched": record["commits_matched"] if record else "",
                "commits_other_tools": record.get("commits_other_tools", "") if record else "",
                "no_signal_shas": ";".join(record["no_signal_shas"]) if record else "",
            })

    ai_rows = 0
    if ai_authorship_path is not None:
        cache = caches.AiAuthorshipCache.load(ai_authorship_path)
        for record in records.values():
            for row in record.get("ai_rows", []):
                cache.put(caches.AiAuthorshipKey(
                    *(row[c] for c in caches.AI_AUTHORSHIP_KEY_COLUMNS)),
                    caches.AiAuthorship.from_row(row))
                ai_rows += 1
        cache.save(ai_authorship_path)
    logs.info("commits", f"Wrote {rows:,} row(s) to {ai_commits_csv} from the commit histories "
                         f"of {len(records):,} repositories.")
    logs.info("commits", f"Wrote {other_rows:,} row(s) of the tools outside the six to "
                         f"{other_csv.name}.")
    logs.info("commits", f"Wrote {len(aggregate):,} unmatched attributions to "
                         f"{unmatched_csv.name} and {len(grouped):,} mentions to "
                         f"{mentions_csv.name}.")
    if ai_authorship_path is not None:
        logs.info("commits", f"Merged {ai_rows:,} AI-authorship row(s) into "
                             f"{ai_authorship_path.name}.")
    return {"rows": rows, "other_rows": other_rows, "history_read": len(records),
            "unmatched": len(aggregate),
            "mentions": len(grouped), "ai_rows": ai_rows,
            "missing_logs": sorted(repo for repo, record in records.items()
                                   if required_log_missing(commits_dir, repo, record)),
            "unfetched": sorted(repo for repo, record in records.items()
                                if record.get("outcome") == "history_unavailable"
                                and not record.get("transient")),
            "read_timed_out": sorted(repo for repo, record in records.items()
                               if record.get("outcome") == "history_read_failed"
                               and record.get("cause") == "time_bound")}
