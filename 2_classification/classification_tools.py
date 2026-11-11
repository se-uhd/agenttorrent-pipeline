"""The MCP server with the tools that give the classification model more than a repository's
description and README. The classification reads the results of `get_file_tree`,
`get_repository_metadata` and `get_issues_and_pull_requests` for every repository and puts them in
the model's input, and the model calls `read_linked_page`.

    get_file_tree(repo_name, revision, max_chars)
        the outline of the repository's files at the commit: its languages, size, top-level
        entries and the files that show engineering practice, from GitHub's list of every file
        and its bytes per language, without a clone, shortened to `max_chars` unless that is 0
    read_linked_page(url, repo_name)
        a page that the description, the homepage or the README links to, as a summary that a
        model makes of it with the page's purpose sentence: a github.com link through GitHub's
        API, any other link over the web
    get_repository_metadata(repo_name)
        what GitHub records about the repository: its description, homepage, topics, license,
        template flag, whether issues are enabled, and its creation and last push
    get_issues_and_pull_requests(repo_name)
        the activity of the repository's issues and pull requests: their counts, the pinned
        issues, and the most recently updated pull requests and issues with their state, their
        authors' association with the repository and the beginning of their last comment

The classification fills in every parameter except `url` and does not show them to the model.

The server applies the reading rules of 0_README.md, which measurements over a random sample of
the sampling frame set, and each comment names the rule that its code applies. Because the server
is a real MCP server over stdio, any MCP client can reproduce the context that a label was decided
on. Each tool checks its input, because a model supplies it: a repository name must have the
`owner/repo` form, and a web page is read only from an address on the public internet. The stage
runs in a container on a network with other machines, and a README can name any address.

Requests to GitHub use the GitHub access tokens of the environment and the credentials file, and
the summaries use the first API key of the summary model's service, OpenAI's or OpenRouter's
(`agentcontext.credentials`, `endpoints.py`). A failure of GitHub or of the summary model is no
evidence about the repository. A tool therefore raises it as a tool error that names its kind
(`failure`), and the classification raises that error in turn instead of passing it to the model.

Usage (the classification starts it itself, through `mcp_bridge.py`):
  python3 classification_tools.py [--summary-model deepseek/deepseek-v4.1-flash] [--summary-effort low]
  python3 classification_tools.py --summary-model deepseek/deepseek-v4.1-flash \
      --summary-provider morph/fp8 --summary-quantization fp8 \
      --summary-hugging-face-id deepseek-ai/DeepSeek-V4.1-Flash \
      --summary-backup-provider deepinfra/fp8
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import os
import re
import socket
import sys
import threading
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from string import Template
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
import requests.adapters
import urllib3
import urllib3.connection
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import endpoints  # noqa: E402
import readmes  # noqa: E402

from agentcontext import credentials, logs  # noqa: E402
from agentcontext.github_api import NoTokens, TokenPool, graphql_errors  # noqa: E402

# The descriptions of the tools for an MCP client. The classification offers the model
# `read_linked_page` alone and reads the other three itself (`1_classify_repos.READ_FIRST`). The
# description that the model reads is part of the classification parameters that a label is
# recorded under, because its wording affects how often the model calls the tool.
FILE_TREE_DESCRIPTION = (
    "Get an outline of the repository's files at the commit its README was read at: the number "
    "of files and directories, the languages with their shares, which leave out data formats "
    "such as YAML and JSON, every top-level directory with its number of files and most common "
    "file types, the files at the root, and the files that show tests, CI, build and packaging, "
    "documentation, contribution guidelines, changelogs and releases, and containers."
)
LINKED_PAGE_DESCRIPTION = (
    "Get a summary of a page that the description, the homepage or the README links to: what "
    "the page is, what it states about the software, the evidence of software engineering "
    "practice it gives, and what it states about the repository's kind and status, such as that "
    "it is no longer maintained. The result also gives the page's own sentence that says what "
    "the project is or does, word for word, when the page has one. An issue or a pull request "
    "of GitHub is read with the comments of the repository's maintainers. Pass the link as the "
    "description, the homepage or the README writes it, relative or absolute, or the link of an "
    "issue or a pull request as the input's issues and pull requests give it. A relative link "
    "is read from the repository. Call it for a page about the project, such as its documentation or an "
    "announcement about its status, that could change a criterion, the upstream or the label: "
    "when the input does not state it outright, when it rests on an inference, or when the "
    "README links to a page about the project's history or origin."
)
METADATA_DESCRIPTION = (
    "Get what GitHub records about the repository: its description, homepage, topics, license, "
    "whether GitHub marks it as a template repository, which lets others create repositories "
    "from it, whether issues are enabled, when it was created and last pushed to, and the "
    "profile description of its owner."
)
ACTIVITY_DESCRIPTION = (
    "Get the activity of the repository's issues and pull requests on GitHub, from one request: "
    "how many pull requests are open, merged and closed without a merge, how many issues are "
    "open and closed, the issues pinned at the top of the issue tracker, and the most recently "
    "updated pull requests and issues, each with its state, its author's association with the "
    "repository, who merged it, and the beginning of its last comment."
)
INSTRUCTIONS = (
    "Supplementary context for classifying a GitHub repository as an engineered software "
    "project: an outline of its files, summaries of the pages its README links to, what GitHub "
    "records about it, and the activity of its issues and pull requests. When a tool reports its "
    "data as unavailable, the missing data is no evidence about the repository."
)

# The summary model by default, DeepSeek V4.1 Flash on its pinned endpoint
# (`endpoints.PRODUCTION`), at the reasoning effort of the two pilots of the classification.
SUMMARY_MODEL = endpoints.PRODUCTION.model
SUMMARY_EFFORT = "low"
SUMMARY_PROMPT = HERE / "prompts" / "page-summary-prompt.md"
# Rule 10: a summary request stays under the 272,000 input tokens above which OpenAI charges the
# long context price. At the 3.57 characters per token of rule 1, 960,000 characters of page text
# with the prompt stay under that limit.
MAX_SUMMARY_INPUT_CHARS = 960_000
# The characters of an HTML page's text that its conversion keeps (`converted_text`). They are one
# more than the summary reads, so that the summary still states that the page was cut. Because
# html2text's output grows with the square of the depth of nested lists, the conversion stops once
# its text exceeds this length by `CONVERSION_MARGIN`. The margin covers the last characters of a
# partial conversion, which can differ from the same characters in the whole conversion.
PAGE_TEXT_CHARS = MAX_SUMMARY_INPUT_CHARS + 1
CONVERSION_MARGIN = 64
# Rule 9: the first 2 MB of a page. The rules document gives the share of readable pages that this
# limit cuts.
MAX_PAGE_BYTES = 2_000_000
# The content codings that a page is requested in. `BodyDecoder` decodes them into at most the
# bytes that rule 9 leaves. urllib3, the library under requests, decodes each chunk that it reads
# in full, through any number of stacked codings and without a limit on the decoded size. With
# urllib3's decoding, a small compressed body would fill the memory before the limit of rule 9 is
# checked. A response in another coding, or in more than one coding, is a page that cannot be read.
ACCEPT_ENCODING = "gzip, deflate"
# Rule 7: at most 5 HTTP redirects and meta refreshes together. The rules document counts the
# links of the calibration sample that need more than 5.
MAX_REDIRECTS = 5
# Rule 8: 10 seconds to connect and 30 per read. The rules document gives the times that the
# pages of the calibration sample took.
PAGE_TIMEOUT = (10, 30)
# The time that reading one page may take, from its first request to the end of its last
# response, over every redirect and meta refresh. The timeout per read does not end a server that
# sends a few bytes within each read. Such a server would keep the tool, and with it the worker
# that labels the repository, waiting without end. GROUNDING: rule 8 gives the times that the
# pages of the calibration sample took, and the deadline is far above their 99th percentile.
PAGE_DEADLINE = 120
# Rule 3: a body is read only of these types, or of a response that names none.
PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown",
              "text/x-markdown")
ACCEPT = "text/html, application/xhtml+xml, text/plain, text/markdown;q=0.9, */*;q=0.1"
USER_AGENT = "agentcontext-classification (+https://github.com/se-uhd/agenttorrent-pipeline)"
# Rule 4: the kinds of github.com link read through GitHub. Other kinds are not offered. An issue
# or a pull request is read through the API, because scripts render its web page. For example,
# streamlink-twitch-gui's README points to issue #1045 for "the development status of this
# project", and the issue states that active development has ceased.
GITHUB_KINDS = ("repository", "blob", "raw", "tree", "releases", "issues", "pull")
# Of an issue or a pull request, the server reads the comments on the first page that the
# repository's owner, members and collaborators wrote, because they speak for the project.
ISSUE_COMMENTS = 100
MAINTAINERS = ("OWNER", "MEMBER", "COLLABORATOR")
# One query of GitHub's GraphQL API, which costs 1 point, returns the activity of a repository's
# issues and pull requests: the counts, the pinned issues, and the most recently updated pull
# requests and issues with the beginning of their last comment. The REST API returns the counts
# only page by page and does not list the pinned issues. The activity shows a project's status and
# whether it accepts contributions. For example, openssl/openssl closes most of its pull requests
# on GitHub without a merge but reviews the open ones ("It needs rebase"), and
# classification-tools-spec.md gives the counts. The classification decides from the merges
# whether a mirror is developed on GitHub (`latest_merge` of 1_classify_repos.py).
ACTIVITY_ITEMS = 10
COMMENT_CHARS = 300
PINNED_ISSUES = 3
_DISCUSSION = """number title url state createdAt closedAt authorAssociation
      comments(last: 1) { nodes { authorAssociation body } }"""
ACTIVITY_QUERY = """query($owner: String!, $name: String!, $items: Int!) {
  repository(owner: $owner, name: $name) {
    hasIssuesEnabled
    openPullRequests: pullRequests(states: OPEN) { totalCount }
    mergedPullRequests: pullRequests(states: MERGED) { totalCount }
    closedPullRequests: pullRequests(states: CLOSED) { totalCount }
    openIssues: issues(states: OPEN) { totalCount }
    closedIssues: issues(states: CLOSED) { totalCount }
    pinnedIssues(first: %d) { nodes { issue { number title url } } }
    pullRequests(last: $items, orderBy: {field: UPDATED_AT, direction: ASC}) {
      nodes { %s mergedAt mergedBy { login } }
    }
    issues(last: $items, orderBy: {field: UPDATED_AT, direction: ASC}) { nodes { %s } }
  }
}""" % (PINNED_ISSUES, _DISCUSSION, _DISCUSSION)
GITHUB_HOSTS = ("github.com", "www.github.com")
# Rule 6: a page that sends the browser on with a meta tag rather than an HTTP redirect.
META_REFRESH = re.compile(rb"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*"""
                          rb"""["']?\s*\d*\s*;?\s*url\s*=\s*['"]?([^"'>\s]+)""", re.I)

# A tool that cannot return what GitHub or a page states about the repository raises a tool error
# that begins with its kind in brackets. The classification raises that error instead of passing
# it to the model (`1_classify_repos.Classifier.tool`). The kinds name the failure: GitHub refuses
# every GitHub access token of the server, the API refuses every request of the summary model, or
# GitHub or the summary model failed. Passed to the model, such an error would let a label be
# decided and cached without the tool, although the failure is no evidence about the repository.
NO_GITHUB_ACCESS_TOKEN, SUMMARY_REFUSED = "no_github_access_token", "summary_refused"
GITHUB_FAILED, SUMMARY_FAILED = "github_failed", "summary_failed"
# A page that could not be reached while this machine reached no server either, as during a
# failure of its own network or name resolution (`network_down`).
NETWORK_FAILED = "network_failed"
# The service of the summary model refuses the summary for spent credit
# (`endpoints.credit_refusal`). The classification waits for the credit and makes the call again.
SUMMARY_NO_CREDIT = "summary_no_credit"
FAILURES = (NO_GITHUB_ACCESS_TOKEN, SUMMARY_REFUSED, GITHUB_FAILED, SUMMARY_FAILED, NETWORK_FAILED,
            SUMMARY_NO_CREDIT)
# The words with which a failure of GitHub states that its GraphQL response has errors in place
# of fields of the repository (`unresolved_fields`). The summary at the end of the classification
# counts these failures (`field_errors`), so that a recurring cause is visible.
IN_PLACE_OF_FIELDS = "has errors in place of"
# The kind in brackets, at the beginning of the error or after the words that the SDK puts before
# a tool's error, such as "Error executing tool get_file_tree: ".
_FAILURE = re.compile(r"^(?:[^\[\n]*: )?\[(%s)\] (.*)$" % "|".join(FAILURES), re.S)
# Why the API refuses every request for a model that it does not offer (`refusal`).
MODEL_NOT_OFFERED = "the API does not offer the model"
# The refusals (HTTP 400) that a new request does not receive again. For example, the API refused
# a request of stretchr/testify's tool rounds because it no longer found the previous response,
# and a new attempt starts without a previous response. Any other refusal concerns the request's
# input, which a new request repeats.
TRANSIENT_REFUSALS = ("previous_response_not_found",)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=True)

# Rule 11: paths that by their presence show a practice that the prompt names, matched on the
# whole path, lowercased. A practice lists every file that matches (rule 12).
PRACTICES = {
    "tests": re.compile(r"(^|/)(tests?|spec|specs|__tests__|testing|test_?data)/"
                        r"|(^|/)test_[^/]+\.py$|_test\.(go|py|rb|exs?)$|\.(test|spec)\.[jt]sx?$"
                        r"|tests?\.(cs|java|kt|swift)$|(^|/)[^/]*tests?\.(cs|java|kt|scala|swift|php)$"),
    "ci": re.compile(r"^\.github/workflows/[^/]+\.ya?ml$|^\.gitlab-ci\.yml$|^\.circleci/"
                     r"|^jenkinsfile$|^azure-pipelines\.ya?ml$|^\.travis\.yml$|^appveyor\.ya?ml$"
                     r"|^\.buildkite/|^bitbucket-pipelines\.yml$|^\.drone\.yml$"),
    "build and packaging": re.compile(
        r"(^|/)(package\.json|pyproject\.toml|setup\.py|setup\.cfg|cargo\.toml|go\.mod|pom\.xml"
        r"|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|cmakelists\.txt|makefile|gemfile"
        r"|composer\.json|[^/]+\.csproj|[^/]+\.sln|[^/]+\.gemspec|mix\.exs|build\.sbt|meson\.build"
        r"|bazel|build\.bazel|workspace|configure\.ac|deno\.json|pubspec\.yaml|package\.swift"
        r"|[^/]+\.cabal|stack\.yaml|rebar\.config|dune-project)$"),
    "documentation": re.compile(r"^(docs?|documentation|website)/|(^|/)mkdocs\.ya?ml$"
                                r"|(^|/)docs?/conf\.py$|^\.readthedocs\.ya?ml$"),
    "contribution": re.compile(r"(^|/)(contributing|code_of_conduct|security|governance"
                               r"|maintainers)(\.(md|markdown|rst|txt|adoc|org))?$"
                               r"|^\.github/(issue_template|pull_request_template)"),
    "changelog and releases": re.compile(r"(^|/)(changelog|changes|history|news|releases?)"
                                         r"(\.(md|markdown|rst|txt|adoc|org))?$"
                                         r"|^\.github/release\.ya?ml$|^\.goreleaser\.ya?ml$"
                                         r"|^\.release-please"),
    "containers and deployment": re.compile(r"(^|/)dockerfile[^/]*$"
                                            r"|(^|/)docker-compose[^/]*\.ya?ml$"
                                            r"|(^|/)(helm|charts|k8s|kubernetes)/"),
}
# The most common file types that the outline lists for each top-level directory.
OUTLINE_FILE_TYPES = 3


class FileTreeResult(BaseModel):
    """An outline of the repository's files."""

    repo_name: str = Field(description="The repository, as owner/repo.")
    revision: str = Field(description="The commit the outline describes, empty for the default "
                                      "branch.")
    available: bool = Field(description="False when GitHub could not list the files, which is no "
                                        "evidence about the repository.")
    file_tree: str = Field(default="", description="The outline.")
    note: str = Field(default="", description="Why the outline is unavailable, or what of it "
                                              "was cut.")


class LinkedPageResult(BaseModel):
    """A summary of one page."""

    url: str = Field(description="The page that was read, after redirects.")
    available: bool = Field(description="False when the page could not be read, which is no "
                                        "evidence about the repository.")
    summary: str = Field(default="", description="What the page is, what it states about the "
                                                 "software, and the evidence it gives.")
    purpose_sentence: str = Field(default="", description="The page's own sentence that says "
                                                          "what the project is or does, word for "
                                                          "word, or empty when it has none.")
    other_repository: str = Field(default="", description="The repository on GitHub that the "
                                                          "page belongs to when it is another "
                                                          "repository, as GitHub names it, or "
                                                          "empty.")
    note: str = Field(default="", description="Why the page is unavailable, or that it was cut.")
    input_tokens: int = Field(default=0, description="Tokens the summary request read.")
    output_tokens: int = Field(default=0, description="Tokens the summary request wrote.")
    model: str = Field(default="", description="The model the summary request reports.")
    cached_tokens: int = Field(default=0, description="Input tokens the summary request read "
                                                      "from the prompt cache.")
    cache_write_tokens: int = Field(default=0, description="Input tokens the summary request "
                                                           "wrote to the prompt cache.")
    service_tier: str = Field(default="", description="The service tier the summary request "
                                                      "reports.")


class RepositoryMetadataResult(BaseModel):
    """What GitHub records about the repository."""

    repo_name: str = Field(description="The repository, as owner/repo.")
    available: bool = Field(description="False when the request to GitHub failed, which is no evidence "
                                        "about the repository.")
    description: str = Field(default="", description="The one-line summary GitHub shows, empty "
                                                     "when the repository has none.")
    homepage: str = Field(default="", description="The website the repository names.")
    topics: list[str] = Field(default_factory=list, description="The repository's topics.")
    license: str = Field(default="", description="The license GitHub recognizes.")
    is_template: bool = Field(default=False, description="Whether GitHub marks the repository "
                                                         "as a template repository, which lets "
                                                         "others create repositories from it.")
    has_issues: bool = Field(default=False, description="Whether issues are enabled.")
    created_at: str = Field(default="", description="When the repository was created.")
    pushed_at: str = Field(default="", description="When it was last pushed to.")
    owner_description: str = Field(default="", description="The profile description of the "
                                                           "repository's owner on GitHub, an "
                                                           "organization's description or a "
                                                           "user's bio, empty when it has none.")
    note: str = Field(default="", description="Why the metadata is unavailable.")


class Discussion(BaseModel):
    """A pull request or an issue."""

    number: int = Field(description="Its number.")
    title: str = Field(description="Its title.")
    url: str = Field(description="Its link, which read_linked_page reads.")
    state: str = Field(description="OPEN, CLOSED or, for a pull request, MERGED.")
    opened: str = Field(description="When it was opened.")
    closed: str = Field(default="", description="When it was merged or closed.")
    author: str = Field(description="Its author's association with the repository, such as "
                                    "OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR or NONE.")
    merged_by: str = Field(default="", description="Who merged the pull request.")
    last_comment: str = Field(default="", description="The beginning of the last comment, after "
                                                      "its author's association.")


class PinnedIssue(BaseModel):
    """An issue that the repository pins at the top of its issue tracker."""

    number: int = Field(description="Its number.")
    title: str = Field(description="Its title.")
    url: str = Field(description="Its link, which read_linked_page reads.")


class ActivityResult(BaseModel):
    """The activity of the repository's issues and pull requests."""

    repo_name: str = Field(description="The repository, as owner/repo.")
    available: bool = Field(description="False when the request to GitHub failed, which is no evidence "
                                        "about the repository.")
    has_issues: bool = Field(default=False, description="Whether issues are enabled.")
    open_pull_requests: int = Field(default=0, description="The open pull requests.")
    merged_pull_requests: int = Field(default=0, description="The pull requests merged in the "
                                                             "repository on GitHub.")
    closed_pull_requests: int = Field(default=0, description="The pull requests closed without "
                                                             "a merge.")
    open_issues: int = Field(default=0, description="The open issues.")
    closed_issues: int = Field(default=0, description="The closed issues.")
    pinned_issues: list[PinnedIssue] = Field(default_factory=list, description="The issues "
                                             "pinned at the top of the issue tracker.")
    recent_pull_requests: list[Discussion] = Field(default_factory=list, description="The most "
                                                   "recently updated pull requests, newest first.")
    recent_issues: list[Discussion] = Field(default_factory=list, description="The most recently "
                                            "updated issues, newest first.")
    note: str = Field(default="", description="Why the activity is unavailable.")


class InvalidInput(ValueError):
    """A tool argument that the tool refuses."""


def failure(kind: str, message: str) -> ToolError:
    """A tool error of `kind`, one of FAILURES, which the classification raises rather than
    passing it to the model."""
    return ToolError(f"[{kind}] {message}")


def failure_of(output: str) -> tuple[str, str] | None:
    """The kind and the message of a tool error that `failure` made, or None for any other
    output."""
    match = _FAILURE.match(output or "")
    return (match.group(1), match.group(2)) if match else None


def refusal(error) -> str:
    """Why the API refuses every request of the run, read from the error of one request, or ""
    when the error concerns that request alone or a top-up ends it.

    The API refuses every request for a rejected API key (HTTP 401), a project or a key without
    access (HTTP 403) and a model that the API does not offer (error code `model_not_found`).
    The classification stops its run on such an error, because every later request would fail
    the same way and a top-up does not end it. A refusal for spent credit
    (`endpoints.credit_refusal`), such as OpenRouter's HTTP 403 for a spent key limit, is none
    of these, because the classification waits for the credit, and neither is OpenRouter's
    HTTP 403 for content that its moderation or a guardrail blocked (`endpoints.blocked`),
    which concerns that request alone."""
    import openai
    if endpoints.credit_refusal(error) is not None or endpoints.blocked(error):
        return ""
    router = endpoints.from_openrouter(error)
    if isinstance(error, openai.AuthenticationError):
        return ("OpenRouter rejects the OpenRouter API key" if router
                else "the API rejects the OpenAI API key")
    if isinstance(error, openai.PermissionDeniedError):
        return ("OpenRouter denies the OpenRouter API key access" if router
                else "the API denies the project access")
    if not isinstance(error, openai.APIStatusError):
        return ""
    if getattr(error, "code", None) == "model_not_found":
        return MODEL_NOT_OFFERED
    return ""


def service_failure(error) -> bool:
    """Whether a request failed for the API's service rather than for its repository or its
    page: a rate limit or a lack of flex capacity (HTTP 429), a server error (HTTP 5xx), a
    timeout or a broken connection, after the SDK's own retries. Through OpenRouter, it is also
    an endpoint that does not respond: "There is no available model provider that meets your
    routing requirements" (HTTP 503), a request that "timed out" (HTTP 408), a request that no
    endpoint matching the provider routing serves (HTTP 404, openrouter.ai/docs/features/
    provider-routing), and the requests in flight that fill their budget (`endpoints.in_flight`).
    The classification then pauses, or sends the request to the backup provider of the model
    (`endpoints.Providers`). A refusal (`refusal`) and a refusal for spent credit
    (`endpoints.credit_refusal`) are no such failure."""
    import openai
    if refusal(error) or endpoints.credit_refusal(error) is not None:
        return False
    if endpoints.in_flight(error):
        return True
    if isinstance(error, openai.APIStatusError):
        return (error.status_code == 429 or error.status_code >= 500
                or (endpoints.from_openrouter(error) and error.status_code in (404, 408)))
    return isinstance(error, openai.APIConnectionError)


_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def checked_repo_name(repo_name: str) -> str:
    """`repo_name` when it is `owner/repo` with GitHub's characters, or InvalidInput otherwise.

    GitHub allows a hyphen at the start of a repository's name, such as
    Hackerprod/-SKYNET-Steam-Emulator in the sampling frame, but not at the start of an
    owner's name. The name goes only into the addresses of API requests."""
    parts = (repo_name or "").strip().split("/")
    if len(parts) != 2 or not all(_NAME_PART.match(p) for p in parts) or \
            any(p in (".", "..") for p in parts) or parts[0].startswith("-"):
        raise InvalidInput(f"repo_name must be owner/repo: {repo_name!r}")
    return "/".join(parts)


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix.lower() or PurePosixPath(path).name


def outline(tree: dict, languages: dict, max_chars: int = 0) -> tuple[str, str]:
    """The outline of a repository from GitHub's recursive git tree and its bytes per language,
    with a note that states what was cut. Nothing is left out (rule 12) unless the outline is
    longer than `max_chars`. Rule 1 then shortens the list of files of each practice to the same
    length and, if the outline is still too long, cuts the outline itself."""
    entries = tree.get("tree") or []
    blobs = [e["path"] for e in entries if e.get("type") == "blob"]
    directories = [e["path"] for e in entries if e.get("type") == "tree"]
    submodules = [e["path"] for e in entries if e.get("type") == "commit"]
    lines = [f"Files: {len(blobs):,}, directories: {len(directories):,}, "
             f"deepest path: {max((p.count('/') + 1 for p in blobs), default=0)} levels"
             + (f", submodules: {len(submodules)}" if submodules else "")
             + (" (GitHub listed only part of the tree)" if tree.get("truncated") else "")]
    total = sum(languages.values()) or 1
    ranked = sorted(languages.items(), key=lambda kv: kv[1], reverse=True)
    lines.append("Languages: " + (", ".join(f"{name} {size / total:.1%}" for name, size in ranked)
                                  if ranked else "none detected"))
    per_top, root_files = defaultdict(list), []
    for path in blobs:
        head, _, rest = path.partition("/")
        (per_top[head] if rest else root_files).append(path)
    lines.append("Top level:")
    for name, files in sorted(per_top.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        kinds = Counter(_extension(p) for p in files).most_common(OUTLINE_FILE_TYPES)
        noun = "file" if len(files) == 1 else "files"
        lines.append(f"  {name}/ ({len(files):,} {noun}: "
                     + ", ".join(f"{k} {c:,}" for k, c in kinds) + ")")
    if root_files:
        lines.append("  " + ", ".join(sorted(root_files)))
    matches = {practice: sorted(p for p in blobs if pattern.search(p.lower()))
               for practice, pattern in PRACTICES.items()}
    matches = {practice: paths for practice, paths in matches.items() if paths}
    lines.append("Paths that show engineering practice:" if matches
                 else "Paths that show engineering practice: none found")

    def text(listed: int | None) -> str:
        """The outline with at most `listed` files named per practice, all with None."""
        found = []
        for practice, paths in matches.items():
            noun = "file" if len(paths) == 1 else "files"
            named = paths if listed is None else paths[:listed]
            more = f", and {len(paths) - len(named):,} more" if len(named) < len(paths) else ""
            found.append(f"  {practice}: {len(paths):,} {noun}"
                         + (" (" + ", ".join(named) + more + ")" if named else ""))
        return "\n".join(lines + found)

    whole = text(None)
    if not max_chars or len(whole) <= max_chars:
        return whole, ""
    # Bisection finds the largest number of files per practice that fits, because the outline's
    # length grows with that number.
    low, high = 0, max(len(paths) for paths in matches.values()) if matches else 0
    while low < high:
        middle = (low + high + 1) // 2
        if len(text(middle)) <= max_chars:
            low = middle
        else:
            high = middle - 1
    shortened = text(low)
    if len(shortened) <= max_chars:
        return shortened, (f"The lists of files per practice name at most {low:,} files each, "
                           f"so that the request stays within its size limit.")
    return whole[:max_chars], (f"The outline is cut after {max_chars:,} characters, so that the "
                               f"request stays within its size limit.")


_github_client = None


def github_client(credentials_path: Path) -> readmes.Client:
    """The client for GitHub's API, with every GitHub access token of the environment and the
    credentials file at `credentials_path`. A GitHub access token is dropped only when GitHub also refuses it for a
    request of its rate limit. Once GitHub has refused every GitHub access token, a tool call
    checks them again after each of `readmes.RECHECK_WAITS` before it reports that none is left
    (`readmes.Client`)."""
    global _github_client
    if _github_client is None:
        tokens = credentials.github_tokens(credentials_path, os.environ)
        _github_client = readmes.Client(TokenPool(tokens))
    return _github_client


def file_tree(client, repo_name: str, revision: str = "", max_chars: int = 0) -> FileTreeResult:
    """The outline of `repo_name` at `revision`, or at the default branch when `revision` is
    empty, in at most `max_chars` characters if that is not 0. When GitHub fails to respond, the
    function raises `readmes.NoResponse`, and the tool raises that error as a failure
    (`from_github` in `build_server`)."""
    base = f"https://api.github.com/repos/{repo_name}"
    tree = client.get(f"{base}/git/trees/{revision or 'HEAD'}?recursive=1")
    if tree.status_code != 200:
        return FileTreeResult(repo_name=repo_name, revision=revision, available=False,
                              note=f"GitHub returned HTTP {tree.status_code} for the files")
    languages = client.get(f"{base}/languages")
    counts = languages.json() if languages.status_code == 200 else {}
    text, note = outline(tree.json(), counts if isinstance(counts, dict) else {}, max_chars)
    return FileTreeResult(repo_name=repo_name, revision=revision, available=True,
                          file_tree=text, note=note)


def _discussion(node: dict) -> Discussion:
    """A pull request or an issue of the GraphQL response."""
    comment = ((node.get("comments") or {}).get("nodes") or [None])[0] or {}
    last = (f"{comment.get('authorAssociation') or ''}: "
            f"{' '.join(str(comment.get('body') or '').split())}"[:COMMENT_CHARS]
            if comment else "")
    return Discussion(number=int(node.get("number") or 0), title=str(node.get("title") or ""),
                      url=str(node.get("url") or ""), state=str(node.get("state") or ""),
                      opened=str(node.get("createdAt") or ""),
                      closed=str(node.get("mergedAt") or node.get("closedAt") or ""),
                      author=str(node.get("authorAssociation") or ""),
                      merged_by=str((node.get("mergedBy") or {}).get("login") or ""),
                      last_comment=last)


def unresolved_fields(body, found: dict) -> list:
    """The fields of `found`, the repository of the GraphQL response `body`, that are null or
    have `nodes` that are null or hold a null, and that an error of `body` names.

    GraphQL sets a field that it could not resolve to null and reports the failure in the
    errors, with the path from the query's root to the field, as GitHub does for a part of a
    query that timed out. Read as a value, such a field would state that the repository has no
    pull requests or issues. An error names the field that is the second element of its path,
    after `repository`. An error without such a path could have left any field null and is taken
    to name every field. A null that no error names is GitHub's value, such as the `pinnedIssues`
    that GitHub's schema declares nullable, beside an error deep in another field."""
    named, anywhere = set(), False
    for error in graphql_errors(body):
        path = error.get("path")
        if isinstance(path, list) and len(path) > 1 and isinstance(path[1], str):
            named.add(path[1])
        else:
            anywhere = True
    return [key for key, value in found.items()
            if (anywhere or key in named)
            and (value is None or isinstance(value, dict) and "nodes" in value
                 and (not isinstance(value["nodes"], list) or None in value["nodes"]))]


def field_errors(message: str) -> bool:
    """Whether the message of a failure of GitHub states that its GraphQL response has errors in
    place of fields of the repository, as `activity` and `latest_merge` of 1_classify_repos.py
    raise it."""
    return IN_PLACE_OF_FIELDS in message


def activity(client, repo_name: str) -> ActivityResult:
    """The activity of the issues and pull requests of `repo_name`, from one GraphQL query.

    A response without the repository means that GitHub did not find the repository when its
    errors state so or when it has no errors. Errors of another kind, such as a failure of GitHub
    that it also reports with HTTP 200, raise `readmes.NoResponse`. Taken for a repository that
    GitHub did not find, the failure would reach the model as a fact about the repository. A body,
    a `data` or a repository that is not an object raises `readmes.NoResponse` too, and so does a
    repository with a field that the errors left null (`unresolved_fields`), which would reach
    the model as a count of zero."""
    owner, name = repo_name.split("/", 1)
    response = client.query(ACTIVITY_QUERY, {"owner": owner, "name": name,
                                             "items": ACTIVITY_ITEMS})
    if response.status_code != 200:
        return ActivityResult(repo_name=repo_name, available=False,
                              note=f"GitHub returned HTTP {response.status_code}")
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    found = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(body, dict) or not isinstance(data, dict | None) \
            or not isinstance(found, dict | None):
        raise readmes.NoResponse(f"GitHub's response for the issues and pull requests of "
                                 f"{repo_name} is not the JSON object that GitHub documents")
    if not found:
        errors = graphql_errors(body)
        kinds = [error.get("type") for error in errors]
        if errors and "NOT_FOUND" not in kinds:
            said = [str(error.get("message") or error.get("type")) for error in errors]
            raise readmes.NoResponse(f"GitHub's response for the issues and pull requests of "
                                     f"{repo_name} has no repository ({', '.join(said)})")
        return ActivityResult(repo_name=repo_name, available=False,
                              note="GitHub's GraphQL API did not find the repository")
    unresolved = unresolved_fields(body, found)
    if unresolved:
        said = [str(error.get("message") or error.get("type")) for error in graphql_errors(body)]
        raise readmes.NoResponse(f"GitHub's response for the issues and pull requests of "
                                 f"{repo_name} {IN_PLACE_OF_FIELDS} {', '.join(unresolved)} "
                                 f"({', '.join(said)})")

    def total(key: str) -> int:
        return int((found.get(key) or {}).get("totalCount") or 0)

    def newest(key: str) -> list:
        return [_discussion(node) for node in reversed((found.get(key) or {}).get("nodes") or [])
                if node]
    return ActivityResult(
        repo_name=repo_name, available=True, has_issues=bool(found.get("hasIssuesEnabled")),
        open_pull_requests=total("openPullRequests"),
        merged_pull_requests=total("mergedPullRequests"),
        closed_pull_requests=total("closedPullRequests"), open_issues=total("openIssues"),
        closed_issues=total("closedIssues"),
        pinned_issues=[PinnedIssue(number=int(node["issue"]["number"]),
                                   title=str(node["issue"]["title"]),
                                   url=str(node["issue"]["url"]))
                       for node in (found.get("pinnedIssues") or {}).get("nodes") or []
                       if (node or {}).get("issue")],
        recent_pull_requests=newest("pullRequests"), recent_issues=newest("issues"))


def owner_description(client, owner: dict, owners: dict) -> str:
    """The profile description of the repository's owner, as GitHub's API gives it: an
    organization's `description` from `GET /orgs/{org}` or a user's `bio` from
    `GET /users/{user}`, or "" when it has none. KDE's organization describes every one of its
    repositories: "Official read-only mirror of the KDE project. Please don't send pull
    requests". `owners` keeps each owner's description for the run, so that the repositories of
    one owner cost one request. A failure other than 404 is not kept, and the server requests the
    description again for the owner's next repository."""
    login, kind = str(owner.get("login") or ""), owner.get("type")
    if not login:
        return ""
    if login.lower() in owners:
        return owners[login.lower()]
    organization = kind == "Organization"
    response = client.get(f"https://api.github.com/{'orgs' if organization else 'users'}/{login}")
    if response.status_code not in (200, 404):
        return ""
    body = response.json() if response.status_code == 200 else {}
    text = str((body.get("description") if organization else body.get("bio")) or "").strip() \
        if isinstance(body, dict) else ""
    owners[login.lower()] = text
    return text


def repository_metadata(client, repo_name: str, owners: dict | None = None
                        ) -> RepositoryMetadataResult:
    """What GitHub records about `repo_name`, from `GET /repos/{owner}/{repo}`, and the
    description of its owner (`owner_description`), with `owners` as that function's record of
    the run."""
    response = client.get(f"https://api.github.com/repos/{repo_name}")
    if response.status_code != 200:
        return RepositoryMetadataResult(repo_name=repo_name, available=False,
                                        note=f"GitHub returned HTTP {response.status_code}")
    body = response.json()
    license_info = body.get("license") or {}
    return RepositoryMetadataResult(
        repo_name=str(body.get("full_name") or repo_name), available=True,
        description=body.get("description") or "", homepage=body.get("homepage") or "",
        topics=list(body.get("topics") or []),
        license=license_info.get("spdx_id") or license_info.get("name") or "",
        is_template=bool(body.get("is_template")), has_issues=bool(body.get("has_issues")),
        created_at=body.get("created_at") or "", pushed_at=body.get("pushed_at") or "",
        owner_description=owner_description(client, body.get("owner") or {},
                                            owners if owners is not None else {}))


# The notes of `read_web` for a page with a host that did not resolve, a request that reached no
# server, a body that lost its connection and a page that the deadline ended. A failure of this
# machine's network gives the same notes (`unreachable`).
DOES_NOT_RESOLVE, REQUEST_FAILED = "does not resolve", "the request failed"
READ_FAILED, TOO_LATE = "the page could not be read", "reading the page took more than"
NOT_A_URL = "the address is not a valid URL"
# When a page could not be reached, the tool connects to GitHub's API on its https port, within
# the connect timeout of the pages, to tell a failure of the page's host from a failure of this
# machine's network. The classification reaches GitHub's API for every repository.
NETWORK_PROBE = ("api.github.com", 443)


def unreachable(note: str) -> bool:
    """Whether `read_web`'s note states that the page's host did not resolve, that the request
    reached no server, that the connection failed while the body arrived or that the deadline
    ended the page."""
    return (note.startswith((REQUEST_FAILED, READ_FAILED, TOO_LATE))
            or f" {DOES_NOT_RESOLVE}: " in note)


def network_down() -> bool:
    """Whether this machine cannot open a connection to NETWORK_PROBE, as when its own network
    or name resolution fails rather than a page's host."""
    try:
        socket.create_connection(NETWORK_PROBE, timeout=PAGE_TIMEOUT[0]).close()
    except OSError:
        return True
    return False


def public_address(host: str) -> str:
    """"" when every address that `host` resolves to is on the public internet, or the reason
    otherwise."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError) as error:
        return f"{host} {DOES_NOT_RESOLVE}: {error}"
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            return f"{host} resolves to {address}, which is not on the public internet"
    return ""


def page_text(body: bytes, content_type: str) -> str:
    """A page's text: HTML through html2text without links or images (`converted_text`), other
    text as it is."""
    charset = re.search(r"charset=([\w-]+)", content_type or "")
    try:
        text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except (LookupError, UnicodeError):
        # A charset that Python does not know, or a known charset that it cannot decode with
        # `errors="replace"`, such as `undefined` or `idna`.
        text = body.decode("utf-8", errors="replace")
    if "html" in (content_type or "") or \
            text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return converted_text(text)
    return _tidied(text)


def _tidied(text: str) -> str:
    """`text` with at most two line breaks in a row and without whitespace at its ends."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _Converted(Exception):
    """Raised out of html2text once the text that it has written is long enough, with the first
    PAGE_TEXT_CHARS characters of that text."""

    def __init__(self, text: str):
        super().__init__()
        self.text = text


def converted_text(html: str) -> str:
    """The text of `html` from html2text, without links or images, cut at PAGE_TEXT_CHARS.

    Because html2text's output grows with the square of the depth of nested lists, the conversion
    stops once the text written so far, tidied as the whole text would be, is longer than
    PAGE_TEXT_CHARS by more than CONVERSION_MARGIN. The text is measured each time the number of
    characters written doubles, which keeps the cost of measuring linear in that number. The
    first PAGE_TEXT_CHARS characters equal those of the whole conversion. Only the last characters
    of the text written so far can differ, by a part of the placeholder that html2text writes for
    a non-breaking space or by a `[` that it takes back, and the margin covers these characters."""
    import html2text
    converter = html2text.HTML2Text()
    converter.ignore_links = converter.ignore_images = True
    converter.body_width = 0
    # finish() replaces the placeholder with a space unless `unicode_snob` is set, which it is not.
    placeholder = html2text.config.UNIFIABLE["nbsp"]
    written, measure_at = 0, PAGE_TEXT_CHARS

    def out(piece: str) -> None:
        nonlocal written, measure_at
        converter.outtextf(piece)
        written += len(piece)
        if written < measure_at:
            return
        so_far = _tidied("".join(converter.outtextlist).replace(placeholder, " "))
        if len(so_far) > PAGE_TEXT_CHARS + CONVERSION_MARGIN:
            raise _Converted(so_far[:PAGE_TEXT_CHARS])
        measure_at = 2 * written

    converter.out = out
    try:
        return _tidied(converter.handle(html))[:PAGE_TEXT_CHARS]
    except _Converted as converted:
        return converted.text


def deadline_session(sockets: list, expired: threading.Event) -> requests.Session:
    """A session with connections that add their sockets to `sockets` as they connect, so that
    `read_web` can shut them down at its deadline. A connection made after the deadline has
    `expired` shuts its socket down at once. The connect timeout bounds each connection, but a
    connection made after the shutdown would otherwise wait on a server that sends a byte within
    every read timeout."""
    def keep(sock) -> None:
        sockets.append(sock)
        if expired.is_set():
            shut([sock])

    class Connection(urllib3.connection.HTTPConnection):
        def connect(self):
            super().connect()
            keep(self.sock)

    class SecureConnection(urllib3.connection.HTTPSConnection):
        def connect(self):
            super().connect()
            keep(self.sock)

    class Pool(urllib3.HTTPConnectionPool):
        ConnectionCls = Connection

    class SecurePool(urllib3.HTTPSConnectionPool):
        ConnectionCls = SecureConnection

    adapter = requests.adapters.HTTPAdapter()
    adapter.poolmanager.pool_classes_by_scheme = {"http": Pool, "https": SecurePool}
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def shut(sockets: list) -> None:
    """Shut down every socket of `sockets`, so that a read blocked on one returns at once."""
    for sock in list(sockets):
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass


def raw_chunks(response, size: int = 65536):
    """The body of `response` as the connection delivers it, in chunks of at most `size` bytes,
    with its content coding left for `BodyDecoder`. urllib3's errors are raised as the exceptions
    of requests that `iter_content` raises for them."""
    try:
        yield from response.raw.stream(size, decode_content=False)
    except urllib3.exceptions.ProtocolError as error:
        raise requests.exceptions.ChunkedEncodingError(error) from error
    except urllib3.exceptions.ReadTimeoutError as error:
        raise requests.exceptions.ConnectionError(error) from error
    except urllib3.exceptions.SSLError as error:
        raise requests.exceptions.SSLError(error) from error


class BodyDecoder:
    """The decoder of a body in one of the content codings of ACCEPT_ENCODING. It returns at most
    the bytes that it is allowed, so that a body that decodes to far more than MAX_PAGE_BYTES is
    never held whole.

    It decodes as urllib3 does: gzip with any further members, ignoring data after a whole member
    that does not decode, and deflate with or without the zlib header, because servers send both.
    A body in another coding, in more than one coding or that does not decode raises
    ContentDecodingError, the exception that requests raises for a body that it cannot decode."""

    def __init__(self, coding: str):
        coding = (coding or "").strip().lower()
        if coding not in ("", "identity", "gzip", "x-gzip", "deflate"):
            raise requests.exceptions.ContentDecodingError(
                f"the request offers {ACCEPT_ENCODING}, and the response is in {coding}")
        self.coding = {"identity": "", "x-gzip": "gzip"}.get(coding, coding)
        self.inflater = zlib.decompressobj(16 + zlib.MAX_WBITS if self.coding == "gzip"
                                           else zlib.MAX_WBITS)
        self.members, self.ignoring = 1, False
        # For deflate: the input before the first output, kept up to the limit of rule 9. When
        # that input does not decode with the zlib header, it is decoded again without the header.
        self.unconfirmed: bytes | None = b"" if self.coding == "deflate" else None

    def decode(self, data: bytes, limit: int) -> bytes:
        """The decoded bytes of `data`, the next part of the body, at most `limit` of them."""
        if not self.coding:
            return data[:limit]
        decoded = bytearray()
        while data and len(decoded) < limit and not self.ignoring:
            if self.unconfirmed is not None:
                self.unconfirmed += data
                if len(self.unconfirmed) > MAX_PAGE_BYTES:
                    raise requests.exceptions.ContentDecodingError(
                        "the deflate body gives no output")
            try:
                output = self.inflater.decompress(data, limit - len(decoded))
            except zlib.error as error:
                if self.coding == "gzip" and self.members > 1:
                    self.ignoring = True
                    break
                if self.unconfirmed is not None:
                    data, self.unconfirmed = self.unconfirmed, None
                    self.inflater = zlib.decompressobj(-zlib.MAX_WBITS)
                    continue
                raise requests.exceptions.ContentDecodingError(error) from error
            if output:
                self.unconfirmed = None
            decoded += output
            if self.inflater.unconsumed_tail:
                data = self.inflater.unconsumed_tail
            elif self.coding == "gzip" and self.inflater.eof and self.inflater.unused_data:
                data = self.inflater.unused_data
                self.inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                self.members += 1
            else:
                break
        return bytes(decoded)

    def flush(self, limit: int) -> bytes:
        """What the decoder holds at the end of the body, at most `limit` bytes."""
        if not self.coding or self.ignoring:
            return b""
        try:
            return self.inflater.flush()[:limit]
        except zlib.error as error:
            raise requests.exceptions.ContentDecodingError(error) from error


def read_web(url: str, session=None, clock=None) -> tuple[str | None, str, str]:
    """The text of the page at `url`, the address that it was read at, and a note: why there is no
    text, or that the download was cut. Redirects and meta refreshes are followed to public
    addresses only (rules 3, 6, 7, 8 and 9). `BodyDecoder` decodes the body up to MAX_PAGE_BYTES,
    and the reading ends PAGE_DEADLINE seconds after it began on `clock`.

    The timeout of each read does not end a read from a server that sends a few bytes within
    every read timeout, whether for a status line, a header or a chunk of the body. The function
    therefore checks the deadline between chunks. With the default session, a timer also shuts
    the connections down at the deadline, which ends a waiting read."""
    sockets: list = []
    expired = threading.Event()
    own = session is None
    if own:
        session = deadline_session(sockets, expired)

    def expire() -> None:
        # The event is set before the shutdown, so that a connection that adds its socket after
        # `shut` has copied the list finds the deadline expired and shuts the socket itself.
        expired.set()
        shut(sockets)

    timer = threading.Timer(PAGE_DEADLINE, expire)
    timer.daemon = True
    timer.start()
    try:
        return _read_web(url, session, clock or time.monotonic, expired)
    finally:
        timer.cancel()
        if own:
            session.close()


def _read_web(url: str, session, clock, expired: threading.Event) -> tuple[str | None, str, str]:
    """`read_web` over `session`, with the deadline checked on `clock` and on `expired`, which
    the timer of `read_web` sets."""
    deadline = clock() + PAGE_DEADLINE
    late = f"{TOO_LATE} {PAGE_DEADLINE} seconds"

    def past() -> bool:
        # Because the timer started before `deadline` was read, it can shut a connection down
        # before `clock` reaches `deadline`. Without the check of `expired`, a body without a
        # length that the shutdown ended would be taken as the whole page.
        return expired.is_set() or clock() >= deadline
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            parts = urlsplit(current)
        except ValueError:
            # For example, an IPv6 address without its closing bracket.
            return None, current, NOT_A_URL
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None, current, "only an http or https address is read"
        # The check reads the host that the request connects to. urlsplit ends the host only at
        # "/", "?" or "#", and the request also at a backslash, so that urlsplit reads
        # "http://10.0.0.5\@docs.example/" as docs.example and the request connects to 10.0.0.5.
        try:
            host = urlsplit(requests.Request("GET", current).prepare().url).hostname
        except ValueError:
            return None, current, NOT_A_URL
        refused = public_address(host)
        if refused:
            return None, current, refused
        if past():
            return None, current, late
        try:
            response = session.get(current, timeout=PAGE_TIMEOUT, allow_redirects=False,
                                   stream=True, headers={"User-Agent": USER_AGENT,
                                                         "Accept": ACCEPT,
                                                         "Accept-Encoding": ACCEPT_ENCODING})
        except requests.RequestException as error:
            if past():
                return None, current, late
            return None, current, f"{REQUEST_FAILED}: {type(error).__name__}"
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            response.close()
            try:
                current = urljoin(current, response.headers["Location"])
            except ValueError:
                return None, current, NOT_A_URL
            continue
        if response.status_code != 200:
            response.close()
            return None, current, f"the page returned HTTP {response.status_code}"
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and content_type.split(";")[0].strip() not in PAGE_TYPES:
            response.close()
            return None, current, f"the page is {content_type.split(';')[0]}, not text"
        body, cut = bytearray(), False
        try:
            decoder = BodyDecoder(response.headers.get("Content-Encoding", ""))
            for chunk in raw_chunks(response):
                body += decoder.decode(chunk, MAX_PAGE_BYTES - len(body))
                if len(body) >= MAX_PAGE_BYTES:
                    cut = True
                    break
                if past():
                    return None, current, late
            else:
                # A body without a length ends when the connection closes, and therefore also
                # when the timer shuts the connection down at the deadline. Without this check,
                # such a body would be taken as the whole page.
                if past():
                    return None, current, late
                body += decoder.flush(MAX_PAGE_BYTES - len(body))
                cut = len(body) >= MAX_PAGE_BYTES
        except requests.RequestException as error:
            if past():
                return None, current, late
            return None, current, f"{READ_FAILED}: {type(error).__name__}"
        finally:
            response.close()
        body = bytes(body)
        refresh = META_REFRESH.search(body[:20000])
        if refresh:
            try:
                target = urljoin(current, refresh.group(1).decode("utf-8", "replace"))
            except ValueError:
                return None, current, NOT_A_URL
            if target != current:
                current = target
                continue
        text = page_text(body, content_type)
        if not text:
            return None, current, "the page has no text"
        return text, current, (f"the download was cut at {MAX_PAGE_BYTES:,} bytes" if cut else "")
    return None, current, f"more than {MAX_REDIRECTS} redirects"


def github_link(url: str) -> tuple[str, str, str, str] | None:
    """For a github.com link, the kind of page that it names, the `owner/repo`, the ref and the
    path. None for a link elsewhere or for an address that is no URL, which `read_web` reports."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in GITHUB_HOSTS:
        return None
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return "account", "", "", ""
    owner_repo = f"{segments[0]}/{segments[1].removesuffix('.git')}"
    if len(segments) == 2:
        return "repository", owner_repo, "", ""
    kind = segments[2].lower()
    ref = segments[3] if len(segments) > 3 else ""
    return kind, owner_repo, ref, "/".join(segments[4:])


def _dot_segment(url: str) -> bool:
    """Whether the path of `url` has a `.` or `..` segment, plain or percent-encoded once or more.

    The client resolves such a segment against the segments before it, and a server can resolve
    an encoded segment that the client sends. A link of a repository's file, such as
    `https://github.com/o/r/blob/main/../../../../user`, would then read another endpoint of
    GitHub's API with the server's GitHub access token, here `/user`, the account of that GitHub
    access token. A backslash also separates segments, because the URL Standard reads it that way
    in an http or https link."""
    path = urlsplit(url).path
    while (decoded := unquote(path)) != path:
        path = decoded
    return any(segment in (".", "..") for segment in re.split(r"[/\\]", path))


def _decoded(data: bytes) -> str | None:
    """The text of a file, or None for a binary file, which contains a null byte."""
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def issue(client, owner_repo: str, number: str) -> tuple[str | None, str]:
    """An issue or a pull request of `owner_repo` as text: its repository, number, title, state,
    author and body, and the comments of the repository's maintainers on the first page of its
    comments. The text names the repository, because a link can name an issue of another
    repository, and an author's association is with the issue's repository."""
    if not number.isdigit():
        return None, f"{number!r} is not the number of an issue or a pull request"
    base = f"https://api.github.com/repos/{owner_repo}"
    answer = client.get(f"{base}/issues/{number}")
    if answer.status_code != 200:
        return None, f"GitHub returned HTTP {answer.status_code} for issue {number}"
    body = answer.json()
    lines = [f"Issue #{number} of {owner_repo}: {body.get('title') or ''} "
             f"({body.get('state') or ''}), opened by "
             f"{(body.get('user') or {}).get('login') or ''} "
             f"({body.get('author_association') or ''}) on {body.get('created_at') or ''}",
             "", body.get("body") or ""]
    comments = client.get(f"{base}/issues/{number}/comments?per_page={ISSUE_COMMENTS}")
    if comments.status_code == 200:
        for comment in comments.json():
            if comment.get("author_association") in MAINTAINERS:
                lines += ["", f"Comment by {(comment.get('user') or {}).get('login') or ''} "
                              f"({comment.get('author_association')}) on "
                              f"{comment.get('created_at') or ''}:", comment.get("body") or ""]
    return "\n".join(lines)[:MAX_PAGE_BYTES], ""


def renamed_branch(client, base: str, ref: str) -> str | None:
    """The branch that the branch `ref` of the repository at the API address `base` was renamed
    to, or None when GitHub names no other branch for it.

    GitHub returns 404 for a file at a branch that was renamed, such as master of
    android/android-test, which is now main, and responds to `GET /branches/{ref}` with a redirect
    to the new branch. A client that follows the redirect receives the new branch with its name."""
    answer = client.get(f"{base}/branches/{ref}")
    if answer.status_code in (301, 302, 307, 308):
        location = urlsplit(answer.headers.get("Location") or "").path
        _, separator, branch = location.partition("/branches/")
        branch = unquote(branch).strip("/") if separator else ""
    elif answer.status_code == 200:
        body = answer.json()
        branch = str(body.get("name") or "") if isinstance(body, dict) else ""
    else:
        return None
    return branch if branch and branch != ref else None


def read_github(client, url: str) -> tuple[str | None, str]:
    """The text of the page that a github.com link names, read through GitHub's API, and a note.

    A repository is read as its README, a file as itself, a directory as its listing, releases as
    their list, and an issue or a pull request with its maintainers' comments. Other kinds are not
    offered (rule 4), and neither is a link with a `.` or `..` segment in its path
    (`_dot_segment`). A file or a directory at a branch that GitHub does not find is read once
    more at the branch that the branch was renamed to (`renamed_branch`). When GitHub fails to
    respond, the function raises `readmes.NoResponse`."""
    if _dot_segment(url):
        return None, "The tool does not read a link with a . or .. segment in its path"
    kind, owner_repo, ref, path = github_link(url)
    if kind not in GITHUB_KINDS:
        return None, (f"The tool does not read GitHub's {kind} pages. It reads repositories, "
                      f"files, directories, releases, issues and pull requests")
    try:
        checked_repo_name(owner_repo)
    except InvalidInput as error:
        return None, str(error)
    base = f"https://api.github.com/repos/{owner_repo}"
    at = f"?ref={ref}" if ref else ""
    if kind == "repository":
        answer = client.get(f"{base}/readme", accept=readmes.RAW)
        if answer.status_code != 200:
            return None, f"GitHub returned HTTP {answer.status_code} for the README"
        text = _decoded(answer.content[:MAX_PAGE_BYTES])
        return (text, "") if text is not None else (None, "the README is not text")
    if kind in ("issues", "pull"):
        return issue(client, owner_repo, ref)
    if kind == "releases":
        answer = client.get(f"{base}/releases?per_page=100")
        if answer.status_code != 200:
            return None, f"GitHub returned HTTP {answer.status_code} for the releases"
        return "\n".join(f"{r.get('tag_name')} {r.get('published_at')} {r.get('name') or ''}"
                         for r in answer.json()) or "No releases.", ""
    answer = client.get(f"{base}/contents/{path}{at}")
    if answer.status_code == 404 and ref not in ("", "HEAD") and not _REVISION.match(ref) and \
            (branch := renamed_branch(client, base, ref)) is not None:
        at = f"?ref={quote(branch, safe='/')}"
        answer = client.get(f"{base}/contents/{path}{at}")
    if answer.status_code != 200:
        return None, f"GitHub returned HTTP {answer.status_code} for {path or 'the root'}"
    body = answer.json()
    if isinstance(body, list):
        # A directory, also behind a `blob` link, which GitHub's web page redirects to the
        # listing.
        return "\n".join(f"{e.get('name')} ({e.get('type')}, {e.get('size', 0)} bytes)"
                         for e in body), ""
    if body.get("encoding") == "base64" and body.get("content"):
        data = base64.b64decode(body["content"])
    else:
        raw = client.get(f"{base}/contents/{path}{at}", accept=readmes.RAW)
        if raw.status_code != 200:
            return None, f"GitHub returned HTTP {raw.status_code} for {path}"
        data = raw.content
    text = _decoded(data[:MAX_PAGE_BYTES])
    return (text, "") if text is not None else (None, f"{path} is not text")


def page_repository(read_at: str) -> str:
    """The `owner/repo` of the repository on GitHub that the page at `read_at`, the address that
    the tool read after redirects, belongs to, or "" for a page off GitHub or of an account. A
    web link that redirects to github.com lands on a page of GitHub too."""
    link = github_link(read_at)
    return link[1] if link is not None else ""


def other_repository(client, owner_repo: str, repo_name: str) -> str:
    """The repository on GitHub that a page of `owner_repo` belongs to, as GitHub's API names it
    after the redirect of a renamed or transferred repository, when it is another repository than
    `repo_name`, or "" when it is the same. GitHub resolves hyperledger/firefly-evmconnect, the
    earlier name of hyperledger-firefly/evmconnect, to the repository itself, and the function
    compares the ids that GitHub returns for the two names. When GitHub returns no id for one of
    them, the function compares the names."""
    if owner_repo.lower() == repo_name.lower():
        return ""
    found = {}
    for name in (owner_repo, repo_name):
        answer = client.get(f"https://api.github.com/repos/{name}")
        body = answer.json() if answer.status_code == 200 else None
        if isinstance(body, dict) and body.get("id") is not None:
            found[name] = (body["id"], str(body.get("full_name") or name))
    if len(found) == 2:
        return "" if found[owner_repo][0] == found[repo_name][0] else found[owner_repo][1]
    return found.get(owner_repo, (None, owner_repo))[1]


# The label in the summary's last line, after which the summary model copies the page's own
# sentence that says what the project is or does (prompts/page-summary-prompt.md). The model can
# write it at the end of the summary's last sentence rather than on a line of its own.
PURPOSE_LINE = re.compile(r"purpose sentence[ \t*_]*:[ \t*_]*(.*?)[ \t*_]*$",
                          re.IGNORECASE | re.MULTILINE)
# What `_words_of` drops from a page and a sentence before it compares their words: the target
# of a Markdown link, an HTML tag and a web address, which a README writes beside the words of a
# sentence, and the soft hyphen, which a page writes inside a word.
_PAGE_MARKUP = re.compile(r"\]\([^)\s]*(?:\s+\"[^\"]*\")?\)|<[^<>\n]+>|https?://\S+")
# The fewest words of a purpose sentence that the server checks against the page, because a
# phrase of one or two words occurs in many pages without saying what the project is.
PURPOSE_MIN_WORDS = 3


def purpose_of(summary: str) -> tuple[str, str]:
    """The summary without its line of the purpose sentence, and the sentence that the line
    copies, empty when the line is missing or says that the page has none."""
    found = list(PURPOSE_LINE.finditer(summary or ""))
    if not found:
        return (summary or "").strip(), ""
    line = found[-1]
    sentence = line.group(1).strip().strip("\"'\u201c\u201d").strip()
    if sentence.lower().rstrip(".") in ("", "none", "n/a"):
        sentence = ""
    rest = summary[:line.start()].rstrip(" \t*_") + summary[line.end():]
    return rest.strip(), sentence


def _words_of(text: str) -> str:
    """The words of a text, lowercase, joined by single spaces, without the targets of links,
    HTML tags, web addresses and soft hyphens (`_PAGE_MARKUP`), so that markup, punctuation and
    line breaks do not decide whether a sentence is in a page. An underscore separates words,
    because html2text writes an emphasized word as `_word_`."""
    plain = _PAGE_MARKUP.sub(" ", (text or "").replace("\u00ad", ""))
    return " ".join(re.findall(r"[^\W_]+", plain.lower()))


def in_page(sentence: str, text: str) -> bool:
    """Whether the page's text contains the sentence word for word, with at least
    PURPOSE_MIN_WORDS words. The summary model writes the sentence, and the check keeps a
    sentence that the summary model changed or made up out of the result, because the
    classification counts a purpose quoted from it as stated (`1_classify_repos.quoted_purpose`)."""
    words = _words_of(sentence)
    return len(words.split()) >= PURPOSE_MIN_WORDS and f" {words} " in f" {_words_of(text)} "


class SummaryEnded(Exception):
    """The API's service ended the response of a summary (`ended_summary`). The server raises it
    as a failure of the summary (`SUMMARY_FAILED`)."""


def ended_summary(response) -> str:
    """Why the API's service ended the response of a summary, or "" when it did not: an error,
    the status "failed" or no list of output items. OpenRouter reports a failure after the status
    200 in a Response with the status "failed", an error and an empty output or none, in which
    the `error_type` states the reason (openrouter.ai/docs/api_reference/errors-and-debugging),
    and the `output_text` of such a Response is empty. Read as the summary, it would make an
    available page say nothing, and the cache would keep the label that the model decided from
    it."""
    error = getattr(response, "error", None)
    if error:
        kind = getattr(response, "error_type", None)
        return (f"the error {getattr(error, 'code', '') or 'without a code'} "
                f"({getattr(error, 'message', '') or error})"
                f"{f' with the error_type {kind}' if kind else ''}")
    if getattr(response, "status", None) == "failed":
        return "the status failed and no error"
    if not isinstance(getattr(response, "output", None), list):
        return f"a {type(response).__name__} without output items"
    return ""


class Summarizer:
    """Summaries of pages, made by a model with the prompt in prompts/page-summary-prompt.md:
    the prompt as the instructions, the page as the input (rule 10). The model is OpenAI's or
    one that OpenRouter serves on the endpoint of `endpoint` (`endpoints.py`).

    With `backup`, the backup provider of the model (`endpoints.backup_of`), a summary request
    that the API's service failed at the pinned provider after the SDK's retries
    (`service_failure`), or that the service ended (`ended_summary`), goes to the backup
    provider at once, and the backup provider serves every summary from then on until the
    pinned provider responds again to the request that tests it every
    `endpoints.PINNED_CHECK_SECONDS` (`endpoints.Providers`). A summary has no pause and no
    attempt of its own, and its failure would fail the tool call. A failure of the backup
    provider fails the call as before, and so does OpenRouter's refusal of a request because the
    requests in flight fill their budget (`endpoints.in_flight`), which belongs to the API key
    and not to the provider. The result names the provider that served the summary as
    its service tier, from which the classification prices the summary and records its
    provider."""

    def __init__(self, client=None, model: str = SUMMARY_MODEL, effort: str = SUMMARY_EFFORT,
                 credentials_path: Path | None = None, endpoint=None, backup=None,
                 clock=time.monotonic):
        self.client, self.effort = client, effort
        self.endpoint = endpoint or endpoints.endpoint(model)
        self.model = self.endpoint.model
        self.credentials_path = credentials_path or credentials.default_path()
        self.prompt = Template(SUMMARY_PROMPT.read_text(encoding="utf-8"))
        self.providers = endpoints.Providers(self.endpoint, backup, "the summary model", clock)

    def _client(self):
        if self.client is None:
            self.client = endpoints.client(self.endpoint, self.credentials_path, timeout=300,
                                           max_retries=6)
        return self.client

    async def __call__(self, repo_name: str, url: str, text: str) -> dict:
        # The request sends no output limit, unlike a request of the classifying model
        # (`endpoints.MAX_OUTPUT_TOKENS`), and the endpoint applies its own. A summary
        # is a short text at reasoning effort low, and a higher limit would only let a summary
        # that never ends cost more.
        cut = len(text) > MAX_SUMMARY_INPUT_CHARS
        arguments = {"model": self.model, "reasoning": {"effort": self.effort},
                     "instructions": self.prompt.safe_substitute(repo_name=repo_name),
                     "input": f'<page url="{url}">\n{text[:MAX_SUMMARY_INPUT_CHARS]}\n</page>'}
        providers, forced = self.providers, None
        while True:
            target, test = (forced, False) if forced is not None else providers.next()
            if target.openrouter:
                arguments["extra_body"] = target.routing()
            # A request that tests the pinned provider goes without the SDK's retries
            # (`endpoints.TEST_RETRIES`).
            client = (self._client().with_options(max_retries=endpoints.TEST_RETRIES) if test
                      else self._client())
            try:
                response = await client.responses.create(**arguments)
                ended = ended_summary(response)
                if ended:
                    raise SummaryEnded(f"the API ended the summary with {ended}")
            except Exception as error:  # noqa: BLE001 - raised again unless the backup takes it
                if (target != providers.pinned or providers.available is None
                        or endpoints.in_flight(error)
                        or not (isinstance(error, SummaryEnded) or service_failure(error))):
                    raise
                if not test:
                    providers.switch(f"failed a summary with {type(error).__name__} ({error})")
                forced = providers.available
                continue
            providers.responded(target)
            break
        usage = getattr(response, "usage", None)
        details = getattr(usage, "input_tokens_details", None)
        # The cached tokens, the tokens written to the prompt cache and the tier that served the
        # request decide what OpenAI bills for it, and the endpoint what OpenRouter bills. The
        # classification counts them for the run's cost record, and the model that reads the
        # summary is not shown them.
        return {"summary": response.output_text, "cut": cut,
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "model": getattr(response, "model", "") or "",
                "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
                "cache_write_tokens": getattr(details, "cache_write_tokens", 0) or 0,
                "service_tier": (target.provider if target.openrouter
                                 else getattr(response, "service_tier", "") or "")}


def build_server(github=None, session=None, summarizer=None,
                 credentials_path: Path | None = None) -> MCPServer:
    """The server with its four tools. GitHub, the web and the summarizer can be replaced.
    Without a client for GitHub, the server reads the GitHub access tokens of the environment
    and of the credentials file at `credentials_path`, or else at `credentials.default_path()`."""
    credentials_path = credentials_path or credentials.default_path()
    # The server logs at WARNING, because its stderr is the classification's log, which contains
    # only lines in the format of agentcontext.logs. At INFO, the SDK would log every tool error
    # and the OpenAI client every request.
    server = MCPServer(name="repository-classification-context",
                       title="Repository classification context", version="2.1.0",
                       instructions=INSTRUCTIONS, log_level="WARNING")
    summarize = summarizer or Summarizer()
    # The descriptions of the repositories' owners for the run that the server serves, one
    # request per owner (`owner_description`).
    owners: dict = {}

    async def from_github(function, *arguments):
        """`function` with the client for GitHub's API and `arguments`, run in a thread. A failure
        that is no evidence about the repository is raised as a failure for the classification:
        GitHub refuses every GitHub access token, returns no usable response, or returns a
        response that is not the JSON that it documents."""
        try:
            return await asyncio.to_thread(function, github or github_client(credentials_path),
                                           *arguments)
        except NoTokens as error:
            raise failure(NO_GITHUB_ACCESS_TOKEN, str(error)) from error
        except (readmes.NoResponse, ValueError, KeyError) as error:
            raise failure(GITHUB_FAILED, f"{type(error).__name__} ({error})") from error

    @server.tool(name="get_file_tree", title="Repository outline",
                 description=FILE_TREE_DESCRIPTION, annotations=READ_ONLY)
    async def get_file_tree(repo_name: str, revision: str, max_chars: int = 0) -> FileTreeResult:
        """The outline of the repository's files at the commit, in at most `max_chars`
        characters if that is not 0."""
        try:
            name = checked_repo_name(repo_name)
            if revision and not _REVISION.match(revision):
                raise InvalidInput(f"revision must be a commit id of 40 hex digits or empty: "
                                   f"{revision!r}")
            if max_chars < 0:
                raise InvalidInput(f"max_chars must be 0 or more: {max_chars}")
        except InvalidInput as error:
            # The SDK passes a ToolError's text to the model and reports any other exception as
            # a crash without the text.
            raise ToolError(str(error)) from error
        return await from_github(file_tree, name, revision, max_chars)

    @server.tool(name="read_linked_page", title="Linked page",
                 description=LINKED_PAGE_DESCRIPTION, annotations=READ_ONLY)
    async def read_linked_page(url: str, repo_name: str) -> LinkedPageResult:
        """A summary of one page that the description, the homepage or the README links to."""
        if github_link(url) is not None:
            text, note = await from_github(read_github, url)
            read_at = url
        else:
            text, read_at, note = await asyncio.to_thread(read_web, url, session)
            if text is None and unreachable(note) and await asyncio.to_thread(network_down):
                # A failure of this machine's network is no evidence about the page, and the
                # classification makes the call again after a wait (`NETWORK_FAILED`). Passed to
                # the model, the failure would let a label be decided and cached without the page.
                raise failure(NETWORK_FAILED, f"could not read {read_at} ({note}), and this "
                                              f"machine could not connect to {NETWORK_PROBE[0]} "
                                              f"either")
        if text is None:
            return LinkedPageResult(url=read_at, available=False, note=note)
        # A page of GitHub names its repository also when a web link redirects to it, as
        # git.openjdk.org/jdk/blob/master/doc/building.md redirects to github.com/openjdk/jdk.
        other = ""
        if owner_repo := page_repository(read_at):
            other = await from_github(other_repository, owner_repo, repo_name)
        try:
            made = await summarize(repo_name, read_at, text)
        except Exception as error:  # noqa: BLE001 - raised for the classification
            # A failure of the summary model is no evidence about the page. The classification
            # stops on a model that the API refuses, because that model would fail every page.
            # After any other failure, the classification makes the call again, and it fails the
            # repository when the failure lasts.
            refused = refusal(error)
            if refused:
                raise failure(SUMMARY_REFUSED, f"{refused} ({error})") from error
            spent = endpoints.credit_refusal(error)
            if spent is not None:
                # A top-up ends the refusal, and the classification waits for it before it makes
                # the call again.
                raise failure(SUMMARY_NO_CREDIT, f"{spent.reason} ({error})") from error
            block = endpoints.blocked(error)
            if block:
                # OpenRouter blocks the page's content in every attempt, as the API refuses a
                # page past the context window below.
                return LinkedPageResult(url=read_at, available=False,
                                        note=f"the summary model's service blocked the page "
                                             f"({block})")
            import openai
            code = getattr(error, "code", None)
            if isinstance(error, openai.BadRequestError) and code not in TRANSIENT_REFUSALS:
                # The API refuses the request because of the page that it contains, such as a
                # page past the summary model's context window, and refuses it the same way in
                # every attempt. Raised as a failure of the summary, the refusal would fail every
                # call of the tool and every retry round, each after the classifying model's paid
                # requests.
                return LinkedPageResult(url=read_at, available=False,
                                        note=f"the summary model refused the page "
                                             f"({code or 'HTTP 400'})")
            raise failure(SUMMARY_FAILED, f"summarizing {read_at} failed with "
                                          f"{type(error).__name__} ({error})") from error
        summary, sentence = purpose_of(made["summary"])
        dropped = bool(sentence) and not in_page(sentence, text)
        notes = [n for n in (note, "the page was cut before the summary" if made["cut"] else "",
                             "the summary's purpose sentence is not in the page, and the result "
                             "leaves it out" if dropped else "") if n]
        return LinkedPageResult(url=read_at, available=True, summary=summary,
                                purpose_sentence="" if dropped else sentence,
                                other_repository=other,
                                note=", and ".join(notes), input_tokens=made["input_tokens"],
                                output_tokens=made["output_tokens"], model=made["model"],
                                cached_tokens=made.get("cached_tokens", 0),
                                cache_write_tokens=made.get("cache_write_tokens", 0),
                                service_tier=made.get("service_tier", ""))

    @server.tool(name="get_repository_metadata", title="Repository metadata",
                 description=METADATA_DESCRIPTION, annotations=READ_ONLY)
    async def get_repository_metadata(repo_name: str) -> RepositoryMetadataResult:
        """What GitHub records about the repository."""
        try:
            name = checked_repo_name(repo_name)
        except InvalidInput as error:
            raise ToolError(str(error)) from error
        return await from_github(repository_metadata, name, owners)

    @server.tool(name="get_issues_and_pull_requests", title="Issues and pull requests",
                 description=ACTIVITY_DESCRIPTION, annotations=READ_ONLY)
    async def get_issues_and_pull_requests(repo_name: str) -> ActivityResult:
        """The activity of the repository's issues and pull requests."""
        try:
            name = checked_repo_name(repo_name)
        except InvalidInput as error:
            raise ToolError(str(error)) from error
        return await from_github(activity, name)

    return server


# How often the server checks that the process that started it is still there.
PARENT_CHECK_SECONDS = 1.0


def exit_with_parent(parent: int, interval: float = PARENT_CHECK_SECONDS) -> threading.Thread:
    """Start a daemon thread that ends this process once `parent`, the process that started it,
    is gone.

    The MCP SDK starts the server in a session of its own, which the stop that the runner
    forwards to the classification does not reach. Without this thread, a classification that a
    stop ends would leave the server running until its tool calls end, which can take hours when a
    call waits for GitHub's rate limit. The server would also hold the runner's pipe of the step's
    output open, the runner would wait for the pipe, and the Stop of the container would end in
    SIGKILL without a handover."""
    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(interval)
        os._exit(1)

    thread = threading.Thread(target=watch, name="exit-with-parent", daemon=True)
    thread.start()
    return thread


def main(argv: list | None = None) -> int:
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(description="Serve the classification tools over stdio.",
                                     allow_abbrev=False)
    parser.add_argument("--summary-model", default=SUMMARY_MODEL,
                        help="The model that summarizes pages (default: %(default)s)")
    parser.add_argument("--summary-effort", default=SUMMARY_EFFORT,
                        help="The reasoning effort of the summary model (default: %(default)s)")
    parser.add_argument("--summary-provider", default="",
                        help="The provider slug of the endpoint of an OpenRouter summary model, "
                             "such as morph/fp8")
    parser.add_argument("--summary-quantization", default="",
                        help="The quantization of that endpoint, such as fp8")
    parser.add_argument("--summary-hugging-face-id", default="",
                        help="The Hugging Face id of the weights of an OpenRouter summary model")
    parser.add_argument("--summary-backup-provider", default="",
                        help="The provider slug of the backup provider of an OpenRouter summary "
                             "model, which serves the same weights in the same quantization "
                             "while the pinned provider fails, such as deepinfra/fp8 (default: "
                             "none)")
    parser.add_argument("--credentials", type=Path, default=credentials.default_path(),
                        help="The credentials file, read after the environment (default: "
                             "%(default)s)")
    args = parser.parse_args(argv)
    try:
        summary = endpoints.endpoint(args.summary_model, args.summary_provider,
                                     args.summary_quantization, args.summary_hugging_face_id)
        backup = (endpoints.endpoint(summary.model, args.summary_backup_provider,
                                     summary.quantization, summary.hugging_face_id)
                  if args.summary_backup_provider else None)
    except ValueError as error:
        parser.error(str(error))
    exit_with_parent(os.getppid())
    # Nothing but MCP messages may go to stdout. The log goes to stderr, which the bridge passes
    # to the classification's stderr. Because the server configures the root logger as it is
    # built, the libraries' logging is taken over after the server is built.
    server = build_server(summarizer=Summarizer(effort=args.summary_effort,
                                                credentials_path=args.credentials,
                                                endpoint=summary, backup=backup),
                          credentials_path=args.credentials)
    logs.capture_libraries()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
