"""
Tests for 1_classify_repos.py

Strategy:
  - The model is a fake OpenAI client. Its `responses.create` computes the response from the
    request, and a case specifies what the model does, such as respond, call a tool or respond
    for another repository. The client records every request with its arguments.
  - The tools are a fake bridge that records each call and returns the server's result for a
    page, with the tokens of its summary. mcp_bridge and classification_tools have suites of
    their own.
  - GitHub is a fake session behind the real `readmes.Client`. The session returns each README,
    and 304 for a request that sends the ETag that it last returned.
  - Every run writes into a temporary cache of labels and a temporary output directory.
  - That an output is on disk before its rename is tested with the real `os.fsync` and
    `os.replace` recording each call (`_synced_writes`).

Requirements: Python 3.14, openai, requests (requirements.txt)
Run with:  python3 -m unittest test_classify_repos -v
"""

import argparse
import asyncio
import base64
import collections
import contextlib
import csv
import dataclasses
import importlib
import importlib.util
import io
import json
import os
import random
import re
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
classify_repos = importlib.import_module("1_classify_repos")
import classification_tools as tools  # noqa: E402
import forks  # noqa: E402
import label_cache  # noqa: E402
import mcp_bridge  # noqa: E402
import readmes  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from agentcontext.credentials import Credential  # noqa: E402
from agentcontext.github_api import TokenPool  # noqa: E402
from agentcontext import runner  # noqa: E402

REVISION, LATER = "a" * 40, "c" * 40
UNDECIDED_CEILING = classify_repos.undecided_ceiling
THRESHOLDS = forks.Thresholds(author_minimum=2, window_start="2026-03-22")
ENGLISH = ("# Tool\n\nA command line tool that formats configuration files. Install it with pip "
           "and run it on a directory. See [the guide](docs/guide.md) and "
           "https://tool.example/docs for the options, and CONTRIBUTING.md for development.")
# German without umlauts, in ASCII throughout. The language check takes such a text for English.
GERMAN = ("# Werkzeug\n\nEin Werkzeug, das Konfigurationsdateien formatiert. Installiere es mit pip "
          "und rufe es auf einem Verzeichnis auf. Die Dokumentation beschreibt alle Optionen, und "
          "die Anleitung fuer Beitraege steht in CONTRIBUTING.md.")
RUSSIAN = ("# Инструмент\n\nБыстрый анализатор конфигурационных файлов. Установите его через pip "
           "и вызовите функцию для файла. Подробности смотрите в документации проекта.")


def _message(text):
    return SimpleNamespace(type="message", content=[SimpleNamespace(text=text)])


def _call(name, arguments, call_id="call-1"):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments),
                           call_id=call_id)


# The facts that make each criterion with an exception apply. Any other criterion applies when
# its `applies` field is true.
APPLYING = {"mirror": {"described_as_mirror": "true"},
            "example_or_template": {"example_or_template": "true"},
            "research_artifact": {"dataset": "true"},
            "no_source_code": {"contains_no_source_code": "true"},
            "work_in_progress": {"unfinished": "true"},
            "personal_or_single_script": {"toy_project": "true"}}
USE = "maintenance_and_use"


def _checklist(applies=(), facts=None):
    """The model's checklist. Every fact is false except the facts that make the criteria in
    `applies` apply and the facts that `facts` sets."""
    checklist = {}
    for key, names in classify_repos.CRITERION_FACTS.items():
        given = {name: "false" for name in names}
        if key in applies:
            given.update(APPLYING.get(key, {"applies": "true"}))
        given.update((facts or {}).get(key, {}))
        checklist[key] = {**given, "evidence": f"The README on {key}."}
    return checklist


def _answer(name, label="true", justification="It is software.", words=None, applies=(),
            fork_of="", purpose="It formats configuration files.", facts=None,
            purpose_quote="A command line tool that formats configuration files."):
    """The model's response, its call of the checklist tool. `words` is the count of the
    README's words by language, all English by default. `applies` names the exclusion criteria
    that apply. With the label "false" and an empty `applies`, `website` applies. `facts` sets
    single fields, and under USE the fields of `maintenance_and_use`, which are false
    otherwise."""
    if words is None:
        words = [{"language": "English", "words": 40}]
    applies = set(applies) or ({"website"} if label == "false" else set())
    exclusions = _checklist(applies, facts)
    use = {"maintained": "false", "used_by_others": "false", **(facts or {}).get(USE, {}),
           "evidence": "The README on maintenance and use."}
    return [_call(classify_repos.CHECKLIST_TOOL_NAME,
                  {"repo_name": name, "readme_words_by_language": words, USE: use,
                   "exclusions": exclusions, "fork_of": fork_of, "purpose_quote": purpose_quote,
                   "purpose": purpose, "engineering": "unclear" if label == "unclear" else "true",
                   "justification": justification}, call_id="call-checklist")]


class _Model:
    """A fake OpenAI client. `script(request, number)` returns the response's output items,
    or raises."""

    def __init__(self, script):
        self.script, self.requests = script, []
        self.responses = self

    def with_options(self, **options):
        """The client with other options of the SDK, such as its retries, which the fake does
        not make."""
        return self

    async def create(self, **request):
        self.requests.append(request)
        output = self.script(request, len(self.requests))
        # The yield lets the repositories that are decided at once interleave, as they do against
        # the API, where a request takes time.
        await asyncio.sleep(0)
        _REPOSITORY_OF_RESPONSE[f"resp-{len(self.requests)}"] = _repository_of(request)
        return SimpleNamespace(
            id=f"resp-{len(self.requests)}", model="gpt-6-luna-2026-09-30", output=output,
            usage=SimpleNamespace(input_tokens=100, output_tokens=10,
                                  input_tokens_details=SimpleNamespace(cached_tokens=5),
                                  output_tokens_details=SimpleNamespace(reasoning_tokens=3)))


class _Billed(_Model):
    """The fake model, with responses that report `tiers(number)` as the service tier that
    served the request, and `cache_write_tokens` of their input tokens as written to the prompt
    cache."""

    def __init__(self, script, tiers, cache_write_tokens=40):
        super().__init__(script)
        self.tiers, self.cache_write_tokens = tiers, cache_write_tokens

    async def create(self, **request):
        response = await super().create(**request)
        response.service_tier = self.tiers(len(self.requests))
        response.usage.input_tokens_details.cache_write_tokens = self.cache_write_tokens
        return response


# The repository of each fake response. A request that continues a conversation names the
# previous response rather than the repository.
_REPOSITORY_OF_RESPONSE = {}


def _repository_of(request):
    """The repository of a request: named in its input, in the first item of the conversation
    that a request through OpenRouter sends, or by the previous response."""
    given = request["input"]
    if isinstance(given, list) and given and isinstance(given[0], dict) and given[0].get(
            "role") == "user":
        given = given[0]["content"]
    if isinstance(given, str):
        return given.split("<repository>")[1].split("</repository>")[0]
    return _REPOSITORY_OF_RESPONSE[request["previous_response_id"]]


# The endpoint of an OpenRouter model, as the command line of a run names it.
DEEPSEEK = {"model": "deepseek/deepseek-v4.1-flash", "provider": "morph/fp8",
            "quantization": "fp8", "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash"}
# Two entries of OpenRouter's endpoint list of Kimi K3 of 2026-10-30, with the fields that the
# run reads (GET /api/v1/models/moonshotai/kimi-k3/endpoints).
KIMI_ENDPOINTS = {"data": {"id": "moonshotai/kimi-k3", "endpoints": [
    {"tag": "chutes/mxfp4", "quantization": "mxfp4",
     "supported_parameters": ["frequency_penalty", "include_reasoning", "max_tokens",
                              "presence_penalty", "reasoning", "reasoning_effort",
                              "repetition_penalty", "response_format", "seed", "stop",
                              "structured_outputs", "temperature", "top_k", "top_p"]},
    {"tag": "wafer/us", "quantization": "mxfp4",
     "supported_parameters": ["frequency_penalty", "include_reasoning", "logit_bias", "max_tokens",
                              "min_p", "presence_penalty", "reasoning", "reasoning_effort",
                              "repetition_penalty", "response_format", "seed", "stop",
                              "structured_outputs", "temperature", "tool_choice", "tools",
                              "top_k", "top_p"]}]}}
# The entries of OpenRouter's model list with the reasoning efforts that each model takes, as the
# list of 2026-11-02 gives them for z-ai/glm-5.3 (GET /api/v1/models).
OPENROUTER_MODELS = {"data": [
    {"id": "moonshotai/kimi-k3", "reasoning": {"supported_efforts": ["high", "low"]}},
    {"id": "z-ai/glm-5.3", "reasoning": {"supported_efforts": ["max", "high", "low"],
                                         "default_effort": "max"}}]}
# The endpoint list of GLM-5.3 with its pinned endpoint, which serves every parameter of the
# requests.
GLM_ENDPOINTS = {"data": {"id": "z-ai/glm-5.3", "endpoints": [
    {"tag": "morph/fp8", "quantization": "fp8", "max_completion_tokens": 943_718,
     "supported_parameters": ["max_tokens", "reasoning", "seed", "temperature", "tools",
                              "top_p"]}]}}


def _openrouter_lists(url, endpoint_lists=None, models=None) -> str:
    """The body of OpenRouter's response to a GET of the model list or of the endpoint list of a
    model: `models`, OPENROUTER_MODELS by default, or the endpoint list of the model in
    `endpoint_lists`, by model id, else KIMI_ENDPOINTS for Kimi K3 and GLM_ENDPOINTS for
    GLM-5.3."""
    if url == "https://openrouter.ai/api/v1/models":
        return json.dumps(models or OPENROUTER_MODELS)
    lists = {"moonshotai/kimi-k3": KIMI_ENDPOINTS, "z-ai/glm-5.3": GLM_ENDPOINTS,
             **(endpoint_lists or {})}
    return json.dumps(next(body for model, body in lists.items() if f"/{model}/" in url))


def _openrouter_error(kind, status, metadata=None):
    """The SDK's error for an error response of OpenRouter, with the HTTP status as `code`."""
    response = SimpleNamespace(request=SimpleNamespace(), status_code=status, headers={})
    return kind("refused", response=response,
                body={"code": status, "message": "Insufficient credits", "metadata": metadata})


class _KeyCredit:
    """The credit of an OpenRouter API key: `remaining()` returns the next of `credits`, and the
    last one from then on, and records the status of the step that the run states at each
    check."""

    def __init__(self, *credits, status=None):
        self.credits, self.status, self.seen = list(credits), status, []

    def remaining(self):
        if self.status is not None:
            self.seen.append(json.loads(self.status.read_text()) if self.status.exists()
                             else None)
        return self.credits.pop(0) if len(self.credits) > 1 else self.credits[0]


def _always_true(request, number):
    return _answer(_repository_of(request))


class _Together(_Model):
    """The fake model. The first request of each repository in `names` waits until each of these
    repositories has sent its first request, as requests in flight together reach a service that
    fails. Without the wait, one repository could use every attempt before the other's README is
    read."""

    def __init__(self, script, names):
        super().__init__(script)
        self.names, self.arrived = set(names), set()

    async def create(self, **request):
        self.arrived.add(_repository_of(request))
        for _ in range(5000):
            if self.arrived >= self.names:
                break
            await asyncio.sleep(0.001)
        return await super().create(**request)


@contextlib.contextmanager
def _short_pauses(**limits):
    """Pauses of milliseconds. The wait before a repository's next attempt is long enough for
    the other repository in flight to send its request, as the seconds between real attempts
    are. `limits` sets further constants of the pause."""
    values = {"PAUSE_FIRST_SECONDS": 0.001, "PAUSE_LONGEST_SECONDS": 0.004,
              "RETRY_SECONDS": 0.02, **limits}
    with contextlib.ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(mock.patch.object(classify_repos, name, value))
        yield


def _server_error(status=500):
    """The error of the SDK for a server error, after its own retries."""
    response = SimpleNamespace(request=SimpleNamespace(), status_code=status, headers={})
    return openai.InternalServerError("Server error", response=response, body=None)


class _Bridge:
    """The tools, as the classification calls them through the bridge. `billing` adds to the
    result of a page what the server reports about the prompt cache and the service tier of the
    summary's request."""

    def __init__(self, billing=None):
        self.calls = []
        self.billing = billing or {}
        self.function_tools = [
            {"type": "function", "name": "get_file_tree", "description": "tree", "strict": True,
             "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "read_linked_page", "description": "page",
             "strict": True, "parameters": {"type": "object", "properties": {}}}]
        self.output_schemas = {name: {"type": "object",
                                      "properties": {"available": {"type": "boolean"}}}
                               for name in classify_repos.SUPPLIED}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "read_linked_page":
            return json.dumps({"url": arguments["url"], "available": True,
                               "summary": f"A summary of {arguments['url']}.", "note": "",
                               "input_tokens": 120, "output_tokens": 30,
                               "model": "gpt-6-luna-2026-09-30", **self.billing}), False
        return json.dumps({"repo_name": arguments.get("repo_name"), "available": True,
                           "file_tree": "Files: 3", "note": ""}), False


class _Response:

    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        return self.body


class _GitHub:
    """A fake GitHub. It returns READMEs by repository and revision, and 304 for a request that
    sends the current ETag. A repository with a README exists, with the description that
    `descriptions` gives it, or none by default. `/rate_limit` returns HTTP 200."""

    def __init__(self, readmes_by_name, repositories=None, descriptions=None, merges=None):
        self.readmes, self.repositories = readmes_by_name, repositories or {}
        self.descriptions, self.merges = descriptions or {}, merges or {}
        self.sent = []

    def post(self, url, headers=None, json=None, timeout=None):
        """GitHub's GraphQL API, returning the merges of a mirror from `merges`: a time at which
        a person's pull request was merged, or a pair of such a time and the type of the author,
        such as "Bot" for a GitHub App."""
        variables = json["variables"]
        name = f"{variables['owner']}/{variables['name']}"
        self.sent.append(("graphql", name))
        nodes = [{"mergedAt": merged, "author": {"__typename": author}}
                 for merged, author in ((m, "User") if isinstance(m, str) else m
                                        for m in self.merges.get(name, []))]
        return _Response(200, {"data": {"repository": {"pullRequests": {"nodes": nodes}}}})

    def get(self, url, headers=None, timeout=None):
        self.sent.append(url)
        if url == readmes.RATE_LIMIT_URL:
            # The request that the pause for GitHub sends, which depends on no repository.
            return _Response(200, {"resources": {"core": {"limit": 5000, "remaining": 4999}}})
        name = url.split("/repos/")[1].split("/readme")[0] if "/readme" in url else None
        if name is None:
            name = url.split("/repos/")[1]
            repository = self.repositories.get(name)
            if repository is None and name in self.readmes:
                repository = {"id": 1000, "full_name": name, "default_branch": "main",
                              "created_at": "2020-01-01T00:00:00Z",
                              "description": self.descriptions.get(name)}
            return _Response(200, repository) if repository else _Response(404, {})
        if name not in self.readmes:
            return _Response(404, {})
        text, sha = self.readmes[name]
        etag = f'W/"{sha}"'
        if headers.get("If-None-Match") == etag:
            return _Response(304, headers={"ETag": etag})
        return _Response(200, {"path": "README.md", "sha": sha, "size": len(text.encode()),
                               "encoding": "base64",
                               "content": base64.b64encode(text.encode()).decode(),
                               "url": f"https://api.github.com/repos/{name}/contents/README.md"},
                         headers={"ETag": etag})


class _Clock:
    """A fake clock, which the run's waits advance instead of waiting."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class _Outage(_GitHub):
    """GitHub that returns HTTP 502 to every request from its `before`-th request on, while
    `clock` shows less than `until`."""

    def __init__(self, readmes_by_name, clock, until, before=0):
        super().__init__(readmes_by_name)
        self.clock, self.until, self.before = clock, until, before
        self.served, self.failed = 0, 0

    def get(self, url, headers=None, timeout=None):
        if self.served >= self.before and self.clock() < self.until:
            self.failed += 1
            return _Response(502, {})
        self.served += 1
        return super().get(url, headers, timeout)


class _ReadmeOutage(_Outage):
    """GitHub that returns HTTP 502 to every README request from its `before`-th README request
    on, while `clock` shows less than `until`. It responds to every other request, `/rate_limit`
    among them, as during an outage of GitHub's storage of repositories."""

    def get(self, url, headers=None, timeout=None):
        if "/readme" not in url:
            return _GitHub.get(self, url, headers, timeout)
        return super().get(url, headers, timeout)


class _GoneRevision(_GitHub):
    """GitHub with the history of the fork o/a and its upstream up/a, which has no commits in
    the sampling window. With `gone`, GitHub no longer has the commit REVISION, as after a force
    push. It then returns 404 for the README and the commits at REVISION, and returns the README
    and the commits of the default branch."""

    COMMITS = [{"sha": f"{n:040x}", "author": {"login": login, "type": "User"},
                "commit": {"author": {"name": login, "email": f"{login}@example.org"}}}
               for n, login in enumerate(("ana", "bo"))]

    def __init__(self, names=("o/a",)):
        super().__init__({name: (ENGLISH.replace("files", f"files of {name}"), name)
                          for name in names}, {
            **{name: {"id": number, "full_name": name, "default_branch": "main",
                      "created_at": "2020-01-01T00:00:00Z", "description": None}
               for number, name in enumerate(names, 1)},
            "up/a": {"id": 99, "full_name": "up/a", "default_branch": "main",
                     "created_at": "2015-01-01T00:00:00Z"}})
        self.gone = False

    def get(self, url, headers=None, timeout=None):
        if self.gone and REVISION in url:
            self.sent.append(url)
            return _Response(404, {"message": f"No commit found for the ref {REVISION}"})
        if "/commits" not in url:
            return super().get(url, headers, timeout)
        self.sent.append(url)
        if url.startswith("https://api.github.com/repos/up/a/"):
            return _Response(200, [])
        if url.endswith("per_page=1"):
            # One commit per page, so that GitHub's link to the last page gives the number of
            # commits in the window.
            return _Response(200, self.COMMITS[:1],
                             headers={"Link": f'<{url}&page=2>; rel="next", '
                                              f'<{url}&page=400>; rel="last"'})
        return _Response(200, self.COMMITS)


def _synced_writes(write, directory: Path) -> list:
    """Run `write` with the real `os.fsync` and `os.replace` recording each call, and return the
    calls in order: `("file", inode)` for a synced file, `("directory", True)` for a sync of
    `directory`, and `("rename", inode, name)` for the rename of the file with that inode to
    `name`."""
    entry = os.stat(directory)
    calls = []
    sync, rename = os.fsync, os.replace

    def recorded_sync(handle):
        status = os.fstat(handle)
        if stat.S_ISDIR(status.st_mode):
            calls.append(("directory", (status.st_dev, status.st_ino)
                          == (entry.st_dev, entry.st_ino)))
        else:
            calls.append(("file", status.st_ino))
        sync(handle)

    def recorded_rename(source, destination):
        calls.append(("rename", os.stat(source).st_ino, Path(destination).name))
        rename(source, destination)

    with mock.patch("os.fsync", recorded_sync), mock.patch("os.replace", recorded_rename):
        write()
    return calls


def _edited_script(case, old, new, module=None):
    """A copy of the script, or of `module`, with `old` replaced by `new`, loaded as a module of
    its own from a directory that `case` removes at its end, as the script would be loaded after
    that edit."""
    original = Path((module or classify_repos).__file__)
    source = original.read_text(encoding="utf-8")
    case.assertEqual(source.count(old), 1, old)
    directory = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = directory / original.name
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"edited_{directory.name}", path)
    module = importlib.util.module_from_spec(spec)
    # The copy puts its own directory first on the path, and the case removes that directory.
    with mock.patch.object(sys, "path", list(sys.path)):
        spec.loader.exec_module(module)
    return module


def _repositories(*names, revision=REVISION, descriptions=None):
    return [{"id": number, "name": name, "default_branch": "main", "revision": revision,
             "is_mirror": False,
             "description": (descriptions or {}).get(name, "")}
            for number, name in enumerate(names, 1)]


def _independent(*names, **kwargs) -> list:
    """`_repositories` with a head commit of its own for each repository after the first, as
    repositories that do not copy each other have. Repositories labeled `true` at one head commit
    are copies, of which the outputs keep one."""
    repositories = _repositories(*names, **kwargs)
    for number, repository in enumerate(repositories[1:], 2):
        repository["revision"] = repository["revision"][:-3] + f"{number:03d}"
    return repositories


class _RunCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache, self.out = self.tmp / "labels", self.tmp / "out"
        for name, value in (("RETRY_SECONDS", 0), ("RETRY_ROUND_SECONDS", 0),
                            ("TOOL_WAITS", (0,) * len(classify_repos.TOOL_WAITS))):
            patcher = mock.patch.object(classify_repos, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # With a ceiling of 0, a run here ends incomplete over a repository left without a
        # decision, and the cases of a stopped run have a run to resume. The cases of the
        # ceiling call `real_ceiling`.
        self.with_ceiling(lambda repositories: 0)
        # The collection stage of the case, where no collection has begun unless the case
        # writes one, rather than the checkout's.
        self.collection = self.tmp / "collection"
        self.collection.mkdir()
        patcher = mock.patch.object(classify_repos, "COLLECTION_DIR", self.collection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def with_ceiling(self, ceiling):
        patcher = mock.patch.object(classify_repos, "undecided_ceiling", ceiling)
        patcher.start()
        self.addCleanup(patcher.stop)

    def real_ceiling(self):
        """The ceiling of the script, under which a run finishes without the repositories that it
        left without a decision."""
        self.with_ceiling(UNDECIDED_CEILING)

    def run_over(self, repositories, github, client, bridge=None, **options):
        # The script's own defaults, with which a test observes what a run without an option does.
        # A run here labels without the second model unless a case gives `no_second_model=False`
        # and the second model's client as `second_client`, and without a backup provider unless
        # a case gives `no_backup_provider=False`.
        args = classify_repos.build_parser().parse_args([])
        for key, value in dict(cache_dir=self.cache, model="gpt-6-luna", reasoning_effort="low",
                               workers=2, summary_model="gpt-6-luna", summary_effort="low",
                               output_dir=self.out, no_second_model=True,
                               no_backup_provider=True).items():
            setattr(args, key, value)
        for key, value in options.items():
            if key not in ("thresholds", "retrieval", "clock", "key_credit", "second_client"):
                setattr(args, key, value)
        bridge = bridge or _Bridge()
        pool = TokenPool([Credential("github", "a", "t")])
        github_client = readmes.Client(pool, session=github, sleep=lambda s: None)
        clock = options.get("clock")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = asyncio.run(classify_repos.classify(args, repositories,
                                                       options.get("retrieval",
                                                                   "2026-10-05T101430Z"),
                                                       options.get("thresholds", THRESHOLDS),
                                                       github_client,
                                                       client, bridge_for=lambda a: bridge,
                                                       sleep=clock and clock.sleep,
                                                       clock=clock,
                                                       key_credit=options.get("key_credit"),
                                                       second_client=options.get(
                                                           "second_client")))
        return code, err.getvalue()

    def rows(self, name):
        with open(self.out / name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def index(self):
        return label_cache.read_index(self.cache)


class TestClassifier(_RunCase):

    def test_a_response_that_is_no_response_is_a_failed_attempt_of_the_model(self):
        """For a response with status 200 and another content type, such as a page of the
        API's edge, the SDK returns the text, for a response that is not JSON it raises a
        JSONDecodeError, and for JSON without output items or an error it returns a Response
        without them. Read as the model's response, the text or such a Response would stop the
        run. Counted as GitHub's failure, the error would pause every worker. The log names what
        the API returned."""
        for name, first, error in (
                ("text", lambda: "<html>Bad gateway</html>",
                 "the API's response is a str without output items or an error"),
                ("not JSON", lambda: (_ for _ in ()).throw(
                    json.JSONDecodeError("Expecting value", "<html>", 0)),
                 "the API's response is not a Response (Expecting value: line 1 column 1 "
                 "(char 0))"),
                ("JSON without output items or an error",
                 lambda: _parsed({"id": "resp_1", "object": "response"}),
                 "the API's response is a Response without output items or an error")):
            with self.subTest(response=name):
                self.setUp()
                class Edge(_Model):
                    async def create(self, **request):
                        if not self.requests:
                            self.requests.append(request)
                            return first()
                        return await super().create(**request)

                with _short_pauses():
                    code, err = self.run_over(_repositories("o/a"),
                                              _GitHub({"o/a": (ENGLISH, "s1")}),
                                              Edge(_always_true))
                self.assertEqual(code, 0, err)
                self.assertIn(f"INFO repository_labeling: Attempt 1 of 3 at o/a by gpt-6-luna "
                              f"failed ({error}).\n", err)
                # The next attempt of the model, not GitHub's retry of the repository.
                self.assertNotIn("Retrying it later", err)

    def test_the_instructions_go_with_every_request(self):
        """The Responses API does not keep the instructions of a previous response. Without
        them in every request, a label decided after a tool call would be made without the
        classification's criteria."""
        def script(request, number):
            return [_call("get_file_tree", {"repo_name": "o/a", "revision": ""})] \
                if number == 1 else _answer("o/a")
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[1]["previous_response_id"], "resp-1")
        for request in model.requests:
            self.assertIn("hosts an engineered software project", request["instructions"])

    def test_every_repository_gets_the_same_instructions(self):
        """The name of the repository is in the input, so that the first request of every
        repository has the same instructions, and the API can read them from its prompt cache
        after a request about another repository."""
        model = _Model(lambda request, number: _answer(_repository_of(request)))
        code, err = self.run_over(_repositories("o/a", "o/b"),
                                  _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")}),
                                  model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len({request["instructions"] for request in model.requests}), 1)
        self.assertEqual(sorted(request["input"].split("\n")[0] for request in model.requests),
                         ["<repository>o/a</repository>", "<repository>o/b</repository>"])

    def test_the_tools_read_the_credentials_file_of_the_classification(self):
        """A credentials file outside the checkout reaches the MCP server of the tools, which
        otherwise reads the file at the root of its own code."""
        args = classify_repos.build_parser().parse_args(
            ["--credentials", "/elsewhere/credentials.csv"])
        arguments = classify_repos.open_bridge(args).arguments
        self.assertEqual(arguments[arguments.index("--credentials") + 1],
                         "/elsewhere/credentials.csv")

    def test_the_instructions_state_both_limits(self):
        """Rules 13 and 14: the model learns the limits before its first tool call, so that it
        can choose which linked pages to read."""
        model = _Model(lambda request, number: _answer("o/a"))
        with mock.patch.object(classify_repos, "MAX_TOOL_ROUNDS", 7), \
                mock.patch.object(classify_repos, "LINKED_PAGES", 3):
            code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                      model)
        self.assertEqual(code, 0, err)
        instructions = model.requests[0]["instructions"]
        self.assertIn("you can call tools in at most 7 rounds and read at most 3 linked pages",
                      instructions)
        self.assertNotIn("$", instructions)

    def test_the_three_results_are_read_before_the_first_request(self):
        """The classification fills in every argument of the outline, the metadata and the
        issues and pull requests, reads them before the first request and puts them in its
        input. The outline is read at the README's revision and last, to the room that the
        request has left (rule 1). A call of one of these tools gets the note that the input
        holds its result."""
        def script(request, number):
            return [_call("get_file_tree", {"repo_name": "someone/else", "revision": "main"})] \
                if number == 1 else _answer("o/a")
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        first = model.requests[0]
        outline = first["input"].split("<outline>")[1].split("</outline>")[0]
        other = (len(first["instructions"]) + len(json.dumps(bridge.function_tools))
                 + len(json.dumps(classify_repos.CHECKLIST_TOOL)))
        room = int(272_000 * 3.57) - other - (len(first["input"]) - len(outline))
        self.assertEqual(bridge.calls, [
            ("get_issues_and_pull_requests", {"repo_name": "o/a"}),
            ("get_repository_metadata", {"repo_name": "o/a"}),
            ("get_file_tree", {"repo_name": "o/a", "revision": REVISION, "max_chars": room})])
        self.assertIn("\n<issues_and_pull_requests>", first["input"])
        self.assertTrue(first["input"].endswith(f"<outline>{outline}</outline>"))
        self.assertEqual(model.requests[1]["input"][0]["output"],
                         "The input contains the result of get_file_tree.")

    def test_an_outline_longer_than_its_room_is_requested_again_shorter(self):
        """The server shortens the outline's text, but the result's other fields and its escapes
        add to that length. The classification then requests the outline again, shorter by the
        added length."""
        class Outline(_Bridge):
            async def call(self, name, arguments):
                if name != "get_file_tree":
                    return await super().call(name, arguments)
                self.calls.append((name, arguments))
                return json.dumps({"file_tree": "y" * min(2_000_000, arguments["max_chars"]),
                                   "note": ""}), False

        model, bridge = _Model(lambda request, number: _answer("o/a")), Outline()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        first = model.requests[0]
        outline = first["input"].split("<outline>")[1].split("</outline>")[0]
        other = (len(first["instructions"]) + len(json.dumps(bridge.function_tools))
                 + len(json.dumps(classify_repos.CHECKLIST_TOOL)))
        room = int(272_000 * 3.57) - other - (len(first["input"]) - len(outline))
        overhead = len(json.dumps({"file_tree": "", "note": ""}))
        self.assertEqual([arguments["max_chars"] for name, arguments in bridge.calls
                          if name == "get_file_tree"], [room, room - overhead])
        self.assertEqual(len(outline), room)

    def test_a_response_that_is_json_but_no_object_is_a_failed_attempt(self):
        """The spec requires a JSON object for the requested repository. JSON of another type,
        such as `[]`, fails the attempt. An AttributeError in its place would stop every worker
        of the run."""
        sent = []

        def script(request, number):
            sent.append(number)
            return ([_call(classify_repos.CHECKLIST_TOOL_NAME, [])] if len(sent) == 1
                    else _answer("o/a"))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  _Model(script))
        self.assertEqual(code, 0, err)
        self.assertNotIn("unexpected error", err)
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.index()[0]["outcome"], "finished")

    def test_the_decision_counts_its_failed_responses(self):
        """The comparison of candidate models weighs the share of failed responses. A final
        response that fails the format is one and uses an attempt. A round with a call of a tool
        that the model is not offered, or with arguments that are no object, is none: the model
        gets an output that says so and continues."""
        def script(request, number):
            if number == 1:
                return [_message("not json")]
            if number == 2:
                return [_call("get_file_tree", {})]
            if number == 3:
                return [SimpleNamespace(type="function_call", name="read_linked_page",
                                        arguments="[1]", call_id="call-2")]
            return _answer(_repository_of(request))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  _Model(script))
        self.assertEqual(code, 0, err)
        [decision] = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual({key: decision["usage"][key] for key in
                          ("attempts", "responses", "failed_responses", "requests")},
                         {"attempts": 2, "responses": 4, "failed_responses": 1, "requests": 3})

    def test_tool_arguments_that_are_no_object_are_no_arguments(self):
        def script(request, number):
            return [_call("read_linked_page", [])] if number == 1 else _answer("o/a")
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests[1]["input"][0]["output"], classify_repos.REFUSED_LINK)

    def test_a_purpose_stated_only_on_the_page_that_the_description_links_counts(self):
        """The description of openjdk/lilliput is the address of the project's page, and its
        README welcomes the reader to the JDK without saying what Lilliput is or linking the
        page. The page tool reads the links of the description and of the homepage too, and a
        purpose quoted from the page's purpose sentence, which the server found in the page,
        counts as stated, with the page as its source. A quote from the summary of the page alone
        does not count."""
        page = "https://openjdk.example/projects/lilliput"
        sentence = ("The goal of the Lilliput Project is to explore techniques to downsize Java "
                    "object headers in the Hotspot JVM.")

        class ProjectPage(_Bridge):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "get_repository_metadata":
                    return json.dumps({"repo_name": arguments["repo_name"], "available": True,
                                       "description": page,
                                       "homepage": "https://home.example/lilliput"}), False
                if name == "read_linked_page":
                    return json.dumps({"url": arguments["url"], "available": True,
                                       "summary": "The page of Project Lilliput.",
                                       "purpose_sentence": sentence, "note": ""}), False
                return await super().call(name, arguments)

        readme = ("# Welcome to the JDK!\n\nFor build instructions please see "
                  "[the documentation](doc/building.md).\n")
        for link, quote, label, source in (
                (page, "explore techniques to downsize Java object headers", "true", page),
                ("https://home.example/lilliput", sentence, "true",
                 "https://home.example/lilliput"),
                (page, "The page of Project Lilliput.", "unclear", "")):
            with self.subTest(link=link, quote=quote):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))

                def script(request, number, link=link, quote=quote):
                    if number == 1:
                        return [_call("read_linked_page", {"url": link})]
                    return _answer("o/a", purpose_quote=quote)
                model = _Model(script)
                described = _repositories("o/a", descriptions={"o/a": page})
                code, err = self.run_over(described, _GitHub({"o/a": (readme, "s1")}), model,
                                          ProjectPage())
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(model.requests[1]["input"][0]["output"])
                                 ["purpose_sentence"], sentence)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision.get("purpose_source", "")),
                                 (label, source))

    def test_only_a_linked_pages_purpose_sentence_counts(self):
        """bbloomf/jgabc has no description and a README without a purpose, and the models
        quoted the purpose sentence of its issue #98, which the list of issues in the input names
        and no link of the description, the README or the homepage does. Only a page that such a
        link names states the purpose, including an issue that the README links. Without such a
        link, the sentence of the issue states no purpose, so the label is false at the first
        attempt and stays false when the quotation is checked again."""
        class Issues(_Bridge):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "read_linked_page":
                    return json.dumps({"url": arguments["url"], "available": True,
                                       "summary": "An issue.",
                                       "purpose_sentence": "A tool that transcribes chant "
                                                           "into gabc notation.",
                                       "other_repository": "", "note": ""}), False
                return await super().call(name, arguments)

        quote = "A tool that transcribes chant into gabc notation."
        for readme, label in (("# jgabc\n\nUsers: see the live example.\n", "false"),
                              ("# jgabc\n\nSee [why](https://github.com/o/a/issues/98).\n",
                               "true")):
            with self.subTest(readme=readme):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))

                def script(request, number):
                    if number % 2:
                        return [_call("read_linked_page",
                                      {"url": "https://github.com/o/a/issues/98"})]
                    return _answer("o/a", purpose_quote=quote)
                model = _Model(script)
                code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (readme, "s1")}),
                                          model, Issues())
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], len(model.requests)), (label, 2))
                said = "purpose_quote" if label == "true" else "purpose_quote_unlinked"
                self.assertEqual(decision[said], quote)
                self.assertNotIn("purpose_quote_refused", decision)
                classify_repos.check_quote_again(decision, "", readme)
                classify_repos.derive(decision)
                self.assertEqual((decision["label"], decision[said]), (label, quote))

    def test_a_homepage_without_its_scheme_is_read_over_https(self):
        """GitHub keeps a homepage as its owner wrote it, such as angryip.org without https. The
        page tool reads it over https, whether the model passes the address as the metadata
        gives it or with the scheme."""
        class Homepage(_Bridge):
            async def call(self, name, arguments):
                if name == "get_repository_metadata":
                    self.calls.append((name, arguments))
                    return json.dumps({"repo_name": arguments["repo_name"], "available": True,
                                       "homepage": "angryip.org"}), False
                return await super().call(name, arguments)

        for passed in ("angryip.org", "https://angryip.org"):
            with self.subTest(passed=passed):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))
                bridge = Homepage()

                def script(request, number, passed=passed):
                    if number == 1:
                        return [_call("read_linked_page", {"url": passed})]
                    return _answer("o/a")
                model = _Model(script)
                code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                          model, bridge)
                self.assertEqual(code, 0, err)
                self.assertIn(("read_linked_page", {"url": "https://angryip.org",
                                                    "repo_name": "o/a"}), bridge.calls)

    def test_the_purpose_sentence_of_another_repositorys_page_does_not_count(self):
        """file-icons/atom credits sommerper/filetype-color, the Atom package that its first
        version was based on, and the purpose sentence of that repository's README says what
        that package is, not what file-icons/atom is. A purpose quoted from a page that the
        server names as another repository's leaves the purpose unclear. The purpose sentence of
        a page of the repository itself counts, including a page under an earlier name of the
        repository, such as github.com/hyperledger/firefly-evmconnect, which the README of
        hyperledger-firefly/evmconnect links and GitHub resolves to the repository itself."""
        others = {"https://github.com/sommerper/filetype-color": "sommerper/filetype-color"}

        class Pages(_Bridge):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "read_linked_page":
                    return json.dumps({"url": arguments["url"], "available": True,
                                       "summary": "A README.",
                                       "purpose_sentence": "Colors the files of the tree view "
                                                           "by their type.",
                                       "other_repository": others.get(arguments["url"], ""),
                                       "note": ""}), False
                return await super().call(name, arguments)

        readme = ("# Icons\n\nVersion 1 was based on "
                  "[filetype-color](https://github.com/sommerper/filetype-color). See "
                  "[the guide](docs/guide.md) and [the old home](https://github.com/old/a).\n")
        for link, label in (("https://github.com/sommerper/filetype-color", "unclear"),
                            ("docs/guide.md", "true"), ("https://github.com/old/a", "true")):
            with self.subTest(link=link):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))

                def script(request, number, link=link):
                    if number == 1:
                        return [_call("read_linked_page", {"url": link})]
                    return _answer("o/a", purpose_quote="Colors the files of the tree view")
                code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (readme, "s1")}),
                                          _Model(script), Pages())
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual(decision["label"], label)

    def test_a_summary_is_counted_and_its_cost_is_not_sent_to_the_model(self):
        def script(request, number):
            return [_call("read_linked_page", {"url": "https://tool.example/docs"})] \
                if number == 1 else _answer("o/a")
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(model.requests[1]["input"][0]["output"]),
                         {"url": "https://tool.example/docs", "available": True,
                          "summary": "A summary of https://tool.example/docs.", "note": ""})
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["usage"]["summary_input_tokens"],
                          decision["usage"]["summary_output_tokens"],
                          decision["usage"]["linked_pages"]),
                         (120, 30, 1))
        self.assertEqual(decision["tools"][-1]["summary_model"], "gpt-6-luna-2026-09-30")
        row = self.index()[0]
        self.assertEqual((row["summary_input_tokens"], row["summary_output_tokens"]),
                         ("120", "30"))

    def test_the_tokens_are_counted_by_the_tier_that_each_response_reports(self):
        """OpenAI bills a request at the tier that served it, which its response reports, and
        bills the input tokens that it wrote to its prompt cache at a rate of their own. The
        run's tokens file counts every response under its model, the tier that its request named
        and the tier that it reports. The model reads the summary without these counts, and for
        the summary, the decision records only its input tokens, its output tokens and its
        model."""
        def script(request, number):
            return [_call("read_linked_page", {"url": "https://tool.example/docs"})] \
                if number == 1 else _answer("o/a")
        model = _Billed(script, {1: "flex", 2: "default"}.get)
        bridge = _Bridge(billing={"cached_tokens": 40, "cache_write_tokens": 60,
                                  "service_tier": "default"})
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        counts = {"input_tokens": 100, "cached_tokens": 5, "cache_write_tokens": 40,
                  "output_tokens": 10, "reasoning_tokens": 3}
        self.assertEqual(label_cache.read_tokens(self.cache, self.index()[0]["run_id"]), [
            {"role": "classifying_model", "model": "gpt-6-luna", "requested_tier": "flex",
             "service_tier": "default", "requests": 1, **counts},
            {"role": "classifying_model", "model": "gpt-6-luna", "requested_tier": "flex",
             "service_tier": "flex", "requests": 1, **counts},
            {"role": "summary_model", "model": "gpt-6-luna", "requested_tier": "auto",
             "service_tier": "default", "requests": 1, "input_tokens": 120, "cached_tokens": 40,
             "cache_write_tokens": 60, "output_tokens": 30, "reasoning_tokens": 0}])
        self.assertEqual(json.loads(model.requests[1]["input"][0]["output"]),
                         {"url": "https://tool.example/docs", "available": True,
                          "summary": "A summary of https://tool.example/docs.", "note": ""})
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual(set(decision["usage"]),
                         {"requests", "tool_rounds", "linked_pages", "input_tokens",
                          "cached_tokens", "output_tokens", "reasoning_tokens",
                          "summary_input_tokens", "summary_output_tokens", "seconds",
                          "attempts", "responses", "failed_responses"})
        self.assertEqual({key for key in decision["tools"][-1] if key.startswith("summary_")},
                         {"summary_input_tokens", "summary_output_tokens", "summary_model"})

    def test_a_page_has_to_be_a_link_of_the_readme(self):
        asked = ["https://elsewhere.example/", "docs/guide.md", "https://tool.example/docs",
                 "CONTRIBUTING.md", "https://tool.example/docs"]

        def script(request, number):
            if number == 1:
                return [_call("read_linked_page", {"url": url}, f"c{i}")
                        for i, url in enumerate(asked)]
            return _answer("o/a")
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        # Rule 5: the classification reads a relative link through GitHub at the README's commit.
        self.assertEqual([call for call in bridge.calls if call[0] == "read_linked_page"], [
            ("read_linked_page", {"url": f"https://github.com/o/a/blob/{REVISION}/docs/guide.md",
                                  "repo_name": "o/a"}),
            ("read_linked_page", {"url": "https://tool.example/docs", "repo_name": "o/a"}),
            ("read_linked_page", {"url": "https://tool.example/docs", "repo_name": "o/a"})])
        outputs = [item["output"] for item in model.requests[1]["input"]]
        self.assertEqual((outputs[0], outputs[3]), (classify_repos.REFUSED_LINK,) * 2)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual([t["tool"] for t in decision["tools"] if t["round"]],
                         ["read_linked_page"] * 5)

    def test_a_link_reference_definition_and_a_branch_of_the_repository_are_read(self):
        """herumi/xbyak's readme.md links its install guide as "[Install]: doc/install.md". The
        model may pass the target, the definition or the label, and each reaches the tool as the
        file at the README's commit. A link of the README to a file of the repository at a branch
        reaches the tool at the README's commit too, because GitHub returns 404 for a file at a
        branch that was renamed since."""
        readme = (ENGLISH + "\nSee [the guide][Install] and "
                  "https://github.com/o/a/blob/master/CONTRIBUTING.md.\n\n"
                  "[Install]: doc/install.md\n")
        passed = ["doc/install.md", "[Install]", "[Install]: doc/install.md",
                  "https://github.com/o/a/blob/master/CONTRIBUTING.md"]

        def script(request, number):
            if number == 1:
                return [_call("read_linked_page", {"url": url}, f"c{i}")
                        for i, url in enumerate(passed)]
            return _answer("o/a")
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (readme, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        guide = f"https://github.com/o/a/blob/{REVISION}/doc/install.md"
        self.assertEqual([call for call in bridge.calls if call[0] == "read_linked_page"],
                         [("read_linked_page", {"url": guide, "repo_name": "o/a"})] * 3 + [
                             ("read_linked_page", {
                                 "url": f"https://github.com/o/a/blob/{REVISION}/CONTRIBUTING.md",
                                 "repo_name": "o/a"})])

    def test_each_form_of_a_link_reaches_the_tool_as_the_page_that_the_readme_links(self):
        """A relative link to a file at a branch, a relative link to another repository, an
        address without its trailing slash or its fragment and a reStructuredText reference each
        reach read_linked_page as the address of the page that the README links. A link to a
        repository that the README does not link gets the refusal, and no request."""
        readme = (ENGLISH + "\nRead [CONTRIBUTING.md](../master/CONTRIBUTING.md), the "
                  "[project](../../../jackson#participation-contributing), "
                  "the `documentation <https://docs.example/>`_, the "
                  "[app](https://github.com/o/mobile#web) and `Contributing "
                  "<pism-contributing_>`_.\n\n"
                  ".. _pism-contributing: http://www.pism.example/docs/contributing\n")
        passed = ["../master/CONTRIBUTING.md", "../../../jackson#participation-contributing",
                  "https://docs.example", "https://github.com/o/mobile", "pism-contributing_",
                  "../../../secret"]

        def script(request, number):
            if number == 1:
                return [_call("read_linked_page", {"url": url}, f"c{i}")
                        for i, url in enumerate(passed)]
            return _answer("o/a")
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (readme, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        self.assertEqual([call[1]["url"] for call in bridge.calls
                          if call[0] == "read_linked_page"],
                         [f"https://github.com/o/a/blob/{REVISION}/CONTRIBUTING.md",
                          "https://github.com/o/jackson", "https://docs.example/",
                          "https://github.com/o/mobile#web",
                          "http://www.pism.example/docs/contributing"])
        self.assertEqual(model.requests[1]["input"][5]["output"], classify_repos.REFUSED_LINK)

    def test_no_more_linked_pages_than_the_limit_are_read(self):
        limit = classify_repos.LINKED_PAGES
        links = "\n".join(f"https://tool.example/p{i}" for i in range(limit + 1))

        def script(request, number):
            if number == 1:
                return [_call("read_linked_page", {"url": f"https://tool.example/p{i}"}, f"c{i}")
                        for i in range(limit + 1)]
            return _answer("o/a")
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"),
                                  _GitHub({"o/a": (ENGLISH + "\n" + links, "s1")}), model, bridge)
        self.assertEqual(code, 0, err)
        self.assertEqual(len([call for call in bridge.calls if call[0] == "read_linked_page"]),
                         limit)
        self.assertEqual(model.requests[1]["input"][-1]["output"],
                         f"The limit of {limit} linked pages per repository is reached.")
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["usage"]["linked_pages"], decision["limits_reached"]),
                         (limit, ["linked_pages"]))

    def test_tool_calls_past_the_round_limit_get_the_limit_as_output_and_the_label_is_decided_without_them(self):
        """Rule 14: the model decides the repository with what it has read, and the decision
        records that the limit was reached."""
        def script(request, number):
            # Past fifty requests the fake returns a label, which ends a classification that has
            # no limit.
            if request["tools"] == [classify_repos.CHECKLIST_TOOL] or number > 50:
                return _answer("o/a", "unclear")
            return [_call("get_file_tree", {}, f"c{number}")]
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        limit = classify_repos.MAX_TOOL_ROUNDS
        self.assertEqual([name for name, _ in bridge.calls],
                         ["get_issues_and_pull_requests", "get_repository_metadata", "get_file_tree"])
        self.assertEqual(len(model.requests), limit + 2)
        self.assertEqual([[tool["name"] for tool in r["tools"]] for r in model.requests],
                         [[*(tool["name"] for tool in _Bridge().function_tools),
                           classify_repos.CHECKLIST_TOOL_NAME]] * (limit + 1)
                         + [[classify_repos.CHECKLIST_TOOL_NAME]])
        self.assertTrue(all("tool_choice" not in r and "text" not in r for r in model.requests))
        self.assertIn(f"The limit of {limit} rounds", model.requests[-1]["input"][0]["output"])
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["limits_reached"],
                          decision["usage"]["tool_rounds"], decision["usage"]["requests"]),
                         ("unclear", ["tool_rounds"], limit, limit + 2))

    def test_a_model_that_calls_another_tool_in_a_request_that_offers_only_the_checklist_fails_the_attempt(self):
        model = _Model(lambda request, number: [_call("get_file_tree", {}, f"c{number}")])
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 1)
        self.assertIn("called another tool in a request that offers only submit_classification",
                      err)
        self.assertEqual(len(model.requests),
                         (classify_repos.RETRY_ROUNDS + 1) * classify_repos.ATTEMPTS
                         * (classify_repos.MAX_TOOL_ROUNDS + 2))

    def test_a_response_for_another_repository_is_tried_again(self):
        def script(request, number):
            return _answer("o/other") if number == 1 else _answer("o/a", "false")
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  _Model(script))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "false")

    def test_a_request_the_api_refuses_ends_the_repository_and_the_run_finishes_without_it(self):
        """A README that the API refuses with HTTP 400, such as `invalid_prompt` under OpenAI's
        usage policy, fails the same way every time. The run sends it once and finishes without
        it within the ceiling. It lists the repository as undecided with its error, and the next
        invocation tries it again in a new run, because no collection over the sample has begun.
        A run that ended incomplete over such a README would stop the stages after the
        classification every time."""
        self.real_ceiling()
        # The parts of a response that the SDK's error reads: its request, status and headers.
        response = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})
        refused = openai.BadRequestError("Invalid prompt: flagged", response=response,
                                         body={"code": "invalid_prompt", "message": "flagged"})

        def script(request, number):
            if _repository_of(request) == "o/a":
                raise refused
            return _answer(_repository_of(request))
        model = _Model(script)
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        code, err = self.run_over(_independent("o/a", "o/b"), github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests].count("o/a"), 1)
        self.assertIn("Could not decide o/a (the API refused the request", err)
        self.assertEqual(self.rows("repos_excluded.csv"), [
            {"repo_name": "o/a", "repo_url": "https://github.com/o/a", "reason": "undecided",
             "detected_language": ""}])
        self.assertEqual([r["repo_name"] for r in self.rows("repos_annotated.csv")], ["o/b"])
        row = self.index()[0]
        self.assertEqual((row["outcome"], row["labeled"], row["excluded"], row["repositories"]),
                         ("finished", "1", "0", "2"))
        undecided = label_cache.read_labels(self.cache, row["run_id"], row["labels_sha256"])[0]
        self.assertEqual((undecided["repo_name"], undecided["outcome"], undecided["decided_in"]),
                         ("o/a", "undecided", ""))
        self.assertEqual(undecided["reason"], "model: ModelFailure: the API refused the request "
                                              "(Invalid prompt: flagged)")
        # The release reads how many repositories the sample lacks because they have no decision.
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual(record["undecided"], 1)
        model = _Model(_always_true)
        code, err = self.run_over(_independent("o/a", "o/b"), github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/a"])
        self.assertEqual(self.rows("repos_excluded.csv"), [])

    def test_an_undecided_repository_is_tried_again_only_while_no_collection_has_begun(self):
        """Missing labels are tried again by default, without `--new-run`. Once a collection
        over the sample has begun, a new run could change the sample that it collects, and the
        next invocation completes the run without the undecided repository and logs a WARNING
        with their count and the remedy. A collection that began before the retrieval of the
        sampling frame collects an earlier sampling frame and does not count. The cases are a
        record of a collection in progress, a record of a collection that ended, and the list
        of finished repositories that a collection ended by the runner of an earlier image leaves
        alone. A monthly run whose GHS download fell back to the previous retrieval reuses its
        retrieval id, and the collection of the previous month began after it. That run's
        `--from repository_cloning` marks the collection as replaced, from its record or from
        the lines, and the undecided repository is tried again until a collection begins
        after the mark."""
        from agentcontext import resume
        self.real_ceiling()
        response = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})
        refused = openai.BadRequestError("Invalid prompt: flagged", response=response,
                                         body={"code": "invalid_prompt", "message": "flagged"})

        def script(request, number):
            if _repository_of(request) == "o/a":
                raise refused
            return _answer(_repository_of(request))
        finished = self.collection / resume.FINISHED_NAME
        after, before = "2026-10-06T090000Z-0123abcd", "2026-10-01T090000Z-0123abcd"

        def in_progress():
            resume.save_collection(finished, {"collection": after, "input_sha256": "x",
                                              "taken_over": set()})

        def ended():
            in_progress()
            resume.close_collection(finished)

        def lines_alone():
            resume.append_finished(finished, "o/b", "o/b,row", None, collection=after)

        def earlier_frame():
            resume.append_finished(finished, "o/b", "o/b,row", None, collection=before)

        def replaced():
            ended()
            lines_alone()
            resume.forget_ended_collection(finished)

        def replaced_lines():
            lines_alone()
            resume.forget_ended_collection(finished)

        def begun_after_the_mark():
            replaced()
            resume.save_collection(finished, {"collection": later, "input_sha256": "x",
                                              "taken_over": set()})
        later = "2026-10-07T090000Z-89abcdef"
        for name, begin, retried, begun in (
                ("in progress", in_progress, False, after),
                ("ended", ended, False, after),
                ("lines of an earlier runner", lines_alone, False, after),
                ("earlier frame", earlier_frame, True, None),
                ("replaced", replaced, True, None),
                ("replaced, from the lines", replaced_lines, True, None),
                ("begun after the mark", begun_after_the_mark, False, later)):
            with self.subTest(collection=name):
                self.setUp()
                self.real_ceiling()
                finished = self.collection / resume.FINISHED_NAME
                github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
                code, err = self.run_over(_independent("o/a", "o/b"), github, _Model(script))
                self.assertEqual(code, 0, err)
                begin()
                model = _Model(_always_true)
                code, err = self.run_over(_independent("o/a", "o/b"), github, model)
                self.assertEqual(code, 0, err)
                self.assertEqual([_repository_of(r) for r in model.requests],
                                 ["o/a"] if retried else [])
                self.assertEqual(len(self.index()), 2 if retried else 1)
                self.assertEqual(self.rows("repos_excluded.csv"), [] if retried else [
                    {"repo_name": "o/a", "repo_url": "https://github.com/o/a",
                     "reason": "undecided", "detected_language": ""}])
                warning = (f"WARNING repository_labeling: Run {self.index()[0]['run_id']} left 1 "
                           f"repositories without a decision, such as o/a, and the collection "
                           f"{begun} over its sample has begun.")
                if retried:
                    self.assertNotIn("WARNING repository_labeling: Run", err)
                else:
                    self.assertIn(warning, err)
                    self.assertIn("label in a new run with --new-run", err)

    def test_more_repositories_without_a_decision_than_the_ceiling_end_the_run_incomplete(self):
        """More repositories without a decision than the ceiling allows point to a cause that
        someone has to check, such as GitHub refusing a kind of request. The run ends incomplete
        with its decisions kept, as a stopped run does."""
        self.real_ceiling()
        names = ("o/a", "o/b", "o/c")
        github = _GitHub({name: (ENGLISH, name) for name in names})
        original = github.get

        def get(url, headers=None, timeout=None):
            if "/o/a/readme" in url:
                return _Response(403, {"message": "Resource not accessible by integration"})
            return original(url, headers, timeout)
        github.get = get
        model = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/b"
            else _answer(_repository_of(request))))
        code, err = self.run_over(_repositories(*names), github, model)
        self.assertEqual(code, 1)
        self.assertIn("Could not decide o/a (GitHub returned HTTP 403 for the README of o/a)",
                      err)
        self.assertIn("2 of 3 repositories have no decision, more than the ceiling of 1 for 3 "
                      "repositories", err)
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["decisions"]),
                         ("incomplete", "1"))
        self.assertFalse((self.out / "repos_excluded.csv").exists())

    def test_a_lost_previous_response_is_tried_again_from_the_start(self):
        """The API can refuse a request of the tool rounds with HTTP 400 because it no longer
        finds the previous response, as for stretchr/testify. Unlike a malformed request, the
        request succeeds in a new attempt that starts without the previous response. Without
        that attempt, the repository would stay without a decision and fail the run."""
        lost = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})

        def script(request_arguments, number):
            if number == 1:
                raise openai.BadRequestError(
                    "Previous response not found.", response=lost,
                    body={"code": "previous_response_not_found",
                          "message": "Previous response not found."})
            return _answer("o/a")
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(next(label_cache.all_decisions(self.cache))[1]["label"], "true")

    def test_a_failing_service_pauses_every_worker_instead_of_failing_repositories(self):
        """When flex capacity runs out, the API returns HTTP 429 to every request for a while.
        Counted as attempts, the failures of the two repositories would use up the attempts of
        one of them, which would then wait for a retry round. With every worker paused, both are
        decided in the pass."""
        names = ("o/a", "o/b")
        github = _GitHub({name: (ENGLISH, name) for name in names})
        # The first failure uses an attempt, because no other repository has failed yet. The
        # failure after the pauses that use no attempt uses one too. No repository uses every
        # attempt.
        failures = classify_repos.PAUSES_BEFORE_ATTEMPTS + 2
        response = SimpleNamespace(request=SimpleNamespace(), status_code=429, headers={})

        def script(request, number):
            if number <= failures:
                raise openai.RateLimitError("Resource unavailable", response=response,
                                            body={"code": "flex_unavailable",
                                                  "message": "Resource unavailable"})
            return _answer(_repository_of(request))
        model = _Together(script, names)
        with _short_pauses():
            code, err = self.run_over(_repositories(*names), github, model, workers=2)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["labeled"], "2")
        self.assertNotIn("Retrying it later", err)
        self.assertNotIn("Retry round", err)
        self.assertEqual(len(model.requests), failures + 2)
        self.assertIn("RateLimitError (Resource unavailable). Pausing every worker for 0:00:00, "
                      "then sending each request again", err)

    def test_a_request_of_a_conversation_that_a_pause_holds_for_is_sent_again_as_it_was(self):
        """Once the first request of o/c has failed, the service fails the second request of o/a
        and of o/b, each sent after the model read a page. Every worker pauses, and each
        conversation sends its second request again with the same previous response, input and
        tool choice. Sending the conversation again from its first request would pay again for
        the first request to the model and for the summary of the page."""
        names = ("o/a", "o/b", "o/c")
        held, waiting = asyncio.Event(), set()

        class Model(_Model):
            async def create(self, **request):
                name = _repository_of(request)
                if name == "o/c" and not held.is_set():
                    # o/c fails only once both second requests are waiting, and the pause then
                    # holds both.
                    for _ in range(5000):
                        if len(waiting) == 2:
                            break
                        await asyncio.sleep(0.001)
                    self.requests.append(request)
                    held.set()
                    raise _server_error()
                if request.get("previous_response_id") and name not in waiting:
                    waiting.add(name)
                    self.requests.append(request)
                    await held.wait()
                    raise _server_error()
                return await super().create(**request)

        def script(request, number):
            name = _repository_of(request)
            if name != "o/c" and not request.get("previous_response_id"):
                return [_call("read_linked_page", {"url": "https://tool.example/docs"})]
            return _answer(name)
        model, bridge = Model(script), _Bridge()
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({name: (ENGLISH, name) for name in names}), model,
                                  bridge, workers=3, clock=_Clock())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["labeled"], "3")
        self.assertIn("Pausing every worker for 0:01:00, then sending each request again", err)
        for name in ("o/a", "o/b"):
            with self.subTest(name=name):
                requests = [r for r in model.requests if _repository_of(r) == name]
                self.assertEqual([bool(r.get("previous_response_id")) for r in requests],
                                 [False, True, True])
                self.assertEqual(requests[1], requests[2])
                self.assertEqual(
                    [arguments["repo_name"] for tool, arguments in bridge.calls
                     if tool == "read_linked_page"].count(name), 1)

    def test_two_repositories_that_the_service_fails_every_time_end_undecided(self):
        """The last two repositories of a pass, of a retry round or of the comparison can fail
        on every request, such as with a server error for each. No other repository's response
        ends the pauses. After the pauses that use no attempt, the failures use attempts, and in
        a retry round a repository that used every attempt begins no pause. Both repositories end
        without a decision, and the run finishes without them rather than pausing without end."""
        self.with_ceiling(lambda repositories: 2)
        names = ("o/a", "o/b")
        github = _GitHub({name: (ENGLISH, name) for name in names})

        def script(request, number):
            if number > 300:
                raise AssertionError("the run sends requests without end")
            raise _server_error()
        model = _Together(script, names)
        with _short_pauses():
            code, err = self.run_over(_repositories(*names), github, model, workers=2)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_excluded.csv"), [
            {"repo_name": name, "repo_url": f"https://github.com/{name}", "reason": "undecided",
             "detected_language": ""} for name in names])
        self.assertIn("After 3 pauses without a response, each failure uses an attempt at the "
                      "repository's label", err)
        # No retry round pauses, because the repositories used every attempt in the first round.
        self.assertNotIn("Pausing every worker", err.split("Retry round 1 of")[1])
        # Each round uses each repository's attempts, and the first round also uses the pauses.
        self.assertLessEqual(len(model.requests),
                             2 * (classify_repos.RETRY_ROUNDS + 1) * classify_repos.ATTEMPTS
                             + 2 * classify_repos.PAUSES_BEFORE_ATTEMPTS)

    def test_an_outage_without_end_stops_the_run_and_the_next_run_resumes_it(self):
        """An outage of the service that outlasts the total of the pauses stops the run after a
        bounded number of requests, with its decisions kept. Without the stop, the run would send
        a request after every longest pause without end. The next run resumes the stopped run."""
        names = [f"o/r{i}" for i in range(6)]
        github = _GitHub({name: (ENGLISH, name) for name in names})

        def down(request, number):
            if number > 300:
                raise AssertionError("the run sends requests without end")
            raise _server_error(503)
        model = _Together(down, names[:2])
        with _short_pauses(PAUSE_TOTAL_SECONDS=0.02):
            code, err = self.run_over(_repositories(*names), github, model, workers=2)
        self.assertEqual(code, 1)
        self.assertIn("ERROR repository_labeling: Stopped the run, because the API failed every "
                      "request through 0:00:00 of pauses since its last response, the last with "
                      "InternalServerError", err)
        self.assertLessEqual(len(model.requests), 20)
        self.assertEqual(self.index()[0]["outcome"], "incomplete")
        model = _Model(_always_true)
        code, err = self.run_over(_repositories(*names), github, model, workers=2)
        self.assertEqual(code, 0, err)
        self.assertEqual([(row["outcome"], row["labeled"]) for row in self.index()],
                         [("finished", "6")])
        self.assertEqual(len(model.requests), 6)

    def test_a_response_is_paid_once_when_githubs_facts_fail_after_it(self):
        """When GitHub fails for the merges of a mirror after the model's response, the
        repository fails for a retry round. The retry reads GitHub's facts again and keeps the
        model's response. Without the kept response, the run would pay again for a response to
        the same input."""
        class Failing(_GitHub):
            def __init__(self):
                super().__init__({"o/a": (ENGLISH, "s1")},
                                 merges={"o/a": ["2026-07-21T00:46:14Z"]})
                self.broken = True

            def post(self, url, headers=None, json=None, timeout=None):
                if self.broken:
                    self.broken = False
                    return _Response(200, {"data": None, "errors": [
                        {"type": "SERVICE_UNAVAILABLE", "message": "Something went wrong"}]})
                return super().post(url, headers, json, timeout)
        model = _Model(lambda request, number: _answer(
            "o/a", facts={"mirror": {"described_as_mirror": "true"}}))
        code, err = self.run_over(_repositories("o/a"), Failing(), model)
        self.assertEqual(code, 0, err)
        self.assertIn("has no repository (Something went wrong)). Retrying it later", err)
        self.assertEqual(len(model.requests), 1)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["mirror_merges"]["latest_merge"]),
                         ("true", "2026-07-21T00:46:14Z"))

    def test_a_tool_failure_that_says_nothing_about_the_repository_is_not_passed_to_the_model(
            self):
        """The server marks the failures of GitHub and of the summary model. Passed to the model,
        the API's refusal of the summary model, or GitHub's refusal of the server's GitHub access
        tokens, would leave labels cached without pages or outlines. A refusal or an empty pool
        of GitHub access tokens stops the run. After a failure of GitHub or of the summary model,
        the classification calls the tool again, here before the first request, and the run pays
        for one request."""
        class Failing(_Bridge):
            def __init__(self, failure):
                super().__init__()
                self.failure = failure

            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                if self.failure:
                    failure, self.failure = self.failure, ""
                    return f"Error executing tool {name}: {failure}", True
                return json.dumps({"repo_name": arguments.get("repo_name"), "available": True,
                                   "file_tree": "Files: 3", "note": ""}), False

        def script(request, number):
            return _answer(_repository_of(request))
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        for kind, message, stops, logged in (
                (tools.NO_GITHUB_ACCESS_TOKEN, "GitHub accepts none of the configured GitHub "
                                               "access tokens", True,
                 "ERROR repository_labeling: Stopped the run, because the MCP server of the "
                 "tools has no GitHub access token left (GitHub accepts none"),
                (tools.SUMMARY_REFUSED, "the API does not offer the model (gone)", True,
                 "ERROR repository_labeling: Stopped the run, because the API refuses the "
                 "requests of the summary model (the API does not offer the model"),
                (tools.GITHUB_FAILED, "NoResponse (GitHub returned HTTP 502)", False,
                 "WARNING repository_labeling: The tool get_issues_and_pull_requests failed for "
                 "o/a (NoResponse (GitHub returned HTTP 502)). Calling it again in 0:00:00"),
                (tools.NETWORK_FAILED, "could not read https://a.example/ (the request failed: "
                                       "ConnectionError)", False,
                 "WARNING repository_labeling: The tool get_issues_and_pull_requests failed for "
                 "o/a (could not read https://a.example/ (the request failed: ConnectionError)). "
                 "Calling it again in 0:00:00"),
                (tools.SUMMARY_FAILED, "summarizing https://a.example/ failed with "
                                       "APIConnectionError (down)", False,
                 "WARNING repository_labeling: The tool get_issues_and_pull_requests failed for "
                 "o/a (summarizing https://a.example/ failed with APIConnectionError (down)). "
                 "Calling it again in 0:00:00")):
            with self.subTest(kind=kind):
                self.setUp()
                bridge, model = Failing(f"[{kind}] {message}"), _Model(script)
                code, err = self.run_over(_repositories("o/a"), github, model, bridge)
                self.assertIn(logged, err)
                decisions = [d for _, d in label_cache.all_decisions(self.cache)]
                if stops:
                    self.assertEqual((code, decisions), (1, []))
                    continue
                self.assertEqual(code, 0, err)
                [decision] = decisions
                self.assertEqual([call["is_error"] for call in decision["tools"]], [False] * 3)
                self.assertEqual(len(bridge.calls), 4)
                self.assertEqual(len(model.requests), 1)
                self.assertNotIn("tried again", err)

    def test_a_tool_failure_through_every_wait_fails_the_repository_for_a_retry_round(self):
        """A tool call that GitHub or the summary model fails through every wait fails the
        repository for a retry round, not an attempt. Another attempt would pay for the first
        request again while the failure lasts. The retry round labels the repository from its
        first request. The failure of a call that the classification makes before the first
        request fails the repository before any request, and the failure of a call of
        read_linked_page in the conversation ends the conversation."""
        class Failing(_Bridge):
            def __init__(self, failure, times, tool):
                super().__init__()
                self.failure, self.times, self.tool, self.failed = failure, times, tool, 0

            async def call(self, name, arguments):
                if name == self.tool and self.failed < self.times:
                    self.failed += 1
                    self.calls.append((name, arguments))
                    return f"Error executing tool {name}: {self.failure}", True
                return await super().call(name, arguments)

        def script(request, number):
            if request.get("previous_response_id") or "page" not in request["input"]:
                return _answer(_repository_of(request))
            return [_call("read_linked_page", {"url": "https://tool.example/docs"})]
        calls = len(classify_repos.TOOL_WAITS) + 1
        for kind, message, tool, requests in (
                (tools.GITHUB_FAILED, "NoResponse (GitHub returned HTTP 502)",
                 "get_issues_and_pull_requests", [False]),
                (tools.NETWORK_FAILED, "could not read https://a.example/ (the request failed: "
                                       "ConnectionError)", "get_issues_and_pull_requests", [False]),
                (tools.SUMMARY_FAILED, "summarizing https://a.example/ failed with "
                                       "APIConnectionError (down)", "read_linked_page",
                 [False, False, True])):
            with self.subTest(kind=kind):
                self.setUp()
                bridge, model = Failing(f"[{kind}] {message}", calls, tool), _Model(script)
                readme = ENGLISH + ("\npage" if tool == "read_linked_page" else "")
                code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (readme, "s1")}),
                                          model, bridge)
                self.assertEqual(code, 0, err)
                self.assertIn(f"Could not decide o/a (the tool {tool} failed, because "
                              f"{message}). Retrying it later", err)
                self.assertEqual(len([c for c in bridge.calls if c[0] == tool]), calls + 1)
                # The conversation that the failure ended, if any, and the retry round's.
                self.assertEqual([bool(r.get("previous_response_id")) for r in model.requests],
                                 requests)
                [decision] = [d for _, d in label_cache.all_decisions(self.cache)]
                self.assertEqual(decision["label"], "true")

    def test_errors_in_place_of_a_field_that_fail_a_repository_are_counted_at_the_end(self):
        """The tool for the issues and pull requests fails every call, because GitHub's GraphQL
        response has errors in place of a field. The repository is left without a decision. The
        summary at the end of the run counts it among the failures of GitHub, as a response with
        errors in place of a field, and a cause that recurs shows in that count."""
        message = ("NoResponse: o/a: GitHub's response for the issues and pull requests has "
                   "errors in place of pinnedIssues: timedout")

        class Failing(_Bridge):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                return f"Error executing tool {name}: [{tools.GITHUB_FAILED}] {message}", True

        def script(request, number):
            if request.get("previous_response_id"):
                return _answer(_repository_of(request))
            return [_call("get_issues_and_pull_requests", {}, f"c{number}")]
        with mock.patch.object(classify_repos, "RETRY_ROUNDS", 0):
            code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                      _Model(script), Failing())
        self.assertEqual(code, 1, err)
        self.assertIn("1 repositories have no decision, 1 for GitHub's response (1 of them a "
                      "GraphQL response with errors in place of a field), 0 for the model's "
                      "response and 0 for an unexpected error. The first is o/a", err)

    def test_a_tool_call_without_a_result_in_time_fails_the_attempt(self):
        """The bridge stops waiting for a call after its time limit, and the call's result is
        unknown. Passed to the model as an error, the timeout would let the label be decided and
        cached without the page. The attempt fails instead, and the next attempt reads the page."""
        class Hanging(_Bridge):
            async def call(self, name, arguments):
                if name == "read_linked_page" and not any(c[0] == name for c in self.calls):
                    self.calls.append((name, arguments))
                    raise mcp_bridge.ToolTimeout(f"the tool {name} returned no result within "
                                                 f"7200 seconds")
                return await super().call(name, arguments)

        def script(request, number):
            if request.get("previous_response_id"):
                return _answer(_repository_of(request))
            return [_call("read_linked_page", {"url": "https://tool.example/docs"})]
        model, bridge = _Model(script), Hanging()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  bridge)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 3)
        self.assertEqual([c[0] for c in bridge.calls if c[0] == "read_linked_page"],
                         ["read_linked_page"] * 2)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual([(call["tool"], call["is_error"]) for call in decision["tools"]
                          if call["round"]], [("read_linked_page", False)])

    def test_a_page_that_the_summary_model_refuses_is_unavailable_and_the_label_decided(self):
        """The API refuses a summary request in every attempt for the page that the request
        contains, such as a page past the summary model's context window. Treated as a failure of
        the summary, the refusal would fail every attempt and retry round, each after paid
        requests to the classifying model. The real server returns the page as unavailable, and
        the label is decided in two requests."""
        class Contents:
            """GitHub's contents API: every file is a short text."""

            def get(self, url, accept=readmes.JSON, etag=""):
                return SimpleNamespace(status_code=200, headers={}, content=b"The guide.",
                                       json=lambda: {"encoding": "base64",
                                                     "content": base64.b64encode(
                                                         b"The guide.").decode()})

        class Refusing:
            async def __call__(self, repo_name, url, text):
                response = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})
                raise openai.BadRequestError(
                    "Your input exceeds the context window of this model.", response=response,
                    body={"code": "context_length_exceeded", "message": "too long"})

        class InProcess(_Bridge):
            """The real server's tools, as the bridge returns their results."""

            def __init__(self):
                super().__init__()
                self.server = tools.build_server(github=Contents(), session=None,
                                                 summarizer=Refusing())

            async def call(self, name, arguments):
                if name != "read_linked_page":
                    return await super().call(name, arguments)
                self.calls.append((name, arguments))
                try:
                    result = await self.server.call_tool(name, arguments)
                except ToolError as error:
                    return str(error), True
                return mcp_bridge.tool_output(result), bool(getattr(result, "is_error", False))

        def script(request, number):
            if request.get("previous_response_id"):
                return _answer(_repository_of(request))
            return [_call("read_linked_page", {"url": "docs/guide.md"})]
        model = _Model(script)
        with contextlib.redirect_stderr(io.StringIO()):
            code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                      model, InProcess())
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        decision = next(label_cache.all_decisions(self.cache))[1]
        output = json.loads(decision["tools"][-1]["output"])
        self.assertEqual((output["available"], output["note"]),
                         (False, "the summary model refused the page (context_length_exceeded)"))

    def test_a_rejected_key_or_an_unknown_model_stops_the_run(self):
        """The API refuses every request of a run with a rejected key, a project or a key without
        access, or a model that it does not offer, and a top-up does not end the refusal. The run
        stops. Failing each repository on its own after its attempts and retry rounds would,
        over the sampling frame, go through every repository."""
        def refused(kind, status, code=None):
            response = SimpleNamespace(request=SimpleNamespace(), status_code=status, headers={})
            return kind("refused", response=response,
                        body={"code": code, "message": "refused"} if code else None)
        names = [f"o/r{i}" for i in range(6)]
        github = _GitHub({name: (ENGLISH, name) for name in names})
        for error, said, endpoint in (
                (refused(openai.AuthenticationError, 401), "the API rejects the OpenAI API key",
                 {}),
                (refused(openai.PermissionDeniedError, 403), "the API denies the project", {}),
                (refused(openai.NotFoundError, 404, "model_not_found"),
                 "the API does not offer the model", {}),
                (refused(openai.BadRequestError, 400, "model_not_found"),
                 "the API does not offer the model", {}),
                (_openrouter_error(openai.AuthenticationError, 401),
                 "OpenRouter rejects the OpenRouter API key", DEEPSEEK),
                (_openrouter_error(openai.PermissionDeniedError, 403),
                 "OpenRouter denies the OpenRouter API key access", DEEPSEEK)):
            with self.subTest(said=said):
                self.setUp()

                def script(request, number, error=error):
                    raise error
                model = _Model(script)
                code, err = self.run_over(_repositories(*names), github, model, workers=2,
                                          **endpoint)
                self.assertEqual(code, 1)
                self.assertLessEqual(len(model.requests), 2)
                self.assertIn(f"ERROR repository_labeling: Stopped the run, because {said}", err)
                self.assertNotIn("Retrying it later", err)
                self.assertNotIn("Retry round", err)
        self.setUp()

        def gone(request, number):
            raise refused(openai.NotFoundError, 404)
        model = _Model(gone)
        code, err = self.run_over(_repositories("o/r0"), github, model)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.requests),
                         (classify_repos.RETRY_ROUNDS + 1) * classify_repos.ATTEMPTS)

    def test_each_decision_counts_its_own_tokens_when_repositories_are_decided_at_once(self):
        """A repository's tokens are not the difference of the run's totals before and after the
        repository. That difference includes the requests of every other repository decided
        meanwhile."""
        def script(request, number):
            if request.get("previous_response_id"):
                return _answer(_repository_of(request))
            return [_call("get_file_tree", {"repo_name": "", "revision": ""})]
        github = _GitHub({f"o/r{i}": (ENGLISH, f"s{i}") for i in range(4)})
        code, err = self.run_over(_repositories(*(f"o/r{i}" for i in range(4))), github,
                                  _Model(script), workers=4)
        self.assertEqual(code, 0, err)
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual([d["usage"]["input_tokens"] for d in decisions], [200] * 4)
        self.assertEqual(self.index()[0]["input_tokens"], "800")

    def test_the_decision_records_what_decided_it(self):
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  _Model(_always_true))
        self.assertEqual(code, 0, err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual({k: decision[k] for k in ("id", "name", "revision", "readme_path",
                                                   "readme_sha", "model", "response_model",
                                                   "label")},
                         {"id": 1, "name": "o/a", "revision": REVISION,
                          "readme_path": "README.md", "readme_sha": "s1",
                          "model": "gpt-6-luna", "response_model": "gpt-6-luna-2026-09-30",
                          "label": "true"})
        self.assertEqual(decision["limits_reached"], [])
        self.assertGreaterEqual(decision["usage"].pop("seconds"), 0)
        self.assertEqual(decision["usage"], {"requests": 1, "tool_rounds": 0, "linked_pages": 0,
                                             "input_tokens": 100,
                                             "cached_tokens": 5, "output_tokens": 10,
                                             "reasoning_tokens": 3, "summary_input_tokens": 0,
                                             "summary_output_tokens": 0, "attempts": 1,
                                             "responses": 1, "failed_responses": 0})
        parameters = label_cache.recorded("parameters", decision["parameters"],
                                             self.cache)
        self.assertEqual(parameters["prompt"],
                         classify_repos.PROMPT.read_text(encoding="utf-8"))
        self.assertEqual(parameters["summary"]["prompt"],
                         (classify_repos.HERE / "prompts" / "page-summary-prompt.md")
                         .read_text(encoding="utf-8"))
        self.assertEqual((parameters["summary"]["model"], parameters["request_tokens"]),
                         ("gpt-6-luna", 272_000))
        # An OpenAI model's requests send no temperature, top_p, seed or output limit, and its
        # decision records no sampling.
        self.assertEqual(parameters["classifying_model"],
                         {"model": "gpt-6-luna", "reasoning_effort": "low"})
        self.assertEqual(decision["sampling"], {})
        self.assertEqual([t["name"] for t in parameters["tools"]],
                         ["get_file_tree", "read_linked_page"])

    def test_the_description_goes_before_the_readme_and_its_absence_is_stated(self):
        model = _Model(_always_true)
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        frame = _repositories("o/a", "o/b", descriptions={"o/a": "Formats configuration files."})
        code, err = self.run_over(frame, github, model)
        self.assertEqual(code, 0, err)
        inputs = {_repository_of(r): r["input"] for r in model.requests}
        self.assertTrue(inputs["o/a"].startswith(
            '<repository>o/a</repository>\n<description>Formats configuration files.'
            '</description>\n<activity>'))
        self.assertTrue(inputs["o/b"].startswith(
            "<repository>o/b</repository>\n<description>GitHub shows no description for this "
            "repository.</description>\n"))
        decisions = {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}
        self.assertEqual((decisions["o/a"]["description"], decisions["o/b"]["description"]),
                         ("Formats configuration files.", ""))

    def test_the_activity_from_the_sampling_frame_goes_with_the_readme(self):
        """The research criterion tests whether code is maintained beyond its paper and used by
        others. Releases, issues and pull requests show such maintenance and use, and a README
        seldom states them."""
        model = _Model(_always_true)
        repositories = _repositories("o/a", "o/b")
        repositories[0]["activity"] = classify_repos.activity_of({
            "commits": 1463, "contributors": 6, "releases": 6, "totalIssues": 88,
            "totalPullRequests": 237, "stargazers": 268, "forks": 31,
            "createdAt": "2020-03-01T10:00:00"})
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        code, err = self.run_over(repositories, github, model)
        self.assertEqual(code, 0, err)
        inputs = {_repository_of(r): r["input"] for r in model.requests}
        self.assertIn("<activity>commits: 1,463; contributors: 6; releases: 6; issues: 88; "
                      "pull requests: 237; created: 2020-03-01</activity>",
                      inputs["o/a"])
        self.assertIn("<activity>The sampling frame records no activity for this "
                      "repository.</activity>", inputs["o/b"])
        decision = {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}["o/a"]
        self.assertEqual(decision["activity"]["releases"], 6)

    def test_the_activity_leaves_out_the_stars_and_the_forks(self):
        """The model reads neither the stars nor the forks. Both measure popularity, which no
        criterion names as evidence, and the share of the sample with a configuration artifact
        rises with the stars."""
        activity = classify_repos.activity_of({"commits": 12, "stargazers": 5000, "forks": 900,
                                               "lastCommit": "2026-09-01T00:00:00Z"})
        self.assertEqual(activity, {"commits": 12, "last commit": "2026-09-01"})

    def test_a_criterion_with_an_exception_is_decided_from_its_fields(self):
        """The model can decide OpenSSL's mirror criterion by its first half, described as a
        mirror, and leave out that the development happens on GitHub. Each fact is a separate
        field of the response, and the classification combines the fields. An unclear field
        leaves the criterion unclear where the criterion depends on that field. Where nothing
        shows whether others use a repository, a boolean would state that nobody does. A
        repository described as a mirror is a mirror unless it shows that it is developed here,
        as the reviewer labels mlichvar/chrony and openstack/charm-cinder from their
        descriptions. `no_source_code` applies in either of its two cases, a repository without
        the source code and software with the source code of its main part not public."""
        mirror, research, personal = "mirror", "research_artifact", "personal_or_single_script"
        source = "no_source_code"
        both = {"maintained": "true", "used_by_others": "true"}
        cases = [({mirror: {"described_as_mirror": "true", "invites_pull_requests_here": "true"}},
                  "true", [], []),
                 ({mirror: {"described_as_mirror": "true"}}, "false", [mirror], []),
                 ({mirror: {"described_as_mirror": "true",
                            "invites_pull_requests_here": "unclear"}},
                  "false", [mirror], []),
                 ({mirror: {"described_as_mirror": "unclear"}}, "unclear", [], [mirror]),
                 ({mirror: {"described_as_mirror": "false",
                            "invites_pull_requests_here": "unclear"}},
                  "true", [], []),
                 ({research: {"research_code": "true"}, USE: both}, "true", [], []),
                 ({research: {"research_code": "true"}, USE: {"maintained": "true"}},
                  "false", [research], []),
                 ({research: {"research_code": "true"},
                   USE: {"maintained": "true", "used_by_others": "unclear"}},
                  "unclear", [], [research]),
                 ({research: {"research_code": "true"}, USE: {"used_by_others": "unclear"}},
                  "false", [research], []),
                 ({research: {"dataset": "true"}, USE: both}, "false", [research], []),
                 ({research: {"research_code": "true"}, "inactive": {"applies": "true"},
                   USE: {"maintained": "true", "used_by_others": "unclear"}},
                  "false", ["inactive"], [research]),
                 # PierfrancescoSoffritti/doodles, "Web Experiments I do for fun", is maintained
                 # and used by others, and a toy project is excluded all the same.
                 ({personal: {"toy_project": "true"}, USE: both}, "false", [personal], []),
                 ({personal: {"toy_project": "unclear"}, USE: both}, "unclear", [], [personal]),
                 ({personal: {"own_use": "true"}, USE: both}, "true", [], []),
                 ({personal: {"own_use": "true"}, USE: {"maintained": "true"}},
                  "false", [personal], []),
                 ({personal: {"single_script": "true"}, USE: {"used_by_others": "true"}},
                  "false", [personal], []),
                 ({personal: {"single_script": "true"},
                   USE: {"maintained": "true", "used_by_others": "unclear"}},
                  "unclear", [], [personal]),
                 ({personal: {"toy_project": "false", "own_use": "false",
                              "single_script": "false"}},
                  "true", [], []),
                 # drawthingsai/draw-things-community contains source code, and its README places
                 # the main app in a private monorepo.
                 ({source: {"main_part_not_public": "true"}}, "false", [source], []),
                 ({source: {"contains_no_source_code": "true"}}, "false", [source], []),
                 ({source: {"contains_no_source_code": "unclear"}}, "unclear", [], [source]),
                 ({source: {"contains_no_source_code": "unclear", "main_part_not_public": "true"}},
                  "false", [source], [])]
        for facts, label, applied, unclear in cases:
            with self.subTest(facts=facts):
                self.setUp()
                model = _Model(lambda request, number, f=facts: _answer("o/a", facts=f))
                code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                          model)
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision["applied_criteria"],
                                  decision["unclear_criteria"]), (label, applied, unclear))
                self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], label)

    def test_a_mirror_that_merged_pull_requests_since_the_window_began_is_developed_here(self):
        """The cases follow openwrt/odhcp6c, which calls itself a mirror and merged pull requests
        on GitHub since the window began. AviatrixSystems/terraform-provider-aviatrix merged its
        latest pull request before the window began, and mlichvar/chrony merged none. The
        classification queries GitHub for the merges only when the model's response makes the
        repository a mirror. The pull requests of GitHub Apps do not count: zzzeek/alembic merged
        two of Dependabot's on 2026-05-31 and a person's before the window began."""
        cases = [(["2026-07-21T00:46:14Z", "2026-01-25T18:05:39Z"], "true", [], "2026-07-21T00:46:14Z"),
                 (["2026-02-23T21:12:04Z"], "false", ["mirror"], "2026-02-23T21:12:04Z"),
                 ([], "false", ["mirror"], ""),
                 ([("2026-05-31T14:04:14Z", "Bot"), ("2026-05-31T13:54:56Z", "Bot"),
                   "2019-08-05T14:24:18Z"], "false", ["mirror"], "2019-08-05T14:24:18Z")]
        for merges, label, applied, latest in cases:
            with self.subTest(merges=merges):
                self.setUp()
                model = _Model(lambda request, number: _answer(
                    "o/a", facts={"mirror": {"described_as_mirror": "true"}}))
                github = _GitHub({"o/a": (ENGLISH, "s1")}, merges={"o/a": merges})
                code, err = self.run_over(_repositories("o/a"), github, model)
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision["applied_criteria"]),
                                 (label, applied))
                self.assertEqual(decision["mirror_merges"], {
                    "latest_merge": latest, "window_start": THRESHOLDS.window_start,
                    "merged_since_window_start": label == "true",
                    "counted": classify_repos.MERGES_COUNTED})
        self.setUp()
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        code, err = self.run_over(_repositories("o/a"), github, _Model(
            lambda request, number: _answer("o/a")))
        self.assertEqual(code, 0, err)
        self.assertNotIn(("graphql", "o/a"), github.sent)

    def test_a_repository_that_github_marks_as_a_mirror_is_a_mirror(self):
        """gcc-mirror/gcc and luci/luci-py are pull mirrors of repositories elsewhere, which
        GitHub marks with `isMirror`, and neither their description nor their README calls them
        mirrors. GitHub's mark counts as a description as a mirror, and the exceptions of the
        `mirror` criterion still apply: A mirror that invites pull requests here, as
        bcgit/bc-java does, or that merged pull requests since the window began, is developed
        here. A reused label follows the mark of the current sampling frame."""
        cases = [({}, [], "false", ["mirror"]),
                 ({"mirror": {"invites_pull_requests_here": "true"}}, [], "true", []),
                 ({}, ["2026-07-21T00:46:14Z"], "true", [])]
        for facts, merges, label, applied in cases:
            with self.subTest(facts=facts, merges=merges):
                self.setUp()
                model = _Model(lambda request, number: _answer("o/a", facts=facts))
                github = _GitHub({"o/a": (ENGLISH, "s1")}, merges={"o/a": merges})
                frame = [{**_repositories("o/a")[0], "is_mirror": True}]
                code, err = self.run_over(frame, github, model)
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision["applied_criteria"]),
                                 (label, applied))
                self.assertEqual((decision["is_mirror"],
                                  decision["exclusions"]["mirror"]["described_as_mirror"]),
                                 (True, "false"))
        published = self.rows("repos_annotated.csv")[0]["justification"]
        self.assertNotIn("GitHub marks it", published)
        self.setUp()
        model = _Model(lambda request, number: _answer("o/a"))
        github = _GitHub({"o/a": (ENGLISH, "s1")}, merges={"o/a": []})
        code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, self.rows("repos_annotated.csv")[0]["engineered_project"]),
                         (0, "true"), err)
        code, err = self.run_over([{**_repositories("o/a")[0], "is_mirror": True}], github,
                                  model)
        self.assertEqual((code, len(model.requests)), (0, 1), err)
        published = self.rows("repos_annotated.csv")[0]
        self.assertEqual(published["engineered_project"], "false")
        self.assertIn("It counts as a mirror, because GitHub marks it as one and no pull "
                      "request was merged in it since the sampling window began on "
                      f"{THRESHOLDS.window_start}, other than pull requests that GitHub Apps "
                      "opened.", published["justification"])

    def test_the_published_justification_names_what_the_checks_of_github_decided(self):
        """Because the model never sees the merges of a mirror or the fork heuristic's verdict,
        its justification can say that an exception holds or that no criterion applies while the
        label is `false`. repos_annotated.csv adds the reason, and the stored decision keeps the
        model's own justification."""
        model = _Model(lambda request, number: _answer(
            "o/a", facts={"mirror": {"described_as_mirror": "true"}}))
        github = _GitHub({"o/a": (ENGLISH, "s1")}, merges={"o/a": ["2026-02-23T21:12:04Z"]})
        code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual(code, 0, err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        published = self.rows("repos_annotated.csv")[0]["justification"]
        self.assertTrue(published.startswith(decision["justification"]))
        self.assertIn("It counts as a mirror, because no pull request was merged in it since "
                      f"the sampling window began on {THRESHOLDS.window_start}, other than pull "
                      "requests that GitHub Apps opened.", published)
        fork = {"justification": "No exclusion criterion applies.", "applied_criteria": ["fork"],
                "label": "false", "fork_of": "Financial Times polyfill-library",
                "fork": {"upstream": "Financial-Times/polyfill-library", "independent": False}}
        self.assertEqual(classify_repos.published_justification(fork),
                         "No exclusion criterion applies. The classification labels it false, "
                         "because the criterion fork applies. The fork heuristic finds that it "
                         "has not become a project of its own apart from its upstream, "
                         "Financial-Times/polyfill-library.")
        # openjdk/jdk11u: the model wrote that its disabled issues exclude it. No criterion
        # applies, and the label is the model's judgment.
        self.assertEqual(classify_repos.published_justification(
            {"justification": "Issues are disabled, so it is excluded.", "applied_criteria": [],
             "unclear_criteria": [], "label": "true"}),
            "Issues are disabled, so it is excluded. The classification labels it true, because "
            "no criterion applies and the model judged the evidence of software engineering "
            "practices true.")
        self.assertEqual(classify_repos.published_justification(
            {"justification": "A tool.", "applied_criteria": [],
             "unclear_criteria": ["inactive", "mirror"], "label": "unclear"}),
            "A tool. The classification labels it unclear, because the criteria inactive and "
            "mirror are unclear.")

    def test_merges_since_the_window_began_lift_mirror_and_not_moved(self):
        """GoogleChromeLabs/chromium-bidi's README states "the source was moved to Chromium" and
        "Please contribute there", and its latest pull request that a person opened was merged on
        2026-08-20, after the window of run 2026-10-23T093036Z began. Merges since the window
        began contradict `mirror`, and an older merge contradicts neither, but no merge
        contradicts a README that states the move, so `moved` applies in both cases."""
        facts = {"mirror": {"described_as_mirror": "true"}, "moved": {"applies": "true"}}
        cases = [(["2026-08-20T13:18:49Z"], "false", ["moved"]),
                 (["2026-02-23T21:12:04Z"], "false", ["moved", "mirror"])]
        for merges, label, applied in cases:
            with self.subTest(merges=merges):
                self.setUp()
                model = _Model(lambda request, number: _answer("o/a", facts=facts))
                github = _GitHub({"o/a": (ENGLISH, "s1")}, merges={"o/a": merges})
                code, err = self.run_over(_repositories("o/a"), github, model)
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision["applied_criteria"]),
                                 (label, applied))
                self.assertEqual(decision["exclusions"]["moved"]["applies"], "true")

    def test_a_guide_that_limits_pull_requests_to_collaborators_excludes_nothing(self):
        """vmware/packer-plugin-vsphere's contribution guide limits pull requests to
        collaborators and keeps issues for the maintainers' work. Development by a core team, or
        through Gerrit, a mailing list or another repository, is no exclusion criterion, so the
        response format that the model is offered has no field for it, and the label is the
        model's judgment of the evidence."""
        model = _Model(lambda request, number: _answer("o/a"))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        offered = next(tool for tool in model.requests[0]["tools"]
                       if tool.get("name") == classify_repos.CHECKLIST_TOOL_NAME)
        criteria = offered["parameters"]["properties"]["exclusions"]["properties"]
        self.assertNotIn("closed_to_contributions", criteria)
        self.assertFalse([fact for facts in criteria.values() for fact in facts["properties"]
                          if fact.startswith("refuses_")])
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual(decision["label"], "true")

    def test_a_github_response_without_the_merges_is_no_mirror(self):
        """GitHub reports an exhausted GraphQL rate limit with HTTP 200, an error and no data.
        Read as a repository that merged no pull request, such a response would make the
        repository a mirror. The step waits for the rate limit to reset, and a response with
        another error fails the repository."""
        class Answers(_GitHub):
            def __init__(self, first):
                super().__init__({"o/a": (ENGLISH, "s1")},
                                 merges={"o/a": ["2026-07-21T00:46:14Z"]})
                self.first = first

            def post(self, url, headers=None, json=None, timeout=None):
                if self.first is not None:
                    first, self.first = self.first, None
                    self.sent.append(("graphql", "o/a"))
                    return first
                return super().post(url, headers, json, timeout)

        spent = _Response(200, {"data": None, "errors": [
            {"type": "RATE_LIMITED", "message": "API rate limit exceeded for user ID 1."}]},
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"})
        broken = _Response(200, {"data": None, "errors": [
            {"type": "SERVICE_UNAVAILABLE", "message": "Something went wrong"}]})
        mirror = lambda request, number: _answer(  # noqa: E731
            "o/a", facts={"mirror": {"described_as_mirror": "true"}})
        github = Answers(spent)
        code, err = self.run_over(_repositories("o/a"), github, _Model(mirror))
        self.assertEqual(code, 0, err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["applied_criteria"]), ("true", []))
        self.assertEqual(github.sent.count(("graphql", "o/a")), 2)
        self.assertIn("A GitHub access token reached its GraphQL rate limit", err)
        self.setUp()
        with mock.patch.object(classify_repos, "RETRY_ROUNDS", 0):
            code, err = self.run_over(_repositories("o/a"), Answers(broken), _Model(mirror))
        self.assertEqual(code, 1)
        self.assertIn("has no repository (Something went wrong)", err)
        self.assertEqual(list(label_cache.all_decisions(self.cache)), [])

    def test_a_malformed_response_for_the_merges_fails_the_repository(self):
        """A proxy can return HTTP 200 with its text in `errors`, or a `data` that is no object.
        Such a body fails the repository for a retry round, and the run goes on. An
        AttributeError would stop the run at every resume that got the same body."""
        class Answers(_GitHub):
            def __init__(self, first):
                super().__init__({"o/a": (ENGLISH, "s1")})
                self.first = first

            def post(self, url, headers=None, json=None, timeout=None):
                return self.first

        mirror = lambda request, number: _answer(  # noqa: E731
            "o/a", facts={"mirror": {"described_as_mirror": "true"}})
        for body, said in (({"data": None, "errors": ["502 Bad Gateway"]}, "502 Bad Gateway"),
                           ({"data": None, "errors": "502 Bad Gateway"}, "502 Bad Gateway"),
                           ({"data": None, "errors": [None, 502]}, "502"),
                           ({"data": "502 Bad Gateway"}, "no error"),
                           ({"data": {"repository": "502 Bad Gateway"}}, "no error")):
            with self.subTest(body=body):
                self.setUp()
                with mock.patch.object(classify_repos, "RETRY_ROUNDS", 0):
                    code, err = self.run_over(_repositories("o/a"),
                                              Answers(_Response(200, body)), _Model(mirror))
                self.assertEqual(code, 1, err)
                self.assertIn(f"has no repository ({said})", err)
                self.assertNotIn("Traceback", err)
                self.assertIn("1 for GitHub's response (0 of them a GraphQL response with errors "
                              "in place of a field)", err)
                self.assertEqual(list(label_cache.all_decisions(self.cache)), [])

    def test_a_response_with_errors_in_place_of_the_merges_fails_the_repository(self):
        """GitHub returns the repository with an error and with null in place of its pull
        requests, their list or one of them, as for a part of a query that timed out. Read as a
        repository that merged no pull request, or none after the one that GitHub returned, the
        response would make the mirror criterion apply. The repository fails for a retry round
        instead."""
        class Answers(_GitHub):
            def __init__(self, first):
                super().__init__({"o/a": (ENGLISH, "s1")})
                self.first = first

            def post(self, url, headers=None, json=None, timeout=None):
                return self.first

        mirror = lambda request, number: _answer(  # noqa: E731
            "o/a", facts={"mirror": {"described_as_mirror": "true"}})
        error = {"type": "SERVICE_UNAVAILABLE", "message": "timedout",
                 "path": ["repository", "pullRequests"]}
        for repository in ({"pullRequests": None}, {"pullRequests": {"nodes": None}},
                           {"pullRequests": {"nodes": [{"mergedAt": "2025-01-10T00:00:00Z"},
                                                       None]}}):
            with self.subTest(repository=repository):
                self.setUp()
                body = {"data": {"repository": repository}, "errors": [error]}
                with mock.patch.object(classify_repos, "RETRY_ROUNDS", 0):
                    code, err = self.run_over(_repositories("o/a"),
                                              Answers(_Response(200, body)), _Model(mirror))
                self.assertEqual(code, 1, err)
                self.assertIn("GitHub's response for the merged pull requests of o/a has errors "
                              "in place of the pull requests (timedout)", err)
                self.assertIn("1 repositories have no decision, 1 for GitHub's response (1 of "
                              "them a GraphQL response with errors in place of a field), 0 for "
                              "the model's response and 0 for an unexpected error", err)
                self.assertEqual(list(label_cache.all_decisions(self.cache)), [])

    def test_the_words_by_language_decide_whether_the_readme_is_mainly_english(self):
        """The model counts the README's words of prose by language, and the classification
        finds the README mainly English when English has more than half of them. A model that
        judges the language itself can call the README of bbossgroups/bboss-elasticsearch mainly
        English, with most of its prose in Chinese, and also the README of
        mercadopago/cart-prestashop-7, which repeats its English warning and documentation in
        Spanish and Portuguese."""
        def count(*pairs):
            return [{"language": language, "words": words} for language, words in pairs]
        cases = [(count(("English", 73), ("Chinese", 125)), "false", ["not_english"]),
                 (count(("English", 125), ("Spanish", 73), ("Portuguese", 73)), "false",
                  ["not_english"]),
                 (count(("English", 50), ("German", 50)), "false", ["not_english"]),
                 (count(("English", 125), ("Spanish", 73)), "true", []),
                 (count(("english", 30), ("EN", 30), ("French", 50)), "true", []),
                 ([], "true", []),
                 # Other names of English, and entries that name no natural language.
                 (count(("English (US)", 40), ("en-US", 10), ("eng", 10), ("French", 50)),
                  "true", []),
                 (count(("Code", 500), ("", 80), ("English", 60), ("German", 40)), "true", []),
                 (count(("English", 50), ("British English", 60)), "true", []),
                 (count(("English (US)", 30), ("Australian English", 30), ("French", 50)), "true",
                  []),
                 (count(("en-Latn-US", 60)), "true", []),
                 (count(("eng-US", 60)), "true", []),
                 (count(("en (US)", 60)), "true", []),
                 (count(("Japanese", 20), ("en-Latn-US", 40)), "true", []),
                 # English named inside the entry of another language is not English.
                 (count(("Non-English", 60), ("English", 40)), "false", ["not_english"]),
                 (count(("Not English", 80), ("English", 30)), "false", ["not_english"]),
                 (count(("Chinese (with English terms)", 60), ("English", 50)), "false",
                  ["not_english"]),
                 (count(("Chinese/English", 100), ("English", 20)), "false", ["not_english"]),
                 # Words of no named language that alone take the majority from English leave
                 # the language unclear, because the README passed Lingua's check of English.
                 (count(("Unknown", 300), ("English", 20)), "unclear", []),
                 (count(("Mixed", 50), ("English", 40), ("German", 30)), "unclear", []),
                 (count(("Other", 10), ("English", 20), ("German", 40)), "false",
                  ["not_english"]),
                 # Lingua finds the README English, and the count names no English. The
                 # classification cannot tell which language a name that it does not read stands
                 # for.
                 (count(("Englisch", 60)), "unclear", []),
                 (count(("Japanese", 20), ("Englisch", 40)), "unclear", []),
                 (count(("Chinese/English", 60)), "unclear", []),
                 (count(("Non-English", 60)), "unclear", []),
                 (count(("so-called", 60)), "unclear", []),
                 # Words of no named language alone name no language either, and neither does an
                 # entry with 0 words.
                 (count(("Unknown", 60)), "unclear", []),
                 (count(("English", 0), ("Unknown", 60)), "unclear", []),
                 (count(("Japanese", 0), ("Unknown", 60)), "unclear", []),
                 # The count of SpriteStudio/SS6PlayerForUnrealEngine4 and of EsupPortail/esup-sgc:
                 # The model names a real language and no English, against Lingua's English.
                 (count(("Japanese", 23)), "false", ["not_english"]),
                 (count(("French", 1)), "false", ["not_english"]),
                 (count(("Chinese (Simplified)", 40)), "false", ["not_english"]),
                 (count(("zh-CN", 40)), "false", ["not_english"]),
                 (count(("jpn", 40)), "false", ["not_english"]),
                 (count(("Norwegian", 40)), "false", ["not_english"]),
                 (count(("Code", 90), ("Japanese", 20)), "false", ["not_english"]),
                 (count(("Japanese", 20), ("Unknown", 40)), "false", ["not_english"])]
        for words, label, applied in cases:
            with self.subTest(words=words):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))
                model = _Model(lambda request, number, w=words: _answer("o/a", words=w))
                code, err = self.run_over(_repositories("o/a"),
                                          _GitHub({"o/a": (ENGLISH, "s1")}), model)
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision["applied_criteria"],
                                  decision["readme_words_by_language"]), (label, applied, words))
                if label == "unclear":
                    self.assertEqual(decision["unclear_criteria"], ["not_english"])

    def test_a_count_of_words_that_is_no_list_of_numbers_is_requested_again(self):
        """A count of the README's words is a list of languages, each with a whole number of
        words. Another form is no count from which the classification can decide."""
        for bad in ([{"language": "English", "words": "many"}],
                    [{"language": "English", "words": -3}],
                    [{"language": "English", "words": 4.5}],
                    [{"language": "English", "words": True}],
                    [{"words": 40}], "English"):
            with self.subTest(bad=bad):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))

                def script(request, number, bad=bad):
                    answer = json.loads(_answer("o/a")[0].arguments)
                    if number == 1:
                        answer["readme_words_by_language"] = bad
                    return [_call(classify_repos.CHECKLIST_TOOL_NAME, answer)]
                model = _Model(script)
                code, err = self.run_over(_repositories("o/a"),
                                          _GitHub({"o/a": (ENGLISH, "s1")}), model)
                self.assertEqual((code, len(model.requests)), (0, 2), err)

    def test_a_readme_without_prose_is_english_whatever_the_model_says(self):
        """vAmigaWeb's README has no prose, and the language check reads it as English. The
        model can still count words of another language for such a README."""
        no_prose = '<p align="center"><img src="logo.png"></p>\n\n![](shot.png)\n'
        for words in ([{"language": "German", "words": 12}], []):
            with self.subTest(words=words):
                self.cache = Path(tempfile.mkdtemp(dir=self.tmp))
                model = _Model(lambda request, number, w=words: _answer("o/a", words=w))
                described = _repositories("o/a", descriptions={
                    "o/a": "A command line tool that formats configuration files."})
                code, err = self.run_over(described, _GitHub({"o/a": (no_prose, "s1")}), model)
                self.assertEqual(code, 0, err)
                decision = next(label_cache.all_decisions(self.cache))[1]
                self.assertEqual((decision["label"], decision["unclear_criteria"],
                                  decision["applied_criteria"]), ("true", [], []))

    def test_a_response_outside_true_false_and_unclear_is_requested_again(self):
        def script(request, number):
            answer = json.loads(_answer("o/a")[0].arguments)
            if number == 1:
                answer["exclusions"]["mirror"]["invites_pull_requests_here"] = True
            return [_call(classify_repos.CHECKLIST_TOOL_NAME, answer)]
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)

    def test_the_service_tier_goes_with_every_request_and_into_the_decision(self):
        """The service tier is flex unless a run requests another tier, because the run of the
        pipeline on the NAS passes no option."""
        def script(request, number):
            return [_call("get_file_tree", {})] if number == 1 else _answer("o/a")
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual([r.get("service_tier") for r in model.requests], ["flex", "flex"])
        model = _Model(_always_true)
        other = ENGLISH.replace("formats configuration files", "checks links")
        self.run_over(_repositories("o/b"), _GitHub({"o/b": (other, "s2")}), model,
                      service_tier="default", retrieval="2026-11-05T101430Z")
        self.assertEqual(model.requests[0].get("service_tier"), "default")

    def test_a_criterion_that_applies_makes_the_label_false_whatever_the_engineering(self):
        """The prompt gives a criterion that applies precedence over the evidence of engineering.
        The model can break that precedence by weighing tests and CI against an exclusion and
        returning true, and the classification applies the precedence to the model's checklist."""
        model = _Model(lambda request, number: _answer("o/a", "true",
                                                       applies={"website", "inactive"}))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "false")
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["model_label"], decision["applied_criteria"]),
                         ("false", "true", ["inactive", "website"]))
        self.assertEqual(decision["exclusions"]["website"],
                         {"applies": "true", "evidence": "The README on website."})

    def test_a_repository_without_a_stated_purpose_is_labeled_false(self):
        """A README such as munin/munin's has instructions to install and run something and no
        word on what it does. A purpose that the model states without a quotation does not
        count."""
        model = _Model(lambda request, number: _answer(
            "o/a", "true", purpose="It imports statistics into a database.", purpose_quote=""))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["applied_criteria"]),
                         ("false", ["no_purpose"]))

    def test_a_purpose_quoted_from_nowhere_is_requested_again(self):
        """The model can state a purpose for munin/munin that its README does not name."""
        quotes = ["A database-backed system for importing stats.",
                  "a command line tool ... formats configuration files"]
        model = _Model(lambda request, number: _answer("o/a", purpose_quote=quotes[number - 1]))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["purpose_quote"], len(model.requests)),
                         ("true", quotes[1], 2))

    def test_a_purpose_quote_that_no_attempt_finds_in_the_readme_is_unclear(self):
        """libcgroup's README states no purpose, and the model can write a new purpose in every
        attempt. Requested again each time, the repository would stay without a decision. The
        label false would be wrong for a README that states its purpose in a form that the check
        does not read, and the purpose is therefore unclear."""
        model = _Model(lambda request, number: _answer(
            "o/a", purpose_quote=f"User-space tools, version {number}."))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["applied_criteria"],
                          decision["unclear_criteria"], decision["purpose_quote"],
                          decision["purpose_quote_refused"], len(model.requests)),
                         ("unclear", [], ["no_purpose"], "", f"User-space tools, version "
                          f"{classify_repos.ATTEMPTS}.", classify_repos.ATTEMPTS))
        self.assertIn(f"WARNING repository_labeling: None of the {classify_repos.ATTEMPTS} "
                      f"attempts at o/a succeeded, and the model's purpose quote is in neither "
                      f"the description, the README nor the purpose sentence of a page that "
                      f"the description, the homepage field or the README links to. Recorded "
                      f"the purpose as unclear", err)

    def test_a_response_without_every_criterion_is_tried_again(self):
        def script(request, number):
            if number == 1:
                answer = json.loads(_answer("o/a")[0].arguments)
                del answer["exclusions"]["website"]
                return [_call(classify_repos.CHECKLIST_TOOL_NAME, answer)]
            return _answer("o/a", "unclear")
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "unclear")

    def test_no_source_code_is_requested_in_its_two_fields(self):
        """`no_source_code` has a field for a repository without the source code and one for
        software with the source code of its main part not public, so that a reviewer sees which
        case the model found. The response format requests both, and a response that sets the
        criterion in one `applies` field lacks a field and is requested again."""
        def script(request, number):
            if number == 1:
                answer = json.loads(_answer("o/a")[0].arguments)
                answer["exclusions"]["no_source_code"] = {"applies": "true",
                                                          "evidence": "The app is closed."}
                return [_call(classify_repos.CHECKLIST_TOOL_NAME, answer)]
            return _answer("o/a", facts={"no_source_code": {"main_part_not_public": "true"}})
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model)
        self.assertEqual(code, 0, err)
        [checklist] = [tool for tool in model.requests[0]["tools"]
                       if tool["name"] == classify_repos.CHECKLIST_TOOL_NAME]
        criterion = (checklist["parameters"]["properties"]["exclusions"]["properties"]
                     ["no_source_code"])
        self.assertEqual(criterion["required"],
                         ["contains_no_source_code", "main_part_not_public", "evidence"])
        self.assertEqual(len(model.requests), 2)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual(decision["exclusions"]["no_source_code"],
                         {"contains_no_source_code": "false", "main_part_not_public": "true",
                          "evidence": "The README on no_source_code.", "applies": "true"})
        self.assertEqual((decision["label"], decision["applied_criteria"]),
                         ("false", ["no_source_code"]))

    def test_the_issues_and_the_metadata_are_of_the_repository_and_its_issues_can_be_read(self):
        """The issues and pull requests distinguish a mirror from a repository developed on GitHub.
        The classification reads them and the metadata for the repository being labeled. The
        model may read an issue or a pull request of the repository, as it reads a link of the
        README, but no issue or pull request of another repository."""
        def script(request, number):
            if number == 1:
                return [_call("get_issues_and_pull_requests", {"repo_name": "other/repository"},
                              "c0"),
                        _call("read_linked_page", {"url": "https://github.com/o/a/issues/9"},
                              "c1"),
                        _call("read_linked_page", {"url": "https://github.com/x/y/pull/3"},
                              "c2")]
            return _answer("o/a")
        model, bridge = _Model(script), _Bridge()
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model, bridge)
        self.assertEqual(code, 0, err)
        self.assertEqual([call for call in bridge.calls if call[0] != "get_file_tree"], [
            ("get_issues_and_pull_requests", {"repo_name": "o/a"}),
            ("get_repository_metadata", {"repo_name": "o/a"}),
            ("read_linked_page", {"url": "https://github.com/o/a/issues/9", "repo_name": "o/a"})])
        self.assertEqual(model.requests[1]["input"][2]["output"], classify_repos.REFUSED_LINK)


def _reasoning(text="I read the README."):
    return SimpleNamespace(type="reasoning", id="rs_1", summary=[], encrypted_content=text)


def _text_message(text):
    """An output item of the type message with `text` as its one output_text part, with the
    fields of the SDK's ResponseOutputMessage. A final response holds such an item when the
    model ends its turn with a message instead of a call of submit_classification."""
    return SimpleNamespace(type="message", id="msg_1", role="assistant", status="completed",
                           content=[SimpleNamespace(type="output_text", text=text,
                                                    annotations=[])])


# The text of a message that a model wrote instead of the call of submit_classification, with a
# line break, and longer than twice KEPT_MESSAGE_CHARS.
WRITTEN = ("I have read the README, the outline and the metadata of the repository.\n"
           + "It holds microbenchmarks of caches and of the latency of memory. " * 8
           + "I will submit the classification next.")


class TestOpenRouter(_RunCase):
    """A model that OpenRouter serves, called on its pinned endpoint through OpenRouter's
    stateless Responses API."""

    def test_each_request_sends_the_whole_conversation_to_the_pinned_endpoint(self):
        """OpenRouter's Responses API "is stateless" and rejects a non-null
        `previous_response_id` (openrouter.ai/docs/api/reference/responses/overview). The request
        after a tool round therefore sends the first input, every output item of the response
        with its reasoning item, and the outputs of the round."""
        def script(request, number):
            if number == 1:
                return [_reasoning(), _call("read_linked_page", {"url": "docs/guide.md"})]
            return [_reasoning("I read the guide."), *_answer(_repository_of(request))]
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        first, second = model.requests
        for request in (first, second):
            self.assertNotIn("previous_response_id", request)
            self.assertNotIn("service_tier", request)
            self.assertEqual(request["extra_body"],
                             {"provider": {"order": ["morph/fp8"], "allow_fallbacks": False,
                                           "quantizations": ["fp8"],
                                           "require_parameters": True}, "seed": 7})
            self.assertEqual((request["model"], request["reasoning"], request["temperature"],
                              request["top_p"]),
                             ("deepseek/deepseek-v4.1-flash", {"effort": "low"}, 0.6, 0.95))
        [opening] = first["input"]
        self.assertEqual((opening["role"], opening["type"]), ("user", "message"))
        self.assertIn("<repository>o/a</repository>", opening["content"])
        self.assertEqual(second["input"][0], opening)
        self.assertEqual(second["input"][1], {"type": "reasoning", "id": "rs_1", "summary": [],
                                              "encrypted_content": "I read the README."})
        self.assertEqual((second["input"][2]["type"], second["input"][2]["name"]),
                         ("function_call", "read_linked_page"))
        self.assertEqual((second["input"][3]["type"], second["input"][3]["call_id"]),
                         ("function_call_output", "call-1"))
        self.assertIn("A summary of", second["input"][3]["output"])
        self.assertIn("The model deepseek/deepseek-v4.1-flash (deepseek-ai/DeepSeek-V4.1-Flash) "
                      "on the endpoint morph/fp8 in fp8 through OpenRouter labels at reasoning "
                      "effort low, temperature 0.6, top_p 0.95, seed 7 and an output limit of "
                      "65,536 tokens with", err)
        self.assertIn("After a failed response at a repository, the model labels the "
                      "repository at temperature 1.0 with an output limit of 262,144 tokens", err)

    def test_the_classifying_requests_set_the_temperature_and_the_seed_of_an_openrouter_model(self):
        """DeepSeek V4.1 Flash labels at the settings of its record by default, effort high,
        temperature 0.6, top_p 0.95 and seed 7, and `--temperature`, `--top-p`, `--seed` and
        `--reasoning-effort` set other values. `--endpoint-default-temperature` sends neither a
        temperature, a top_p nor a seed. An OpenAI model's requests send none of them, because
        OpenAI's reasoning models reject a temperature, and an OpenAI model given one of them is
        refused."""
        def labeled(**options):
            self.setUp()
            model = _Model(_always_true)
            code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                      model, **options)
            self.assertEqual(code, 0, err)
            [request] = model.requests
            return request
        request = labeled(**DEEPSEEK, temperature=0.5, top_p=0.9, seed=3)
        self.assertEqual((request["temperature"], request["top_p"], request["extra_body"]["seed"],
                          request["reasoning"]), (0.5, 0.9, 3, {"effort": "low"}))
        request = labeled(**DEEPSEEK, reasoning_effort=None)
        self.assertEqual((request["temperature"], request["top_p"], request["extra_body"]["seed"],
                          request["reasoning"], request["max_output_tokens"]),
                         (0.6, 0.95, 7, {"effort": "high"}, 65_536))
        request = labeled(**DEEPSEEK, endpoint_default_temperature=True)
        for name in ("temperature", "top_p"):
            self.assertNotIn(name, request)
        self.assertNotIn("seed", request["extra_body"])
        self.assertEqual(request["max_output_tokens"], 65_536)
        request = labeled()
        for name in ("temperature", "top_p", "max_output_tokens"):
            self.assertNotIn(name, request)
        self.assertNotIn("seed", request.get("extra_body") or {})
        for options in (["--temperature", "0.2"], ["--top-p", "0.95"], ["--seed", "7"],
                        ["--endpoint-default-temperature"]):
            with self.subTest(options=options), self.assertRaises(ValueError):
                classify_repos.run_settings(classify_repos.build_parser().parse_args(
                    ["--model", "gpt-6-luna", *options]))
        with self.assertRaisesRegex(ValueError, "takes neither --temperature, --top-p nor"):
            classify_repos.run_settings(classify_repos.build_parser().parse_args(
                ["--endpoint-default-temperature", "--top-p", "0.9"]))

    def test_the_tokens_are_counted_under_the_pinned_endpoint_that_sets_their_price(self):
        """OpenRouter has none of OpenAI's service tiers, and the endpoints of a model have prices
        of their own. Without fallbacks, the pinned endpoint serves every request."""
        model = _Billed(_always_true, tiers=lambda number: "default")
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        [row] = label_cache.read_tokens(self.cache, self.index()[-1]["run_id"])
        self.assertEqual({key: row[key] for key in label_cache.TOKEN_KEY},
                         {"role": "classifying_model", "model": "deepseek/deepseek-v4.1-flash",
                          "requested_tier": "morph/fp8", "service_tier": "morph/fp8"})

    def test_an_endpoint_that_does_not_respond_pauses_the_run_on_it(self):
        """"There is no available model provider that meets your routing requirements" (HTTP
        503) and a request that no endpoint serves (HTTP 404) are failures of the service, as a
        lack of flex capacity is. The run pauses and sends the same request to the same endpoint
        again, rather than using the attempts of the repositories."""
        names = ("o/a", "o/b")
        # As in the pause for a lack of flex capacity, the first failure uses an attempt, and so
        # does the failure after the pauses that use no attempt. No repository uses every attempt.
        failures = classify_repos.PAUSES_BEFORE_ATTEMPTS + 2

        def script(request, number):
            if number <= failures:
                raise _openrouter_error(openai.NotFoundError if number % 2
                                        else openai.InternalServerError,
                                        404 if number % 2 else 503)
            return _answer(_repository_of(request))
        model = _Together(script, names)
        with _short_pauses():
            code, err = self.run_over(_repositories(*names),
                                      _GitHub({n: (ENGLISH, n) for n in names}), model,
                                      **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertIn("Pausing every worker", err)
        self.assertNotIn("Retry round", err)
        self.assertEqual(len(model.requests), failures + 2)
        self.assertTrue(all(request["extra_body"]["provider"]["order"] == ["morph/fp8"]
                            for request in model.requests))


class TestBlockedContent(_RunCase):
    """OpenRouter blocks content that its moderation or a guardrail flags (HTTP 403)."""

    def test_a_readme_that_openrouter_blocks_fails_that_repository_alone(self):
        """The same README is blocked in every attempt and retry round. Read as a key without
        access, the block would stop the run, and every later run would stop at the same
        README."""
        names = ["o/a", "o/b", "o/c"]

        def script(request, number):
            if _repository_of(request) == "o/b":
                raise _openrouter_error(openai.PermissionDeniedError, 403, {
                    "patterns": ["ignore all previous instructions"]})
            return _answer(_repository_of(request))
        model = _Model(script)
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model, **DEEPSEEK)
        self.assertEqual(code, 1)
        self.assertNotIn("Stopped the run", err)
        self.assertEqual(sum(1 for r in model.requests if _repository_of(r) == "o/b"), 1)
        self.assertIn("a guardrail of OpenRouter blocked the request for the patterns "
                      "'ignore all previous instructions'", err)
        self.assertEqual(sorted(name for _, d in label_cache.all_decisions(self.cache)
                                for name in [d["name"]]), ["o/a", "o/c"])


def _parameters_with_provider(found):
    """The block of an endpoint as the classification recorded it before 2026-11-04, with the
    provider of the endpoint, as `endpoints.Endpoint.parameters` wrote it then."""
    if not found.openrouter:
        return {"model": found.model}
    return {"model": found.model, "hugging_face_id": found.hugging_face_id,
            "provider": found.provider, "quantization": found.quantization}


class TestTheProviderIsNoParameter(_RunCase):
    """The maintainer decided on 2026-11-04 that the provider that serves a model is no
    classification parameter. A run under the parameters that the classification recorded
    before, with the provider, such as the run of the NAS under 95c7ac80cbec, resumes under the
    parameters without it and labels none of its repositories again, and a finished run's
    decisions are reused. Parameters that differ in the weights, the quantization or a setting
    label again."""

    @staticmethod
    def earlier():
        """The code before 2026-11-04, which recorded the provider of each model."""
        return mock.patch.object(classify_repos.endpoints.Endpoint, "parameters",
                                 _parameters_with_provider)

    def test_a_run_stopped_under_the_parameters_with_the_provider_resumes_and_labels_nothing_again(
            self):
        """The earlier code labels o/a and o/b and then stops at o/c, whose request OpenRouter
        refuses. A kill leaves the run running, with its decisions in its partial file alone.
        The current code resumes that run, keeps the decisions of o/a and o/b and labels o/c
        alone."""
        names = ("o/a", "o/b", "o/c")
        github = _GitHub({name: (ENGLISH, name) for name in names})

        def refused_at_c(request, number):
            if _repository_of(request) == "o/c":
                raise _openrouter_error(openai.AuthenticationError, 401)
            return _answer(_repository_of(request))
        with self.earlier():
            code, err = self.run_over(_independent(*names), github, _Model(refused_at_c),
                                      workers=1, **DEEPSEEK)
        self.assertEqual(code, 1, err)
        [row] = self.index()
        earlier = row["parameters"]
        self.assertEqual(label_cache.recorded("parameters", earlier,
                                              self.cache)["classifying_model"]["provider"],
                         "morph/fp8")
        label_cache.put_index_row({"run_id": row["run_id"], "outcome": "running",
                                   "decisions": "", "decisions_sha256": ""}, self.cache)
        model = _Model(_always_true)
        code, err = self.run_over(_independent(*names), github, model, workers=1, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(request) for request in model.requests], ["o/c"])
        [row] = self.index()
        self.assertEqual((row["outcome"], row["labeled"], row["parameters"]),
                         ("finished", "3", earlier))
        self.assertIn(f"Run {row['run_id']} resumed over 3 repositories", err)
        current = label_cache.ParameterKeys(self.cache)(earlier)
        self.assertNotEqual(current, earlier)
        self.assertIn(f"Run {row['run_id']} started under the classification parameters "
                      f"{earlier[:12]}, which differ from the current parameters {current[:12]} "
                      f"only in the provider of a model.", err)
        self.assertEqual({d["name"]: d["parameters"]
                          for _, d in label_cache.all_decisions(self.cache)},
                         {"o/a": earlier, "o/b": earlier, "o/c": current})

    def test_the_decisions_of_a_finished_run_with_the_provider_are_reused(self):
        names = ("o/a", "o/b")
        github = _GitHub({name: (ENGLISH, name) for name in names})
        with self.earlier():
            code, err = self.run_over(_independent(*names), github, _Model(_always_true),
                                      **DEEPSEEK)
        self.assertEqual(code, 0, err)
        model = _Model(_always_true)
        code, err = self.run_over(_independent(*names), github, model, new_run=True, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])
        self.assertEqual(self.index()[-1]["reused"], "2")

    def test_parameters_that_differ_in_the_weights_the_quantization_or_a_setting_label_again(self):
        names = ("o/a", "o/b")
        for option, value in (("quantization", "mxfp4"),
                              ("hugging_face_id", "deepseek-ai/DeepSeek-V4.1-Flash-0911"),
                              ("temperature", 0.5), ("reasoning_effort", "high")):
            with self.subTest(option=option):
                self.setUp()
                github = _GitHub({name: (ENGLISH, name) for name in names})
                with self.earlier():
                    code, err = self.run_over(_independent(*names), github,
                                              _Model(_always_true), **DEEPSEEK)
                self.assertEqual(code, 0, err)
                model = _Model(_always_true)
                code, err = self.run_over(_independent(*names), github, model,
                                          **{**DEEPSEEK, option: value})
                self.assertEqual(code, 0, err)
                self.assertEqual(len(model.requests), 2)

    def test_a_label_of_the_second_model_recorded_with_its_provider_applies_alike(self):
        """A decision of the second model before 2026-11-04 records its provider beside its
        settings. The provider is no setting, and the decision applies to a run with the same
        second model, whichever provider served it."""
        classifier = classify_repos.Classifier(None, _Bridge(), DEEPSEEK["model"], "",
                                               classify_repos.endpoints.PRODUCTION.settings, "p",
                                               endpoint=classify_repos.endpoints.PRODUCTION,
                                               higher=classify_repos.endpoints.PRODUCTION
                                               .settings.higher())
        fallbacks = classify_repos.run_fallbacks(classifier, classify_repos.endpoints.SECOND_MODEL)
        recorded = {**_parameters_with_provider(classify_repos.endpoints.SECOND_MODEL),
                    **classify_repos.endpoints.SECOND_MODEL.settings.parameters(),
                    "reason": "output_limit", "error": "stopped",
                    "classifying_model_failures": {}}
        self.assertTrue(classify_repos.applies({"second_model": recorded}, fallbacks))
        self.assertFalse(classify_repos.applies({"second_model": {**recorded, "temperature": 0.9}},
                                                fallbacks))


def _rate_limited(model="deepseek/deepseek-v4.1-flash"):
    """OpenRouter's HTTP 429 for a model that its pinned provider rate-limits, as Morph's
    endpoint returned it on 2026-11-04, after the SDK's own retries."""
    response = SimpleNamespace(request=SimpleNamespace(), status_code=429, headers={})
    return openai.RateLimitError(
        "rate-limited", response=response,
        body={"code": 429, "message": f"{model} is temporarily rate-limited upstream. Please "
                                      f"retry shortly, or add your own provider key to use your "
                                      f"own limits"})


def _in_flight():
    """OpenRouter's HTTP 402 for a request that the requests in flight of the API key would
    take beyond their budget, with the `limit_source` of that budget
    (openrouter.ai/docs/api-reference/limits)."""
    response = SimpleNamespace(request=SimpleNamespace(), status_code=402, headers={})
    return openai.APIStatusError(
        "in flight", response=response,
        body={"code": 402, "message": "Too many requests in flight",
              "metadata": {"limit_source": "openrouter_in_flight_budget"}})


def _checklist_response(name: str, number: int) -> dict:
    """The JSON body of a completed Response of the Responses API that calls CHECKLIST_TOOL for
    repository `name`."""
    return {"id": f"resp_{number}", "object": "response", "created_at": 0,
            "model": "deepseek/deepseek-v4.1-flash", "status": "completed",
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "output": [{"type": "function_call", "id": f"fc_{number}",
                        "call_id": f"call_{number}", "status": "completed",
                        "name": classify_repos.CHECKLIST_TOOL_NAME,
                        "arguments": _answer(name)[0].arguments}],
            "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens": 10, "output_tokens_details": {"reasoning_tokens": 3},
                      "total_tokens": 110}}


class _Providers(_Model):
    """The fake client of OpenRouter, which routes each request to the provider that its
    provider routing names. The providers in `failing` fail each request with `error()` while
    `down(clock)` holds, and the others serve it. Each request takes `seconds` on `clock`, as a
    request takes time against the API. `served` lists the provider of each request in order."""

    def __init__(self, script, clock, down, failing=("morph/fp8",), seconds=30,
                 error=_rate_limited, names=()):
        super().__init__(script)
        self.clock, self.down, self.failing, self.seconds = clock, down, failing, seconds
        self.error, self.names, self.arrived, self.served = error, set(names), set(), []

    async def create(self, **request):
        # The first requests of the repositories in `names` arrive together, as requests in
        # flight reach a provider that fails.
        self.arrived.add(_repository_of(request))
        for _ in range(5000):
            if self.arrived >= self.names:
                break
            await asyncio.sleep(0.001)
        provider = request["extra_body"]["provider"]["order"][0]
        self.served.append(provider)
        self.clock.now += self.seconds
        if provider in self.failing and self.down(self.clock.now):
            self.requests.append(request)
            await asyncio.sleep(0)
            raise self.error()
        return await super().create(**request)


class TestTheBackupProvider(_RunCase):
    """The maintainer decided on 2026-11-04 on a backup provider for each model. Where a
    failure of the pinned provider would use an attempt, the same request goes to the backup
    provider of the same weights in the same quantization, with the same settings, and once the
    pinned provider has failed the requests of two repositories through the pauses that use no
    attempt, the backup provider serves every request until the pinned provider responds again to
    the request that tests it. A failure of the backup provider is a failure of the service as
    without it."""

    def labeling(self, repositories, client, **options):
        """A run of DeepSeek V4.1 Flash with its backup provider, over `repositories`, each with
        a README of its own, on the fake clock of `client`."""
        github = _GitHub({repository["name"]: (ENGLISH, repository["name"])
                          for repository in repositories})
        return self.run_over(repositories, github, client, clock=client.clock,
                             **{"no_backup_provider": False, **DEEPSEEK, **options})

    def decisions(self) -> dict:
        return {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}

    def test_a_wave_of_rate_limits_that_outlasts_the_pauses_is_served_by_the_backup(self):
        """Morph's endpoint rate-limits every request for an hour. The run pauses every worker
        three times, then the backup provider serves every request with the same settings, no
        repository uses an attempt, the second model labels nothing, and once a request that
        tests Morph's endpoint gets a response, Morph's endpoint serves the rest. Each decision
        names the provider of each response, the tokens file counts the tokens of each provider,
        and the record beside the labels lists the repositories that the backup served."""
        names = [f"o/r{number:02d}" for number in range(20)]
        clock = _Clock()
        model = _Providers(_always_true, clock, down=lambda now: now < 3600, names=names[:2],
                           seconds=300)
        second = _Glm(_always_true)
        code, err = self.labeling(_independent(*names), model, no_second_model=False,
                                  second_client=second)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["labeled"], str(len(names)))
        self.assertEqual(second.requests, [])
        for absent in ("Attempt 1 of", "Retry round", "Retrying it later"):
            self.assertNotIn(absent, err)
        # The pauses that use no attempt come before the backup provider while they hold.
        self.assertLessEqual(err.count("Pausing every worker"),
                             classify_repos.PAUSES_BEFORE_ATTEMPTS)
        self.assertIn("WARNING repository_labeling: The pinned provider morph/fp8 of the "
                      "classifying model, deepseek/deepseek-v4.1-flash, failed the requests of 2 "
                      "repositories", err)
        self.assertIn("since its last response, the last with RateLimitError (rate-limited). The "
                      "backup provider deepinfra/fp8 in fp8 serves the requests of the model with "
                      "the same settings", err)
        self.assertIn("INFO repository_labeling: The pinned provider morph/fp8 of the "
                      "classifying model, deepseek/deepseek-v4.1-flash, responds again after", err)
        # One request every ten minutes tests Morph's endpoint while the backup serves.
        switched = model.served.index("deepinfra/fp8")
        returned = len(model.served) - model.served[::-1].index("deepinfra/fp8")
        tests = model.served[switched:returned].count("morph/fp8")
        self.assertGreaterEqual(tests, 1)
        self.assertLessEqual(tests, 3600 // classify_repos.endpoints.PINNED_CHECK_SECONDS + 1)
        self.assertEqual(model.served[-1], "morph/fp8")
        self.assertIn("deepinfra/fp8", model.served)
        decisions = self.decisions()
        served = {name: decision["served_by"] for name, decision in decisions.items()}
        self.assertTrue(all(decision["usage"]["attempts"] == 1
                            for decision in decisions.values()))
        by_backup = sorted(name for name, entries in served.items()
                           if entries == [{"provider": "deepinfra/fp8", "backup": True}])
        by_pinned = sorted(name for name, entries in served.items()
                           if entries == [{"provider": "morph/fp8"}])
        self.assertTrue(by_backup and by_pinned)
        self.assertEqual(sorted(by_backup + by_pinned), sorted(names))
        tokens = {(row["requested_tier"], row["service_tier"])
                  for row in label_cache.read_tokens(self.cache, self.index()[0]["run_id"])}
        self.assertEqual(tokens, {("morph/fp8", "morph/fp8"), ("deepinfra/fp8", "deepinfra/fp8")})
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(entry["repo_name"] for entry in record["backup_provider_labels"]),
                         by_backup)
        self.assertTrue(all(entry["providers"] == ["deepinfra/fp8"] and entry["wholly"]
                            for entry in record["backup_provider_labels"]))
        self.assertIn(f"A backup provider served the requests of {len(by_backup):,} "
                      f"repositories wholly or in part", err)

    def test_the_requests_to_the_backup_send_the_same_model_settings_and_conversation(self):
        """The same request goes to the backup provider: the model, the settings, the tools, the
        instructions and the conversation, with the provider routing of the backup provider."""
        names = ("o/a", "o/b")
        clock = _Clock()
        model = _Providers(_always_true, clock, down=lambda now: True, names=names)
        code, err = self.labeling(_independent(*names), model)
        self.assertEqual(code, 0, err)
        pinned = [request for request in model.requests
                  if request["extra_body"]["provider"]["order"] == ["morph/fp8"]]
        for request in pinned:
            with self.subTest(repository=_repository_of(request)):
                mirrored = {**request, "extra_body": {
                    "provider": {"order": ["deepinfra/fp8"], "allow_fallbacks": False,
                                 "quantizations": ["fp8"], "require_parameters": True},
                    "seed": 7}}
                self.assertTrue(any(sent == mirrored for sent in model.requests))

    def test_one_repository_that_the_pinned_provider_fails_goes_to_the_backup_alone(self):
        """A failure of the pinned provider for one repository, while the requests of the other
        repositories succeed, would use an attempt. The same request goes to the backup provider
        instead, and the pinned provider goes on serving the other requests."""
        names = ("o/a", "o/b", "o/c")
        clock = _Clock()
        failed = []

        def down(now):
            if not failed:
                failed.append(now)
                return True
            return False
        model = _Providers(_always_true, clock, down=down)
        code, err = self.labeling(_independent(*names), model, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.served, ["morph/fp8", "deepinfra/fp8", "morph/fp8", "morph/fp8"])
        self.assertNotIn("WARNING repository_labeling: The pinned provider", err)
        self.assertNotIn("Attempt 1 of", err)

    def test_a_failure_of_the_backup_is_a_failure_of_the_service(self):
        """Both providers fail every request. After the switch, the failures of the backup
        provider pause every worker as failures of the service do, and after the pauses they use
        attempts, so that both repositories end without a decision rather than pausing without
        end."""
        self.with_ceiling(lambda repositories: 2)
        names = ("o/a", "o/b")

        def script(request, number):
            raise AssertionError("no provider serves a request")
        # The run waits on the real clock with the pauses of milliseconds of `_short_pauses`, and
        # the clock of the fake client counts only the time of its requests.
        model = _Providers(script, _Clock(), down=lambda now: len(model.served) < 400,
                           failing=("morph/fp8", "deepinfra/fp8"), names=names)
        github = _GitHub({name: (ENGLISH, name) for name in names})
        with _short_pauses():
            code, err = self.run_over(_independent(*names), github, model,
                                      no_backup_provider=False, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual([row["reason"] for row in self.rows("repos_excluded.csv")],
                         ["undecided", "undecided"])
        switched = err.split("The backup provider deepinfra/fp8 in fp8 serves")[1]
        self.assertIn("Pausing every worker", switched)
        self.assertIn("After 3 pauses without a response, each failure uses an attempt at the "
                      "repository's label", switched)
        self.assertIn("Attempt 1 of 3 at o/a by deepseek/deepseek-v4.1-flash failed "
                      "(RateLimitError", switched)
        self.assertLess(len(model.served), 400)

    def test_a_refusal_for_the_budget_of_the_requests_in_flight_goes_to_no_backup(self):
        """OpenRouter refuses every request with HTTP 402 for ten minutes, because the requests
        in flight fill the budget of the API key. The budget belongs to the key and not to the
        pinned provider, and the backup provider, with higher prices, would reserve more of it.
        The refusals pause every worker as without a backup provider, and no request goes to
        the backup provider."""
        names = [f"o/r{number:02d}" for number in range(6)]
        clock = _Clock()
        model = _Providers(_always_true, clock, down=lambda now: now < 600, names=names[:2],
                           error=_in_flight)
        code, err = self.labeling(_independent(*names), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["labeled"], str(len(names)))
        self.assertNotIn("deepinfra/fp8", model.served)
        self.assertIn("Pausing every worker", err)
        self.assertNotIn("WARNING repository_labeling: The pinned provider", err)
        self.assertTrue(all(entries == [{"provider": "morph/fp8"}]
                            for entries in (decision["served_by"]
                                            for decision in self.decisions().values())))

    def test_a_request_that_tests_the_pinned_provider_goes_without_the_retries_of_the_sdk(self):
        """During a switch, the request that tests the pinned provider goes once, without the
        retries that the client of the SDK makes for an HTTP 500 otherwise, so that a test that
        fails holds its worker for one request, and the same request goes on to the backup
        provider."""
        import httpx2
        sent = []

        def respond(request):
            provider = json.loads(request.content)["provider"]["order"][0]
            sent.append(provider)
            if provider == "morph/fp8":
                return httpx2.Response(500, headers={"retry-after-ms": "1"},
                                       json={"error": {"code": 500, "message": "Upstream "
                                                       "error from Morph"}})
            return httpx2.Response(200, json=_checklist_response("o/a", len(sent)))
        endpoints = classify_repos.endpoints
        client = openai.AsyncOpenAI(api_key="sk-or-v1-test", base_url=endpoints.OPENROUTER_API,
                                    max_retries=classify_repos.SDK_RETRIES,
                                    http_client=httpx2.AsyncClient(
                                        transport=httpx2.MockTransport(respond)))
        clock = _Clock()
        pinned = endpoints.PRODUCTION
        classifier = classify_repos.Classifier(client, _Bridge(), pinned.model, "",
                                               pinned.settings, "p", clock=clock,
                                               sleep=clock.sleep, endpoint=pinned,
                                               backup=endpoints.backup_of(pinned))
        with contextlib.redirect_stderr(io.StringIO()):
            classifier.providers.switch("failed")
            clock.now = endpoints.PINNED_CHECK_SECONDS
            served = []
            response = asyncio.run(classifier.create(
                "Label.", [{"role": "user", "content": "<repository>o/a</repository>"}],
                repository="o/a", served=served))
        self.assertEqual(sent, ["morph/fp8", "deepinfra/fp8"])
        self.assertEqual(response.output[0].name, classify_repos.CHECKLIST_TOOL_NAME)
        self.assertEqual(served, [{"provider": "deepinfra/fp8", "backup": True}])

    def test_a_backup_that_ignores_the_response_format_returns_the_requests_to_the_pinned_one(
            self):
        """Morph's endpoint rate-limits every request for 50 minutes, and the backup provider
        returns prose instead of a call of the checklist. The streak of the backup provider's
        responses that fail the format would pause every worker and after FORMAT_PAUSES pauses
        stop the run, although only the pinned provider is down. The streak retires the backup
        provider instead, the pinned provider serves the requests again, its failures pause and
        use attempts as without a backup provider, and once it responds, the run labels every
        repository."""
        names = [f"o/r{number:02d}" for number in range(12)]
        clock = _Clock()

        def script(request, number):
            if request["extra_body"]["provider"]["order"][0] == "deepinfra/fp8":
                return [_message("not json")]
            return _always_true(request, number)
        model = _Providers(script, clock, down=lambda now: now < 3000, names=names[:2])
        code, err = self.labeling(_independent(*names), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["labeled"], str(len(names)))
        self.assertIn(f"WARNING repository_labeling: The backup provider deepinfra/fp8 of the "
                      f"classifying model, deepseek/deepseek-v4.1-flash, returned "
                      f"{classify_repos.FORMAT_STREAK} final responses in a row, for", err)
        self.assertIn("that failed the response format, the last because the model's response "
                      "does not call submit_classification", err)
        self.assertIn("The pinned provider morph/fp8 serves the requests of the model again, and "
                      "the backup provider serves none of them for the rest of this invocation.",
                      err)
        self.assertNotIn("does not call submit_classification with its schema", err)
        self.assertNotIn("Responses in the format for", err)
        retired = len(model.served) - model.served[::-1].index("deepinfra/fp8")
        self.assertTrue(model.served[retired:])
        self.assertEqual(set(model.served[retired:]), {"morph/fp8"})
        self.assertTrue(all(entries == [{"provider": "morph/fp8"}]
                            for entries in (decision["served_by"]
                                            for decision in self.decisions().values())))

    def test_a_streak_of_the_response_format_names_the_provider_that_returned_it(self):
        """A request that was in flight to the backup provider when the backup provider was
        retired returns after it. Its response completes a streak of responses that failed the
        format, and the pause names the backup provider, which returned the response, not the
        pinned provider, which serves the requests."""
        pinned = classify_repos.endpoints.PRODUCTION
        backup = classify_repos.endpoints.backup_of(pinned)
        classifier = classify_repos.Classifier(None, _Bridge(), pinned.model, "",
                                               pinned.settings, "p", clock=_Clock(),
                                               endpoint=pinned, backup=backup)
        error = classify_repos.ModelFailure("the model's response does not call "
                                            "submit_classification")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            classifier.providers.retire("failed")
            for number in range(classify_repos.FORMAT_STREAK):
                last = number == classify_repos.FORMAT_STREAK - 1
                classifier.format_failed(f"o/r{number % 5}", error, classifier.pause.pauses,
                                         backup if last else pinned)
        self.assertIn(f"WARNING repository_labeling: {classify_repos.FORMAT_STREAK} final "
                      f"responses in a row of deepseek/deepseek-v4.1-flash "
                      f"(deepseek-ai/DeepSeek-V4.1-Flash) on the endpoint deepinfra/fp8 in fp8 "
                      f"through OpenRouter, for 5 repositories, failed the response format",
                      err.getvalue())

    def test_a_run_whose_pinned_endpoint_was_missing_from_the_list_starts_on_the_backup(self):
        """The check before the first request found the pinned endpoint of the classifying model
        missing from OpenRouter's endpoint list and its backup provider serving the requests.
        The first requests go to the backup provider, with a WARNING, and the request that tests
        the pinned provider after PINNED_CHECK_SECONDS returns the requests to it once it
        responds."""
        names = [f"o/r{number:02d}" for number in range(12)]
        clock = _Clock()
        model = _Providers(_always_true, clock, down=lambda now: now < 900, seconds=120)
        code, err = self.labeling(_independent(*names), model, started_on_backups={
            "classifying model": "OpenRouter lists no endpoint morph/fp8 of "
                                 "deepseek/deepseek-v4.1-flash"})
        self.assertEqual(code, 0, err)
        self.assertEqual(model.served[0], "deepinfra/fp8")
        self.assertIn("WARNING repository_labeling: The pinned provider morph/fp8 of the "
                      "classifying model, deepseek/deepseek-v4.1-flash, is missing from "
                      "OpenRouter's endpoint list of the model at the start of the invocation "
                      "(OpenRouter lists no endpoint morph/fp8 of deepseek/deepseek-v4.1-flash). "
                      "The backup provider deepinfra/fp8 in fp8 serves the requests", err)
        self.assertNotIn("Pausing every worker", err)
        self.assertEqual(model.served[-1], "morph/fp8")

    def test_the_second_model_has_a_backup_of_its_own(self):
        """The second model labels a repository that the classifying model left without a
        decision after a failed response. While Morph's endpoint fails its requests, the backup
        provider of GLM-5.3 serves them with its settings."""
        clock = _Clock()
        second = _Providers(_always_true, clock, down=lambda now: True)
        first = _Runaway({"o/b"})
        github = _GitHub({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")})
        code, err = self.run_over(_independent("o/a", "o/b"), github, first, clock=clock,
                                  no_backup_provider=False, no_second_model=False,
                                  second_client=second, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual(second.served, ["morph/fp8", "gmicloud/fp8"])
        pinned, backup = second.requests
        self.assertEqual(backup, {**pinned, "extra_body": {
            "provider": {"order": ["gmicloud/fp8"], "allow_fallbacks": False,
                         "quantizations": ["fp8"], "require_parameters": True}, "seed": 7}})
        decision = self.decisions()["o/b"]
        self.assertEqual(decision["served_by"], [{"provider": "gmicloud/fp8", "backup": True}])
        self.assertEqual(decision["second_model"]["model"], "z-ai/glm-5.3")
        self.assertNotIn("provider", decision["second_model"])

    def test_a_summary_that_the_backup_served_is_recorded_with_its_provider(self):
        """The server of the tools reports the provider that served a summary as its service
        tier. The decision names it, the tokens file counts the summary under it, and the record
        beside the labels lists the repository as served in part."""
        def script(request, number):
            if number == 1:
                return [_call("read_linked_page", {"url": "docs/guide.md"})]
            return _answer(_repository_of(request))
        clock = _Clock()
        model = _Providers(script, clock, down=lambda now: False)
        bridge = _Bridge(billing={"service_tier": "deepinfra/fp8", "cached_tokens": 0,
                                  "cache_write_tokens": 0})
        github = _GitHub({"o/a": (ENGLISH, "o/a")})
        code, err = self.run_over(_repositories("o/a"), github, model, bridge, clock=clock,
                                  no_backup_provider=False, summary_model=DEEPSEEK["model"],
                                  summary_provider=DEEPSEEK["provider"],
                                  summary_quantization=DEEPSEEK["quantization"],
                                  summary_hugging_face_id=DEEPSEEK["hugging_face_id"],
                                  **DEEPSEEK)
        self.assertEqual(code, 0, err)
        decision = self.decisions()["o/a"]
        [page] = [call for call in decision["tools"] if call["tool"] == "read_linked_page"]
        self.assertEqual((page["summary_provider"], page["summary_backup"]),
                         ("deepinfra/fp8", True))
        self.assertEqual(decision["served_by"], [{"provider": "morph/fp8"}] * 2)
        rows = label_cache.read_tokens(self.cache, self.index()[0]["run_id"])
        self.assertIn(("summary_model", "deepinfra/fp8", "deepinfra/fp8"),
                      {(row["role"], row["requested_tier"], row["service_tier"]) for row in rows})
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual(record["backup_provider_labels"], [
            {"repo_name": "o/a", "github_id": 1, "classifying_model": DEEPSEEK["model"],
             "model": DEEPSEEK["model"], "providers": ["deepinfra/fp8"], "wholly": False}])

    def test_the_option_turns_the_backup_off(self):
        """With --no-backup-provider, the run sends every request to the pinned provider, also
        where a failure uses an attempt, and the server of the tools gets no backup provider of
        the summary model."""
        names = ("o/a", "o/b")
        clock = _Clock()
        model = _Providers(_always_true, clock, down=lambda now: now < 1200, names=names)
        code, err = self.labeling(_independent(*names), model, no_backup_provider=True)
        self.assertNotIn("deepinfra/fp8", model.served)
        self.assertIn("Attempt 1 of 3", err)
        for options, expected in (([], ["--summary-backup-provider", "deepinfra/fp8"]),
                                  (["--no-backup-provider"], [])):
            with self.subTest(options=options):
                args = classify_repos.build_parser().parse_args(["--credentials", os.devnull,
                                                                 *options])
                arguments = classify_repos.open_bridge(args).arguments
                self.assertEqual([value for value in arguments
                                  if value in ("--summary-backup-provider", "deepinfra/fp8")],
                                 expected)

    def test_the_log_line_of_the_start_names_the_backup_providers(self):
        clock = _Clock()
        model = _Providers(_always_true, clock, down=lambda now: False)
        code, err = self.labeling(_repositories("o/a"), model,
                                  summary_model=DEEPSEEK["model"],
                                  summary_provider=DEEPSEEK["provider"],
                                  summary_quantization=DEEPSEEK["quantization"],
                                  summary_hugging_face_id=DEEPSEEK["hugging_face_id"])
        self.assertEqual(code, 0, err)
        self.assertIn("The model deepseek/deepseek-v4.1-flash (deepseek-ai/DeepSeek-V4.1-Flash) "
                      "on the endpoint morph/fp8 in fp8 through OpenRouter, with the backup "
                      "provider deepinfra/fp8 in fp8 labels at", err)
        self.assertIn("and deepseek/deepseek-v4.1-flash (deepseek-ai/DeepSeek-V4.1-Flash) on the "
                      "endpoint morph/fp8 in fp8 through OpenRouter, with the backup provider "
                      "deepinfra/fp8 in fp8 summarizes the linked pages.", err)

    def backup_lists(self, glm_backup: dict) -> dict:
        """OpenRouter's endpoint lists of DeepSeek V4.1 Flash, with morph/fp8 and deepinfra/fp8,
        and of GLM-5.3, with morph/fp8 and the entry `glm_backup` of gmicloud/fp8."""
        deepseek = {"data": {"id": DEEPSEEK["model"], "endpoints": [
            {"tag": tag, "quantization": "fp8", "max_completion_tokens": 393_216,
             "supported_parameters": ["max_tokens", "reasoning", "seed", "temperature", "tools",
                                      "top_p"]} for tag in ("morph/fp8", "deepinfra/fp8")]}}
        glm = {"data": {"id": "z-ai/glm-5.3", "endpoints": [
            *GLM_ENDPOINTS["data"]["endpoints"],
            {"tag": "gmicloud/fp8", "quantization": "fp8", **glm_backup}]}}
        return {DEEPSEEK["model"]: deepseek, "z-ai/glm-5.3": glm}

    def check_backups(self, lists: dict) -> tuple[dict, str]:
        classifying = classify_repos.endpoints.PRODUCTION
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            unserved = classify_repos.backup_check(
                classifying, classifying.settings, classifying,
                classify_repos.endpoints.SECOND_MODEL,
                get=lambda url, headers: _openrouter_lists(url, lists))
        return unserved, err.getvalue()

    def test_the_backups_of_the_endpoint_lists_of_2026_11_04_serve_every_role(self):
        """OpenRouter's endpoint list of 2026-11-04 gives gmicloud/fp8, the backup provider of
        GLM-5.3, with every parameter of the second model's requests and a
        max_completion_tokens of 943,718, above the output limit of those requests. The check
        before the first request leaves no backup provider out."""
        unserved, err = self.check_backups(self.backup_lists(
            {"max_completion_tokens": 943_718,
             "supported_parameters": ["reasoning", "include_reasoning", "max_tokens",
                                      "temperature", "top_p", "seed", "response_format", "tools",
                                      "tool_choice", "reasoning_effort"]}))
        self.assertEqual(unserved, {})
        self.assertNotIn("WARNING", err)
        args = classify_repos.build_parser().parse_args([])
        args.unserved_backups = unserved
        self.assertEqual(classify_repos.backup_for(args, classify_repos.endpoints.SECOND_MODEL,
                                                   "second model").provider, "gmicloud/fp8")

    def test_a_backup_that_the_endpoint_list_shows_unable_to_serve_is_not_used(self):
        """An endpoint list that gives the backup provider of GLM-5.3 without `seed` among its
        supported parameters and with a max_completion_tokens below the output limit of the
        second model's requests leaves the second model without a backup provider, while the
        backup of DeepSeek V4.1 Flash serves both of its roles."""
        unserved, err = self.check_backups(self.backup_lists(
            {"max_completion_tokens": 131_072,
             "supported_parameters": ["max_tokens", "reasoning", "temperature", "tools",
                                      "top_p"]}))
        self.assertEqual(list(unserved), ["second model"])
        self.assertIn("The endpoint gmicloud/fp8 of z-ai/glm-5.3 lacks seed among its supported "
                      "parameters", unserved["second model"])
        self.assertIn("WARNING repository_labeling: The backup provider gmicloud/fp8 of the "
                      "second model, z-ai/glm-5.3, serves no request of the second model in this "
                      "invocation.", err)
        args = classify_repos.build_parser().parse_args([])
        args.unserved_backups = unserved
        self.assertIsNone(classify_repos.backup_for(args, classify_repos.endpoints.SECOND_MODEL,
                                                    "second model"))
        self.assertEqual(classify_repos.backup_for(args, classify_repos.endpoints.PRODUCTION,
                                                   "classifying model").provider,
                         "deepinfra/fp8")


class _Runaway(_Model):
    """The fake model of an OpenRouter run. Its first `runaways` responses for each repository
    in `names`, or all of them, reason until the output limit of their request and call no
    tool, which OpenRouter reports with `status` "incomplete" and the reason
    "max_output_tokens". Its other responses come from `script`, the checklist by default."""

    def __init__(self, names, runaways=None, script=_always_true):
        self.names, self.runaways, self.sent = set(names), runaways, collections.Counter()

        def respond(request, number):
            if self.runs_away(request):
                return [_reasoning()]
            return script(request, number)
        super().__init__(respond)

    def runs_away(self, request) -> bool:
        name = _repository_of(request)
        return name in self.names and (self.runaways is None or self.sent[name] <= self.runaways)

    async def create(self, **request):
        self.sent[_repository_of(request)] += 1
        runaway = self.runs_away(request)
        response = await super().create(**request)
        if runaway:
            response.status = "incomplete"
            response.incomplete_details = SimpleNamespace(reason="max_output_tokens")
            response.usage.output_tokens = request["max_output_tokens"]
            response.usage.output_tokens_details.reasoning_tokens = request["max_output_tokens"]
        return response


class _Glm(_Model):
    """The fake second model, whose responses name the dated model of OpenRouter."""

    async def create(self, **request):
        response = await super().create(**request)
        response.model = "z-ai/glm-5.3-20260816"
        return response


class TestTheSecondModel(_RunCase):
    """After a failed response at a repository, the classifying model labels the repository at
    its higher temperature. The second model labels a repository that the
    classifying model still leaves without a decision after the retry rounds, and the decision,
    the run's record and its cost record name the temperature or the model that made the
    label."""

    ROUNDS = classify_repos.ATTEMPTS * (classify_repos.RETRY_ROUNDS + 1)
    GLM = {"model": "z-ai/glm-5.3", "hugging_face_id": "zai-org/GLM-5.3",
           "quantization": "fp8", "reasoning_effort": "high",
           "temperature": 1.0, "top_p": 0.95, "seed": 7, "max_output_tokens": 262_144}

    @staticmethod
    def limited(output_tokens: int) -> str:
        """The error of a response of `_Runaway` that stopped at its output limit of
        `output_tokens`, with how the response ended."""
        return (f"the model's response stopped at its output limit before it called "
                f"submit_classification (the status incomplete for the reason max_output_tokens, "
                f"the output items [reasoning] and {output_tokens:,} output tokens)")

    def labeling(self, repositories, first, second=None, **options):
        """A run of DeepSeek V4.1 Flash over `repositories`, each with a README of its own, with
        `first` as its model and `second` as the second model, or without one."""
        github = options.pop("github", None) or _GitHub(
            {repository["name"]: (ENGLISH, repository["name"]) for repository in repositories})
        if second is not None:
            options.update(no_second_model=False, second_client=second)
        return self.run_over(repositories, github, first, **DEEPSEEK, **options)

    def decisions(self) -> dict:
        return {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}

    def prices(self):
        """Round prices of the two pinned endpoints in USD per million tokens."""
        path = self.tmp / "model_prices.csv"
        path.write_text(
            "model,service_tier,uncached_input_usd_per_million,cached_input_usd_per_million,"
            "cache_write_usd_per_million,output_usd_per_million,doc_url,doc_verified,notes\n"
            "deepseek/deepseek-v4.1-flash,morph/fp8,1,1,1,1,https://prices.example/,"
            "2026-11-02,\n"
            "z-ai/glm-5.3,morph/fp8,1000,100,1000,10000,https://prices.example/,2026-11-02,\n",
            encoding="utf-8")
        patcher = mock.patch.object(classify_repos.costs, "PRICES", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_request_of_an_openrouter_model_sends_the_output_limit(self):
        """A request without an output limit gets the endpoint's own, which no page of
        OpenRouter or of the provider states and which counts the reasoning. Each request sends
        the limit of the model's settings. An OpenAI model's requests send none."""
        model = _Model(lambda request, number: (
            [_reasoning(), _call("read_linked_page", {"url": "docs/guide.md"})] if number == 1
            else _answer(_repository_of(request))))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual([request["max_output_tokens"] for request in model.requests],
                         [65_536, 65_536])
        self.setUp()
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model)
        self.assertEqual(code, 0, err)
        self.assertNotIn("max_output_tokens", model.requests[0])

    def test_after_a_failed_response_the_model_labels_at_its_higher_temperature(self):
        """o/b's first response stops at its output limit, and o/c's first final response is no
        checklist. The next attempts at them use temperature 1.0 with the same top_p and seed,
        and their decisions record that temperature, the reason and the failed attempt. o/d's
        first purpose quote is in no source, which is no failed response, and its next attempt
        stays at 0.6, as o/a's only one. The record beside the labels lists o/b and o/c, and the
        second model labels nothing."""
        tried = collections.Counter()

        def script(request, number):
            name = _repository_of(request)
            tried[name] += 1
            if tried[name] == 1 and name == "o/c":
                return [_message("not json")]
            if tried[name] == 1 and name == "o/d":
                return _answer(name, purpose_quote="Words that no source of o/d contains.")
            return _answer(name)
        first, second = _Runaway({"o/b"}, runaways=1, script=script), _Glm(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b", "o/c", "o/d"), first, second)
        self.assertEqual(code, 0, err)
        self.assertEqual(second.requests, [])
        temperatures = collections.defaultdict(list)
        for request in first.requests:
            temperatures[_repository_of(request)].append((request["temperature"],
                                                           request["top_p"],
                                                           request["extra_body"]["seed"],
                                                           request["max_output_tokens"]))
        lower, higher = (0.6, 0.95, 7, 65_536), (1.0, 0.95, 7, 262_144)
        self.assertEqual(dict(temperatures), {"o/a": [lower], "o/b": [lower, higher],
                                              "o/c": [lower, higher], "o/d": [lower, lower]})
        decisions = self.decisions()
        for name in ("o/a", "o/d"):
            self.assertEqual(decisions[name]["sampling"],
                             {"temperature": 0.6, "top_p": 0.95, "seed": 7})
            self.assertNotIn("higher_temperature", decisions[name])
        self.assertEqual(decisions["o/b"]["sampling"],
                         {"temperature": 1.0, "top_p": 0.95, "seed": 7})
        self.assertEqual(decisions["o/b"]["higher_temperature"], {
            "temperature": 1.0, "top_p": 0.95, "seed": 7, "max_output_tokens": 262_144,
            "reason": "output_limit", "error": self.limited(65_536),
            "classifying_model_failures": {"attempts": 1, "higher_temperature_attempts": 0,
                                           "requests": 1, "input_tokens": 100,
                                           "cached_tokens": 5, "output_tokens": 65_536,
                                           "reasoning_tokens": 65_536}})
        self.assertEqual((decisions["o/c"]["higher_temperature"]["reason"],
                          decisions["o/c"]["higher_temperature"]["error"]),
                         ("response_format", "the model's response does not call "
                                             "submit_classification (the status none, the output "
                                             "items [message] and 10 output tokens)"))
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual((record["higher_temperature_labels"], record["second_model_labels"],
                          record["models"]),
                         ([{"repo_name": name, "github_id": number,
                            "classifying_model": "deepseek/deepseek-v4.1-flash",
                            "model": "deepseek/deepseek-v4.1-flash", "temperature": 1.0,
                            "reason": reason}
                           for name, number, reason in (("o/b", 2, "output_limit"),
                                                        ("o/c", 3, "response_format"))],
                          [], ["deepseek/deepseek-v4.1-flash"]))
        self.assertIn("The classifying model at its higher temperature labeled 2 repositories "
                      "after a failed response of the classifying model, the first of them o/b",
                      err)

    def test_a_retry_round_and_a_resumed_run_continue_at_the_higher_temperature(self):
        """The model reaches its output limit in every attempt at o/b, the first at 0.6 and the
        others, in every retry round, at 1.0. The run ends without o/b. The next invocation
        resumes the run and labels o/b at 1.0 from its first request, because the partial file
        records the failed attempts, and the decision counts the failed attempts of both
        invocations."""
        first = _Runaway({"o/b"})
        code, err = self.labeling(_independent("o/a", "o/b"), first)
        self.assertEqual(code, 1, err)
        self.assertEqual([request["temperature"] for request in first.requests
                          if _repository_of(request) == "o/b"],
                         [0.6] + [1.0] * (self.ROUNDS - 1))
        model = _Model(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), model)
        self.assertEqual(code, 0, err)
        self.assertIn("resumed", err)
        self.assertEqual([request["temperature"] for request in model.requests], [1.0])
        decision = self.decisions()["o/b"]
        self.assertEqual((decision["higher_temperature"]["reason"],
                          decision["higher_temperature"]["classifying_model_failures"]["attempts"],
                          decision["sampling"]["temperature"]),
                         ("output_limit", self.ROUNDS, 1.0))

    def test_a_repository_that_the_model_never_labels_is_labeled_by_the_second_model(self):
        """The model reaches its output limit in every attempt of every round for o/b, at 0.6
        and then at 1.0. The second model labels o/b with the same instructions and input, on
        its pinned endpoint with its own settings: effort high, temperature 1.0, top_p 0.95,
        seed 7 and an output limit of 262,144 tokens. The decision names the second model with
        its settings, the reason and the failed requests of the classifying model, the record
        beside the labels lists o/b with the classifying model, and the cost record prices the
        second model's tokens at its prices."""
        self.prices()
        first, second = _Runaway({"o/b"}), _Glm(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), first, second)
        self.assertEqual(code, 0, err)
        failed = [request for request in first.requests if _repository_of(request) == "o/b"]
        self.assertEqual(len(failed), self.ROUNDS)
        [request] = second.requests
        self.assertEqual((request["model"], request["max_output_tokens"], request["temperature"],
                          request["top_p"], request["reasoning"]),
                         ("z-ai/glm-5.3", 262_144, 1.0, 0.95, {"effort": "high"}))
        self.assertEqual(request["extra_body"],
                         {"provider": {"order": ["morph/fp8"], "allow_fallbacks": False,
                                       "quantizations": ["fp8"], "require_parameters": True},
                          "seed": 7})
        self.assertEqual((request["instructions"], request["input"]),
                         (failed[0]["instructions"], failed[0]["input"]))
        decisions = self.decisions()
        self.assertNotIn("second_model", decisions["o/a"])
        decision = decisions["o/b"]
        self.assertEqual((decision["model"], decision["label"], decision["response_model"],
                          decision["sampling"]),
                         ("deepseek/deepseek-v4.1-flash", "true", "z-ai/glm-5.3-20260816",
                          {"temperature": 1.0, "top_p": 0.95, "seed": 7}))
        self.assertNotIn("higher_temperature", decision)
        self.assertEqual(decision["second_model"], {
            **self.GLM, "reason": "output_limit", "error": self.limited(262_144),
            "classifying_model_failures": {
                "attempts": self.ROUNDS, "higher_temperature_attempts": self.ROUNDS - 1,
                "requests": self.ROUNDS,
                "input_tokens": 100 * self.ROUNDS, "cached_tokens": 5 * self.ROUNDS,
                "output_tokens": 65_536 + 262_144 * (self.ROUNDS - 1),
                "reasoning_tokens": 65_536 + 262_144 * (self.ROUNDS - 1)}})
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual((record["models"], record["second_model_labels"],
                          record["higher_temperature_labels"], record["second_model_failures"]),
                         (["deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3"],
                          [{"repo_name": "o/b", "github_id": 2,
                            "classifying_model": "deepseek/deepseek-v4.1-flash",
                            "model": "z-ai/glm-5.3", "reason": "output_limit"}], [], []))
        self.assertEqual({row["repo_name"] for row in self.rows("repos_annotated.csv")},
                         {"o/a", "o/b"})
        self.assertIn("The second model labeled 1 repository after a failed response of the "
                      "classifying model, the first of them o/b", err)
        self.assertIn("labels the 1 repository that deepseek/deepseek-v4.1-flash", err)
        rid = self.index()[-1]["run_id"]
        self.assertEqual(int(self.index()[-1]["model_requests"]), len(first.requests) + 1)
        with open(label_cache.cost_path(self.cache, rid), newline="", encoding="utf-8") as f:
            glm = {row["billed_as"]: row for row in csv.DictReader(f)
                   if row["model"] == "z-ai/glm-5.3"}
        # One response of 100 input tokens, 5 of them cached, and 10 output tokens.
        self.assertEqual({rate: (row["service_tier"], row["tokens"], row["usd"])
                          for rate, row in glm.items()},
                         {"uncached_input": ("morph/fp8", "95", "0.095"),
                          "cached_input": ("morph/fp8", "5", "0.0005"),
                          "output": ("morph/fp8", "10", "0.1")})

    def test_a_second_model_that_fails_too_leaves_the_repository_for_the_next_run(self):
        """A final response that never follows the response format is a failed response as
        well. The second model labels once, and when it fails too, the repository has no
        decision. Its line names the second model, the kind of the classifying model's failed
        response and the failed attempts of the classifying model with those at the higher
        temperature, and so does the reason of the run's labels, which the cache keeps after
        the partial file is gone. Each failed attempt records the temperature and the top_p of
        its requests. The record beside the labels lists the repository among those that the
        second model left without a decision, also when a later invocation completes the
        outputs of the finished run from its labels."""
        self.real_ceiling()

        def broken(request, number):
            name = _repository_of(request)
            return [_message("not json")] if name == "o/b" else _answer(name)
        first, second = _Model(broken), _Glm(broken)
        partial = []
        real_unlink = Path.unlink

        def kept(path, missing_ok=False):
            if path.name.endswith(".partial.jsonl"):
                partial.extend(label_cache.partial_lines(path, ("failed",)))
            real_unlink(path, missing_ok=missing_ok)
        with mock.patch.object(Path, "unlink", kept):
            code, err = self.labeling(_independent("o/a", "o/b"), first, second)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(second.requests), classify_repos.ATTEMPTS)
        self.assertEqual([line.get("model_failure") for line in partial],
                         ["response_format"] * (classify_repos.RETRY_ROUNDS + 2))
        self.assertEqual((partial[-1]["model"], partial[-1]["retry"],
                          partial[-1]["fallback_reason"]),
                         ("z-ai/glm-5.3", False, "response_format"))
        self.assertEqual([(attempt["temperature"], attempt["top_p"])
                          for attempt in partial[0]["attempts"]],
                         [(0.6, 0.95), (1.0, 0.95), (1.0, 0.95)])
        self.assertEqual([(attempt["temperature"], attempt["top_p"])
                          for attempt in partial[-1]["attempts"]],
                         [(1.0, 0.95)] * classify_repos.ATTEMPTS)
        self.assertEqual((partial[0]["usage"]["higher_temperature_attempts"],
                          partial[-1]["classifying_model_failures"]["attempts"],
                          partial[-1]["classifying_model_failures"][
                              "higher_temperature_attempts"]),
                         (classify_repos.ATTEMPTS - 1, self.ROUNDS, self.ROUNDS - 1))
        self.assertNotIn("model", partial[0])
        listed = [{"repo_name": "o/b", "github_id": 2,
                   "classifying_model": "deepseek/deepseek-v4.1-flash", "model": "z-ai/glm-5.3",
                   "reason": "response_format"}]
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual((record["second_model_failures"], record["second_model_labels"],
                          record["undecided"]), (listed, [], 1))
        row = self.index()[-1]
        [label] = [label for label in label_cache.read_labels(self.cache, row["run_id"],
                                                              row["labels_sha256"])
                   if label["repo_name"] == "o/b"]
        self.assertEqual((label["outcome"], label["reason"]),
                         ("undecided", "the second model z-ai/glm-5.3, after the classifying "
                                       "model's response_format, model: ModelFailure: the "
                                       "model's response does not call submit_classification "
                                       "(the status none, the output items [message] and 10 "
                                       "output tokens)"))
        (self.out / "repos_annotated.json").unlink()
        model = _Model(broken)
        code, err = self.labeling(_independent("o/a", "o/b"), model, _Glm(broken))
        self.assertEqual((code, model.requests, len(self.index())), (0, [], 1), err)
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual(record["second_model_failures"], listed)

    def test_the_second_model_labels_after_any_failed_response_of_the_model(self):
        """Every attempt at o/b reaches the output limit except the last attempt of the last
        round, which the API refuses as a request that a new attempt does not receive again.
        Every other attempt ended in a failed response, and the second model labels o/b with the
        reason of the last failed response."""
        lost = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})

        class LastLost(_Runaway):
            async def create(inner, **request):
                if (_repository_of(request) == "o/b"
                        and inner.sent["o/b"] == self.ROUNDS - 1):
                    inner.sent["o/b"] += 1
                    inner.requests.append(request)
                    raise openai.BadRequestError(
                        "Previous response not found.", response=lost,
                        body={"code": "previous_response_not_found",
                              "message": "Previous response not found."})
                return await super().create(**request)
        first, second = LastLost({"o/b"}), _Glm(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), first, second)
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(1 for request in first.requests
                             if _repository_of(request) == "o/b"), self.ROUNDS)
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(self.decisions()["o/b"]["second_model"]["reason"], "output_limit")

    def test_a_tool_call_that_the_request_does_not_offer_is_labeled_by_the_second_model(self):
        """In every attempt at o/b, the model calls read_linked_page in every round, also after
        the last round of tool calls, in the request that offers only submit_classification.
        Each attempt fails as a tool call, and the second model labels o/b with that reason."""
        def script(request, number):
            name = _repository_of(request)
            if name == "o/b":
                return [_call("read_linked_page", {"url": "docs/guide.md"},
                              call_id=f"call-{number}")]
            return _answer(name)
        first, second = _Model(script), _Glm(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), first, second)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(self.decisions()["o/b"]["second_model"]["reason"], "tool_call")

    def test_every_request_of_an_attempt_at_the_higher_temperature_sends_it(self):
        """o/b's first response stops at its output limit. In the next attempt, at 1.0, the
        model calls read_linked_page in every round until the request that offers only
        submit_classification, to which it responds with the checklist. The request after each
        round of tool calls and the request after the last round send 1.0 as well, as the
        decision's `sampling` states."""
        offered_only = []

        def script(request, number):
            name = _repository_of(request)
            if name == "o/b" and len(request["tools"]) > 1:
                return [_call("read_linked_page", {"url": "docs/guide.md"},
                              call_id=f"call-{number}")]
            if name == "o/b":
                offered_only.append(request["temperature"])
            return _answer(name)
        first = _Runaway({"o/b"}, runaways=1, script=script)
        code, err = self.labeling(_independent("o/a", "o/b"), first)
        self.assertEqual(code, 0, err)
        sent = [request["temperature"] for request in first.requests
                if _repository_of(request) == "o/b"]
        self.assertEqual(sent, [0.6] + [1.0] * (classify_repos.MAX_TOOL_ROUNDS + 2))
        self.assertEqual(offered_only, [1.0])
        self.assertEqual(self.decisions()["o/b"]["sampling"]["temperature"], 1.0)

    def test_a_response_that_completes_at_the_output_limit_raises_the_temperature(self):
        """An endpoint can report a response that generated as many output tokens as its
        request allowed with the status "completed". Such a response without the call of
        submit_classification stopped at its output limit as well, and the decision of the next
        attempt records that kind as the reason of the higher temperature."""
        class Completed(_Runaway):
            async def create(inner, **request):
                response = await super().create(**request)
                if getattr(response, "status", None) == "incomplete":
                    response.status, response.incomplete_details = "completed", None
                return response
        first = Completed({"o/b"}, runaways=1)
        code, err = self.labeling(_independent("o/a", "o/b"), first)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.decisions()["o/b"]["higher_temperature"]["reason"],
                         "output_limit")

    def test_the_records_name_the_first_and_the_last_failed_response_by_kind(self):
        """o/c's first response is no checklist and its second stops at its output limit,
        before its third labels it at 1.0: its decision names the first failed response, which
        raised the temperature. Every attempt at o/b stops at its output limit except the
        first, which is no checklist: the second model's record names the last failed
        response."""
        tried = collections.Counter()

        def script(request, number):
            name = _repository_of(request)
            tried[name] += 1
            if name in ("o/b", "o/c") and tried[name] == 1:
                return [_message("not json")]
            return _answer(name)

        class Mixed(_Runaway):
            def runs_away(inner, request):
                name = _repository_of(request)
                return (name == "o/b" and inner.sent[name] > 1) or (
                    name == "o/c" and inner.sent[name] == 2)
        first, second = Mixed(set(), script=script), _Glm(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), first, second)
        self.assertEqual(code, 0, err)
        decisions = self.decisions()
        self.assertEqual((decisions["o/c"]["higher_temperature"]["reason"],
                          decisions["o/c"]["higher_temperature"]["classifying_model_failures"][
                              "attempts"]), ("response_format", 2))
        self.assertEqual(decisions["o/b"]["second_model"]["reason"], "output_limit")

    def test_a_resumed_run_takes_the_first_failed_response_and_not_the_second_models(self):
        """In the first invocation, o/b's first response is no checklist and every later one
        stops at its output limit, and the second model fails o/b too, while GitHub's failures
        for o/c leave the run incomplete. The resumed invocation labels o/b at 1.0. Its record
        names the first failed response of the classifying model, and it counts the failed
        attempts of the classifying model alone, not the second model's line of the partial
        file."""
        github = _GitHub({name: (ENGLISH, name) for name in ("o/a", "o/b", "o/c")})
        original = github.get

        def get(url, headers=None, timeout=None):
            if "/o/c/readme" in url:
                response = _Response(200, None)
                response.json = lambda: (_ for _ in ()).throw(ValueError("not JSON"))
                return response
            return original(url, headers, timeout)
        github.get = get
        tried = collections.Counter()

        def script(request, number):
            name = _repository_of(request)
            tried[name] += 1
            if name == "o/b" and tried[name] == 1:
                return [_message("not json")]
            return _answer(name)

        class Later(_Runaway):
            def runs_away(inner, request):
                return _repository_of(request) == "o/b" and inner.sent["o/b"] > 1
        second = _Glm(lambda request, number: [_message("not json")])
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), Later(set(), script=script),
                                  second, github=github)
        self.assertEqual((code, len(second.requests)), (1, classify_repos.ATTEMPTS), err)
        model = _Model(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), model, github=github)
        self.assertIn("resumed", err)
        self.assertEqual([(_repository_of(request), request["temperature"])
                          for request in model.requests], [("o/b", 1.0)])
        record = self.decisions()["o/b"]["higher_temperature"]
        self.assertEqual((record["reason"], record["classifying_model_failures"]["attempts"],
                          record["classifying_model_failures"]["requests"]),
                         ("response_format", self.ROUNDS, self.ROUNDS))

    def test_a_decision_after_a_message_without_the_call_keeps_the_text_of_the_message(self):
        """o/b's first final response is the text of a message instead of the call of
        submit_classification, and its next attempt labels it at 1.0. Every final response for
        o/c is such a message, and the second model labels it. The record of the higher
        temperature keeps the length, the start and the end of the text of the first failed
        response beside its reason and its error, which names the start of the text, and the
        record of the second model keeps those of the last. A run over the next sampling frame
        reuses both labels without a request, because the text tells why a label was made, not
        under which settings."""
        keep, logged = classify_repos.KEPT_MESSAGE_CHARS, classify_repos.LOGGED_MESSAGE_CHARS
        tried = collections.Counter()

        def script(request, number):
            name = _repository_of(request)
            tried[name] += 1
            if name == "o/c" or (name == "o/b" and tried[name] == 1):
                return [_reasoning(), _text_message(WRITTEN)]
            return _answer(name)
        repositories = _independent("o/a", "o/b", "o/c")
        code, err = self.labeling(repositories, _Model(script), _Glm(_always_true))
        self.assertEqual(code, 0, err)
        kept = {"chars": len(WRITTEN), "start": WRITTEN[:keep], "end": WRITTEN[-keep:]}
        decisions = self.decisions()
        higher = decisions["o/b"]["higher_temperature"]
        self.assertEqual((higher["reason"], higher["message"]), ("response_format", kept))
        self.assertIn(f"a message of {len(WRITTEN):,} characters that begins with "
                      f"{WRITTEN[:logged]!r}", higher["error"])
        second = decisions["o/c"]["second_model"]
        self.assertEqual((second["reason"], second["message"]), ("response_format", kept))
        model = _Model(_always_true)
        code, err = self.labeling(repositories, model, _Glm(_always_true),
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual((code, model.requests), (0, []), err)

    def test_a_resumed_run_keeps_the_text_of_the_message_of_the_first_failed_response(self):
        """Every final response for o/b in the first invocation is the text of a message
        instead of the call of submit_classification, and the run ends without o/b. The next
        invocation resumes the run and labels o/b at 1.0. Its record of the higher temperature
        keeps the length, the start and the end of the text of the first failed response, which
        the partial file records."""
        keep = classify_repos.KEPT_MESSAGE_CHARS

        def script(request, number):
            if _repository_of(request) == "o/b":
                return [_reasoning(), _text_message(WRITTEN)]
            return _answer(_repository_of(request))
        code, err = self.labeling(_independent("o/a", "o/b"), _Model(script))
        self.assertEqual(code, 1, err)
        model = _Model(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), model)
        self.assertEqual(code, 0, err)
        self.assertIn("resumed", err)
        self.assertEqual([request["temperature"] for request in model.requests], [1.0])
        self.assertEqual(self.decisions()["o/b"]["higher_temperature"]["message"],
                         {"chars": len(WRITTEN), "start": WRITTEN[:keep],
                          "end": WRITTEN[-keep:]})

    def test_a_decision_at_the_settings_of_a_failed_response_keeps_the_first_failed_response(
            self):
        """With --endpoint-default-temperature, DeepSeek V4.1 Flash has no higher temperature.
        Its first final response for o/b is the text of a message instead of the call of
        submit_classification, and its next attempt labels o/b at the same settings. Each of its
        final responses for o/c is such a message, and the second model, which labels at its own
        settings in every attempt, writes another message in its first response before its next
        attempt labels o/c. Each of the two decisions keeps the kind and the error of the first
        failed response of the model that made it, and the length, the start and the end of the
        text of its message, under first_failed_response, and the decision of o/c keeps the
        record of the second model beside it. The decision of o/a follows no failed response and
        keeps none. A run over the next sampling frame reuses the three labels without a
        request, because the record tells why a label was made, not under which settings."""
        keep = classify_repos.KEPT_MESSAGE_CHARS
        other = "The checklist follows.\n" + "Each fact of the checklist is false. " * 12
        tried = collections.Counter()

        def script(request, number):
            name = _repository_of(request)
            tried[name] += 1
            if name == "o/c" or (name == "o/b" and tried[name] == 1):
                return [_reasoning(), _text_message(WRITTEN)]
            return _answer(name)

        def second(request, number):
            return ([_reasoning(), _text_message(other)] if number == 1
                    else _answer(_repository_of(request)))

        def kept(text):
            return {"chars": len(text), "start": text[:keep], "end": text[-keep:]}
        repositories = _independent("o/a", "o/b", "o/c")
        code, err = self.labeling(repositories, _Model(script), _Glm(second),
                                  endpoint_default_temperature=True)
        self.assertEqual(code, 0, err)
        decisions = self.decisions()
        first = decisions["o/b"]["first_failed_response"]
        self.assertEqual((first["reason"], first["message"]), ("response_format", kept(WRITTEN)))
        self.assertIn(f"a message of {len(WRITTEN):,} characters that begins with", first["error"])
        self.assertNotIn("higher_temperature", decisions["o/b"])
        first = decisions["o/c"]["first_failed_response"]
        self.assertEqual((first["reason"], first["message"]), ("response_format", kept(other)))
        self.assertIn(f"a message of {len(other):,} characters that begins with", first["error"])
        self.assertEqual(decisions["o/c"]["second_model"]["message"], kept(WRITTEN))
        self.assertNotIn("first_failed_response", decisions["o/a"])
        model, glm = _Model(_always_true), _Glm(_always_true)
        code, err = self.labeling(repositories, model, glm, endpoint_default_temperature=True,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual((code, model.requests, glm.requests), (0, [], []), err)

    def test_the_attempt_of_a_decision_with_an_unquoted_purpose_is_no_failed_attempt(self):
        """o/b's first response stops at its output limit, and both later attempts, at 1.0,
        quote a purpose that no source contains. The classification takes the decision of the
        last attempt with the purpose unclear. Its record counts the failed attempts before it,
        the response that stopped at the output limit and the first unquoted purpose, and not the
        attempt whose decision it is."""
        def script(request, number):
            name = _repository_of(request)
            if name == "o/b":
                return _answer(name, purpose_quote="Words that no source of o/b contains.")
            return _answer(name)
        first = _Runaway({"o/b"}, runaways=1, script=script)
        code, err = self.labeling(_independent("o/a", "o/b"), first)
        self.assertEqual(code, 0, err)
        decision = self.decisions()["o/b"]
        self.assertEqual((decision["usage"]["attempts"], decision["sampling"]["temperature"]),
                         (classify_repos.ATTEMPTS, 1.0))
        self.assertEqual(decision["higher_temperature"]["classifying_model_failures"],
                         {"attempts": 2, "higher_temperature_attempts": 1, "requests": 2,
                          "input_tokens": 200, "cached_tokens": 10,
                          "output_tokens": 65_536 + 10, "reasoning_tokens": 65_536 + 3})

    def test_a_renamed_repository_reaches_the_second_model_and_resumes_at_the_higher_one(self):
        """GitHub returns the README of o/b under o/new-b, the name that a rename led to, and
        every response for it stops at its output limit. The run keeps the failed attempts by
        the GitHub id, which stays: the second model labels the repository, and an invocation
        without the second model that resumes the run labels it at 1.0 from its first
        request."""
        class Renamed(_GitHub):
            def get(inner, url, headers=None, timeout=None):
                response = super().get(url, headers, timeout)
                if "/repos/o/b/readme" in url and isinstance(response.body, dict):
                    response.body = {**response.body, "url": response.body["url"].replace(
                        "/repos/o/b/", "/repos/o/new-b/")}
                return response

        def github():
            return Renamed({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")})
        second = _Glm(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), _Runaway({"o/new-b"}), second,
                                  github=github())
        self.assertEqual((code, len(second.requests)), (0, 1), err)
        decision = self.decisions()["o/new-b"]
        self.assertEqual((decision["id"], decision["second_model"]["reason"]),
                         (2, "output_limit"))
        self.setUp()
        first = _Runaway({"o/new-b"})
        code, err = self.labeling(_independent("o/a", "o/b"), first, github=github())
        self.assertEqual(code, 1, err)
        self.assertEqual([request["temperature"] for request in first.requests
                          if _repository_of(request) == "o/new-b"],
                         [0.6] + [1.0] * (self.ROUNDS - 1))
        model = _Model(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b"), model, github=github())
        self.assertEqual(code, 0, err)
        self.assertIn("resumed", err)
        self.assertEqual([request["temperature"] for request in model.requests], [1.0])

    def test_a_second_model_that_cannot_label_keeps_its_earlier_labels(self):
        """The second model labels o/b in a run. A later run over the next sampling frame, and
        the resumed invocation of a run that GitHub's failures for o/c left incomplete, cannot
        label with the second model, as when its endpoint is unserved. Both reuse its label of
        o/b without a request, because the run is configured with the same second model, while
        --no-second-model labels o/b again."""
        code, err = self.labeling(_independent("o/a", "o/b"), _Runaway({"o/b"}),
                                  _Glm(_always_true))
        self.assertEqual(code, 0, err)
        model = _Runaway({"o/b"})
        code, err = self.labeling(_independent("o/a", "o/b"), model,
                                  retrieval="2026-11-05T101430Z", no_second_model=False,
                                  second_model_unavailable="The endpoint morph/fp8 is unserved")
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual({d["name"]: bool(d.get("second_model")) for d in
                          self.decisions().values()}, {"o/a": False, "o/b": True})
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["repo_name"] for entry in record["second_model_labels"]],
                         ["o/b"])
        self.setUp()
        github = _GitHub({name: (ENGLISH, name) for name in ("o/a", "o/b", "o/c")})
        original = github.get

        def get(url, headers=None, timeout=None):
            if "/o/c/readme" in url:
                response = _Response(200, None)
                response.json = lambda: (_ for _ in ()).throw(ValueError("not JSON"))
                return response
            return original(url, headers, timeout)
        github.get = get
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), _Runaway({"o/b"}),
                                  _Glm(_always_true), github=github)
        self.assertEqual(code, 1, err)
        model = _Runaway({"o/b"})
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), model, github=github,
                                  no_second_model=False,
                                  second_model_unavailable="The endpoint morph/fp8 is unserved")
        self.assertIn("resumed", err)
        self.assertEqual(model.requests, [])
        self.assertTrue(self.decisions()["o/b"]["second_model"])

    def test_a_response_stops_at_its_output_limit_by_either_signal(self):
        """OpenRouter reports a response that stopped at its output limit with the status
        "incomplete" and the reason "max_output_tokens", and the output tokens of such a
        response reach the limit of its request. Either signal alone makes a response without
        the call of submit_classification fail as output_limit, and a response without either
        fails as response_format."""
        def response(status, output_tokens, reason="max_output_tokens"):
            return SimpleNamespace(
                status=status, output=[_reasoning()],
                incomplete_details=SimpleNamespace(reason=reason),
                usage=SimpleNamespace(output_tokens=output_tokens))
        for case, kind in ((response("incomplete", 1_000), "output_limit"),
                           (response("completed", 262_144), "output_limit"),
                           (response("completed", 255_999), "response_format"),
                           (response("incomplete", 1_000, "content_filter"), "response_format")):
            with self.subTest(status=case.status, output=case.usage.output_tokens):
                with self.assertRaises(classify_repos.ModelFailure) as raised:
                    classify_repos.checked_response(case, "o/a", 262_144)
                self.assertEqual(raised.exception.kind, kind)
        with self.assertRaises(classify_repos.ModelFailure) as raised:
            classify_repos.checked_response(response("completed", 262_144), "o/a", None)
        self.assertEqual(raised.exception.kind, "response_format")

    def test_the_pass_of_the_second_model_does_not_stop_for_the_format_streak(self):
        """Seven repositories, more than FORMAT_STREAK_REPOSITORIES, reach the output limit in
        every attempt of the classifying model, which counts no response that stopped at its
        output limit toward the streak, and the second model returns no checklist for any of
        them, more than FORMAT_STREAK responses in a row. The pass holds nothing but
        repositories that the classifying model failed, and the run finishes without them
        instead of pausing and stopping as if the endpoint ignored the response format."""
        self.with_ceiling(lambda repositories: 10)
        bad = [f"o/r{i}" for i in range(7)]
        names = [*bad, "o/x", "o/y"]
        second = _Glm(lambda request, number: [_message("not json")])
        code, err = self.labeling(_independent(*names), _Runaway(set(bad)), second, workers=1)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Stopped the run", err)
        self.assertNotIn("failed the response format", err)
        self.assertEqual(len(second.requests), len(bad) * classify_repos.ATTEMPTS)
        self.assertGreater(len(second.requests), classify_repos.FORMAT_STREAK)
        row = self.index()[-1]
        self.assertEqual(row["outcome"], "finished")
        self.assertEqual(sorted(label["repo_name"] for label in label_cache.read_labels(
            self.cache, row["run_id"], row["labels_sha256"]) if label["outcome"] == "undecided"),
            bad)

    def test_the_pass_of_the_second_model_takes_over_the_pauses_of_an_open_streak(self):
        """The classifying model's endpoint fails every response from the third repository on,
        and with one worker its streak pauses every worker twice before the round ends. With no
        retry round, the pass of the second model follows while the streak is open, and the
        second model fails during the same outage, as when the two models share a provider. The
        pass waits out the second pause, which holds when the round ends, and the failed
        responses of the second model count, although the classifying model failed their
        repositories: its streak pauses the workers of the pass, with the third pause of the run
        and twice the wait of the second, instead of using up its attempts at once."""
        self.with_ceiling(lambda repositories: 10)
        bad = [f"o/r{i}" for i in range(7)]
        names = ["o/x", "o/y", *bad]
        clock = _Clock()
        first = _Model(lambda request, number: (
            _answer(_repository_of(request)) if number <= 2 else [_message("not json")]))
        second = _Glm(lambda request, number: [_message("not json")])
        with mock.patch.object(classify_repos, "RETRY_ROUNDS", 0):
            code, err = self.labeling(_independent(*names), first, second, workers=1,
                                      clock=clock)
        self.assertEqual(code, 0, err)
        pauses = TestTheResponseFormat.pauses(err)
        self.assertEqual(pauses, TestTheResponseFormat.waits(4))
        self.assertIn("WARNING repository_labeling: 20 final responses in a row of z-ai/glm-5.3 "
                      "(zai-org/GLM-5.3) on the endpoint morph/fp8 in fp8 through OpenRouter, for "
                      "7 repositories, failed the response format", err)
        self.assertEqual(len(second.requests), len(bad) * classify_repos.ATTEMPTS)
        # The last pause of the pass holds no request.
        self.assertEqual(clock.now, classify_repos.PAUSE_FIRST_SECONDS * (1 + 2 + 4))

    def test_a_refusal_of_the_second_model_ends_its_pass_and_not_the_run(self):
        """The API rejects every request of the second model. The run's retry rounds have
        finished, and a stop would make the next invocation pay for every attempt of the
        classifying model at these repositories again. The pass ends instead, its repositories
        stay without a decision, and the run finishes within its ceiling."""
        self.with_ceiling(lambda repositories: 10)
        names = ["o/a", "o/b", "o/c"]

        def rejected(request, number):
            raise _openrouter_error(openai.AuthenticationError, 401)
        second = _Glm(rejected)
        code, err = self.labeling(_independent(*names), _Runaway({"o/b", "o/c"}), second,
                                  workers=1)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Stopped the run", err)
        self.assertEqual(len(second.requests), 1)
        self.assertIn("WARNING repository_labeling: The second model labels no further "
                      "repository, because ", err)
        row = self.index()[-1]
        self.assertEqual(row["outcome"], "finished")
        self.assertEqual(sorted(label["repo_name"] for label in label_cache.read_labels(
            self.cache, row["run_id"], row["labels_sha256"]) if label["outcome"] == "undecided"),
            ["o/b", "o/c"])

    def test_a_failure_of_github_while_the_second_model_labels_is_not_tried_again(self):
        """GitHub returns no usable README for o/b in the pass of the second model, after the
        retry rounds. No retry round follows, and the line names the second model rather than
        saying that the repository is tried again later."""
        self.real_ceiling()
        github = _GitHub({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")})
        original, readme_requests = github.get, []

        def get(url, headers=None, timeout=None):
            if "/o/b/readme" in url:
                readme_requests.append(url)
                if len(readme_requests) > classify_repos.RETRY_ROUNDS + 1:
                    response = _Response(200, None)
                    response.json = lambda: (_ for _ in ()).throw(ValueError("not JSON"))
                    return response
            return original(url, headers, timeout)
        github.get = get
        lines = []
        real_unlink = Path.unlink

        def kept(path, missing_ok=False):
            if path.name.endswith(".partial.jsonl"):
                lines.extend(label_cache.partial_lines(path, ("failed",)))
            real_unlink(path, missing_ok=missing_ok)
        second = _Glm(_always_true)
        with mock.patch.object(Path, "unlink", kept):
            code, err = self.labeling(_independent("o/a", "o/b"), _Runaway({"o/b"}), second,
                                      github=github)
        self.assertEqual(code, 0, err)
        self.assertEqual(second.requests, [])
        self.assertIn("Could not decide o/b (not JSON).\n", err)
        self.assertNotIn("Could not decide o/b (not JSON). Retrying it later", err)
        self.assertEqual((lines[-1]["reason"], lines[-1]["retry"], lines[-1]["model"]),
                         ("github", False, "z-ai/glm-5.3"))

    def test_a_response_of_the_second_model_restored_after_a_stop_still_names_it(self):
        """The second model's response for o/b makes it a mirror, and GitHub then rejects every
        GitHub access token for the merges, which stops the run. The next invocation restores the
        response from the partial file and reads only GitHub's facts, and the decision names the
        second model that made the response."""
        class Rejecting(_GitHub):
            rejecting = True

            def post(self, url, headers=None, json=None, timeout=None):
                if self.rejecting:
                    return _Response(401, {"message": "Bad credentials"})
                return super().post(url, headers, json, timeout)

            def get(self, url, headers=None, timeout=None):
                if self.rejecting and url == readmes.RATE_LIMIT_URL:
                    return _Response(401, {"message": "Bad credentials"})
                return super().get(url, headers, timeout)
        github = Rejecting({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")},
                           merges={"o/b": ["2026-07-21T00:46:14Z"]})
        mirror = _answer("o/b", facts={"mirror": {"described_as_mirror": "true"}})
        second = _Glm(lambda request, number: mirror)
        code, err = self.labeling(_independent("o/a", "o/b"), _Runaway({"o/b"}), second,
                                  github=github)
        self.assertEqual((code, len(second.requests)), (1, 1), err)
        self.assertIn("GitHub accepts none of the configured GitHub access tokens", err)
        github.rejecting = False
        first, second = _Model(_always_true), _Glm(lambda request, number: mirror)
        code, err = self.labeling(_independent("o/a", "o/b"), first, second, github=github)
        self.assertEqual((code, first.requests, second.requests), (0, [], []), err)
        decision = self.decisions()["o/b"]
        self.assertEqual((decision["second_model"]["model"], decision["response_model"],
                          decision["mirror_merges"]["latest_merge"]),
                         ("z-ai/glm-5.3", "z-ai/glm-5.3-20260816", "2026-07-21T00:46:14Z"))

    def test_the_progress_lines_of_the_pass_count_the_second_models_requests(self):
        """The progress line after the second model's label of o/b counts its request among
        the run's model requests, beside the classifying model's."""
        first, second = _Runaway({"o/b"}), _Glm(_always_true)
        with mock.patch.object(classify_repos, "PROGRESS_EVERY", 1):
            code, err = self.labeling(_independent("o/a", "o/b"), first, second, workers=1)
        self.assertEqual(code, 0, err)
        lines = [line for line in err.splitlines() if " done, " in line]
        self.assertIn(f"{len(first.requests) + 1:,} model requests", lines[-1])

    def test_a_label_of_the_higher_temperature_or_the_second_model_is_reused_only_alike(self):
        """The second model labels o/b and the higher temperature o/c in a run. A run with the
        same settings over the next sampling frame reuses both labels without a request, and the
        classification parameters are the same with and without the second model. A run without
        the second model, or with other settings of it or of the higher temperature, labels
        each of them again with the classifying model, and keeps o/a's label."""
        repositories = _independent("o/a", "o/b", "o/c")

        class Mixed(_Runaway):
            def runs_away(inner, request):
                name = _repository_of(request)
                return name == "o/b" or (name == "o/c" and inner.sent[name] == 1)

        def labeled_again(changed=contextlib.nullcontext, without=False) -> list:
            """The repositories that a run over the next sampling frame sends the classifying
            model, after a run in which the second model labeled o/b and the higher temperature
            o/c."""
            self.setUp()
            code, err = self.labeling(repositories, Mixed(set()), _Glm(_always_true))
            self.assertEqual(code, 0, err)
            decisions = self.decisions()
            self.assertTrue(decisions["o/b"]["second_model"])
            self.assertTrue(decisions["o/c"]["higher_temperature"])
            model = _Model(_always_true)
            with changed():
                code, err = self.labeling(repositories, model,
                                          None if without else _Glm(_always_true),
                                          retrieval="2026-11-05T101430Z")
            self.assertEqual(code, 0, err)
            self.assertEqual(len({row["parameters"] for row in self.index()}), 1)
            return sorted(_repository_of(request) for request in model.requests)
        self.assertEqual(labeled_again(), [])
        self.assertEqual(labeled_again(without=True), ["o/b"])
        glm = classify_repos.endpoints.SECOND_MODEL
        other = dataclasses.replace(glm, settings=dataclasses.replace(glm.settings,
                                                                      temperature=0.9))
        self.assertEqual(labeled_again(lambda: mock.patch.object(classify_repos.endpoints,
                                                                 "SECOND_MODEL", other)),
                         ["o/b"])
        production = classify_repos.endpoints.PRODUCTION
        hotter = dataclasses.replace(production, settings=dataclasses.replace(
            production.settings, higher_temperature=1.2))
        self.assertEqual(labeled_again(lambda: mock.patch.dict(classify_repos.endpoints.PINNED,
                                                               {production.model: hotter})),
                         ["o/c"])

    def test_a_resumed_run_without_the_second_model_labels_its_label_again(self):
        """The second model labels o/b in a run that GitHub's failures for o/c leave incomplete.
        The next invocation resumes the run under the same classification parameters with
        --no-second-model, and the classifying model labels o/b again rather than keeping the
        second model's label of the earlier invocation."""
        github = _GitHub({name: (ENGLISH, name) for name in ("o/a", "o/b", "o/c")})
        original = github.get

        def get(url, headers=None, timeout=None):
            if "/o/c/readme" in url:
                response = _Response(200, None)
                response.json = lambda: (_ for _ in ()).throw(ValueError("not JSON"))
                return response
            return original(url, headers, timeout)
        github.get = get
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), _Runaway({"o/b"}),
                                  _Glm(_always_true), github=github)
        self.assertEqual(code, 1, err)
        self.assertTrue(self.decisions()["o/b"]["second_model"])
        model = _Model(_always_true)
        code, err = self.labeling(_independent("o/a", "o/b", "o/c"), model, github=github)
        self.assertIn("resumed", err)
        self.assertEqual([_repository_of(request) for request in model.requests], ["o/b"])

    def test_a_kept_response_of_the_second_model_is_restored_only_alike(self):
        """The second model's response for o/b makes it a mirror, and GitHub fails the merges
        through the run, which finishes without o/b and keeps the response. The next run over
        the next sampling frame restores the response with the same second model, also when the
        second model cannot label in that invocation. With --no-second-model, the classifying
        model labels o/b."""
        self.real_ceiling()

        class Failing(_GitHub):
            broken = True

            def post(self, url, headers=None, json=None, timeout=None):
                if self.broken:
                    return _Response(200, {"data": None, "errors": [
                        {"type": "SERVICE_UNAVAILABLE", "message": "Something went wrong"}]})
                return super().post(url, headers, json, timeout)
        mirror = _answer("o/b", facts={"mirror": {"described_as_mirror": "true"}})
        for without, unavailable, requested in ((False, "", []), (True, "", ["o/b"]),
                                                (False, "The endpoint is unserved", [])):
            with self.subTest(without=without, unavailable=unavailable):
                self.setUp()
                self.real_ceiling()
                github = Failing({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")},
                                 merges={"o/b": ["2026-07-21T00:46:14Z"]})
                code, err = self.labeling(_independent("o/a", "o/b"), _Runaway({"o/b"}),
                                          _Glm(lambda request, number: mirror), github=github)
                self.assertEqual(code, 0, err)
                github.broken = False
                model = _Model(_always_true)
                options = ({"no_second_model": False, "second_model_unavailable": unavailable}
                           if unavailable else {})
                code, err = self.labeling(_independent("o/a", "o/b"), model,
                                          None if without or unavailable else _Glm(_always_true),
                                          github=github, retrieval="2026-11-05T101430Z",
                                          **options)
                self.assertEqual(code, 0, err)
                self.assertEqual([_repository_of(request) for request in model.requests],
                                 requested)
                self.assertEqual(bool(self.decisions()["o/b"].get("second_model")), not without)

    def test_a_finished_run_with_a_label_of_the_second_model_is_completed_alike(self):
        """A finished run with a label of the second model lacks its outputs. The next
        invocation with the same second model completes the run without a new one, and an
        invocation without the second model starts a new run, in which the classifying model
        labels o/b again."""
        for without, runs in ((False, 1), (True, 2)):
            with self.subTest(without=without):
                self.setUp()
                code, err = self.labeling(_independent("o/a", "o/b"), _Runaway({"o/b"}),
                                          _Glm(_always_true))
                self.assertEqual(code, 0, err)
                (self.out / "repos_annotated.json").unlink()
                model = _Model(_always_true)
                code, err = self.labeling(_independent("o/a", "o/b"), model,
                                          None if without else _Glm(_always_true))
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.index()), runs)
                self.assertTrue((self.out / "repos_annotated.json").is_file())
                self.assertEqual([_repository_of(request) for request in model.requests],
                                 ["o/b"] if without else [])

    def test_a_failure_of_github_or_of_the_input_is_not_labeled_by_the_second_model(self):
        """GitHub returns no usable README for o/a in any round, and OpenRouter blocks the
        README of o/c in every attempt. The second model would get the same failure, and it
        labels neither: o/a's README is requested once per round and not once more for it."""
        names = ["o/a", "o/b", "o/c"]
        github = _GitHub({name: (ENGLISH, name) for name in names})
        original = github.get
        readme_requests = []

        def get(url, headers=None, timeout=None):
            if "/o/a/readme" in url:
                readme_requests.append(url)
                if len(readme_requests) <= classify_repos.RETRY_ROUNDS + 1:
                    response = _Response(200, None)
                    response.json = lambda: (_ for _ in ()).throw(ValueError("not JSON"))
                    return response
            return original(url, headers, timeout)
        github.get = get

        def script(request, number):
            if _repository_of(request) == "o/c":
                raise _openrouter_error(openai.PermissionDeniedError, 403, {
                    "patterns": ["ignore all previous instructions"]})
            return _answer(_repository_of(request))
        second = _Glm(_always_true)
        code, err = self.run_over(_independent(*names), github, _Model(script), **DEEPSEEK,
                                  no_second_model=False, second_client=second)
        self.assertEqual(code, 1, err)
        self.assertEqual(second.requests, [])
        self.assertEqual(len(readme_requests), classify_repos.RETRY_ROUNDS + 1)
        self.assertIn("Could not decide o/a (not JSON)", err)
        self.assertIn("a guardrail of OpenRouter blocked the request", err)

    def test_a_check_of_one_model_and_a_run_of_the_second_model_label_without_it(self):
        """--no-second-model, which a check of one model passes, leaves the repository without a
        decision, and so does a run of the second model itself, which would label again with the
        model that failed."""
        glm = {"model": "z-ai/glm-5.3", "provider": "morph/fp8", "quantization": "fp8",
               "hugging_face_id": "zai-org/GLM-5.3"}
        for options in ({**DEEPSEEK, "no_second_model": True},
                        {**glm, "no_second_model": False}):
            with self.subTest(model=options["model"]):
                self.setUp()
                second = _Glm(_always_true)
                code, err = self.run_over(_independent("o/a", "o/b"),
                                          _GitHub({"o/a": (ENGLISH, "s1"),
                                                   "o/b": (ENGLISH, "s2")}),
                                          _Runaway({"o/b"}), second_client=second, **options)
                self.assertEqual(code, 1, err)
                self.assertEqual(second.requests, [])
                self.assertIn(f"Could not decide o/b ({self.limited(262_144)})", err)


def _raw_call(arguments, **marks):
    """A call of submit_classification with `arguments` as the model wrote them, which need not
    be JSON. `marks` sets further attributes of the call."""
    return SimpleNamespace(type="function_call", name=classify_repos.CHECKLIST_TOOL_NAME,
                           arguments=arguments, call_id="call-checklist", **marks)


# Arguments of submit_classification that end in the middle, as in a response that the service
# ended.
CUT_OFF = '{"repo_name": "o/a", "readme_words_by_language": [{"language": "Eng'
# The error of a response that the service ended.
PROVIDER_ENDED = SimpleNamespace(code="server_error", message="The provider ended the response")


class _Ending(_Together):
    """The fake model of an OpenRouter run. Its responses to the requests for which
    `ended(request, number)` is true end early: with the status "failed" and `error`, as when the
    service ends a response, or with `error` None, with the status "incomplete" for `reason`.
    With `inside`, such a response ends in the arguments of submit_classification, and otherwise
    before the call, with a reasoning item alone. Its other responses come from `script`, the
    checklist by default. The first requests of the repositories in `names` reach it together
    (`_Together`)."""

    def __init__(self, ended, names=(), script=_always_true, error=PROVIDER_ENDED,
                 reason="content_filter", inside=True):
        self.error, self.reason = error, reason

        def respond(request, number):
            if ended(request, number):
                return [_raw_call(CUT_OFF, ended=True) if inside
                        else SimpleNamespace(type="reasoning", id="rs_1", summary=[],
                                             encrypted_content="", ended=True)]
            return script(request, number)
        super().__init__(respond, names)

    async def create(self, **request):
        response = await super().create(**request)
        if any(getattr(item, "ended", False) for item in response.output):
            if self.error is None:
                response.status = "incomplete"
                response.incomplete_details = SimpleNamespace(reason=self.reason)
            else:
                response.status, response.error = "failed", self.error
        return response


# The body of OpenRouter's response with status 200 to a request of the Responses API that is not
# streamed when the provider fails after the headers: a Response with the status "failed", the
# code of its error and the `error_type` at the top level, and without `output`, as in the example
# of the section of the Responses API (openrouter.ai/docs/api_reference/errors-and-debugging).
PROVIDER_FAILED = {"id": "resp_abc123", "object": "response", "status": "failed",
                   "error": {"code": "server_error", "message": "Provider returned error"},
                   "error_type": "provider_unavailable"}
# The same failure with an empty output and without usage.
PROVIDER_FAILED_EMPTY = {**PROVIDER_FAILED, "output": [], "usage": None}
# How the log names the failure of PROVIDER_FAILED and of PROVIDER_FAILED_EMPTY.
PROVIDER_FAILED_LOGGED = ("ServiceEnded (the API ended the response with the error server_error "
                          "(Provider returned error) with the error_type provider_unavailable, "
                          "the status failed, no output items and no count of the output tokens)")


def _parsed(body):
    """The Response that the SDK's client returns for a response with status 200 and the JSON
    `body`, which a transport of httpx2, the HTTP client that the SDK installs and uses, returns
    to its request."""
    import httpx2
    client = openai.OpenAI(api_key="sk-or-v1-test", base_url="https://openrouter.ai/api/v1",
                           max_retries=0, http_client=httpx2.Client(
                               transport=httpx2.MockTransport(
                                   lambda request: httpx2.Response(200, json=body))))
    return client.responses.create(model="deepseek/deepseek-v4.1-flash", input="")


class _ErrorBodies(_Together):
    """The fake model of an OpenRouter run. It responds to the requests for which
    `failing(request, number)` is true with the Response that the SDK's client parses from
    `body`, a body with status 200 that holds a Response with an error, and to its other
    requests from `script`, the checklist by default. The first requests of the repositories in
    `names` reach it together (`_Together`)."""

    def __init__(self, failing, names=(), body=PROVIDER_FAILED, script=_always_true):
        self.failing, self.body = failing, body
        super().__init__(script, names)

    async def create(self, **request):
        response = await super().create(**request)
        return _parsed(self.body) if self.failing(request, len(self.requests)) else response


class _Batches(_Model):
    """The fake model. While `holding()` is true, each request waits until `size` requests have
    reached it in its batch, so that the responses of a batch return after each of its requests
    was sent, as the requests of every worker are in flight together against a service that
    takes time for each. The wait ends sooner once `holding()` is false."""

    def __init__(self, script, size, holding):
        super().__init__(script)
        self.size, self.holding, self.arrived = size, holding, 0

    async def create(self, **request):
        if self.holding():
            self.arrived += 1
            filled = -(-self.arrived // self.size) * self.size
            for _ in range(5000):
                if self.arrived >= filled or not self.holding():
                    break
                await asyncio.sleep(0.001)
        return await super().create(**request)


class TestTheResponseFormat(_RunCase):
    """A streak of final responses that fail the response format pauses every worker, and an
    endpoint that ignores the JSON schema of the response format stops the run after the
    pauses."""

    STREAK = ("{count} final responses in a row of deepseek/deepseek-v4.1-flash "
              "(deepseek-ai/DeepSeek-V4.1-Flash) on the endpoint morph/fp8 in fp8 through "
              "OpenRouter, for {repositories} repositories, failed the response format")
    NO_CALL = ("the model's response does not call submit_classification (the status none, the "
               "output items [message] and 10 output tokens)")
    PAUSES = classify_repos.FORMAT_PAUSES

    def paused(self, count, repositories, wait, pause):
        """The WARNING of the pause of every worker for a streak of `count` failed responses."""
        return (f"WARNING repository_labeling: "
                f"{self.STREAK.format(count=count, repositories=repositories)}, the last because "
                f"{self.NO_CALL}. Pausing every worker for {wait} (pause {pause} of "
                f"{self.PAUSES}). Responses in the format for "
                f"{classify_repos.FORMAT_STREAK_REPOSITORIES} repositories in a row make the next "
                f"pause the first again, and after pause {self.PAUSES}, the failed response that "
                f"would begin another pause ends the labeling with the endpoint.")

    @staticmethod
    def pauses(err):
        """The wait and the number of each pause for the response format, in the order logged."""
        return re.findall(r"Pausing every worker for (\S+) \(pause (\d+) of "
                          + str(classify_repos.FORMAT_PAUSES) + r"\)\.", err)

    @staticmethod
    def waits(pauses):
        """The waits of the first `pauses` pauses as the log gives them, with their numbers."""
        return [(classify_repos.logs.hms(classify_repos.PAUSE_FIRST_SECONDS * 2 ** index),
                 str(index + 1)) for index in range(pauses)]

    def test_an_endpoint_that_ignores_the_response_format_stops_the_run_after_the_pauses(self):
        """OpenRouter's endpoint siliconflow/fp8 of Qwen3.8 2.4T-A95B returned YAML, XML or
        Markdown instead of the JSON of the response format once tools were in the request, for
        every repository. After FORMAT_STREAK such responses, every worker pauses, and each
        response after a pause fails again and begins the next pause, which is twice as long.
        The response after the last of the FORMAT_PAUSES pauses, which take about two hours in
        all, stops the run instead of paying for every attempt at every repository in every
        retry round."""
        clock = _Clock()
        names = [f"o/r{i}" for i in range(12)]
        model = _Model(lambda request, number: [_message(
            f"repo_name: {_repository_of(request)}\nreadme_words_by_language: []\n")])
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model,
                                  workers=1, clock=clock, **DEEPSEEK)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.requests),
                         classify_repos.FORMAT_STREAK + classify_repos.FORMAT_PAUSES)
        # With one worker, the streak of `count` responses holds the repositories that its
        # attempts reached, three attempts each.
        for (wait, pause), count in zip(self.waits(self.PAUSES), range(20, 27)):
            self.assertIn(self.paused(count, -(-count // classify_repos.ATTEMPTS), wait, pause),
                          err)
        self.assertEqual(self.pauses(err), self.waits(self.PAUSES))
        self.assertIn(f"ERROR repository_labeling: Stopped the run, because "
                      f"{self.STREAK.format(count=27, repositories=9)}, the last because "
                      f"{self.NO_CALL}. Through 7 pauses of every worker, 7 of the 7 final "
                      f"responses after the first pause failed the format, without responses in "
                      f"the format for 5 repositories in a row. The endpoint does not call "
                      f"submit_classification with its schema. Label with another endpoint or "
                      f"model", err)
        self.assertEqual(clock.now,
                         classify_repos.PAUSE_FIRST_SECONDS * (1 + 2 + 4 + 8 + 16 + 32 + 64))
        self.assertEqual(self.index()[0]["outcome"], "incomplete")

    def test_the_requests_of_an_endpoint_that_ignores_the_format_stay_bounded_over_many_workers(
            self):
        """With many workers, the requests in flight when a pause begins fail after it began and
        begin no further pause. After each pause and after the stop, each worker finishes at
        most the attempts at the repository that it holds, so the run spends a number of
        requests that the workers bound, not the repositories in the sampling frame."""
        workers = 8
        names = [f"o/r{i:03d}" for i in range(200)]
        model = _Model(lambda request, number: [_message("not json")])
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model,
                                  workers=workers, clock=_Clock(), **DEEPSEEK)
        self.assertEqual(code, 1)
        self.assertEqual(self.pauses(err), self.waits(self.PAUSES))
        self.assertIn("The endpoint does not call submit_classification with its schema. Label "
                      "with another endpoint or model", err)
        self.assertLessEqual(len(model.requests),
                             classify_repos.FORMAT_STREAK
                             + (classify_repos.FORMAT_PAUSES + 1) * workers
                             * classify_repos.ATTEMPTS)

    def test_a_burst_of_failed_responses_pauses_every_worker_and_the_run_labels_the_rest(self):
        """A working endpoint, such as morph/fp8 of DeepSeek V4.1 Flash on 2026-11-03, can fail
        the response format for a streak of final responses in a row and then follow it again.
        Here the endpoint fails every response until the first pause has passed, and the
        requests of the six workers are in flight together. The streak reaches FORMAT_STREAK
        with the second response of a batch, and the failed responses to the four requests that
        were in flight when the pause began count toward the streak but begin no further pause
        and do not stop the run. After the pause, the run labels every repository, those that
        failed every attempt during the streak in its retry round."""
        clock = _Clock()
        names = [f"o/r{i:02d}" for i in range(40)]
        workers = 6
        self.assertEqual(classify_repos.FORMAT_STREAK % workers, 2)

        def failing() -> bool:
            return clock.now < classify_repos.PAUSE_FIRST_SECONDS

        def script(request, number):
            if failing():
                return [_message("not json")]
            return _answer(_repository_of(request))
        model = _Batches(script, workers, failing)
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model,
                                  workers=workers, clock=clock, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["labeled"], str(len(names)))
        self.assertEqual(self.pauses(err), [("0:01:00", "1")])
        self.assertRegex(err, re.escape("WARNING repository_labeling: 20 final responses in a row "
                                        "of deepseek/deepseek-v4.1-flash")
                         + r".*" + re.escape(". Pausing every worker for 0:01:00 (pause 1 of 7)."))
        self.assertNotIn("Stopped the run", err)

    def test_responses_in_the_format_for_enough_repositories_make_the_next_pause_the_first(self):
        """Two streaks of failed responses, each through four pauses, with responses in the
        format for FORMAT_STREAK_REPOSITORIES repositories in a row between them. These responses
        show that the endpoint has recovered, so the second streak begins with the shortest pause
        again, and the run does not stop although the pauses of both streaks together exceed
        FORMAT_PAUSES."""
        clock = _Clock()
        names = [f"o/r{i:02d}" for i in range(30)]
        # With one worker, each streak of FORMAT_STREAK + 3 failed responses fails seven
        # repositories through their attempts and pauses before each of its last four responses.
        # The responses in the format between the streaks decide the eighth repository on its
        # third attempt and each of the next four on its first.
        pauses = 4
        streak = classify_repos.FORMAT_STREAK + pauses - 1
        recovery = classify_repos.FORMAT_STREAK_REPOSITORIES
        self.assertGreater(2 * pauses, classify_repos.FORMAT_PAUSES)
        failing = {*range(1, streak + 1),
                   *range(streak + recovery + 1, 2 * streak + recovery + 1)}

        def script(request, number):
            if number in failing:
                return [_message("not json")]
            return _answer(_repository_of(request))
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), _Model(script),
                                  workers=1, clock=clock, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pauses(err), self.waits(pauses) * 2)
        self.assertEqual(err.count(self.paused(20, 7, "0:01:00", 1)), 2)
        self.assertEqual(self.index()[0]["labeled"], str(len(names)))
        self.assertEqual(clock.now, 2 * classify_repos.PAUSE_FIRST_SECONDS * (1 + 2 + 4 + 8))
        self.assertNotIn("Stopped the run", err)

    def test_responses_in_the_format_for_too_few_repositories_leave_the_pauses_counting(self):
        """A streak of failed responses through four pauses, then responses in the format for one
        repository fewer than FORMAT_STREAK_REPOSITORIES, then failed responses again. Each
        response in the format ends a streak, so that the next pause waits for FORMAT_STREAK
        failed responses in a row, but the endpoint has not recovered: the pauses count on with
        waits that keep growing, and the run stops after FORMAT_PAUSES pauses."""
        clock = _Clock()
        names = [f"o/r{i:02d}" for i in range(40)]
        streak = classify_repos.FORMAT_STREAK + 3
        passing = classify_repos.FORMAT_STREAK_REPOSITORIES - 1
        failing = {*range(1, streak + 1), *range(streak + passing + 1, 200)}

        def script(request, number):
            if number in failing:
                return [_message("not json")]
            return _answer(_repository_of(request))
        model = _Model(script)
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model,
                                  workers=1, clock=clock, **DEEPSEEK)
        self.assertEqual(code, 1)
        self.assertEqual(self.pauses(err), self.waits(self.PAUSES))
        # After the responses in the format, FORMAT_STREAK failed responses in a row begin the
        # fifth pause, and each of the next three responses begins a further pause or stops the
        # run. Of the final responses after the first pause, the three failed responses of the
        # first streak, the responses in the format and the failed responses of the second streak
        # count.
        second = classify_repos.FORMAT_STREAK + 3
        self.assertEqual(len(model.requests), streak + passing + second)
        self.assertIn(f"ERROR repository_labeling: Stopped the run, because "
                      f"{self.STREAK.format(count=second, repositories=8)}, the last because "
                      f"{self.NO_CALL}. Through 7 pauses of every worker, {3 + second} of the "
                      f"{3 + passing + second} final responses after the first pause failed the "
                      f"format, without responses in the format for 5 repositories in a row.", err)
        self.assertEqual(clock.now,
                         classify_repos.PAUSE_FIRST_SECONDS * (1 + 2 + 4 + 8 + 16 + 32 + 64))

    def test_an_endpoint_that_fails_most_responses_stops_the_run_after_the_pauses(self):
        """An endpoint that fails about 95 of 100 final responses, over 32 workers, returns a
        response in the format now and then. Each such response ends a streak, but responses in
        the format for FORMAT_STREAK_REPOSITORIES repositories in a row do not follow, so the
        endpoint never recovers: the pauses count on, their waits keep growing, and the run stops
        after FORMAT_PAUSES pauses instead of paying for every attempt at every repository. The
        second model labels nothing."""
        rng = random.Random(7)
        failing = [rng.random() < 0.95 for _ in range(10_000)]
        names = [f"o/r{i:03d}" for i in range(300)]

        def script(request, number):
            if failing[number - 1]:
                return [_message("not json")]
            return _answer(_repository_of(request))
        model, second = _Model(script), _Glm(_always_true)
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model,
                                  workers=32, clock=_Clock(), no_second_model=False,
                                  second_client=second, **DEEPSEEK)
        self.assertEqual(code, 1)
        self.assertEqual(self.pauses(err), self.waits(self.PAUSES))
        self.assertIn("The endpoint does not call submit_classification with its schema. Label "
                      "with another endpoint or model", err)
        self.assertEqual(second.requests, [])
        # Responses in the format came between the pauses, more than one per pause.
        self.assertGreater(failing[:len(model.requests)].count(False),
                           classify_repos.FORMAT_PAUSES)
        self.assertLess(len(model.requests), len(names) * classify_repos.ATTEMPTS)

    def test_an_outage_at_the_end_of_the_main_round_pauses_the_retry_rounds(self):
        """The endpoint fails every response from the 191st request on, over 32 workers and 200
        repositories, so that the last ten repositories of the main round fail. Their failed
        responses pause every worker, and they use up their attempts during the pauses, so the
        main round ends while the endpoint still fails. The open streak counts the failed
        responses of the retry rounds, although their repositories failed an earlier round, and
        they pause every worker as in the main round. An outage that the pauses of the retry
        rounds outlast ends, and the classifying model labels every repository, without the
        second model. An outage longer than FORMAT_PAUSES pauses stops the run before the pass of
        the second model, instead of leaving the repositories to the second model or without a
        decision."""
        names = [f"o/r{i:03d}" for i in range(200)]
        for minutes, stops in ((60, False), (180, True)):
            with self.subTest(minutes=minutes):
                self.setUp()
                self.real_ceiling()
                clock, began = _Clock(), []

                def script(request, number, minutes=minutes):
                    if number > 190:
                        began[:] = began or [clock.now]
                        if clock.now - began[0] < minutes * 60:
                            return [_message("not json")]
                    return _answer(_repository_of(request))
                second = _Glm(_always_true)
                with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 60):
                    code, err = self.run_over(_independent(*names),
                                              _GitHub({n: (ENGLISH, n) for n in names}),
                                              _Model(script), workers=32, clock=clock,
                                              no_second_model=False, second_client=second,
                                              **DEEPSEEK)
                self.assertIn("Retry round 1 of", err)
                # The main round ends before its pauses run out. How many pauses it takes depends
                # on which requests of the 32 workers are in flight when the outage begins.
                main = err.split("Retry round 1 of")[0]
                self.assertLess(main.count("(pause "), classify_repos.FORMAT_PAUSES)
                decisions = [d for _, d in label_cache.all_decisions(self.cache)]
                self.assertEqual(second.requests, [])
                if stops:
                    self.assertEqual(code, 1)
                    self.assertEqual(self.pauses(err), self.waits(self.PAUSES))
                    self.assertIn("Stopped the run, because ", err)
                else:
                    self.assertEqual(code, 0, err)
                    self.assertEqual(len(decisions), len(names))
                    self.assertFalse([d for d in decisions if d.get("second_model")])
                    self.assertNotIn("Stopped the run", err)

    def test_a_response_that_the_service_ended_is_sent_again_after_a_pause(self):
        """The service can end a response with an error, here in the arguments of
        submit_classification, which are then not JSON, although the model did not fail the
        format. Such responses for two repositories pause every worker as server errors do, and
        the request is sent again after the pause. As for server errors, the first such response
        uses an attempt, because no other repository has failed yet, and so does the response
        after the pauses that use no attempt. No response counts as a failed response, and the
        attempts keep the temperature."""
        names = ("o/a", "o/b")
        ended = classify_repos.PAUSES_BEFORE_ATTEMPTS + 2
        model = _Ending(lambda request, number: number <= ended, names)
        with _short_pauses():
            code, err = self.run_over(_independent(*names),
                                      _GitHub({name: (ENGLISH, name) for name in names}), model,
                                      workers=2, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertIn("The API failed the requests of 2 repositories since its last response, the "
                      "last with ServiceEnded (the API ended the response with the error "
                      "server_error (The provider ended the response), the status failed, the "
                      "output items [function_call] and 10 output tokens). Pausing every worker "
                      "for 0:00:00, then sending each request again", err)
        self.assertNotIn("failed the response format", err)
        self.assertNotIn("Retrying it later", err)
        self.assertEqual(len(model.requests), ended + 2)
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(sum(d["usage"]["attempts"] for d in decisions), len(names) + 2)
        self.assertEqual({r["temperature"] for r in model.requests}, {0.6})
        for decision in decisions:
            with self.subTest(name=decision["name"]):
                self.assertEqual(decision["usage"]["failed_responses"], 0)
                self.assertNotIn("higher_temperature", decision)

    def test_a_body_with_only_an_error_is_a_response_that_the_service_ended(self):
        """When the provider fails after the headers, OpenRouter responds to a request of the
        Responses API that is not streamed with status 200 and a Response with the status failed
        that holds only an error, with the `error_type` at the top level, which the SDK's client
        parses into a Response without output items, or with an empty output when the body has
        one. In both forms, such Responses for two repositories pause every worker as server
        errors do, with the code, the message and the `error_type` of the error in the WARNING,
        and the request is sent again after the pause. As for server errors, the first such
        Response uses an attempt, because no other repository has failed yet, and so does the
        Response after the pauses that use no attempt. A Response that holds only an error is no
        failed response and, without usage, no response of its repository, and the attempts keep
        the temperature."""
        names = ("o/a", "o/b")
        ended = classify_repos.PAUSES_BEFORE_ATTEMPTS + 2
        for form, body in (("without output", PROVIDER_FAILED),
                           ("with an empty output", PROVIDER_FAILED_EMPTY)):
            with self.subTest(form=form):
                self.setUp()
                model = _ErrorBodies(lambda request, number: number <= ended, names, body=body)
                with _short_pauses():
                    code, err = self.run_over(_independent(*names),
                                              _GitHub({name: (ENGLISH, name) for name in names}),
                                              model, workers=2, **DEEPSEEK)
                self.assertEqual(code, 0, err)
                self.assertIn("The API failed the requests of 2 repositories since its last "
                              f"response, the last with {PROVIDER_FAILED_LOGGED}. Pausing every "
                              "worker for 0:00:00, then sending each request again", err)
                self.assertNotIn("Retrying it later", err)
                self.assertEqual(len(model.requests), ended + 2)
                decisions = [d for _, d in label_cache.all_decisions(self.cache)]
                self.assertEqual(sum(d["usage"]["attempts"] for d in decisions), len(names) + 2)
                self.assertEqual(sum(d["usage"]["responses"] for d in decisions), len(names))
                self.assertEqual(sum(d["usage"]["requests"] for d in decisions), len(names))
                [row] = label_cache.read_tokens(self.cache, self.index()[-1]["run_id"])
                self.assertEqual(row["requests"], len(names))
                self.assertEqual({r["temperature"] for r in model.requests}, {0.6})
                for decision in decisions:
                    with self.subTest(name=decision["name"]):
                        self.assertEqual(decision["usage"]["failed_responses"], 0)
                        self.assertNotIn("higher_temperature", decision)

    def test_an_outage_of_bodies_with_only_an_error_ends_as_an_outage_of_server_errors(self):
        """From the 21st request on, every request gets a body with status 200 that holds only
        an error, or the SDK's error for HTTP 502, over four workers and forty repositories. The
        pauses of every worker outlast an outage of an hour, and the run labels every
        repository. An outage without end stops the run once the pauses since the last response
        have taken PAUSE_TOTAL_SECONDS, with the last failure in the ERROR. Both forms of the
        outage end the same way."""
        names = [f"o/r{i:02d}" for i in range(40)]
        ended = {"body": PROVIDER_FAILED_LOGGED, "http": "InternalServerError (Server error)"}
        for form, minutes in [(form, minutes) for form in ended for minutes in (60, None)]:
            with self.subTest(form=form, minutes=minutes):
                self.setUp()
                clock, began = _Clock(), []

                class Provider(_Model):
                    async def create(self, form=form, minutes=minutes, **request):
                        if len(self.requests) >= 20 and (not began or minutes is None
                                                         or clock.now - began[0] < minutes * 60):
                            began[:] = began or [clock.now]
                            self.requests.append(request)
                            await asyncio.sleep(0)
                            if form == "body":
                                return _parsed(PROVIDER_FAILED)
                            raise _server_error(502)
                        return await super().create(**request)
                code, err = self.run_over(_independent(*names),
                                          _GitHub({n: (ENGLISH, n) for n in names}),
                                          Provider(_always_true), workers=4, clock=clock,
                                          **DEEPSEEK)
                pauses = re.findall(r"Pausing every worker for (\S+), then", err)
                self.assertGreater(len(pauses), classify_repos.PAUSES_BEFORE_ATTEMPTS)
                if minutes:
                    self.assertEqual(code, 0, err)
                    self.assertEqual(len(list(label_cache.all_decisions(self.cache))),
                                     len(names))
                else:
                    self.assertEqual(code, 1)
                    self.assertRegex(err, re.escape("ERROR repository_labeling: Stopped the run, "
                                                    "because the API failed every request "
                                                    "through ")
                                     + r"\S+" + re.escape(" of pauses since its last response, "
                                                          f"the last with {ended[form]}."))
                    self.assertGreaterEqual(clock.now, classify_repos.PAUSE_TOTAL_SECONDS)

    def test_responses_that_the_service_ended_do_not_count_toward_the_streak(self):
        """More responses in a row than FORMAT_STREAK, for more repositories than
        FORMAT_STREAK_REPOSITORIES, end with an error of the service, in the arguments of
        submit_classification or before the model called it. They are failures of the service,
        not of the response format, whether or not they have the call: no worker pauses for a
        streak, the run does not stop, and the later attempts at their repositories keep the
        temperature. With one worker, no other repository fails at the same time, and each such
        response uses an attempt, as a server error does."""
        names = [f"o/r{i:02d}" for i in range(10)]
        # Eight repositories through every attempt.
        ended = 8 * classify_repos.ATTEMPTS
        self.assertGreater(ended, classify_repos.FORMAT_STREAK)
        for inside, items in ((True, "function_call"), (False, "reasoning")):
            with self.subTest(inside=inside):
                self.setUp()
                model = _Ending(lambda request, number: number <= ended, inside=inside)
                code, err = self.run_over(_repositories(*names),
                                          _GitHub({n: (ENGLISH, n) for n in names}), model,
                                          workers=1, clock=_Clock(), **DEEPSEEK)
                self.assertEqual(code, 0, err)
                self.assertNotIn("failed the response format", err)
                self.assertIn(f"Could not decide o/r00 (the API ended the response with the error "
                              f"server_error (The provider ended the response), the status "
                              f"failed, the output items [{items}] and 10 output tokens). "
                              f"Retrying it later", err)
                self.assertEqual(self.index()[0]["labeled"], str(len(names)))
                self.assertEqual({r["temperature"] for r in model.requests}, {0.6})
                for _, decision in label_cache.all_decisions(self.cache):
                    self.assertNotIn("higher_temperature", decision)
                    self.assertEqual(decision["usage"]["failed_responses"], 0)

    def test_a_response_that_a_content_filter_ended_is_a_failed_response(self):
        """A response with the status incomplete for another reason than the output limit, such
        as a content filter, is the model's failed response, in the arguments of
        submit_classification and before the call alike: the content decides such a reason, and
        the same request would end the same way. No worker pauses for the service, the next
        attempts at the repository use the higher temperature, and once the retry rounds leave
        it without a decision, the second model labels it."""
        rounds = classify_repos.ATTEMPTS * (classify_repos.RETRY_ROUNDS + 1)
        for inside, error in (
                (True, "the arguments of submit_classification are not JSON (Unterminated string "
                       "starting at: line 1 column 64 (char 63), 67 characters ending in "
                       f"{CUT_OFF!r}) (the status incomplete for the reason content_filter, the "
                       "output items [function_call] and 10 output tokens)"),
                (False, "the model's response does not call submit_classification (the status "
                        "incomplete for the reason content_filter, the output items [reasoning] "
                        "and 10 output tokens)")):
            with self.subTest(inside=inside):
                self.setUp()
                model = _Ending(lambda request, number: _repository_of(request) == "o/b",
                                error=None, inside=inside)
                second = _Glm(_always_true)
                code, err = self.run_over(_independent("o/a", "o/b"),
                                          _GitHub({"o/a": (ENGLISH, "s1"),
                                                   "o/b": (ENGLISH, "s2")}),
                                          model, workers=1, no_second_model=False,
                                          second_client=second, **DEEPSEEK)
                self.assertEqual(code, 0, err)
                self.assertNotIn("The API failed the requests", err)
                self.assertEqual([request["temperature"] for request in model.requests
                                  if _repository_of(request) == "o/b"],
                                 [0.6] + [1.0] * (rounds - 1))
                decision = {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}["o/b"]
                self.assertEqual((decision["second_model"]["reason"],
                                  decision["second_model"]["error"]), ("response_format", error))
                self.assertEqual(len(second.requests), 1)

    def test_only_an_error_or_the_status_failed_is_a_response_that_the_service_ended(self):
        """A response with an error or with the status failed is one that the service ended,
        with a call of submit_classification or without one, unless it stopped at its output
        limit. A response with the status incomplete, for the output limit or for a content
        filter, and a completed response stay the model's. A message of the error with a line
        break stays on one line of the log."""
        def response(status, reason=None, error=None, output_tokens=1_000, call=True):
            return SimpleNamespace(
                status=status, error=error, incomplete_details=SimpleNamespace(reason=reason),
                output=[_raw_call(CUT_OFF)] if call else [_reasoning()],
                usage=SimpleNamespace(output_tokens=output_tokens))
        ended = "the error server_error (The provider ended the response)"
        for case, expected in (
                (response("failed", error=PROVIDER_ENDED), ended),
                (response("failed", error=PROVIDER_ENDED, call=False), ended),
                (response("completed", error=PROVIDER_ENDED), ended),
                (response("failed"), "the status failed and no error"),
                (response("failed", call=False), "the status failed and no error"),
                (response("failed", error=PROVIDER_ENDED, output_tokens=65_536), ""),
                (response("incomplete", "content_filter"), ""),
                (response("incomplete", "content_filter", call=False), ""),
                (response("incomplete", "max_output_tokens", error=PROVIDER_ENDED), ""),
                (response("completed"), ""),
                (response("completed", call=False), "")):
            with self.subTest(status=case.status, reason=case.incomplete_details.reason,
                              error=case.error is not None, output=case.output[0].type,
                              output_tokens=case.usage.output_tokens):
                self.assertEqual(classify_repos.service_ended(case, 65_536), expected)
        broken = SimpleNamespace(code="server_error", message="ended\nearly")
        self.assertEqual(classify_repos.service_ended(response("failed", error=broken)),
                         "the error server_error ('ended\\nearly')")

    def test_the_text_of_a_failed_response_names_how_the_response_ended(self):
        """The text of a failed response is what the log keeps of it, and each names the status
        of the response, the types of its output items and its output tokens. Arguments of
        submit_classification that are not JSON also give the error of `json.loads`, their length
        and their last LOGGED_ARGUMENT_CHARS characters, with a line break escaped, and so do the
        failures of the schema after a call. A call in the format still passes."""
        def response(output, status="completed"):
            return SimpleNamespace(status=status, error=None, incomplete_details=None,
                                   output=output, usage=SimpleNamespace(output_tokens=812))

        def failure(output):
            with self.assertRaises(classify_repos.ModelFailure) as caught:
                classify_repos.checked_response(response(output), "o/r")
            return caught.exception
        ended = " (the status completed, the output items [function_call] and 812 output tokens)"
        cut = '{"repo_name":"o/r","exclusions":{"inactive":{"applies":"fal'
        failed = failure([_raw_call(cut)])
        self.assertEqual(failed.kind, classify_repos.RESPONSE_FORMAT)
        self.assertEqual(str(failed), f"the arguments of submit_classification are not JSON "
                                      f"(Unterminated string starting at: line 1 column 56 "
                                      f"(char 55), {len(cut)} characters ending in {cut!r})"
                                      f"{ended}")
        self.assertIn("59 characters", str(failed))
        long = '{"repo_name": "o/r", "justification": "' + "word " * 40 + '\nline'
        failed = failure([_raw_call(long)])
        tail = long[-classify_repos.LOGGED_ARGUMENT_CHARS:]
        self.assertTrue(str(failed).endswith(f"{len(long)} characters ending in {tail!r})"
                                             f"{ended}"))
        self.assertNotIn("\n", str(failed))
        for arguments, error in (('[]', "the model's response is not a JSON object"),
                                 ('{"repo_name": "o/r"}',
                                  "the model's response sets engineering to None")):
            with self.subTest(arguments=arguments):
                failed = failure([_raw_call(arguments)])
                self.assertEqual((failed.kind, str(failed)),
                                 (classify_repos.RESPONSE_FORMAT, f"{error}{ended}"))
        failed = failure([_reasoning()])
        self.assertEqual((failed.kind, str(failed)),
                         (classify_repos.RESPONSE_FORMAT,
                          "the model's response does not call submit_classification (the status "
                          "completed, the output items [reasoning] and 812 output tokens)"))
        checklist = response(_answer("o/r"))
        self.assertEqual(classify_repos.checked_response(checklist, "o/r")["repo_name"], "o/r")

    def test_a_response_without_the_call_keeps_the_text_of_its_message(self):
        """A final response with the output items [reasoning, message] and no call of
        submit_classification, as the SDK's client parses it from the body of the response,
        fails the response format. The failure keeps the length of the text of the message and
        its first and its last KEPT_MESSAGE_CHARS characters, the text that the SDK's
        `output_text` gives, and its text names the length and the first LOGGED_MESSAGE_CHARS
        characters with the line break escaped. A short message is kept whole at both ends, and
        the text of two parts is kept as one. A response at its output limit without the call
        keeps the text of its message as well, and none without a message. A response with a
        call of submit_classification keeps no message, and its text names none."""
        keep, logged = classify_repos.KEPT_MESSAGE_CHARS, classify_repos.LOGGED_MESSAGE_CHARS
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}

        def message(*parts):
            return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": part, "annotations": []}
                                for part in parts]}

        def parsed(*output, status="completed", reason=None, tokens=8_332):
            return _parsed({"id": "resp_1", "object": "response", "status": status,
                            "incomplete_details": {"reason": reason} if reason else None,
                            "output": list(output),
                            "usage": {"input_tokens": 9_000, "output_tokens": tokens,
                                      "input_tokens_details": {"cached_tokens": 0},
                                      "output_tokens_details": {"reasoning_tokens": 8_000},
                                      "total_tokens": 9_000 + tokens}})

        def failure(response, limit=None):
            with self.assertRaises(classify_repos.ModelFailure) as caught:
                classify_repos.checked_response(response, "o/r", limit)
            return caught.exception
        response = parsed(reasoning, message(WRITTEN))
        self.assertEqual(response.output_text, WRITTEN)
        self.assertGreater(len(WRITTEN), 2 * keep)
        self.assertIn("\n", WRITTEN[:logged])
        failed = failure(response)
        self.assertEqual((failed.kind, failed.model_message),
                         (classify_repos.RESPONSE_FORMAT,
                          {"chars": len(WRITTEN), "start": WRITTEN[:keep],
                           "end": WRITTEN[-keep:]}))
        self.assertEqual(str(failed),
                         f"the model's response does not call submit_classification (the status "
                         f"completed, the output items [reasoning, message], 8,332 output tokens "
                         f"and a message of {len(WRITTEN):,} characters that begins with "
                         f"{WRITTEN[:logged]!r})")
        self.assertNotIn("\n", str(failed))
        response = parsed(reasoning, message("The checklist ", "follows."))
        self.assertEqual(failure(response).model_message,
                         {"chars": len(response.output_text), "start": "The checklist follows.",
                          "end": "The checklist follows."})
        at_limit = failure(parsed(reasoning, message(WRITTEN), status="incomplete",
                                  reason="max_output_tokens", tokens=65_536), 65_536)
        self.assertEqual((at_limit.kind, at_limit.model_message["chars"]),
                         (classify_repos.OUTPUT_LIMIT, len(WRITTEN)))
        self.assertIn(f"a message of {len(WRITTEN):,} characters", str(at_limit))
        self.assertIsNone(failure(parsed(reasoning, status="incomplete",
                                         reason="max_output_tokens", tokens=65_536),
                                  65_536).model_message)
        called = failure(parsed(message(WRITTEN), {
            "type": "function_call", "id": "fc_1", "call_id": "call-checklist",
            "name": "submit_classification", "arguments": '{"repo_name": "o/r"}',
            "status": "completed"}))
        self.assertEqual((called.model_message, str(called)),
                         (None, "the model's response sets engineering to None (the status "
                                "completed, the output items [message, function_call] and 8,332 "
                                "output tokens)"))

    def test_arguments_that_the_output_limit_cut_off_are_a_response_at_the_output_limit(self):
        """A response with the status incomplete for the reason max_output_tokens, at the output
        limit of its request, stopped in the arguments of submit_classification, which are then
        not JSON. It fails as a response at the output limit, as a response that stopped before
        the call does, and its text names how it ended. A completed response with the same
        arguments fails the response format."""
        limit = 65_536
        cut = '{"repo_name": "o/r", "readme_words_by_language": [{"language": "Eng'
        error = (f"(Unterminated string starting at: line 1 column 64 (char 63), {len(cut)} "
                 f"characters ending in {cut!r})")
        for status, reason, kind, text in (
                ("incomplete", "max_output_tokens", classify_repos.OUTPUT_LIMIT,
                 f"the model's response stopped at its output limit in the arguments of "
                 f"submit_classification, which are not JSON {error} (the status incomplete for "
                 f"the reason max_output_tokens, the output items [reasoning, function_call] and "
                 f"65,536 output tokens)"),
                ("completed", None, classify_repos.RESPONSE_FORMAT,
                 f"the arguments of submit_classification are not JSON {error} (the status "
                 f"completed, the output items [reasoning, function_call] and 1,000 output "
                 f"tokens)")):
            with self.subTest(status=status):
                response = SimpleNamespace(
                    status=status, error=None,
                    incomplete_details=SimpleNamespace(reason=reason) if reason else None,
                    output=[_reasoning(), _raw_call(cut)],
                    usage=SimpleNamespace(output_tokens=limit if reason else 1_000))
                with self.assertRaises(classify_repos.ModelFailure) as caught:
                    classify_repos.checked_response(response, "o/r", limit)
                self.assertEqual((caught.exception.kind, str(caught.exception)), (kind, text))

    def test_responses_cut_off_in_the_arguments_by_the_output_limit_do_not_count_toward_the_streak(
            self):
        """More final responses in a row than FORMAT_STREAK, for more repositories than
        FORMAT_STREAK_REPOSITORIES, stop at their output limit in the arguments of
        submit_classification. They are failed responses at the output limit, which an endpoint
        that holds the model to the schema returns too: no worker pauses for a streak, the next
        attempts at their repositories use the higher temperature with its output limit, and the
        second model labels the repositories that every attempt left without a decision with the
        reason output_limit."""
        self.with_ceiling(lambda repositories: 10)
        bad = [f"o/r{i}" for i in range(7)]
        cut = '{"repo_name": "o/r", "readme_words_by_language": [{"language": "Eng'

        class CutOff(_Runaway):
            def __init__(self, names):
                super().__init__(names)
                runs_away = self.script

                def script(request, number):
                    output = runs_away(request, number)
                    return [*output, _raw_call(cut)] if self.runs_away(request) else output
                self.script = script
        second = _Glm(_always_true)
        code, err = self.run_over(_independent(*bad, "o/x", "o/y"),
                                  _GitHub({n: (ENGLISH, n) for n in [*bad, "o/x", "o/y"]}),
                                  CutOff(set(bad)), workers=1, clock=_Clock(),
                                  no_second_model=False, second_client=second, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertNotIn("failed the response format", err)
        self.assertIn("the model's response stopped at its output limit in the arguments of "
                      "submit_classification, which are not JSON", err)
        decisions = {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}
        self.assertEqual({name: decisions[name]["second_model"]["reason"] for name in bad},
                         dict.fromkeys(bad, "output_limit"))
        self.assertEqual(
            decisions["o/r0"]["second_model"]["classifying_model_failures"][
                "higher_temperature_attempts"], TestTheSecondModel.ROUNDS - 1)

    def test_every_failed_response_reaches_the_log(self):
        """The first two attempts at o/a end in failed responses of different kinds, and the
        third labels it. The log names each failed response with its text, although no failure
        is the last of the repository or of a streak."""
        cut = '{"repo_name": "o/a", "engineering": "tr'

        def script(request, number):
            return ([_raw_call(cut)], [_reasoning()])[number - 1] if number <= 2 \
                else _answer("o/a")
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  _Model(script), workers=1, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        attempt = "INFO repository_labeling: Attempt {} of 3 at o/a by deepseek/deepseek-v4.1-flash"
        self.assertIn(f"{attempt.format(1)} failed (the arguments of submit_classification are "
                      f"not JSON (Unterminated string starting at: line 1 column 37 (char 36), "
                      f"{len(cut)} characters ending in {cut!r}) (the status none, the output "
                      f"items [function_call] and 10 output tokens)).\n", err)
        self.assertIn(f"{attempt.format(2)} failed (the model's response does not call "
                      f"submit_classification (the status none, the output items [reasoning] "
                      f"and 10 output tokens)).\n", err)
        self.assertNotIn("Could not decide", err)

    def test_a_response_sent_again_after_a_pause_counts_toward_the_requests_of_its_repository(
            self):
        """The service ends the first responses for two repositories, which reach it together.
        The first fails an attempt, because no other repository has failed yet, and the second
        pauses every worker and is sent again. Each response that the API bills counts toward the
        requests and the responses of its repository, as its tokens do: the repository of the
        second counts both of its responses as requests of the attempt that decided it, and the
        repository of the first counts the response of its failed attempt among its
        responses."""
        names = ("o/a", "o/b")
        model = _Ending(lambda request, number: number <= 2, names)
        with _short_pauses():
            code, err = self.run_over(_independent(*names),
                                      _GitHub({name: (ENGLISH, name) for name in names}), model,
                                      workers=2, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        usages = [d["usage"] for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(sorted((usage["attempts"], usage["requests"], usage["responses"],
                                 usage["input_tokens"]) for usage in usages),
                         [(1, 2, 2, 200), (2, 1, 2, 100)])
        self.assertEqual(sum(usage["responses"] for usage in usages), len(model.requests))

    def test_an_attempt_that_the_service_ended_counts_its_requests(self):
        """With one worker, no other repository fails at the same time, and each response that
        the service ended uses an attempt. The `failed` line of the repository counts each such
        response among the requests of the failed attempts, beside its tokens, and the entry of
        each attempt keeps the length, the start and the end of the arguments of
        submit_classification at which the service ended the response."""
        model = _Ending(lambda request, number: True)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  workers=1, **DEEPSEEK)
        self.assertEqual(code, 1)
        line = next(label_cache.partial_lines(next(self.cache.glob("*.partial.jsonl")),
                                              ("failed",)))
        attempts = classify_repos.ATTEMPTS
        self.assertEqual({key: line["usage"][key] for key in ("attempts", "requests",
                                                              "input_tokens")},
                         {"attempts": attempts, "requests": attempts,
                          "input_tokens": 100 * attempts})
        self.assertNotIn("model_failure", line)
        keep = classify_repos.KEPT_ARGUMENT_CHARS
        self.assertEqual([attempt.get("arguments") for attempt in line["attempts"]],
                         [{"chars": len(CUT_OFF), "start": CUT_OFF[:keep],
                           "end": CUT_OFF[-keep:]}] * attempts)

    def test_an_attempt_that_the_service_ended_keeps_the_text_of_its_message(self):
        """The service ends every response for o/a with an error after the text of a message
        instead of the call of submit_classification. With one worker, each such response uses
        an attempt, and the entry of each attempt keeps the length of the text and its first and
        its last KEPT_MESSAGE_CHARS characters, and its error names the length and the first
        LOGGED_MESSAGE_CHARS characters."""
        keep, logged = classify_repos.KEPT_MESSAGE_CHARS, classify_repos.LOGGED_MESSAGE_CHARS

        class Ended(_Model):
            async def create(self, **request):
                response = await super().create(**request)
                response.status, response.error = "failed", PROVIDER_ENDED
                return response
        model = Ended(lambda request, number: [_reasoning(), _text_message(WRITTEN)])
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  workers=1, **DEEPSEEK)
        self.assertEqual(code, 1)
        line = next(label_cache.partial_lines(next(self.cache.glob("*.partial.jsonl")),
                                              ("failed",)))
        self.assertEqual([attempt.get("message") for attempt in line["attempts"]],
                         [{"chars": len(WRITTEN), "start": WRITTEN[:keep],
                           "end": WRITTEN[-keep:]}] * classify_repos.ATTEMPTS)
        self.assertEqual(line["attempts"][0]["error"],
                         f"the API ended the response with the error server_error (The provider "
                         f"ended the response), the status failed, the output items [reasoning, "
                         f"message], 10 output tokens and a message of {len(WRITTEN):,} "
                         f"characters that begins with {WRITTEN[:logged]!r}")

    def test_a_failure_of_the_response_format_keeps_the_start_and_the_end_of_the_arguments(self):
        """The `failed` line of a repository that the model left without a decision lists each
        failed attempt. An attempt that failed the response format with a call of
        submit_classification keeps the length of the arguments and their first and their last
        KEPT_ARGUMENT_CHARS characters, from which a person can tell YAML from JSON that was cut
        off. An attempt without the call keeps none."""
        keep = classify_repos.KEPT_ARGUMENT_CHARS
        yaml = ("repo_name: o/a\nreadme_words_by_language:\n"
                + "  - language: English\n    words: 40\n" * 40 + "engineering: true\n")
        self.assertGreater(len(yaml), 2 * keep)
        short = '{"repo_name": "o/a"}'
        outputs = ([_raw_call(yaml)], [_raw_call(short)], [_message("not json")])
        model = _Model(lambda request, number: outputs[(number - 1) % len(outputs)])
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  workers=1)
        self.assertEqual(code, 1)
        lines = list(label_cache.partial_lines(next(self.cache.glob("*.partial.jsonl")),
                                               ("failed",)))
        self.assertEqual(len(lines), classify_repos.RETRY_ROUNDS + 1)
        tail = yaml[-classify_repos.LOGGED_ARGUMENT_CHARS:]
        for line in lines:
            called = " (the status none, the output items [function_call] and 10 output tokens)"
            self.assertEqual([attempt["error"] for attempt in line["attempts"]],
                             [f"the arguments of submit_classification are not JSON (Expecting "
                              f"value: line 1 column 1 (char 0), {len(yaml):,} characters ending "
                              f"in {tail!r}){called}",
                              f"the model's response sets engineering to None{called}",
                              self.NO_CALL])
            self.assertEqual([attempt.get("arguments") for attempt in line["attempts"]],
                             [{"chars": len(yaml), "start": yaml[:keep], "end": yaml[-keep:]},
                              {"chars": len(short), "start": short, "end": short}, None])

    def test_a_failed_attempt_without_the_call_keeps_the_text_of_its_message(self):
        """Every attempt at o/a fails: the first with the text of a message instead of the call
        of submit_classification, the second with a message beside a call with arguments that
        lack the label, and the third with a reasoning item alone. The `failed` line of each
        round keeps the length of the text of the first attempt's message and its first and its
        last KEPT_MESSAGE_CHARS characters, and keeps nothing of the message beside the call or
        of the third response. The INFO line of the first attempt names the length and the
        first LOGGED_MESSAGE_CHARS characters of the text, with its line break escaped, so that
        the text cannot break the line of the log."""
        keep, logged = classify_repos.KEPT_MESSAGE_CHARS, classify_repos.LOGGED_MESSAGE_CHARS
        outputs = ([_reasoning(), _text_message(WRITTEN)],
                   [_text_message("The checklist follows."), _raw_call('{"repo_name": "o/a"}')],
                   [_reasoning()])
        model = _Model(lambda request, number: outputs[(number - 1) % len(outputs)])
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}), model,
                                  workers=1, **DEEPSEEK)
        self.assertEqual(code, 1)
        lines = list(label_cache.partial_lines(next(self.cache.glob("*.partial.jsonl")),
                                               ("failed",)))
        self.assertEqual(len(lines), classify_repos.RETRY_ROUNDS + 1)
        for line in lines:
            self.assertEqual([attempt.get("message") for attempt in line["attempts"]],
                             [{"chars": len(WRITTEN), "start": WRITTEN[:keep],
                               "end": WRITTEN[-keep:]}, None, None])
            self.assertEqual([bool(attempt.get("arguments")) for attempt in line["attempts"]],
                             [False, True, False])
        self.assertIn(f"INFO repository_labeling: Attempt 1 of 3 at o/a by "
                      f"deepseek/deepseek-v4.1-flash failed (the model's response does not call "
                      f"submit_classification (the status none, the output items [reasoning, "
                      f"message], 10 output tokens and a message of {len(WRITTEN):,} characters "
                      f"that begins with {WRITTEN[:logged]!r})).\n", err)
        self.assertIn("INFO repository_labeling: Attempt 2 of 3 at o/a by "
                      "deepseek/deepseek-v4.1-flash failed (the model's response sets engineering "
                      "to None (the status none, the output items [message, function_call] and "
                      "10 output tokens)).\n", err)
        self.assertNotIn("\nIt holds", err)

    def test_a_call_of_another_tool_keeps_the_text_of_its_message(self):
        """In the first attempt at o/a and at o/b, the model calls get_file_tree in every round
        and also in the request that offers only submit_classification, beside the text of a
        message at o/a and without one at o/b. Each attempt fails as a tool call, and the next
        attempt labels its repository at 1.0. The record of the higher temperature of o/a keeps
        the length, the start and the end of the text of the message, and its error names how
        the response ended with the length and the start of the text, with the line break
        escaped. The record of o/b keeps no message, and its error names how the response
        ended."""
        keep, logged = classify_repos.KEPT_MESSAGE_CHARS, classify_repos.LOGGED_MESSAGE_CHARS

        def script(request, number):
            name = _repository_of(request)
            if request["temperature"] == 1.0:
                return _answer(name)
            call = _call("get_file_tree", {}, f"call-{number}")
            if len(request["tools"]) > 1 or name == "o/b":
                return [call]
            return [_text_message(WRITTEN), call]
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        code, err = self.run_over(_repositories("o/a", "o/b"), github, _Model(script),
                                  **DEEPSEEK)
        self.assertEqual(code, 0, err)
        decisions = {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}
        offered = ("the model called another tool in a request that offers only "
                   "submit_classification")
        higher = decisions["o/a"]["higher_temperature"]
        self.assertEqual((higher["reason"], higher["message"]),
                         ("tool_call", {"chars": len(WRITTEN), "start": WRITTEN[:keep],
                                        "end": WRITTEN[-keep:]}))
        self.assertEqual(higher["error"],
                         f"{offered} (the status none, the output items [message, function_call], "
                         f"10 output tokens and a message of {len(WRITTEN):,} characters that "
                         f"begins with {WRITTEN[:logged]!r})")
        higher = decisions["o/b"]["higher_temperature"]
        self.assertEqual((higher["reason"], "message" in higher), ("tool_call", False))
        self.assertEqual(higher["error"], f"{offered} (the status none, the output items "
                                          f"[function_call] and 10 output tokens)")

    def test_the_responses_of_a_repository_add_up_over_its_retry_rounds(self):
        """The comparison of models weighs the share of failed responses. A repository that
        fails every attempt of the first round and is decided in the second counts the failed
        responses of both rounds."""
        def script(request, number):
            if number <= classify_repos.ATTEMPTS:
                return [_message("not json")]
            return _answer(_repository_of(request))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  _Model(script), workers=1)
        self.assertEqual(code, 0, err)
        [decision] = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual((decision["usage"]["responses"], decision["usage"]["failed_responses"]),
                         (classify_repos.ATTEMPTS + 1, classify_repos.ATTEMPTS))

    def test_repositories_that_always_fail_do_not_stop_a_run_in_its_retry_rounds(self):
        """A retry round holds nothing but the repositories that failed, and so does the main
        round of a resumed run. Seven repositories whose responses never follow the format,
        among sixty that do, are left without a decision. The run neither pauses nor stops as if
        the endpoint ignored the format, either in the retry rounds or when the next run resumes
        it with only them pending."""
        self.with_ceiling(lambda repositories: 10)
        names = [f"o/r{i:02d}" for i in range(67)]
        bad = set(names[5::10][:7])

        def script(request, number):
            name = _repository_of(request)
            return [_message("not json")] if name in bad else _answer(name)
        github = _GitHub({n: (ENGLISH, n) for n in names})
        code, err = self.run_over(_repositories(*names), github, _Model(script), workers=8)
        self.assertEqual(code, 0, err)
        self.assertNotIn("failed the response format", err)
        self.assertEqual(len(bad), 7)

    def test_a_resumed_run_with_only_failed_repositories_left_does_not_stop(self):
        """The run ends incomplete with more repositories without a decision than its ceiling.
        The next invocation has only them pending, all failing again, and resumes without the
        pauses and the stop for an endpoint that ignores the format."""
        names = [f"o/r{i:02d}" for i in range(50)]
        bad = set(names[::2])

        def script(request, number):
            name = _repository_of(request)
            return [_message("not json")] if name in bad else _answer(name)
        github = _GitHub({n: (ENGLISH, n) for n in names})
        code, err = self.run_over(_repositories(*names), github, _Model(script), workers=1)
        self.assertEqual(code, 1)
        self.assertNotIn("Stopped the run", err)
        self.assertNotIn("failed the response format", err)
        model = _Model(script)
        code, err = self.run_over(_repositories(*names), github, model, workers=1)
        self.assertEqual(code, 1)
        self.assertIn("resumed", err)
        self.assertNotIn("Stopped the run", err)
        self.assertNotIn("failed the response format", err)
        self.assertEqual(len(model.requests), len(bad) * classify_repos.ATTEMPTS
                         * (classify_repos.RETRY_ROUNDS + 1))

    def test_a_run_that_tries_again_the_failed_repositories_of_a_finished_run_does_not_stop(
            self):
        """A run over the sampling frame finished with eight repositories without a decision,
        among sixty that it labeled, because every response for them failed the response format.
        No collection over the sample has begun, so the next invocation over the same retrieval
        tries the eight again in a new run (`complete_finished_run`). That run reuses every other
        decision, and its main round holds nothing but the eight. Their responses count as the
        responses of repositories that failed an earlier round, and the run finishes with them
        left without a decision again, without the pauses and the stop for an endpoint that
        ignores the format. Counted, their failures would pause every worker through
        FORMAT_PAUSES pauses and then stop the run with exit 1."""
        self.with_ceiling(lambda repositories: 10)
        failing = [f"o/f{i}" for i in range(8)]
        names = [name for i, bad in enumerate(failing)
                 for name in (bad, *(f"o/g{j}" for j in range(5 * i, 5 * i + 5)))]
        names += [f"o/g{j}" for j in range(40, 60)]

        def script(request, number):
            name = _repository_of(request)
            return [_message("Here is my checklist.")] if name in failing else _answer(name)
        repositories = _independent(*names)
        github = _GitHub({name: (ENGLISH.replace("files", f"files of {name}"), name)
                          for name in names})
        code, err = self.run_over(repositories, github, _Model(script), workers=8,
                                  clock=_Clock())
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(row["repo_name"] for row in self.rows("repos_excluded.csv")
                                if row["reason"] == "undecided"), failing)
        model, clock = _Model(script), _Clock()
        code, err = self.run_over(repositories, github, model, workers=8, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual([row["outcome"] for row in self.index()], ["finished", "finished"])
        self.assertEqual({_repository_of(request) for request in model.requests}, set(failing))
        self.assertNotIn("failed the response format", err)
        self.assertEqual(clock.now, 0)
        self.assertEqual(sorted(row["repo_name"] for row in self.rows("repos_excluded.csv")
                                if row["reason"] == "undecided"), failing)

    def test_failed_responses_between_responses_in_the_format_do_not_stop_the_run(self):
        """A working endpoint fails a response now and then. Here the first attempt at each
        repository fails and the second follows the format, more failures in all than
        FORMAT_STREAK but never two in a row, and no worker pauses."""
        names = [f"o/r{i}" for i in range(25)]
        tried = set()

        def script(request, number):
            name = _repository_of(request)
            if name not in tried:
                tried.add(name)
                return [_message("not json")]
            return _answer(name)
        model = _Model(script)
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 50)
        self.assertNotIn("failed the response format", err)


class TestSpentCredit(_RunCase):
    """A service that refuses the requests for spent credit pauses the run until the credit is
    back, and the refused requests use no attempt at a label and fail no repository."""

    def setUp(self):
        super().setUp()
        self.status = self.tmp / "repository_labeling.status.json"
        patcher = mock.patch.dict(os.environ, {"AGENTCONTEXT_STEP_STATUS": str(self.status)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_openrouter_run_waits_for_credit_and_continues_by_itself(self):
        """The run checks the credit of the OpenRouter API key, a check that costs nothing, and
        sends no request while the key shows no credit. While the run waits, the runner's state
        records the wait."""
        names = ("o/a", "o/b", "o/c")
        clock = _Clock()
        refusals = []

        def script(request, number):
            if number <= 2:
                refusals.append(_repository_of(request))
                raise _openrouter_error(openai.APIStatusError, 402)
            return _answer(_repository_of(request))
        credit = _KeyCredit(5.0, 0, 0, 5.0, status=self.status)
        model = _Model(script)
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model, clock=clock,
                                  key_credit=credit, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(sorted(d["name"] for d in decisions), list(names))
        self.assertEqual({d["usage"]["attempts"] for d in decisions}, {1})
        self.assertNotIn("Could not decide", err)
        self.assertIn("WARNING repository_labeling: OpenRouter refuses the requests for "
                      "insufficient credit (HTTP 402). Every worker waits for credit. To "
                      "continue, add "
                      "credits at https://openrouter.ai/settings/credits, or raise the credit "
                      "limit of the OpenRouter API key. The run checks the credit every 0:10:00 "
                      "and continues by itself", err)
        self.assertIn("INFO repository_labeling: OpenRouter shows no credit after", err)
        self.assertIn("INFO repository_labeling: OpenRouter responds again after", err)
        # The first check comes before the first request, the others while the run waits.
        waiting = [status for status in credit.seen[1:] if status]
        self.assertTrue(waiting)
        self.assertEqual({(s["waiting_for"], s["service"], s["to_continue"]) for s in waiting},
                         {("credit", "OpenRouter", endpoints_top_up())})
        self.assertFalse(self.status.exists())
        # The refused requests cost nothing, and the tokens file counts only the responses.
        [row] = label_cache.read_tokens(self.cache, self.index()[-1]["run_id"])
        self.assertEqual(row["requests"], len(model.requests) - len(refusals))

    def test_a_body_with_an_error_of_the_code_402_is_no_refusal_for_spent_credit(self):
        """A key or an account out of credits gets a request error with "the same status code as
        `error.code`" (openrouter.ai/docs/api_reference/errors-and-debugging), and no 402 in a
        body with status 200 is documented. A body with status 200 that holds an error is a
        response that the service ended, whatever the code of the error, also the code 402 with
        the `error_type` payment_required: every worker pauses as for a failure of the service,
        no worker waits for credit, and the first such body uses an attempt, because no other
        repository has failed yet."""
        names = ("o/a", "o/b")
        body = {**PROVIDER_FAILED, "error_type": "payment_required",
                "error": {"code": 402, "message": "Insufficient credits",
                          "metadata": {"limit_source": "openrouter_credits"}}}
        model = _ErrorBodies(lambda request, number: number <= 2, names, body=body)
        with _short_pauses(CREDIT_CHECK_SECONDS=0.001):
            code, err = self.run_over(_independent(*names),
                                      _GitHub({n: (ENGLISH, n) for n in names}), model,
                                      key_credit=_KeyCredit(5.0), workers=2, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(sorted(d["name"] for d in decisions), list(names))
        self.assertNotIn("waits for credit", err)
        self.assertIn("The API failed the requests of 2 repositories since its last response, the "
                      "last with ServiceEnded (the API ended the response with the error 402 "
                      "(Insufficient credits) with the error_type payment_required, the status "
                      "failed, no output items and no count of the output tokens). Pausing every "
                      "worker", err)
        self.assertEqual(sum(d["usage"]["attempts"] for d in decisions), len(names) + 1)
        self.assertNotIn("Retrying it later", err)

    def test_a_body_with_only_an_error_ends_the_wait_for_credit(self):
        """OpenRouter sends the status 200 once the provider accepts the request, and a request
        of a key without credit gets a request error instead. A body with status 200 that holds
        only an error therefore ends the wait for credit at the first check of the credit, in
        both forms of the body, as any other response does. The response to the request that
        tested the credit is one that the service ended and uses an attempt, and the next
        attempt labels the repository."""
        for form, body in (("without output", PROVIDER_FAILED),
                           ("with an empty output", PROVIDER_FAILED_EMPTY)):
            with self.subTest(form=form):
                self.setUp()

                def script(request, number):
                    if number == 1:
                        raise _openrouter_error(openai.APIStatusError, 402)
                    return _answer(_repository_of(request))
                model = _ErrorBodies(lambda request, number: number == 2, (), body=body,
                                     script=script)
                code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                          model, clock=_Clock(), key_credit=_KeyCredit(5.0),
                                          **DEEPSEEK)
                self.assertEqual(code, 0, err)
                waited = classify_repos.logs.hms(classify_repos.CREDIT_CHECK_SECONDS)
                self.assertIn(f"INFO repository_labeling: OpenRouter responds again after "
                              f"{waited} of waiting for credit. The run continues.", err)
                self.assertEqual(len(model.requests), 3)
                [decision] = [d for _, d in label_cache.all_decisions(self.cache)]
                self.assertEqual(decision["usage"]["attempts"], 2)

    def test_spent_openai_quota_is_waited_for_with_one_request_at_a_time(self):
        """A refused request of OpenAI costs nothing, and one request tests the quota after each
        wait."""
        names = ("o/a", "o/b")
        clock = _Clock()

        def script(request, number):
            if number <= 4:
                raise openai.RateLimitError(
                    "quota", response=SimpleNamespace(request=SimpleNamespace(), status_code=429,
                                                      headers={}),
                    body={"code": "insufficient_quota", "message": "quota"})
            return _answer(_repository_of(request))
        model = _Model(script)
        code, err = self.run_over(_repositories(*names), _GitHub({n: (ENGLISH, n) for n in names}),
                                  model, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING repository_labeling: OpenAI refuses every request of the project "
                      "until its quota or billing limit is raised, with the code "
                      "insufficient_quota. Every worker waits for credit. To continue, raise the "
                      "quota or the "
                      "billing limit of the OpenAI project", err)
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual({d["usage"]["attempts"] for d in decisions}, {1})
        self.assertEqual(len(decisions), 2)
        # Two requests in flight when the quota ran out, then one request per check.
        self.assertEqual(len(model.requests), 4 + 2)

    def test_a_run_without_credit_through_the_longest_wait_stops_and_the_next_resumes(self):
        names = ("o/a", "o/b", "o/c")
        github = _GitHub({n: (ENGLISH, n) for n in names})

        def script(request, number):
            if number == 1:
                return _answer(_repository_of(request))
            raise _openrouter_error(openai.APIStatusError, 402,
                                    {"limit_source": "openrouter_credits"})
        with mock.patch.object(classify_repos, "CREDIT_WAIT_SECONDS", 1800):
            code, err = self.run_over(_repositories(*names), github, _Model(script),
                                      clock=_Clock(), key_credit=_KeyCredit(0), workers=1,
                                      **DEEPSEEK)
        self.assertEqual(code, 1, err)
        self.assertIn("ERROR repository_labeling: Stopped the run, because OpenRouter refuses "
                      "the requests for insufficient credit (HTTP 402, openrouter_credits) "
                      "through 0:30:00 of waiting for credit. To continue, add credits at "
                      "https://openrouter.ai/settings/credits", err)
        self.assertNotIn("Could not decide", err)
        self.assertFalse(self.status.exists())
        [kept] = [d["name"] for _, d in label_cache.all_decisions(self.cache)]
        model = _Model(_always_true)
        code, err = self.run_over(_repositories(*names), github, model, clock=_Clock(),
                                  key_credit=_KeyCredit(5.0), **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertIn("resumed", err)
        self.assertEqual(sorted(_repository_of(r) for r in model.requests),
                         sorted(n for n in names if n != kept))

    def test_a_spent_key_limit_is_waited_for_and_a_key_without_access_stops(self):
        """OpenRouter refuses with HTTP 403 both a key with a spent credit limit and a key that
        it denies access. Only the refusal of the first key ends with a top-up."""
        for metadata, stops in (({"limit_source": "openrouter_key_limit"}, False),
                                (None, True)):
            with self.subTest(metadata=metadata):
                self.setUp()

                def script(request, number, metadata=metadata):
                    if number == 1:
                        raise _openrouter_error(openai.PermissionDeniedError, 403, metadata)
                    return _answer(_repository_of(request))
                code, err = self.run_over(_repositories("o/a"),
                                          _GitHub({"o/a": (ENGLISH, "o/a")}), _Model(script),
                                          clock=_Clock(), key_credit=_KeyCredit(1.0),
                                          **DEEPSEEK)
                self.assertEqual(code, 1 if stops else 0, err)
                self.assertEqual("Stopped the run" in err, stops)

    def test_a_run_that_another_worker_stops_ends_while_a_worker_waits_for_credit(self):
        """The worker of o/a waits for credit, which the key never shows, while OpenRouter
        rejects the key in response to the request for o/b, and the rejection stops the run. The
        run ends at once instead of after the longest wait."""
        def script(request, number):
            if _repository_of(request) == "o/a":
                raise _openrouter_error(openai.APIStatusError, 402)
            raise _openrouter_error(openai.AuthenticationError, 401)
        clock = _Clock()
        code, err = self.run_over(_repositories("o/a", "o/b"),
                                  _GitHub({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")}),
                                  _Together(script, ["o/a", "o/b"]), clock=clock,
                                  key_credit=_KeyCredit(0), workers=2, **DEEPSEEK)
        self.assertEqual(code, 1, err)
        self.assertIn("Stopped the run, because OpenRouter rejects the OpenRouter API key", err)
        self.assertLess(clock.now, classify_repos.CREDIT_WAIT_SECONDS)

    def test_an_unexpected_error_during_a_credit_pause_ends_the_run_at_once(self):
        """o/a waits for credit that the key never shows, and for o/b the tool server raises an
        error that the classification does not expect, which stops the run."""
        class Breaking(_Bridge):
            async def call(self, name, arguments):
                if arguments.get("repo_name") == "o/b":
                    raise RuntimeError("the tool server broke")
                return await super().call(name, arguments)

        def script(request, number):
            raise _openrouter_error(openai.APIStatusError, 402)
        clock = _Clock()
        code, err = self.run_over(_repositories("o/a", "o/b"),
                                  _GitHub({"o/a": (ENGLISH, "o/a"), "o/b": (ENGLISH, "o/b")}),
                                  _Model(script), bridge=Breaking(), clock=clock,
                                  key_credit=_KeyCredit(0), workers=2, **DEEPSEEK)
        self.assertEqual(code, 1, err)
        self.assertIn("An unexpected error stopped the run at o/b", err)
        self.assertLess(clock.now, classify_repos.CREDIT_WAIT_SECONDS)

    def test_a_summary_refused_for_spent_credit_is_requested_again_after_the_credit(self):
        class Spent(_Bridge):
            def __init__(self):
                super().__init__()
                self.refuse = 1

            async def call(self, name, arguments):
                if name == "read_linked_page" and self.refuse:
                    self.refuse -= 1
                    self.calls.append((name, arguments))
                    return (f"Error executing tool {name}: [{tools.SUMMARY_NO_CREDIT}] OpenRouter "
                            f"refuses the requests for insufficient credit (HTTP 402)"), True
                return await super().call(name, arguments)

        def script(request, number):
            if number == 1:
                return [_call("read_linked_page", {"url": "docs/guide.md"})]
            return _answer(_repository_of(request))
        bridge, model = Spent(), _Model(script)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model, bridge, clock=_Clock(), key_credit=_KeyCredit(2.0),
                                  summary_model=DEEPSEEK["model"],
                                  summary_provider=DEEPSEEK["provider"],
                                  summary_quantization=DEEPSEEK["quantization"],
                                  summary_hugging_face_id=DEEPSEEK["hugging_face_id"])
        self.assertEqual(code, 0, err)
        self.assertEqual([name for name, _ in bridge.calls].count("read_linked_page"), 2)
        self.assertEqual(len(model.requests), 2)
        [decision] = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(decision["usage"]["attempts"], 1)
        # gpt-6-luna labels through OpenAI, so the requests of the classifying model go on.
        self.assertIn("The requests of the summary model wait for credit, and the requests of "
                      "the classifying model go on", err)

    def test_a_response_of_the_classifying_model_ends_no_pause_for_the_summary_model(self):
        """DeepSeek V4.1 Flash labels through OpenRouter, and gpt-6-luna summarizes the pages
        through OpenAI, which refuses the summary of o/a's page for spent quota until the first
        check of the credit. The response for o/b comes while that summary waits. A response of
        OpenRouter shows nothing about OpenAI's quota, so the summary is requested again only
        at the check, and one pause covers the whole wait."""
        class Spent(_Bridge):
            def __init__(self, clock):
                super().__init__()
                self.clock, self.refused = clock, False

            async def call(self, name, arguments):
                if (name == "read_linked_page"
                        and self.clock.now < classify_repos.CREDIT_CHECK_SECONDS):
                    self.calls.append((name, arguments))
                    self.refused = True
                    return (f"Error executing tool {name}: [{tools.SUMMARY_NO_CREDIT}] OpenAI "
                            f"refuses every request of the project until its quota or billing "
                            f"limit is raised, with the code insufficient_quota"), True
                return await super().call(name, arguments)

        class AfterTheRefusal(_Model):
            """The fake model, which responds for o/b once OpenAI refused o/a's summary."""

            def __init__(self, script, bridge):
                super().__init__(script)
                self.bridge, self.served = bridge, set()

            async def create(self, **request):
                name = _repository_of(request)
                while name == "o/b" and not self.bridge.refused:
                    await asyncio.sleep(0.001)
                response = await super().create(**request)
                self.served.add(name)
                return response

        class HeldClock(_Clock):
            """The fake clock, whose waits begin once the model has responded for o/b."""

            def __init__(self):
                super().__init__()
                self.model = None

            async def sleep(self, seconds):
                while "o/b" not in self.model.served:
                    await asyncio.sleep(0.001)
                await super().sleep(seconds)

        def script(request, number):
            name = _repository_of(request)
            if name == "o/a" and len(request["input"]) == 1:
                return [_call("read_linked_page", {"url": "docs/guide.md"})]
            return _answer(name)
        clock = HeldClock()
        bridge = Spent(clock)
        model = clock.model = AfterTheRefusal(script, bridge)
        names = ("o/a", "o/b")
        code, err = self.run_over(_repositories(*names),
                                  _GitHub({n: (ENGLISH, n) for n in names}), model, bridge,
                                  clock=clock, key_credit=_KeyCredit(5.0), **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertEqual([name for name, _ in bridge.calls].count("read_linked_page"), 2)
        self.assertEqual(err.count("The requests of the summary model wait for credit"), 1, err)
        self.assertEqual([line for line in err.splitlines() if "responds again" in line],
                         ["INFO repository_labeling: OpenAI responds again after 0:10:00 of "
                          "waiting for credit. The run continues."])
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(sorted(d["name"] for d in decisions), list(names))
        self.assertEqual({d["usage"]["attempts"] for d in decisions}, {1})

    def test_the_credit_left_is_compared_with_the_cost_of_the_repositories_left(self):
        """Before the first request, the run logs the credit of the OpenRouter API key and what
        the repositories left cost at the tokens per repository of the latest run and the prices
        of the endpoint, and warns when the credit is short."""
        prices = self.tmp / "model_prices.csv"
        prices.write_text("model,service_tier,uncached_input_usd_per_million,"
                          "cached_input_usd_per_million,cache_write_usd_per_million,"
                          "output_usd_per_million,doc_url,doc_verified,notes\n"
                          "deepseek/deepseek-v4.1-flash,morph/fp8,1,0.5,1,10,"
                          "https://openrouter.ai/x,2026-10-30,\n")
        label_cache.put_index_row({"run_id": "2026-10-23T093036Z", "outcome": "finished",
                                   "model": "gpt-6-luna", "parameters": "p", "repositories": 4,
                                   "reused": 1, "gone": 1, "input_tokens": 3_000_000,
                                   "cached_tokens": 1_000_000, "output_tokens": 200_000},
                                  self.cache)
        # Per repository sent to the model: (2M x $1 + 1M x $0.5 + 0.2M x $10) / 2 = $2.25.
        names = [f"o/r{i}" for i in range(4)]
        with mock.patch.object(classify_repos.costs, "PRICES", prices):
            code, err = self.run_over(_repositories(*names),
                                      _GitHub({n: (ENGLISH, n) for n in names}),
                                      _Model(_always_true), key_credit=_KeyCredit(5.0),
                                      **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertIn("INFO repository_labeling: The OpenRouter API key has $5.00 of credit "
                      "left. The 4 repositories left cost about $9.00 at $2.2500 per "
                      "repository, the tokens per repository of run 2026-10-23T093036Z of "
                      "gpt-6-luna", err)
        self.assertIn("WARNING repository_labeling: The credit of the OpenRouter API key covers "
                      "$5.00 of the estimated $9.00, $4.00 short. The run starts", err)

    def test_the_cost_of_the_repositories_left_is_also_given_at_the_backup_prices(self):
        """While the pinned provider fails, the backup provider serves the requests at its own,
        higher prices. The line before the first request also states what the repositories left
        cost if the backup provider serves every request, and a WARNING says when the credit
        covers the estimate at the pinned provider but not that one."""
        prices = self.tmp / "model_prices.csv"
        prices.write_text("model,service_tier,uncached_input_usd_per_million,"
                          "cached_input_usd_per_million,cache_write_usd_per_million,"
                          "output_usd_per_million,doc_url,doc_verified,notes\n"
                          "deepseek/deepseek-v4.1-flash,morph/fp8,1,0.5,1,10,"
                          "https://openrouter.ai/x,2026-10-30,\n"
                          "deepseek/deepseek-v4.1-flash,deepinfra/fp8,2,1,2,20,"
                          "https://openrouter.ai/x,2026-11-04,\n")
        label_cache.put_index_row({"run_id": "2026-10-23T093036Z", "outcome": "finished",
                                   "model": "gpt-6-luna", "parameters": "p", "repositories": 4,
                                   "reused": 1, "gone": 1, "input_tokens": 3_000_000,
                                   "cached_tokens": 1_000_000, "output_tokens": 200_000},
                                  self.cache)
        # Per repository sent to the model at the backup provider:
        # (2M x $2 + 1M x $1 + 0.2M x $20) / 2 = $4.50.
        names = [f"o/r{i}" for i in range(4)]
        with mock.patch.object(classify_repos.costs, "PRICES", prices):
            code, err = self.run_over(_repositories(*names),
                                      _GitHub({n: (ENGLISH, n) for n in names}),
                                      _Model(_always_true), key_credit=_KeyCredit(10.0),
                                      no_backup_provider=False, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertIn("INFO repository_labeling: The OpenRouter API key has $10.00 of credit "
                      "left. The 4 repositories left cost about $9.00 at $2.2500 per "
                      "repository, the tokens per repository of run 2026-10-23T093036Z of "
                      "gpt-6-luna at the prices of model_prices.csv, and about $18.00 at "
                      "$4.5000 per repository if the backup provider deepinfra/fp8 serves every "
                      "request.", err)
        self.assertIn("WARNING repository_labeling: The credit of the OpenRouter API key covers "
                      "the estimated $9.00 at the pinned providers, but only $10.00 of the "
                      "estimated $18.00 if the backup providers serve every request.", err)

    def credit_log(self, prices: str) -> str:
        """The log of a run of two repositories with DeepSeek V4.1 Flash on its pinned endpoint,
        $5.00 of credit and the price file `prices`."""
        path = self.tmp / "model_prices.csv"
        path.write_text("model,service_tier,uncached_input_usd_per_million,"
                        "cached_input_usd_per_million,cache_write_usd_per_million,"
                        "output_usd_per_million,doc_url,doc_verified,notes\n" + prices)
        names = ["o/r0", "o/r1"]
        with mock.patch.object(classify_repos.costs, "PRICES", path):
            code, err = self.run_over(_repositories(*names),
                                      _GitHub({n: (ENGLISH, n) for n in names}),
                                      _Model(_always_true), key_credit=_KeyCredit(5.0),
                                      **DEEPSEEK)
        self.assertEqual(code, 0, err)
        return err

    def test_without_a_finished_run_the_log_says_that_no_run_gives_the_tokens(self):
        """Each run of `check --runs` labels into a cache of labels of its own, which holds no
        finished run when the run starts, and the price of the endpoint alone gives no estimate
        of the cost."""
        err = self.credit_log("deepseek/deepseek-v4.1-flash,morph/fp8,1,0.5,1,10,"
                              "https://openrouter.ai/x,2026-10-30,\n")
        self.assertIn("The OpenRouter API key has $5.00 of credit left. The cost of the 2 "
                      "repositories left has no estimate, because no finished run in the cache "
                      "of labels gives the tokens per repository", err)

    def test_without_a_price_of_the_endpoint_the_log_names_the_missing_price(self):
        label_cache.put_index_row({"run_id": "2026-10-23T093036Z", "outcome": "finished",
                                   "model": "gpt-6-luna", "parameters": "p", "repositories": 4,
                                   "reused": 0, "gone": 0, "input_tokens": 3_000_000,
                                   "cached_tokens": 0, "output_tokens": 200_000}, self.cache)
        self.assertIn("The cost of the 2 repositories left has no estimate, because "
                      "model_prices.csv has no price of deepseek/deepseek-v4.1-flash at "
                      "morph/fp8", self.credit_log(""))


def endpoints_top_up():
    return classify_repos.endpoints.TOP_UP[classify_repos.endpoints.OPENROUTER]


class _ResponsesStub:
    """A server on 127.0.0.1 that speaks the part of the Responses API that the classifier uses,
    with the error responses of spent credit: OpenRouter's HTTP 402 with its error body
    (openrouter.ai/docs/api-reference/errors) and OpenAI's HTTP 429 with `insufficient_quota`.
    It refuses the next `refusals` requests after the first `served_before`, serves
    GET /api/v1/key with the next of `credits`, and records each request body and the status of
    the step that the run states at each refusal and each check of the key."""

    OPENROUTER_REFUSAL = (402, {"error": {"code": 402, "message": "Insufficient credits. Add more "
                                          "using https://openrouter.ai/credits",
                                          "metadata": {"limit_source": "openrouter_credits"}}})
    OPENAI_REFUSAL = (429, {"error": {"message": "You exceeded your current quota.",
                                      "type": "insufficient_quota", "param": None,
                                      "code": "insufficient_quota"}})

    def __init__(self, refusal, refusals, credits=(), status=None, served_before=0):
        import http.server
        import threading
        self.refusal, self.refusals, self.credits = refusal, refusals, list(credits)
        self.status, self.served_before = status, served_before
        self.bodies, self.seen, self.served = [], [], 0
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def send(self, status, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802 - the name http.server calls
                stub.seen.append(stub.observed())
                credit = stub.credits.pop(0) if len(stub.credits) > 1 else stub.credits[0]
                self.send(200, {"data": {"label": "sk-or-v1-te...st", "limit": 10,
                                         "limit_remaining": credit, "effective_budget": None}})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.bodies.append(body)
                if stub.served >= stub.served_before and stub.refusals:
                    stub.refusals -= 1
                    stub.seen.append(stub.observed())
                    self.send(*stub.refusal)
                    return
                stub.served += 1
                given = body["input"]
                text = given[0]["content"] if isinstance(given, list) else given
                name = text.split("<repository>")[1].split("</repository>")[0]
                self.send(200, {
                    "id": f"resp_{stub.served}", "object": "response", "created_at": 0,
                    "model": body["model"], "status": "completed", "parallel_tool_calls": True,
                    "tool_choice": "auto", "tools": [],
                    "output": [{"type": "message", "id": f"msg_{stub.served}",
                                "status": "completed", "role": "assistant",
                                "content": []}, {
                        "type": "function_call", "id": f"fc_{stub.served}",
                        "call_id": f"call_{stub.served}", "status": "completed",
                        "name": classify_repos.CHECKLIST_TOOL_NAME,
                        "arguments": _answer(name)[0].arguments}],
                    "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens": 10,
                              "output_tokens_details": {"reasoning_tokens": 3},
                              "total_tokens": 110}})

            def log_message(self, format, *args):  # noqa: A002 - http.server's signature
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def observed(self):
        """The status that the step states now, or None."""
        if self.status is None or not self.status.exists():
            return None
        return json.loads(self.status.read_text())

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestTheEndsOfTheCreditPause(unittest.TestCase):
    """The credit pause ends by the longest wait or with the run, whatever its probe does."""

    SERVICE = classify_repos.endpoints.OPENROUTER
    REFUSAL = classify_repos.endpoints.CreditRefusal(SERVICE, "OpenRouter refuses the requests")

    def test_the_status_of_the_wait_is_written_before_the_line_that_begins_it_and_removed_after(
            self):
        """The runner copies the step's status into its state at each line of the step's log,
        and the next line of a wait comes CREDIT_CHECK_SECONDS later. The status is therefore
        in place when the warning that begins the wait is logged, and gone when the line that
        ends it is logged."""
        events = []
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock)
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(classify_repos.logs, "step_status",
                                  lambda status: events.append(("status", status is not None))), \
                mock.patch.object(classify_repos.logs, "warning",
                                  lambda component, text: events.append(("warning", text[:20]))), \
                mock.patch.object(classify_repos.logs, "info",
                                  lambda component, text: events.append(("info", text[:20]))):
            pause.refused(self.REFUSAL, probe=False)
            pause.succeeded(self.SERVICE)
        self.assertEqual([kind if kind != "status" else (kind, value)
                          for kind, value in events],
                         [("status", True), "warning", ("status", False), "info"])

    def test_a_probe_that_never_reports_holds_no_worker_past_the_longest_wait(self):
        """A request that tests the credit and ends with an error that is not reported to the
        pause leaves the pause waiting for the outcome of the test. The other workers stop
        waiting at the longest wait."""
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock)
        pause.refused(self.REFUSAL, probe=False)

        async def scenario():
            self.assertTrue(await pause.ready(self.SERVICE))
            with self.assertRaises(classify_repos.CreditDown):
                await pause.ready(self.SERVICE)
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(classify_repos.logs, "step_status"):
            asyncio.run(asyncio.wait_for(scenario(), 10))
        self.assertLessEqual(clock.now, classify_repos.CREDIT_WAIT_SECONDS)

    def test_a_test_given_after_a_response_ended_the_pause_holds_no_later_pause(self):
        """The check of the credit of the OpenRouter API key waits for OpenRouter, and the
        response of a request in flight can end the pause meanwhile. The worker of the check
        then tests the credit of a pause that has ended, and its response ends nothing. A later
        refusal begins a pause that checks the credit after CREDIT_CHECK_SECONDS and lets one
        request test it, rather than waiting for the outcome of that earlier test until the
        longest wait stops the run."""
        clock = _Clock()

        async def check(service):
            # The response of a request in flight comes while the key is read.
            pause.succeeded(service)
            return True
        pause = classify_repos.CreditPause(clock.sleep, clock, check=check)

        async def scenario():
            pause.refused(self.REFUSAL, probe=False)
            self.assertTrue(await pause.ready(self.SERVICE))
            pause.succeeded(self.SERVICE)
            pause.check = lambda service: _true()
            pause.refused(self.REFUSAL, probe=False)
            self.assertTrue(await pause.ready(self.SERVICE))
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(classify_repos.logs, "step_status"):
            asyncio.run(asyncio.wait_for(scenario(), 10))
        self.assertEqual(clock.now, 2 * classify_repos.CREDIT_CHECK_SECONDS)

    def test_a_run_that_stops_ends_the_pause_of_every_waiting_worker(self):
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock, check=lambda service: _false())
        pause.refused(self.REFUSAL, probe=False)

        async def scenario():
            waiting = asyncio.ensure_future(pause.ready(self.SERVICE))
            await asyncio.sleep(0)
            pause.end("the run stopped, because GitHub failed")
            with self.assertRaises(classify_repos.CreditDown):
                await waiting
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(classify_repos.logs, "step_status"):
            asyncio.run(asyncio.wait_for(scenario(), 10))
        self.assertLess(clock.now, classify_repos.CREDIT_WAIT_SECONDS)


    def test_a_refusal_after_the_run_stopped_starts_no_pause(self):
        """Another worker can stop the run before this worker's request is refused for spent
        credit. The refusal then starts no pause."""
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock, check=lambda service: _false())
        pause.end("the run stopped, because OpenRouter rejects the OpenRouter API key")
        pause.refused(self.REFUSAL, probe=False)

        async def scenario():
            with self.assertRaises(classify_repos.CreditDown):
                await pause.ready(self.SERVICE)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(classify_repos.logs, "step_status"):
            asyncio.run(asyncio.wait_for(scenario(), 10))
        self.assertEqual(clock.now, 0)
        self.assertNotIn("Every worker waits for credit", err.getvalue())


    def test_a_run_that_stops_lets_the_requests_without_a_refusal_go_on(self):
        """The workers in the middle of a repository when another worker stops the run finish
        it, as without a pause for credit, so that the next run does not pay for it again."""
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock, check=lambda service: _false())
        pause.end("the run stopped, because GitHub failed")

        async def scenario():
            self.assertFalse(await pause.ready(self.SERVICE))
        asyncio.run(asyncio.wait_for(scenario(), 10))


    def test_a_response_after_the_run_stopped_logs_no_end_of_a_pause(self):
        """A request in flight can succeed after another worker's request was refused for spent
        credit after the stop. No pause began, and the log states no wait for credit."""
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock, check=lambda service: _false())
        pause.end("the run stopped, because GitHub failed")
        pause.refused(self.REFUSAL, probe=False)
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(classify_repos.logs, "step_status"):
            pause.succeeded(self.SERVICE)

            async def next_request():
                # The worker of the response sends its next request and finishes its repository.
                self.assertFalse(await pause.ready(self.SERVICE))
            asyncio.run(asyncio.wait_for(next_request(), 10))
        self.assertNotIn("The run continues", err.getvalue())

    def test_a_probe_that_succeeds_after_the_run_stopped_lets_its_worker_go_on(self):
        """The request that tests the credit can succeed after another worker stopped the run.
        Its worker finishes its repository instead of raising at its next request."""
        clock = _Clock()
        pause = classify_repos.CreditPause(clock.sleep, clock, check=lambda service: _true())
        pause.refused(self.REFUSAL, probe=False)

        async def scenario():
            self.assertTrue(await pause.ready(self.SERVICE))
            pause.end("the run stopped, because GitHub failed")
            pause.succeeded(self.SERVICE)
            self.assertFalse(await pause.ready(self.SERVICE))
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(classify_repos.logs, "step_status"):
            asyncio.run(asyncio.wait_for(scenario(), 10))

    def test_a_run_that_stops_ends_the_wait_of_a_pause_for_github(self):
        """A pause for GitHub lasts up to hours (`readmes.RECHECK_WAITS`). A worker that is
        waiting in the pause when the run stops raises at once instead of waiting until the pause
        ends."""
        async def endless(seconds):
            # A wait that only the end of the pause can cut short.
            await asyncio.Event().wait()
        pause = classify_repos.GitHubPause(lambda: 0, lambda: True, endless, clock=lambda: 0.0)
        # A pause holds, as after GitHub failed the repositories of two workers.
        pause.until = 3600.0

        async def scenario():
            waiting = asyncio.ensure_future(pause.ready())
            await asyncio.sleep(0)
            pause.end("the run stopped, because the API refuses the key")
            with self.assertRaises(classify_repos.GitHubPauseEnded):
                await waiting
        asyncio.run(asyncio.wait_for(scenario(), 10))

    def test_a_run_that_stops_ends_the_wait_of_a_pause_for_the_service(self):
        """A pause for a failing service lasts up to PAUSE_LONGEST_SECONDS. A worker that is
        waiting in the pause when the run stops raises at once instead of waiting until the pause
        ends."""
        async def endless(seconds):
            # A wait that only the end of the pause can cut short, as a pause of hours is.
            await asyncio.Event().wait()
        pause = classify_repos.ServicePause(endless, clock=lambda: 0.0)
        error = _server_error(503)
        with contextlib.redirect_stderr(io.StringIO()):
            pause.failed("o/a", 0, error)
            pause.failed("o/b", 0, error)
        self.assertGreater(pause.until, 0.0)

        async def scenario():
            waiting = asyncio.ensure_future(pause.ready())
            await asyncio.sleep(0)
            pause.end("the run stopped, because GitHub failed")
            with self.assertRaises(classify_repos.PauseEnded):
                await waiting
        asyncio.run(asyncio.wait_for(scenario(), 10))


async def _false():
    return False


async def _true():
    return True


class TestTheCreditPauseOverHTTP(_RunCase):
    """The pause for spent credit through the real client of the SDK, against a server on
    127.0.0.1 that refuses for spent credit and then serves, with the waits of the pause
    shortened through their constants."""

    def setUp(self):
        super().setUp()
        self.status = self.tmp / "repository_labeling.status.json"
        for name, value in (("CREDIT_CHECK_SECONDS", 0.05), ("CREDIT_WAIT_SECONDS", 30)):
            patcher = mock.patch.object(classify_repos, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environ = mock.patch.dict(os.environ, {"AGENTCONTEXT_STEP_STATUS": str(self.status)})
        environ.start()
        self.addCleanup(environ.stop)
        self.names = ("o/a", "o/b", "o/c")
        self.github = _GitHub({n: (ENGLISH, n) for n in self.names})

    def stub(self, *arguments, **options):
        stub = _ResponsesStub(*arguments, status=self.status, **options)
        self.addCleanup(stub.close)
        return stub

    def openrouter(self, stub):
        """The real client of OpenRouter and the real reader of the key's credit, both against
        `stub`."""
        endpoints = classify_repos.endpoints
        patcher = mock.patch.object(endpoints, "OPENROUTER_API", f"{stub.url}/api/v1")
        patcher.start()
        self.addCleanup(patcher.stop)
        endpoint = endpoints.Endpoint(**DEEPSEEK)
        client = endpoints.client(endpoint, None, timeout=5, max_retries=0,
                                  environ={"OPENROUTER_API_KEY": "sk-or-v1-test"})
        return client, endpoints.KeyCredit("sk-or-v1-test")

    def finished_every_label(self, err, attempts=1):
        decisions = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual(sorted(d["name"] for d in decisions), list(self.names))
        self.assertEqual({d["usage"]["attempts"] for d in decisions}, {attempts})
        self.assertNotIn("Could not decide", err)
        self.assertNotIn("Retry round", err)
        return decisions

    def test_an_openrouter_run_waits_for_credit_and_continues_by_itself(self):
        stub = self.stub(_ResponsesStub.OPENROUTER_REFUSAL, 3, credits=[0, 0, 0, 5])
        client, credit = self.openrouter(stub)
        code, err = self.run_over(_repositories(*self.names), self.github, client,
                                  key_credit=credit, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.finished_every_label(err)
        self.assertIn("WARNING repository_labeling: OpenRouter refuses the requests for "
                      "insufficient credit (HTTP 402, openrouter_credits). Every worker waits for "
                      "credit. To continue, add credits at "
                      "https://openrouter.ai/settings/credits", err)
        self.assertIn("INFO repository_labeling: OpenRouter responds again after", err)
        waiting = [status for status in stub.seen[1:] if status]
        self.assertTrue(waiting)
        self.assertTrue(all(status["waiting_for"] == "credit"
                            and status["to_continue"] == endpoints_top_up() for status in waiting))
        self.assertFalse(self.status.exists())
        self.assertTrue(all(body["provider"]["order"] == ["morph/fp8"]
                            and "previous_response_id" not in body for body in stub.bodies))
        [row] = label_cache.read_tokens(self.cache, self.index()[-1]["run_id"])
        self.assertEqual((row["requests"], row["input_tokens"]), (stub.served, 100 * stub.served))
        self.assertEqual(stub.served, len(self.names))

    def test_an_openai_run_waits_for_its_quota_and_continues_by_itself(self):
        stub = self.stub(_ResponsesStub.OPENAI_REFUSAL, 3)
        environ = mock.patch.dict(os.environ, {"OPENAI_BASE_URL": f"{stub.url}/v1"})
        environ.start()
        self.addCleanup(environ.stop)
        client = classify_repos.endpoints.client(classify_repos.endpoints.Endpoint("gpt-6-luna"),
                                                 None, timeout=5, max_retries=0,
                                                 environ={"OPENAI_API_KEY": "sk-proj-test"})
        code, err = self.run_over(_repositories(*self.names), self.github, client)
        self.assertEqual(code, 0, err)
        self.finished_every_label(err)
        self.assertIn("WARNING repository_labeling: OpenAI refuses every request of the project "
                      "until its quota or billing limit is raised, with the code "
                      "insufficient_quota. Every worker waits for credit. To continue, raise the "
                      "quota or the "
                      "billing limit of the OpenAI project", err)
        later = [status for status in stub.seen[1:] if status]
        self.assertTrue(later)
        self.assertEqual({status["service"] for status in later}, {"OpenAI"})
        [row] = label_cache.read_tokens(self.cache, self.index()[-1]["run_id"])
        self.assertEqual(row["requests"], stub.served)

    def test_credit_spent_past_the_longest_wait_stops_and_the_next_invocation_finishes(self):
        stub = self.stub(_ResponsesStub.OPENROUTER_REFUSAL, 10_000, credits=[0],
                         served_before=1)
        client, credit = self.openrouter(stub)
        with mock.patch.object(classify_repos, "CREDIT_WAIT_SECONDS", 0.3):
            code, err = self.run_over(_repositories(*self.names), self.github, client,
                                      key_credit=credit, workers=1, **DEEPSEEK)
        self.assertEqual(code, 1, err)
        self.assertIn("ERROR repository_labeling: Stopped the run, because OpenRouter refuses "
                      "the requests for insufficient credit", err)
        self.assertNotIn("Could not decide", err)
        self.assertEqual(len([d for _, d in label_cache.all_decisions(self.cache)]), 1)
        stub.refusals, stub.credits = 0, [5]
        code, err = self.run_over(_repositories(*self.names), self.github, client,
                                  key_credit=credit, **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.assertIn("resumed", err)
        self.finished_every_label(err)
        self.assertEqual(stub.served, len(self.names))


class TestRun(_RunCase):

    def sampling_frame(self):
        repositories = _repositories("o/a", "o/b", "o/c", "o/d")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (" \n", "s2")},
                         {"o/d": {"id": 4, "full_name": "o/d"}})
        return repositories, github

    def test_a_run_labels_excludes_and_writes_the_outputs_in_the_order_of_the_sampling_frame(self):
        repositories, github = self.sampling_frame()
        model = _Model(_always_true)
        code, err = self.run_over(repositories, github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_annotated.csv"), [
            {"repo_name": "o/a", "github_id": "1", "default_branch": "main",
             "engineered_project": "true",
         "justification": "It is software. The classification labels it true, because no "
                          "criterion applies and the model judged the evidence of software "
                          "engineering practices true."}])
        self.assertEqual(self.rows("repos_excluded.csv"), [
            {"repo_name": "o/b", "repo_url": "https://github.com/o/b", "reason": "empty_readme",
             "detected_language": ""},
            {"repo_name": "o/c", "repo_url": "https://github.com/o/c",
             "reason": "not_found_or_no_access", "detected_language": ""},
            {"repo_name": "o/d", "repo_url": "https://github.com/o/d",
             "reason": "no_readme_found", "detected_language": ""}])
        self.assertEqual(len(model.requests), 1)
        row = self.index()[0]
        self.assertEqual({k: row[k] for k in ("outcome", "repositories", "labeled", "excluded",
                                              "gone", "reused", "decisions", "model_requests",
                                              "input_tokens")},
                         {"outcome": "finished", "repositories": "4", "labeled": "1",
                          "excluded": "2", "gone": "1", "reused": "0", "decisions": "3",
                          "model_requests": "1", "input_tokens": "100"})
        self.assertFalse(list(self.cache.glob("*.partial.jsonl")))
        self.assertIn("INFO repository_labeling: Labeled 1 repositories, excluded 2 and found 1 "
                      "gone. Reused 0 decisions, 0 of them carried over, and made 3 new ones", err)
        self.assertIn("INFO repository_labeling: Spent ", err)
        self.assertIn("1 model requests with 100 input tokens, 5 of them cached, and 10 output "
                      "tokens", err)

    def prices(self):
        """A price file of round prices in USD per million tokens, which the run reads for its
        cost record in place of OpenAI's prices."""
        path = self.tmp / "model_prices.csv"
        path.write_text(
            "model,service_tier,uncached_input_usd_per_million,cached_input_usd_per_million,"
            "cache_write_usd_per_million,output_usd_per_million,doc_url,doc_verified,notes\n"
            "gpt-6-luna,flex,1000,100,2000,10000,https://prices.example/,2026-10-28,\n"
            "gpt-6-luna,default,4000,400,5000,20000,https://prices.example/,2026-10-28,\n",
            encoding="utf-8")
        patcher = mock.patch.object(classify_repos.costs, "PRICES", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cost_record(self, rid):
        with open(label_cache.cost_path(self.cache, rid), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_a_run_that_ends_writes_its_cost_record_and_logs_its_total(self):
        """Two repositories, each labeled in one request on flex. Each response read 100 input
        tokens, 5 of them from the prompt cache and 40 written to it, and wrote 10 output tokens.
        At the round prices of `prices`, the 110 uncached input tokens cost $0.11, the 10 cached
        ones $0.001, the 80 written to the cache $0.16 and the 20 output tokens $0.20."""
        self.prices()
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        code, err = self.run_over(_repositories("o/a", "o/b"), github,
                                  _Billed(_always_true, lambda number: "flex"))
        self.assertEqual(code, 0, err)
        rid = self.index()[0]["run_id"]
        price = {"price_url": "https://prices.example/", "price_read": "2026-10-28", "note": ""}
        self.assertEqual(self.cost_record(rid), [
            {"role": "classifying_model", "model": "gpt-6-luna", "service_tier": "flex",
             "billed_as": billed_as, "tokens": tokens, "usd_per_million": rate, "usd": usd,
             **price}
            for billed_as, tokens, rate, usd in (("uncached_input", "110", "1000", "0.11"),
                                                 ("cached_input", "10", "100", "0.001"),
                                                 ("cache_write", "80", "2000", "0.16"),
                                                 ("output", "20", "10000", "0.2"))])
        self.assertIn(f"INFO repository_labeling: Run {rid} cost $0.47 at the prices of "
                      f"model_prices.csv, read on 2026-10-28. Wrote the "
                      f"cost by model, service tier and rate to "
                      f"{label_cache.cost_path(self.cache, rid)}.\n", err)

    def test_a_stopped_run_writes_its_cost_record(self):
        """The model labels the first repository, and then the API rejects the OpenAI API key,
        which stops the run. The cost record contains the tokens of the one response."""
        self.prices()

        def script(request, number):
            if number > 1:
                response = SimpleNamespace(request=SimpleNamespace(), status_code=401, headers={})
                raise openai.AuthenticationError("refused", response=response, body=None)
            return _answer(_repository_of(request))
        names = [f"o/r{i}" for i in range(3)]
        github = _GitHub({name: (ENGLISH.replace("files", f"files of {name}"), name)
                          for name in names})
        code, err = self.run_over(_repositories(*names), github,
                                  _Billed(script, lambda number: "flex"), workers=1)
        self.assertEqual(code, 1, err)
        row = self.index()[0]
        self.assertEqual(row["outcome"], "incomplete")
        self.assertEqual({record["billed_as"]: record["tokens"]
                          for record in self.cost_record(row["run_id"])},
                         {"uncached_input": "55", "cached_input": "5", "cache_write": "40",
                          "output": "10"})
        self.assertIn(f"Run {row['run_id']} cost $0.24 at the prices of model_prices.csv", err)

    def test_the_record_beside_the_labels_names_the_run_the_models_and_the_retrieval(self):
        """The release reads from this record the models of the sample, rather than a string in
        its code, how many repositories the run left without a decision, which repositories the
        classifying model labeled at its higher temperature or the second model labeled, and
        which ones the second model left without a decision too."""
        repositories, github = self.sampling_frame()
        code, err = self.run_over(repositories, github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        row = self.index()[0]
        record = json.loads((self.out / "repos_annotated.json").read_text(encoding="utf-8"))
        self.assertEqual(record, {
            "description": f"run {row['run_id']} of the classification stage",
            "run_id": row["run_id"], "parameters": row["parameters"],
            "retrieval_id": "2026-10-05T101430Z", "models": ["gpt-6-luna"],
            "response_models": ["gpt-6-luna-2026-09-30"], "undecided": 0,
            "higher_temperature_labels": [], "second_model_labels": [],
            "second_model_failures": [], "backup_provider_labels": []})

    def test_every_line_is_in_the_log_format_under_the_step_key(self):
        """The runner labels a printed line with the step key, and the script logs under that
        key too, as the cloning step does."""
        repositories, github = self.sampling_frame()
        code, err = self.run_over(repositories, github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        components = {line.split(":")[0] for line in err.splitlines()}
        self.assertEqual(components, {"INFO repository_labeling"})

    def test_a_progress_line_after_every_hundred_finished_repositories(self):
        repositories = _repositories(*(f"o/r{i}" for i in range(4)))
        github = _GitHub({f"o/r{i}": (ENGLISH, f"s{i}") for i in range(4)})
        with mock.patch.object(classify_repos, "PROGRESS_EVERY", 2):
            code, err = self.run_over(repositories, github, _Model(_always_true), workers=1)
        self.assertEqual(code, 0, err)
        lines = [line for line in err.splitlines() if " done, " in line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(
            "INFO repository_labeling: 2/4 (50.0%) done, 2 ok and 0 failed this run, "))
        self.assertIn("repositories/min, elapsed ", lines[0])
        # A README request for each repository. The description comes from the sampling frame.
        self.assertIn("4 GitHub and 4 model requests, 400 input and 40 output tokens", lines[1])

    def test_an_unexpected_error_stops_the_run_and_logs_its_traceback(self):
        """A defect fails every later repository the same way and could spend the model's tokens
        on each of them. The cloning step stops on a defect too."""
        def broken(request, number):
            raise RuntimeError("a defect")
        model = _Model(broken)
        code, err = self.run_over(_repositories("o/a", "o/b", "o/c"),
                                  _GitHub({n: (ENGLISH, n) for n in ("o/a", "o/b", "o/c")}),
                                  model, workers=1)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("ERROR repository_labeling: An unexpected error stopped the run at o/a",
                      err)
        self.assertIn("RuntimeError: a defect", err)
        self.assertIn("Traceback (most recent call last)", err)
        failed = [json.loads(line) for line in next(self.cache.glob("*.partial.jsonl"))
                  .read_text().splitlines() if '"status"' in line]
        self.assertEqual([(f["name"], f["status"], f["reason"]) for f in failed],
                         [("o/a", "failed", "error")])

    def test_a_tool_server_that_ended_stops_the_run_and_keeps_no_label(self):
        """After the server process ended, every tool call returns an error. The run stops,
        because the labels that the model decided without the tools would otherwise be kept
        permanently."""
        import mcp_bridge

        class Ended(_Bridge):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                raise mcp_bridge.ServerGone("the connection to the MCP server is closed")

        def script(request, number):
            return [_call("get_file_tree", {})] if number == 1 else _answer("o/a")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        code, err = self.run_over(_repositories("o/a", "o/b"), github, _Model(script), Ended(),
                                  workers=1)
        self.assertEqual(code, 1)
        self.assertIn("ServerGone: the connection to the MCP server is closed", err)
        self.assertEqual(list(label_cache.all_decisions(self.cache)), [])
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)

    def test_a_readme_the_language_check_finds_in_another_language_is_excluded(self):
        """Rule 2: the language check excludes such a README without a request to the model, and
        the decision records what the check read."""
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (RUSSIAN, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])
        self.assertEqual(self.rows("repos_excluded.csv"), [
            {"repo_name": "o/a", "repo_url": "https://github.com/o/a",
             "reason": "readme_non_english", "detected_language": "ru"}])
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual(decision["language"]["detected"], "ru")

    def test_a_readme_the_model_finds_not_mainly_english_is_labeled_false(self):
        """Rule 2: the language check passes a README in a language written in ASCII, and the
        model's response counts the README's words by language. The classification applies
        the thresholds."""
        model = _Model(lambda request, number: _answer(
            "o/a", "true", words=[{"language": "German", "words": 30}],
            purpose_quote="Ein Werkzeug, das Konfigurationsdateien formatiert."))
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (GERMAN, "s1")}), model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "false")
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["model_label"],
                          decision["readme_mainly_english"], decision["language"]["detected"]),
                         ("false", "true", "false", "de"))

    def test_a_second_run_reuses_every_decision_without_querying_the_model(self):
        repositories, github = self.sampling_frame()
        self.run_over(repositories, github, _Model(_always_true))
        model = _Model(_always_true)
        code, err = self.run_over(repositories, github, model, new_run=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])
        self.assertEqual(self.index()[1]["reused"], "3")
        self.assertEqual(self.index()[1]["decisions"], "0")
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")

    def test_the_same_readme_at_a_later_revision_is_reused(self):
        repositories, github = self.sampling_frame()
        self.run_over(repositories, github, _Model(_always_true))
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a", "o/b", "o/c", "o/d", revision=LATER), github,
                                  model)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])
        self.assertIn(f"readme?ref={LATER}", github.sent[-1] + "".join(github.sent))

    def test_a_changed_readme_is_labeled_again(self):
        repositories, github = self.sampling_frame()
        self.run_over(repositories, github, _Model(_always_true))
        github.readmes["o/a"] = (ENGLISH + " Now with tests.", "s3")
        model = _Model(lambda request, number: _answer("o/a", "unclear"))
        code, err = self.run_over(repositories, github, model, new_run=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "unclear")

    def test_another_model_labels_again_and_keeps_the_first_models_labels(self):
        repositories, github = self.sampling_frame()
        self.run_over(repositories, github, _Model(_always_true))
        model = _Model(lambda request, number: _answer("o/a", "false"))
        code, err = self.run_over(repositories, github, model, model="gpt-6.1-sol")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.requests[0]["model"], "gpt-6.1-sol")
        first, second = (row["parameters"] for row in self.index())
        self.assertEqual(label_cache.Decisions.load(self.cache, model="gpt-6-luna",
                                                    parameters=first)
                         .label(1, "gpt-6-luna", first)["label"], "true")
        self.assertEqual(label_cache.Decisions.load(self.cache, model="gpt-6.1-sol",
                                                    parameters=second)
                         .label(1, "gpt-6.1-sol", second)["label"], "false")
        # The parameters name each model, and they differ in nothing else.
        self.assertNotEqual(first, second)
        self.assertEqual(label_cache.shared_parameters(self.cache, first),
                         label_cache.shared_parameters(self.cache, second))

    def fork_verdicts(self, verdicts):
        """Replace forks.independence with a function that returns the verdict of `verdicts` for
        the upstream's link. The fork heuristic has its own suite, test_forks.py."""
        decided = []

        def independence(client, found, revision, upstream, thresholds):
            decided.append((found["full_name"], revision, upstream, thresholds))
            return {"upstream": upstream, "thresholds": thresholds.as_dict(), **verdicts[upstream]}
        patcher = mock.patch.object(classify_repos.forks, "independence", independence)
        patcher.start()
        self.addCleanup(patcher.stop)
        return decided

    def test_the_fork_heuristic_labels_a_fork_that_has_not_become_its_own_project_false(self):
        decided = self.fork_verdicts({
            "https://github.com/up/a": {"independent": False, "reasons": ["tracks its upstream"]},
            "https://github.com/up/b": {"independent": True, "reasons": []}})

        def script(request, number):
            name = _repository_of(request)
            return _answer(name, fork_of={"o/a": "https://github.com/up/a",
                                          "o/b": "https://github.com/up/b"}.get(name, ""))
        github = _GitHub({n: (ENGLISH, n) for n in ("o/a", "o/b", "o/c")})
        repositories = _independent("o/a", "o/b", "o/c")
        code, err = self.run_over(repositories, github, _Model(script))
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(decided),
                         [("o/a", REVISION, "https://github.com/up/a", THRESHOLDS),
                          ("o/b", repositories[1]["revision"], "https://github.com/up/b",
                           THRESHOLDS)])
        self.assertEqual([r["engineered_project"] for r in self.rows("repos_annotated.csv")],
                         ["false", "true", "true"])
        decisions = {d["name"]: d for _, d in label_cache.all_decisions(self.cache)}
        self.assertEqual(decisions["o/a"]["applied_criteria"], ["fork"])
        self.assertEqual(decisions["o/a"]["fork"]["reasons"], ["tracks its upstream"])
        self.assertNotIn("fork", decisions["o/c"])

    def test_a_new_sampling_frame_applies_the_fork_heuristic_again_without_the_model(self):
        """A new sampling frame has new thresholds, and the fork heuristic decides again under
        them from the stored response. A fork that is no longer independent is labeled false
        without a request to the model."""
        verdicts = {"https://github.com/up/a": {"independent": True, "reasons": []}}
        decided = self.fork_verdicts(verdicts)

        def script(request, number):
            name = _repository_of(request)
            return _answer(name, fork_of="https://github.com/up/a" if name == "o/a" else "")
        github = _GitHub({n: (ENGLISH.replace("files", f"files of {n}"), n)
                          for n in ("o/a", "o/b")})
        self.run_over(_repositories("o/a", "o/b"), github, _Model(script))
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model)
        self.assertEqual((code, model.requests, len(decided)), (0, [], 1), err)
        later = forks.Thresholds(author_minimum=2, window_start="2026-04-22")
        verdicts["https://github.com/up/a"] = {"independent": False, "reasons": ["too few"]}
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model, thresholds=later)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(decided[-1], ("o/a", REVISION, "https://github.com/up/a", later))
        self.assertEqual([r["engineered_project"] for r in self.rows("repos_annotated.csv")],
                         ["false", "true"])
        third = self.index()[-1]
        self.assertEqual((third["reused"], third["decisions"]), ("2", "1"))
        carried = next(d for _, d in label_cache.all_decisions(self.cache)
                       if d.get("carried_from"))
        self.assertEqual((carried["name"], carried["label"], carried["carried_from"]["label"],
                          carried["fork_thresholds"]), ("o/a", "false", "true", later.as_dict()))

    def test_a_new_fork_heuristic_decides_a_reused_label_again_without_the_model(self):
        """A reused label takes the fork verdict of the current fork heuristic while the
        sampling frame's thresholds stay the same. A fix to forks.py, such as counting every
        commit of a fork that shares no history with its upstream, then reaches the label."""
        verdicts = {"https://github.com/up/a": {"independent": True, "reasons": []}}
        decided = self.fork_verdicts(verdicts)

        def script(request, number):
            name = _repository_of(request)
            return _answer(name, fork_of="https://github.com/up/a" if name == "o/a" else "")
        github = _GitHub({n: (ENGLISH.replace("files", f"files of {n}"), n)
                          for n in ("o/a", "o/b")})
        self.run_over(_repositories("o/a", "o/b"), github, _Model(script))
        model = _Model(script)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model)
        self.assertEqual((code, model.requests, len(decided)), (0, [], 1), err)
        verdicts["https://github.com/up/a"] = {"independent": False, "reasons": ["too few"]}
        with mock.patch.object(classify_repos.forks, "HEURISTIC", "a later version"):
            code, err = self.run_over(_repositories("o/a", "o/b"), github, model)
        self.assertEqual((code, model.requests, len(decided)), (0, [], 2), err)
        self.assertEqual([r["engineered_project"] for r in self.rows("repos_annotated.csv")],
                         ["false", "true"])
        carried = next(d for _, d in label_cache.all_decisions(self.cache)
                       if d.get("carried_from"))
        self.assertEqual((carried["name"], carried["fork"]["heuristic"]),
                         ("o/a", "a later version"))

    def test_a_reused_forks_facts_are_read_where_github_returned_the_readme(self):
        """GitHub no longer has the commit that the sampling frame recorded, as after a force
        push, and the README comes from the default branch. A new version of the fork heuristic
        decides the reused label again at the default branch. At the recorded commit, GitHub
        would return 404 for the fork's commits, and the repository would stay without a
        decision."""
        github = _GoneRevision()
        model = _Model(lambda request, number: _answer("o/a", fork_of="https://github.com/up/a"))
        code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual(code, 0, err)
        self.assertIn(f"https://api.github.com/repos/o/a/commits?sha={REVISION}&since="
                      f"{THRESHOLDS.window_start}T00:00:00Z&per_page=1", github.sent)
        github.gone, github.sent = True, []
        model = _Model(_always_true)
        with mock.patch.object(classify_repos.forks, "HEURISTIC", "a later version"):
            code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")
        self.assertEqual([url for url in github.sent if "/commits" in url and REVISION in url],
                         [])
        self.assertIn("https://api.github.com/repos/o/a/commits?sha=main&since="
                      f"{THRESHOLDS.window_start}T00:00:00Z&per_page=1", github.sent)
        carried = [d for _, d in label_cache.all_decisions(self.cache)][-1]
        self.assertEqual((carried["revision"], carried["fork"]["heuristic"],
                          carried["fork"]["own_commits_in_window"],
                          carried["fork"]["independent"]), ("", "a later version", 400, True))

    def test_a_resumed_runs_check_reads_the_facts_where_github_returns_the_readme(self):
        """The check of a label that an earlier invocation of the run decided sends GitHub no
        README request unless the current rules need one. When the rules need GitHub's facts,
        the check requests the README first, because GitHub may no longer have the commit of the
        sampling frame. It then reads the facts where GitHub returned the README."""
        repositories = _independent("o/a", "o/b")
        github = _GoneRevision(("o/a", "o/b"))
        failing_b = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/b"
            else _answer("o/a", fork_of="https://github.com/up/a")))
        code, err = self.run_over(repositories, github, failing_b)
        self.assertEqual(code, 1, err)
        github.gone, github.sent = True, []
        with mock.patch.object(classify_repos.forks, "HEURISTIC", "a later version"):
            code, err = self.run_over(repositories, github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        self.assertEqual({r["repo_name"]: r["engineered_project"]
                          for r in self.rows("repos_annotated.csv")},
                         {"o/a": "true", "o/b": "true"})
        self.assertEqual([url for url in github.sent if "/commits" in url and REVISION in url],
                         [])
        row = self.index()[0]
        [checked] = [d for d in label_cache.read_decisions(self.cache, row["run_id"],
                                                          row["decisions_sha256"])
                     if d["name"] == "o/a"]
        self.assertEqual((checked["revision"], checked["fork"]["heuristic"]),
                         ("", "a later version"))

    def test_of_the_copies_labeled_true_at_one_head_commit_the_first_created_stays(self):
        """Repositories at one head commit have one history. zzzeek/sqlalchemy, a personal copy
        that GitHub created first, is labeled false, so of the copies labeled true the outputs
        keep sqlalchemy/sqlalchemy, which GitHub created before o/copy, even though o/copy comes
        first in the sampling frame and has the lower id. They list o/copy as a copy. A
        repository at a head commit of its own stays."""
        created = {"o/other": "2016-01-01T00:00:00Z", "zzzeek/sqlalchemy": "2013-05-25T20:05:42Z",
                   "sqlalchemy/sqlalchemy": "2018-11-27T03:35:03Z",
                   "o/copy": "2020-01-01T00:00:00Z"}
        repositories = _repositories("o/other", "zzzeek/sqlalchemy", "o/copy",
                                     "sqlalchemy/sqlalchemy")
        for repository in repositories:
            repository["created_at"] = created[repository["name"]]
        repositories[0]["revision"] = LATER
        github = _GitHub({r["name"]: (ENGLISH, r["name"]) for r in repositories})
        code, err = self.run_over(repositories, github, _Model(
            lambda request, number: _answer(
                _repository_of(request),
                label="false" if _repository_of(request) == "zzzeek/sqlalchemy" else "true")))
        self.assertEqual(code, 0, err)
        self.assertEqual([(r["repo_name"], r["engineered_project"])
                          for r in self.rows("repos_annotated.csv")],
                         [("o/other", "true"), ("zzzeek/sqlalchemy", "false"),
                          ("sqlalchemy/sqlalchemy", "true")])
        self.assertEqual(self.rows("repos_excluded.csv"), [
            {"repo_name": "o/copy", "repo_url": "https://github.com/o/copy", "reason": "copy",
             "detected_language": ""}])

    def test_of_the_copies_labeled_true_one_not_described_as_a_mirror_stays(self):
        """o/personal, created first, calls itself a copy of o/project and is labeled true,
        because a person's pull request was merged in it since the window began. The outputs
        keep o/project, which nothing describes as a mirror, and list o/personal as a copy."""
        repositories = _repositories("o/personal", "o/project")
        repositories[0]["created_at"] = "2013-05-22T21:37:41Z"
        repositories[1]["created_at"] = "2018-11-27T03:02:53Z"
        github = _GitHub({r["name"]: (ENGLISH, r["name"]) for r in repositories},
                         merges={"o/personal": ["2026-07-21T00:46:14Z"]})
        code, err = self.run_over(repositories, github, _Model(
            lambda request, number: _answer(
                _repository_of(request),
                facts={"mirror": {"described_as_mirror": "true"}}
                if _repository_of(request) == "o/personal" else None)))
        self.assertEqual(code, 0, err)
        self.assertEqual([(r["repo_name"], r["engineered_project"])
                          for r in self.rows("repos_annotated.csv")], [("o/project", "true")])
        self.assertEqual([(r["repo_name"], r["reason"]) for r in self.rows("repos_excluded.csv")],
                         [("o/personal", "copy")])

    def test_the_outputs_are_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """If a rename reached the disk before the data, a power loss shortly after would leave
        an empty file under the name. An empty `repos_annotated.csv` stops the engineered project
        selection. Each output is synced under a temporary name before its rename, and the
        directory is synced after the rename."""
        self.out.mkdir(parents=True)
        names = ["repos_annotated.csv", "repos_excluded.csv", classify_repos.ANNOTATION_RECORD]
        for name in names:
            (self.out / name).write_text("earlier")
        calls = _synced_writes(lambda: classify_repos.write_outputs(
            self.out, _repositories("o/a"), {1: {"status": classify_repos.GONE}},
            {"run": "2026-10-05T120000Z"}), self.out)
        inodes = {name: (self.out / name).stat().st_ino for name in names}
        self.assertEqual(calls, [entry for name in names for entry in (
            ("file", inodes[name]), ("rename", inodes[name], name), ("directory", True))])
        self.assertEqual(self.rows("repos_excluded.csv")[0]["repo_name"], "o/a")
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), sorted(names))

    def readme_requests(self, github, name):
        return [url for url in github.sent if url.startswith(f"https://api.github.com/repos/{name}/readme")]

    def test_a_readme_changed_only_in_badges_and_numbers_keeps_its_label(self):
        """The label is carried over to the new blob without the model, and the next run gets a
        304 for that blob."""
        before = ENGLISH + " Release 1.2.3. [![CI](https://ci.example/a.svg)](https://ci.example/a)"
        after = ENGLISH + " Release 1.2.4. [![CI](https://ci.example/b.svg)](https://ci.example/b)"
        github = _GitHub({"o/a": (before, "s1")})
        self.run_over(_repositories("o/a"), github, _Model(_always_true))
        github.readmes["o/a"] = (after, "s2")
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a"), github, model, new_run=True)
        self.assertEqual((code, model.requests), (0, []), err)
        second = self.index()[1]
        self.assertEqual((second["reused"], second["decisions"]), ("1", "1"))
        carried = next(d for _, d in label_cache.all_decisions(self.cache) if d.get("carried_from"))
        self.assertEqual((carried["readme_sha"], carried["carried_from"]["readme_sha"],
                          carried["label"]), ("s2", "s1", "true"))
        code, err = self.run_over(_repositories("o/a"), github, model,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.index()[2]["decisions"], "0")

    def test_the_description_comes_from_the_sampling_frame_and_a_changed_one_is_labeled_again(
            self):
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        frame = [{**_repositories("o/a")[0], "description": "A formatter."}]
        model = _Model(_always_true)
        self.run_over(frame, github, model)
        self.assertIn("A formatter.", model.requests[0]["input"])
        self.assertNotIn("https://api.github.com/repos/o/a", github.sent)
        model = _Model(_always_true)
        code, err = self.run_over([{**frame[0], "description": "A formatter.  "}], github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        code, err = self.run_over([{**frame[0], "description": "A deprecated formatter."}],
                                  github, model)
        self.assertEqual((code, len(model.requests)), (0, 1), err)
        self.assertIn("A deprecated formatter.", model.requests[0]["input"])

    def test_a_fix_to_the_rules_reaches_a_reused_label_without_the_model(self):
        """The current rules derive a reused label again from its stored response."""
        model = _Model(lambda request, number: _answer(
            "o/a", facts={"work_in_progress": {"unfinished": "true", "in_use": "true"}}))
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        self.run_over(_repositories("o/a"), github, model)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")
        model = _Model(_always_true)
        rule = {"work_in_progress": lambda f: f["unfinished"]}
        with mock.patch.dict(classify_repos.CRITERION_APPLIES, rule):
            code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "false")
        carried = next(d for _, d in label_cache.all_decisions(self.cache) if d.get("carried_from"))
        self.assertEqual(carried["applied_criteria"], ["work_in_progress"])
        self.assertEqual(self.readme_requests(github, "o/a")[-1:], [f"https://api.github.com/"
                                                                   f"repos/o/a/readme?ref={REVISION}"])

    def test_a_fix_to_the_purpose_check_reaches_a_reused_label(self):
        """A label made under another version of the purpose check has its quotation checked
        again, over the README's text fetched after the 304."""
        model = _Model(lambda request, number: _answer("o/a", purpose_quote="Nothing like it."))
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        self.run_over(_repositories("o/a"), github, model)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "unclear")
        model = _Model(_always_true)
        requests = len(self.readme_requests(github, "o/a"))
        with mock.patch.object(classify_repos, "quote_check", lambda: "another"), \
                mock.patch.object(classify_repos, "quoted", lambda quote, *sources: True):
            code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")
        self.assertEqual(len(self.readme_requests(github, "o/a")) - requests, 2)

    def test_a_stored_quotation_of_a_readme_with_markup_is_accepted_without_the_model(self):
        """The README of qor/media writes its purpose with a link and emphasis. A decision that
        an earlier purpose check refused becomes the model's label under the current check, with
        the quotation that the decision stored and no request to the model."""
        readme = ("# Media Library\n\nMedia is a [Golang](http://golang.org/) library that "
                  "supports the upload of *files*/*images*/*videos* to a filesystem or cloud "
                  "storage as well as *linked videos* (i.e. YouTube, Vimeo, etc.). The plugin "
                  "includes: cropping and resizing images.")
        quote = ("Media is a Golang library that supports the upload of files/images/videos to a "
                 "filesystem or cloud storage as well as linked videos (i.e. YouTube, Vimeo, "
                 "etc.).")
        github = _GitHub({"o/a": (readme, "s1")})
        with mock.patch.object(classify_repos, "quote_check", lambda: "an earlier check"), \
                mock.patch.object(classify_repos, "quoted", lambda quote, *sources: False):
            code, err = self.run_over(_repositories("o/a"), github, _Model(
                lambda request, number: _answer("o/a", purpose_quote=quote)))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "unclear")
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a"), github, model, new_run=True)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")
        decision = next(d for _, d in label_cache.all_decisions(self.cache)
                        if d.get("carried_from"))
        self.assertEqual((decision["purpose_quote"], decision["unclear_criteria"],
                          decision["carried_from"]["label"]), (quote, [], "unclear"))
        self.assertNotIn("purpose_quote_refused", decision)

    def test_a_stored_count_that_names_a_real_language_and_no_english_becomes_false(self):
        """The count of SpriteStudio/SS6PlayerForUnrealEngine4 names Japanese, and Lingua measures
        the README as English. A decision stored as unclear for this reason becomes false under
        the current rule, from its stored count and measurement, without a request to the model."""
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        words = [{"language": "Japanese", "words": 23}]
        with mock.patch.object(classify_repos.readmes, "names_language", lambda name: False):
            code, err = self.run_over(_repositories("o/a"), github, _Model(
                lambda request, number: _answer("o/a", words=words)))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "unclear")
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a"), github, model, new_run=True)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "false")
        decision = next(d for _, d in label_cache.all_decisions(self.cache)
                        if d.get("carried_from"))
        self.assertEqual((decision["readme_mainly_english"], decision["applied_criteria"],
                          decision["model_label"], decision["language"]["detected"]),
                         ("false", ["not_english"], "true", "en"))

    def test_a_reworded_docstring_of_the_purpose_check_reuses_the_label_unchanged(self):
        """A reworded docstring makes no new version of the purpose check. The reused label
        costs only the README request with its ETag, for which GitHub returns 304, and is not
        written again as a carried decision."""
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        code, err = self.run_over(_repositories("o/a"), github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        edited = _edited_script(self, '"""Whether one of the sources contains the quotation',
                                '"""Whether a description or a README contains the quotation')
        sent = len(github.sent)
        model = _Model(_always_true)
        with mock.patch.object(classify_repos, "quote_check", edited.quote_check):
            code, err = self.run_over(_repositories("o/a"), github, model, new_run=True)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(github.sent[sent:],
                         [f"https://api.github.com/repos/o/a/readme?ref={REVISION}"])
        self.assertEqual((self.index()[1]["reused"], self.index()[1]["decisions"]), ("1", "0"))
        self.assertEqual([d for _, d in label_cache.all_decisions(self.cache)
                          if d.get("carried_from")], [])

    def test_the_decisions_of_an_incomplete_run_are_reused_by_a_run_of_another_scope(self):
        """A run that ends with a failed repository writes the decisions that it made. A run
        over another scope then does not send these repositories to the model again."""
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH.replace("files", "logs"), "s2")})

        def failing(request, number):
            return [_message("not json")] if _repository_of(request) == "o/b" else _answer("o/a")
        code, err = self.run_over(_repositories("o/a", "o/b"), github, _Model(failing))
        self.assertEqual(code, 1)
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["decisions"]),
                         ("incomplete", "1"))
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model,
                                  repository_list="ids.csv", output_dir=None)
        self.assertEqual(code, 0, err)
        self.assertEqual({_repository_of(r) for r in model.requests}, {"o/b"})

    def test_a_new_lingua_checks_the_language_again_and_not_the_model(self):
        """New thresholds apply to the stored measures, and a new version of Lingua checks the
        README's language again. Neither changes the classification parameters or sends a request
        to the model."""
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        self.run_over(_repositories("o/a"), github, _Model(_always_true))
        model = _Model(_always_true)
        requests = len(self.readme_requests(github, "o/a"))
        with mock.patch.object(readmes, "LANGUAGE_CHECK",
                               {**readmes.LANGUAGE_CHECK, "min_letters": 61}):
            code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(len(self.readme_requests(github, "o/a")) - requests, 1)
        with mock.patch.object(readmes, "lingua_version", lambda: "0.0.0"):
            code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(len(self.readme_requests(github, "o/a")) - requests, 3)
        self.assertEqual(len({row["parameters"] for row in self.index()}), 1)
        latest = [d for _, d in label_cache.all_decisions(self.cache)][-1]
        self.assertEqual((latest["label"], latest["language_check"]["lingua"]), ("true", "0.0.0"))

    def test_an_exclusion_for_the_language_carried_to_a_new_lingua_names_its_origin(self):
        github = _GitHub({"o/r": (RUSSIAN, "s1")})
        self.run_over(_repositories("o/r"), github, _Model(_always_true))
        with mock.patch.object(readmes, "lingua_version", lambda: "0.0.0"):
            code, err = self.run_over(_repositories("o/r"), github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        first, second = [d for _, d in label_cache.all_decisions(self.cache)]
        self.assertEqual((second["reason"], second["language_check"]["lingua"]),
                         (readmes.NOT_ENGLISH, "0.0.0"))
        self.assertEqual(second["carried_from"]["decided_at"], first["decided_at"])
        self.assertEqual(self.index()[1]["decisions"], "1")

    def test_a_forks_label_at_a_later_commit_queries_github_once_per_sampling_frame(self):
        """The run requests GitHub's facts at the commit of the new sampling frame, and the label
        records that commit. The next run over that sampling frame then sends no request."""
        decided = self.fork_verdicts({"https://github.com/up/a": {"independent": True,
                                                                  "reasons": []}})
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        model = _Model(lambda request, number: _answer("o/a", fork_of="https://github.com/up/a"))
        self.run_over(_repositories("o/a"), github, model)
        later = forks.Thresholds(author_minimum=2, window_start="2026-04-22")
        for _ in range(2):
            code, err = self.run_over(_repositories("o/a", revision=LATER), github,
                                      _Model(_always_true), thresholds=later)
            self.assertEqual(code, 0, err)
        self.assertEqual([(revision, thresholds) for _, revision, _, thresholds in decided],
                         [(REVISION, THRESHOLDS), (LATER, later)])

    def test_a_fixed_mirror_rule_queries_github_for_the_merges_of_a_reused_label(self):
        """A fixed rule that makes a stored response a mirror needs the merges that decide
        whether the repository is developed here. The run that made the label never requested
        these merges."""
        answer = lambda request, number: _answer(
            "o/a", facts={"mirror": {"described_as_mirror": "true"}})
        github = _GitHub({"o/a": (ENGLISH, "s1")}, merges={"o/a": ["2026-07-21T00:46:14Z"]})
        never = {"mirror": lambda facts: classify_repos.FALSE}
        with mock.patch.dict(classify_repos.CRITERION_APPLIES, never):
            self.run_over(_repositories("o/a"), github, _Model(answer))
        self.assertNotIn(("graphql", "o/a"), github.sent)
        model = _Model(answer)
        code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertIn(("graphql", "o/a"), github.sent)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")

    def test_merges_counted_with_the_pull_requests_of_github_apps_are_read_again(self):
        """A reused label of a mirror with merges counted with the pull requests of GitHub Apps
        reads the merges again without the model. Dependabot's
        merge since the window began then no longer keeps the mirror in the sample."""
        answer = lambda request, number: _answer(
            "o/a", facts={"mirror": {"described_as_mirror": "true"}})
        github = _GitHub({"o/a": (ENGLISH, "s1")},
                         merges={"o/a": [("2026-05-31T14:04:14Z", "Bot")]})
        # A decision that counted the latest merge whoever opened the pull request.
        with mock.patch.object(classify_repos, "MERGES_COUNTED", "every pull request"), \
                mock.patch.object(classify_repos, "latest_merge",
                                  lambda client, name: "2026-05-31T14:04:14Z"):
            self.run_over(_repositories("o/a"), github, _Model(answer))
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")
        github.sent = []
        model = _Model(answer)
        code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertIn(("graphql", "o/a"), github.sent)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "false")

    def test_an_empty_readme_does_not_inherit_the_label_of_one_with_badges_alone(self):
        """Both READMEs have the same wording, which contains no words."""
        github = _GitHub({"o/a": ("[![CI](https://ci.example/a.svg)](https://ci.example/a)", "s1")})
        self.run_over(_repositories("o/a"), github, _Model(_always_true))
        github.readmes["o/a"] = ("", "s2")
        code, err = self.run_over(_repositories("o/a"), github, _Model(_always_true),
                                  new_run=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("repos_excluded.csv")[0]["reason"], "empty_readme")

    def test_a_label_excluded_for_its_language_after_a_304_keeps_its_readme(self):
        """New thresholds can exclude a README for which GitHub returned 304, a response without
        the blob. The exclusion names the README that the label was made for."""
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        self.run_over(_repositories("o/a"), github, _Model(_always_true))
        with mock.patch.object(readmes, "english", lambda measures: False):
            code, err = self.run_over(_repositories("o/a"), github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        excluded = [d for _, d in label_cache.all_decisions(self.cache)][-1]
        self.assertEqual((excluded["reason"], excluded["readme_sha"], excluded["readme_path"]),
                         (readmes.NOT_ENGLISH, "s1", "README.md"))
        self.assertTrue(excluded["readme_wording"])

    def test_a_stored_response_that_the_rules_cannot_read_stops_the_run(self):
        """The failure is a defect of the rules, not a response of GitHub to retry four times."""
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        self.run_over(_repositories("o/a"), github, _Model(_always_true))
        model = _Model(_always_true)
        requests = len(self.readme_requests(github, "o/a"))
        broken = {"website": lambda facts: facts["no_such_fact"]}
        with mock.patch.dict(classify_repos.CRITERION_APPLIES, broken):
            code, err = self.run_over(_repositories("o/a"), github, model)
        self.assertEqual((code, model.requests), (1, []))
        self.assertIn("An unexpected error stopped the run", err)
        self.assertIn("deriving the stored response for o/a again raised", err)
        self.assertEqual(len(self.readme_requests(github, "o/a")) - requests, 1)

    def test_a_readme_changed_in_a_number_has_its_quotation_checked_again(self):
        """The wording leaves numbers out, and a quotation with a number in it can be gone from
        a README with the same wording."""
        before, after = ENGLISH + " Version 2 formats files.", ENGLISH + " Version 3 formats files."
        github = _GitHub({"o/a": (before, "s1")})
        quote = lambda request, number: _answer("o/a", purpose_quote="Version 2 formats files.")
        self.run_over(_repositories("o/a"), github, _Model(quote))
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "true")
        github.readmes["o/a"] = (after, "s2")
        model = _Model(quote)
        code, err = self.run_over(_repositories("o/a"), github, model, new_run=True)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_annotated.csv")[0]["engineered_project"], "unclear")

    def test_the_label_cache_stays_readable_when_a_run_stops_while_writing_its_decisions(self):
        """A resumed run writes the decisions of the incomplete run again. A stop between the
        files and the index row leaves every checksum of the index matching a file."""
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH.replace("files", "logs"), "s2")})

        def failing(request, number):
            return [_message("not json")] if _repository_of(request) == "o/b" else _answer("o/a")
        self.assertEqual(self.run_over(_repositories("o/a", "o/b"), github, _Model(failing))[0], 1)
        written = label_cache.write_decisions

        def stopped(cache_dir, rid, decisions):
            written(cache_dir, rid, list(decisions)[:1])
            raise KeyboardInterrupt
        with mock.patch.object(label_cache, "write_decisions", stopped), \
                self.assertRaises(KeyboardInterrupt):
            self.run_over(_repositories("o/a", "o/b"), github, _Model(_always_true))
        self.assertEqual(len(list(label_cache.all_decisions(self.cache))), 0)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, _Model(_always_true))
        self.assertEqual(code, 0, err)

    def test_a_repository_gone_when_its_readme_is_read_again_is_gone(self):
        """GitHub returns 304, then 404 when the README's text is fetched again. The repository
        is recorded as gone, an outcome that no decision caches, and not as an exclusion."""
        github = _GitHub({"o/a": (ENGLISH, "s1")})
        frame = [{**_repositories("o/a")[0], "description": "A formatter."}]
        self.run_over(frame, github, _Model(_always_true))
        original = github.get

        def get(url, headers=None, timeout=None):
            if (headers or {}).get("If-None-Match"):
                return original(url, headers, timeout)
            return _Response(404, {})
        github.get = get
        model = _Model(_always_true)
        code, err = self.run_over([{**frame[0], "description": "Another tool."}], github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual(self.rows("repos_excluded.csv")[0]["reason"], "not_found_or_no_access")
        self.assertEqual(self.index()[1]["gone"], "1")

    def test_a_repository_without_a_readme_at_the_same_commit_costs_no_request(self):
        """The exclusion stands without a request to GitHub, because a commit's tree does not
        change."""
        github = _GitHub({}, {"o/d": {"id": 1, "full_name": "o/d"}})
        self.run_over(_repositories("o/d"), github, _Model(_always_true))
        self.assertEqual(len(github.sent), 4)
        code, err = self.run_over(_repositories("o/d"), github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(github.sent), 4)
        self.assertEqual(self.rows("repos_excluded.csv")[0]["reason"], "no_readme_found")
        self.run_over(_repositories("o/d", revision=LATER), github, _Model(_always_true))
        self.assertEqual(len(github.sent), 8)
        # The exclusion is recorded at the later commit, and the next run over that commit sends
        # no request either.
        self.run_over(_repositories("o/d", revision=LATER), github, _Model(_always_true))
        self.assertEqual(len(github.sent), 8)

    def test_a_stopped_run_resumes_and_queries_the_model_only_for_the_rest(self):
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})

        def failing(request, number):
            if _repository_of(request) == "o/b":
                return [_message("not json")]
            return _answer("o/a")
        code, err = self.run_over(repositories, github, _Model(failing))
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 repositories have no decision", err)
        self.assertIn("1 repositories have no decision, 0 for GitHub's response, 1 for the "
                      "model's response and 0 for an unexpected error. The first is o/b", err)
        partial = next(self.cache.glob("*.partial.jsonl"))
        failed = [json.loads(line) for line in partial.read_text().splitlines()
                  if '"failed"' in line]
        self.assertEqual([(f["name"], f["reason"], f["error"]) for f in failed],
                         [("o/b", "model", "ModelFailure: the model's response does not call "
                                           "submit_classification (the status none, the output "
                                           "items [message] and 10 output tokens)")]
                         * (classify_repos.RETRY_ROUNDS + 1))
        self.assertEqual(self.index()[0]["outcome"], "incomplete")
        self.assertFalse((self.out / "repos_annotated.csv").exists())
        model = _Model(_always_true)
        code, err = self.run_over(repositories, github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/b"])
        self.assertEqual(len(self.index()), 1)
        row = self.index()[0]
        self.assertEqual((row["outcome"], row["decisions"], row["labeled"]),
                         ("finished", "2", "2"))
        self.assertEqual(int(row["model_requests"]),
                         (classify_repos.RETRY_ROUNDS + 1) * classify_repos.ATTEMPTS + 2)

    def test_a_resumed_run_derives_its_earlier_labels_under_the_current_rules(self):
        """A new image can change a rule between two invocations of a run. The labels of the
        earlier invocation are derived again under the current rules, as the labels of an
        earlier run are. The outputs then do not mix the labels of two versions of the rules, and
        a label that the rules decide from its stored response costs no request."""
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        in_use = {"work_in_progress": {"unfinished": "true", "in_use": "true"}}
        failing_b = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/b"
            else _answer("o/a", facts=in_use)))
        code, err = self.run_over(repositories, github, failing_b)
        self.assertEqual(code, 1, err)
        partial = next(self.cache.glob("*.partial.jsonl"))
        [decided] = classify_repos.partial_decisions(partial)
        # The rule of the first invocation makes an exception for unfinished software in use.
        self.assertEqual((decided["name"], decided["label"]), ("o/a", "true"))
        sent = len(github.sent)
        model = _Model(_always_true)
        # The new image's rule makes no such exception.
        stricter = {"work_in_progress": lambda facts: facts["unfinished"]}
        with mock.patch.dict(classify_repos.CRITERION_APPLIES, stricter):
            code, err = self.run_over(repositories, github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/b"])
        self.assertEqual({r["repo_name"]: r["engineered_project"]
                          for r in self.rows("repos_annotated.csv")},
                         {"o/a": "false", "o/b": "true"})
        self.assertEqual([url for url in github.sent[sent:] if "/o/a/" in str(url)], [])
        row = self.index()[0]
        [carried] = [d for d in label_cache.read_decisions(self.cache, row["run_id"],
                                                          row["decisions_sha256"])
                     if d["name"] == "o/a"]
        self.assertEqual((carried["label"], carried["applied_criteria"]),
                         ("false", ["work_in_progress"]))

    def test_a_label_whose_check_under_the_current_rules_github_failed_is_not_paid_again(self):
        """A resumed run checks o/a's label under a new purpose check, which needs the README,
        and GitHub fails the README request through every round. The label stays in the run's
        decisions, and the next invocation checks it without the model. Dropped with the line
        that failed, the label would be paid for again."""
        repositories = _independent("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        failing_b = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/b"
            else _answer("o/a")))
        code, err = self.run_over(repositories, github, failing_b)
        self.assertEqual(code, 1, err)
        answer = github.get

        def failing_for_a(url, headers=None, timeout=None):
            if "/o/a/readme" in url:
                return _Response(502, {})
            return answer(url, headers, timeout)
        with mock.patch.object(classify_repos, "quote_check", lambda: "a new purpose check"):
            github.get = failing_for_a
            code, err = self.run_over(repositories, github, _Model(_always_true))
            self.assertEqual(code, 1, err)
            self.assertIn("Could not decide o/a (", err)
            github.get = answer
            model = _Model(_always_true)
            code, err = self.run_over(repositories, github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        self.assertEqual({r["repo_name"]: r["engineered_project"]
                          for r in self.rows("repos_annotated.csv")},
                         {"o/a": "true", "o/b": "true"})

    def test_a_line_that_a_stop_cut_short_leaves_the_next_decision_whole(self):
        """A kill in the middle of writing a line leaves the line without its end. Appended to that
        line, the resumed run's next decision would be missing from the run's decisions file,
        while the labels file names the run as the run that decided it."""
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        failing = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/b" else _answer("o/a")))
        code, err = self.run_over(repositories, github, failing)
        self.assertEqual(code, 1)
        partial = next(self.cache.glob("*.partial.jsonl"))
        with open(partial, "a", encoding="utf-8") as f:
            f.write('{"id":2,"status":"decided","decision":{"id":2,"outc')
        code, err = self.run_over(repositories, github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        row = self.index()[0]
        decided = [d["name"] for d in label_cache.read_decisions(self.cache, row["run_id"],
                                                                row["decisions_sha256"])]
        self.assertEqual(sorted(decided), ["o/a", "o/b"])

    def test_a_killed_runs_requests_count_when_it_is_resumed(self):
        """The index records a run's requests and tokens at the run's end, which an invocation
        that a kill ended never reaches. Each line of the partial file therefore records them as
        well."""
        class Killed(KeyboardInterrupt):
            pass

        names = ("o/a", "o/b", "o/c")
        github = _GitHub({name: (ENGLISH, name) for name in names})

        def killed_at_the_third(request, number):
            if number == 3:
                raise Killed()
            return _answer(_repository_of(request))
        # Interrupted, the invocation records what it spent after its last line as well. Killed,
        # it does not, and the README of the repository in flight goes uncounted.
        for interrupted, uncounted in ((True, 0), (False, 1)):
            with self.subTest(interrupted=interrupted):
                self.setUp()
                github = _GitHub({name: (ENGLISH, name) for name in names})
                with contextlib.ExitStack() as stack:
                    if not interrupted:
                        stack.enter_context(mock.patch.object(classify_repos.Run, "record_spent",
                                                              lambda run: None))
                    with self.assertRaises(Killed):
                        self.run_over(_repositories(*names), github,
                                      _Model(killed_at_the_third), workers=1)
                self.assertEqual(self.index()[0]["model_requests"], "")
                code, err = self.run_over(_repositories(*names), github, _Model(_always_true),
                                          workers=1)
                self.assertEqual(code, 0, err)
                row = self.index()[0]
                decisions = list(label_cache.read_decisions(self.cache, row["run_id"],
                                                            row["decisions_sha256"]))
                self.assertEqual(int(row["model_requests"]),
                                 sum(d["usage"]["requests"] for d in decisions))
                self.assertEqual((row["model_requests"], row["input_tokens"],
                                  row["github_requests"]),
                                 ("3", "300", str(len(github.sent) - uncounted)))

    def test_a_killed_runs_decisions_are_reused_by_a_run_over_another_retrieval(self):
        """A kill, such as by the out-of-memory killer or SIGTERM, ends a run before it writes
        its decisions file, and a run over a new retrieval does not resume the killed run. The
        new run reuses the decisions of the partial file and writes them. Its labels then name a
        run with a decisions file that contains each decision."""
        class Killed(KeyboardInterrupt):
            pass

        names = ("o/a", "o/b", "o/c")
        github = _GitHub({name: (ENGLISH, name) for name in names})

        def killed_at_c(request, number):
            if _repository_of(request) == "o/c":
                raise Killed()
            return _answer(_repository_of(request))
        with mock.patch.object(classify_repos.Run, "record_spent", lambda run: None), \
                self.assertRaises(Killed):
            self.run_over(_repositories(*names), github, _Model(killed_at_c), workers=1)
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["decisions"]),
                         ("running", ""))
        model = _Model(_always_true)
        code, err = self.run_over(_repositories(*names), github, model, workers=1,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/c"])
        row = self.index()[1]
        self.assertEqual((row["retrieval_id"], row["labeled"], row["reused"]),
                         ("2026-11-05T101430Z", "3", "2"))
        rows = label_cache.read_labels(self.cache, row["run_id"], row["labels_sha256"])
        wanted = {}
        for labeled in rows:
            wanted.setdefault(labeled["decided_in"], set()).add(int(labeled["id"]))
        self.assertEqual(len(label_cache.decisions_of(self.cache, wanted)), 3)

    def test_a_line_cut_inside_a_character_does_not_stop_a_run_that_reads_the_partial_file(
            self):
        """A kill while a run appends a decision can cut the last line of its partial file inside
        a character of UTF-8. Every later run reads the decisions in the partial file of a run
        with the outcome running, and only the run itself drops the cut line before it appends.
        Decoded as a whole, the file would stop every later run before its first repository."""
        class Killed(KeyboardInterrupt):
            pass

        names = ("o/a", "o/b", "o/c")
        github = _GitHub({name: (ENGLISH, name) for name in names})

        def killed_at_c(request, number):
            if _repository_of(request) == "o/c":
                raise Killed()
            return _answer(_repository_of(request), justification="Ein Werkzeug für Dateien.")
        with mock.patch.object(classify_repos.Run, "record_spent", lambda run: None), \
                self.assertRaises(Killed):
            self.run_over(_repositories(*names), github, _Model(killed_at_c), workers=1)
        partial = label_cache.partial_path(self.cache, self.index()[0]["run_id"])
        line = json.dumps({"id": 3, "status": "decided", "decision": {"justification": "für"}},
                          ensure_ascii=False).encode("utf-8")
        with open(partial, "ab") as f:
            f.write(line[:line.index("ü".encode("utf-8")) + 1])
        model = _Model(_always_true)
        code, err = self.run_over(_repositories(*names), github, model, workers=1,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/c"])
        self.assertEqual(self.index()[1]["reused"], "2")

    def test_a_decision_carried_from_a_killed_run_names_the_run_that_wrote_it(self):
        """A stopped run over the sampling frame resumes after an evaluation, which a kill ended,
        decided one of its repositories. The resumed run writes that decision as carried, and a
        later run that reuses the decision names the resumed run, with the decision in its
        decisions file. The evaluation wrote no decisions file. Named after the evaluation, the
        decision could never be read for the comparison, and the comparison would fail at every
        invocation."""
        class Killed(KeyboardInterrupt):
            pass

        def killed_at_a(request, number):
            if _repository_of(request) == "o/a":
                raise Killed()
            return _answer(_repository_of(request))

        def killed_at_the_end(*arguments):
            raise Killed()
        github = _GitHub({name: (ENGLISH, name) for name in ("o/b", "o/a")})
        repositories = _repositories("o/b", "o/a")
        with self.assertRaises(Killed):
            self.run_over(repositories, github, _Model(killed_at_a), workers=1)
        with mock.patch.object(classify_repos, "report", killed_at_the_end), \
                self.assertRaises(Killed):
            self.run_over(repositories[1:], github, _Model(_always_true), workers=1,
                          repository_list="evaluation_sample.csv")
        model = _Model(_always_true)
        code, err = self.run_over(repositories, github, model, workers=1)
        self.assertEqual((code, model.requests), (0, []), err)
        code, err = self.run_over(repositories, github, model, workers=1,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual((code, model.requests), (0, []), err)
        resumed, killed, later = self.index()
        self.assertEqual((resumed["outcome"], killed["outcome"], killed["decisions_sha256"]),
                         ("finished", "running", ""))
        rows = label_cache.read_labels(self.cache, later["run_id"], later["labels_sha256"])
        self.assertEqual({row["repo_name"]: row["decided_in"] for row in rows},
                         {"o/b": resumed["run_id"], "o/a": resumed["run_id"]})
        self.assertEqual(len(label_cache.decisions_of(self.cache,
                                                      {resumed["run_id"]: {1, 2}})), 2)

    def test_a_line_separator_in_a_response_keeps_its_decision(self):
        """JSON keeps U+2028 in a string as it is. A reader of the partial file that ended a
        line there would drop the decision, and the next run would request it from the model
        again."""
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        model = _Model(lambda request, number: _answer(_repository_of(request),
                                                       justification="Software. Yes."))
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[0]["decisions"], "2")
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])

    def test_a_repository_left_without_a_decision_is_decided_in_a_retry_round(self):
        """A connection that drops in flight leaves a repository without a decision. A retry
        round decides the repository, and the run does not end incomplete over it."""
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        dropped = {"o/b": classify_repos.ATTEMPTS}

        def script(request, number):
            name = _repository_of(request)
            if dropped.get(name):
                dropped[name] -= 1
                raise openai.APIConnectionError(request=SimpleNamespace())
            return _answer(name)
        code, err = self.run_over(repositories, github, _Model(script))
        self.assertEqual(code, 0, err)
        self.assertIn("Retry round 1 of", err)
        self.assertIn("for the 1 repositories without a decision", err)
        row = self.index()[0]
        self.assertEqual((row["outcome"], row["labeled"]), ("finished", "2"))

    def test_an_outage_of_github_longer_than_the_retry_rounds_is_waited_out(self):
        """From the last repositories of the pass on, GitHub returns HTTP 502 to every request for
        an hour, longer than the retry rounds take. Once two repositories have failed, every
        worker pauses, with waits that grow as the waits of the sampling stage's query do. The
        run labels every repository once GitHub responds again, instead of ending incomplete."""
        names = [f"o/r{i}" for i in range(4)]
        clock = _Clock()
        outage = 3600
        self.assertGreater(outage, classify_repos.RETRY_ROUNDS * 60)
        github = _Outage({name: (ENGLISH, name) for name in names}, clock, until=outage, before=2)
        model = _Model(_always_true)
        with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 60):
            code, err = self.run_over(_repositories(*names), github, model, workers=2,
                                      clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["labeled"]),
                         ("finished", "4"))
        self.assertEqual(sorted(_repository_of(r) for r in model.requests), names)
        self.assertGreaterEqual(clock.now, outage)
        self.assertIn("GitHub returned no usable response for 2 repositories since its last "
                      "response, the last for o/r3 with NoResponse", err)
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)

    def test_an_outage_of_the_readme_requests_alone_is_waited_out(self):
        """From the last repositories of the pass on, GitHub fails every README request for an
        hour and still responds to `/rate_limit`, which reads no repository. The request that
        tests whether GitHub responds is the README that GitHub served last. That request fails
        as well, every worker pauses, and the run labels every repository once GitHub responds
        again."""
        names = [f"o/r{i}" for i in range(4)]
        clock = _Clock()
        outage = 3600
        github = _ReadmeOutage({name: (ENGLISH, name) for name in names}, clock, until=outage,
                               before=2)
        model = _Model(_always_true)
        with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 60):
            code, err = self.run_over(_repositories(*names), github, model, workers=2,
                                      clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["labeled"]),
                         ("finished", "4"))
        self.assertGreaterEqual(clock.now, outage)
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)

    def test_an_outage_of_the_readme_requests_under_way_at_the_start_is_waited_out(self):
        """The outage of the README requests above is under way when the next run starts, and
        GitHub serves no README in that run. The README of an earlier decision is the request
        that tests whether GitHub responds, and every worker pauses. `/rate_limit`, to which
        GitHub responds, would let every repository fail on its own and the run end incomplete."""
        names = [f"o/r{i}" for i in range(4)]
        code, err = self.run_over(_repositories(*names), _GitHub(
            {name: (ENGLISH, name) for name in names}), _Model(_always_true))
        self.assertEqual(code, 0, err)
        clock = _Clock()
        github = _ReadmeOutage({name: (ENGLISH, name) for name in names}, clock, until=3600)
        with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 60):
            code, err = self.run_over(_repositories(*names), github, _Model(_always_true),
                                      workers=2, clock=clock, retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.index()[-1]["outcome"], self.index()[-1]["reused"]),
                         ("finished", "4"))
        self.assertGreaterEqual(clock.now, 3600)
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)

    def test_an_outage_of_github_without_end_stops_the_run_after_the_pauses(self):
        """When GitHub fails every request through every pause, the run stops with its decisions
        kept, after the total of the pauses and a bounded number of requests. It does not fail
        every repository left in the pass. The next run resumes the stopped run."""
        names = [f"o/r{i}" for i in range(8)]
        clock = _Clock()
        github = _Outage({name: (ENGLISH, name) for name in names}, clock, until=float("inf"),
                         before=2)
        code, err = self.run_over(_repositories(*names), github, _Model(_always_true),
                                  workers=2, clock=clock)
        self.assertEqual(code, 1)
        self.assertIn("ERROR repository_labeling: Stopped the run, because GitHub returned no "
                      "usable response through 7:20:00 of pauses since its last response", err)
        self.assertGreaterEqual(clock.now, sum(classify_repos.GITHUB_PAUSES))
        self.assertLess(clock.now, sum(classify_repos.GITHUB_PAUSES) + 3600)
        self.assertLessEqual(github.failed, 30 * readmes.ATTEMPTS)
        self.assertEqual(self.index()[0]["outcome"], "incomplete")
        github.until = 0
        model = _Model(_always_true)
        code, err = self.run_over(_repositories(*names), github, model, workers=2)
        self.assertEqual(code, 0, err)
        self.assertEqual([(row["outcome"], row["labeled"]) for row in self.index()],
                         [("finished", "8")])
        self.assertEqual(sorted(_repository_of(r) for r in model.requests), names[2:])

    def failing_github(self, names, failing):
        """GitHub that returns HTTP 502 to every request for the repositories in `failing` and
        responds for the others."""
        class Failing(_GitHub):
            def get(self, url, headers=None, timeout=None):
                if any(f"/repos/{name}/" in url for name in failing):
                    self.sent.append(url)
                    return _Response(502, {})
                return super().get(url, headers, timeout)
        return Failing({name: (ENGLISH, name) for name in names})

    def undecided(self):
        return sorted(row["repo_name"] for row in self.rows("repos_excluded.csv")
                      if row["reason"] == "undecided")

    def test_repositories_that_github_fails_every_time_end_undecided_without_a_pause(self):
        """GitHub returns HTTP 502 to every request for ten repositories spread through a pass of
        400 and responds for the others. In the pass, these responses keep each failure on its
        own. A retry round holds only the ten. Counted toward the pause, their failures would
        pause every worker through every wait of GITHUB_PAUSES and then stop the run, and every
        resume would stop the same way. After each of their failures in the retry rounds, GitHub
        serves the README that it served last again. The ten therefore end undecided without a
        pause, and the run finishes."""
        names = [f"o/bad{i}" if i % 40 == 7 else f"o/r{i}" for i in range(400)]
        failing = {name for name in names if name.startswith("o/bad")}
        self.with_ceiling(lambda repositories: len(failing))
        clock = _Clock()
        github = self.failing_github(names, failing)
        with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 60):
            code, err = self.run_over(_repositories(*names), github, _Model(_always_true),
                                      workers=classify_repos.WORKERS, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.undecided(), sorted(failing))
        self.assertEqual(self.index()[0]["outcome"], "finished")
        self.assertLess(clock.now, classify_repos.GITHUB_PAUSES[0])
        self.assertNotIn("Pausing every worker", err)
        # The pass requests the README of each other repository once, and each failure in a
        # retry round requests one of these READMEs again.
        served = collections.Counter(url for url in github.sent if "/readme" in url
                                     and not any(f"/repos/{name}/" in url for name in failing))
        self.assertEqual(sum(served.values()) - len(served),
                         classify_repos.RETRY_ROUNDS * len(failing))

    def test_two_repositories_that_github_fails_at_the_end_of_a_pass_begin_no_pause(self):
        """At the end of a pass, the other workers are idle, and no response of GitHub comes
        between the failures of the last two repositories. Counted toward the pause, the second
        failure would pause every worker through every wait of GITHUB_PAUSES, although GitHub
        served every other repository."""
        names = [f"o/bad{i}" if i in (398, 399) else f"o/r{i}" for i in range(400)]
        failing = {name for name in names if name.startswith("o/bad")}
        self.with_ceiling(lambda repositories: len(failing))
        clock = _Clock()
        github = self.failing_github(names, failing)
        with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 60):
            code, err = self.run_over(_repositories(*names), github, _Model(_always_true),
                                      workers=classify_repos.WORKERS, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.undecided(), sorted(failing))
        self.assertNotIn("Pausing every worker", err)
        self.assertLess(clock.now, classify_repos.GITHUB_PAUSES[0])

    def test_a_run_that_tries_again_repositories_that_github_failed_begins_no_pause(self):
        """The run over the first retrieval labeled every repository. GitHub then returns HTTP
        502 to every request for o/bad0 and o/bad1, the first two repositories in the
        sampling frame, and the run over the next retrieval leaves both without a decision. No
        collection over the sample has begun, so the next invocation tries them again in a new
        run, which begins with them. They count as repositories that GitHub failed earlier in
        the run, and until GitHub has served a README in the invocation, the README of an
        earlier decision on either does not stand in for the README that GitHub served last
        (`readmes.Client.served_earlier`). The README of another repository stands in, GitHub
        serves it, and each of the two fails on its own. Counted as the first failures of the
        run, the two would hold every worker through every wait of GITHUB_PAUSES, because the
        README of the earlier decision on o/bad0 would stand in, and GitHub fails it too."""
        names = ["o/bad0", "o/bad1", *(f"o/r{i}" for i in range(10))]
        failing: set = set()
        self.with_ceiling(lambda repositories: 2)
        github = self.failing_github(names, failing)
        code, err = self.labeling(_repositories(*names), github, _Model(_always_true), None,
                                  workers=1)
        self.assertEqual(code, 0, err)
        failing |= {"o/bad0", "o/bad1"}
        code, err = self.labeling(_repositories(*names), github, _Model(_always_true), None,
                                  retrieval="2026-11-05T101430Z", clock=_Clock(), workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.undecided(), ["o/bad0", "o/bad1"])
        clock = _Clock()
        code, err = self.run_over(_repositories(*names), github, _Model(_always_true),
                                  retrieval="2026-11-05T101430Z", clock=clock, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual([row["outcome"] for row in self.index()], ["finished"] * 3)
        self.assertEqual(self.undecided(), ["o/bad0", "o/bad1"])
        self.assertNotIn("Pausing every worker", err)
        self.assertLess(clock.now, classify_repos.GITHUB_PAUSES[0])

    def test_the_failures_of_a_finished_run_are_read_from_the_reasons_of_its_labels(self):
        """The labels of a finished run give each repository that the run left without a
        decision what failed it in its reason (`labels`): the model, GitHub, or GitHub or the
        model while the second model labeled it. A run that follows under the same model,
        classification parameters, retrieval and scope counts the repositories that the model
        failed as failed by the model in an earlier round, and the repositories that GitHub
        failed as failed by GitHub earlier in the run. The second model labels only a repository
        that the classifying model failed through every attempt, so a failure of GitHub in its
        pass counts for both. A finished run under another retrieval names none."""
        repositories = _repositories("o/a", "o/b", "o/c", "o/d")
        key = {"model": "gpt-6-luna", "parameters": "p" * 64,
               "retrieval_id": "2026-11-05T101430Z", "scope": label_cache.SAMPLING_FRAME_SCOPE}
        rid = label_cache.add_run({"outcome": "running", "started_at": "2026-11-05T11:00:00Z",
                                   **key}, self.cache)
        failed = {1: {"reason": classify_repos.MODEL_FAILED, "error": "ModelFailure: no call"},
                  2: {"reason": classify_repos.GITHUB_FAILED, "error": "NoResponse: HTTP 502"},
                  3: {"reason": classify_repos.GITHUB_FAILED, "error": "NoResponse: HTTP 502",
                      "model": "z-ai/glm-5.3",
                      "fallback_reason": classify_repos.RESPONSE_FORMAT}}
        gone = {4: {"id": 4, "status": classify_repos.GONE, "name": "o/d"}}
        rows = classify_repos.labels(rid, repositories, gone, failed)
        label_cache.put_index_row({"run_id": rid, "outcome": "finished",
                                   "labels_sha256": label_cache.write_labels(self.cache, rid,
                                                                             rows)},
                                  self.cache)
        self.assertEqual(classify_repos.failed_in_finished_run(self.cache, key),
                         ({1, 3}, {"o/b", "o/c"}))
        self.assertEqual(classify_repos.failed_in_finished_run(
            self.cache, {**key, "retrieval_id": "2026-12-05T101430Z"}), (set(), set()))

    def test_an_outage_of_github_that_begins_in_the_retry_rounds_is_waited_out(self):
        """In the pass, GitHub fails four of forty repositories on their own and responds for
        the others. From the start of the first retry round, it then fails every request for
        twenty minutes, longer than the retry rounds take. The round holds only the four
        repositories that GitHub failed before. GitHub returns no response to the README that it
        served last either. Their failures therefore count toward the pause, and every
        repository is decided after the pause. Left on their own, the four would fail in every
        round of the outage and end undecided."""
        names = [f"o/bad{i}" if i % 10 == 3 else f"o/r{i}" for i in range(40)]
        flaky = {name for name in names if name.startswith("o/bad")}
        clock, outage = _Clock(), {"until": None}
        self.assertGreater(20 * 60, classify_repos.RETRY_ROUNDS * 90)
        on_the_clock = clock.sleep

        async def sleep(seconds):
            if seconds == 90 and outage["until"] is None:
                outage["until"] = clock.now + 20 * 60
            await on_the_clock(seconds)
        clock.sleep = sleep

        class GitHub(_GitHub):
            def get(self, url, headers=None, timeout=None):
                if outage["until"] is None:
                    if any(f"/repos/{name}/" in url for name in flaky):
                        return _Response(502, {})
                elif clock() < outage["until"]:
                    self.sent.append(url)
                    return _Response(502, {})
                return super().get(url, headers, timeout)
        github = GitHub({name: (ENGLISH, name) for name in names})
        self.with_ceiling(lambda repositories: len(flaky))
        model = _Model(_always_true)
        with mock.patch.object(classify_repos, "RETRY_ROUND_SECONDS", 90):
            code, err = self.run_over(_repositories(*names), github, model,
                                      workers=classify_repos.WORKERS, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["labeled"]),
                         ("finished", "40"))
        self.assertEqual(self.undecided(), [])
        self.assertEqual(sorted(_repository_of(r) for r in model.requests), sorted(names))
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)
        self.assertGreaterEqual(clock.now, outage["until"])
        # The README that GitHub served last, of a repository that no retry round holds, was
        # requested again.
        self.assertTrue(any(f"/repos/{name}/readme" in url for url in github.sent
                            for name in set(names) - flaky))

    def test_a_resumed_run_does_not_pause_for_the_repositories_that_github_failed_before(self):
        """A run that stopped after its pass, here because more repositories than its ceiling
        have no decision, leaves its resume only the repositories that GitHub failed. Counted
        toward the pause, their failures would pause every worker through every wait of
        GITHUB_PAUSES and then stop the run at every resume. These repositories fail on their
        own, and the resumed run finishes without them."""
        names = [f"o/r{i}" for i in range(6)]
        failing = set(names[1::2])
        code, err = self.run_over(_repositories(*names), self.failing_github(names, failing),
                                  _Model(_always_true), workers=1, clock=_Clock())
        self.assertEqual(code, 1, err)
        self.assertEqual(self.index()[0]["outcome"], "incomplete")
        self.with_ceiling(lambda repositories: len(failing))
        clock = _Clock()
        model = _Model(_always_true)
        code, err = self.run_over(_repositories(*names), self.failing_github(names, failing),
                                  model, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.undecided(), model.requests), (sorted(failing), []))
        self.assertLess(clock.now, classify_repos.GITHUB_PAUSES[0])
        self.assertNotIn("Pausing every worker", err)

    def test_the_checks_of_a_resumed_run_wait_for_the_pause_for_github(self):
        """A resumed run checks its earlier labels under a new purpose check, which needs their
        READMEs, while GitHub fails every request for an hour. A check that waits for the pause
        reads its line again after the pause, and the labels cost no request to the model."""
        names = ("o/a", "o/b", "o/c")
        clock = _Clock()
        github = _Outage({name: (ENGLISH, name) for name in names}, clock, until=0)
        failing_c = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/c"
            else _answer(_repository_of(request))))
        code, err = self.run_over(_repositories(*names), github, failing_c)
        self.assertEqual(code, 1, err)
        github.until, github.before = 3600, github.served
        model = _Model(_always_true)
        with mock.patch.object(classify_repos, "quote_check", lambda: "a new purpose check"):
            code, err = self.run_over(_repositories(*names), github, model, clock=clock)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/c"])
        self.assertIn("Pausing every worker for 0:05:00", err)
        # The first check that GitHub failed fails for the pass, and the other waits.
        self.assertEqual(err.count("Retrying it later"), 1)
        self.assertGreaterEqual(clock.now, 3600)
        self.assertLess(clock.now, sum(classify_repos.GITHUB_PAUSES))

    def test_a_response_kept_for_githubs_facts_is_not_paid_again_after_a_stop(self):
        """The model's response makes o/a a mirror, or GitHub marks o/a as one, as it marks
        luci/recipes-py, whatever the response says. GitHub then rejects every GitHub access
        token for the merges, and the run stops. The response is in the partial file, and the
        next run reads only GitHub's facts. A response kept in memory alone would be paid for
        again."""
        cases = [(_answer("o/a", facts={"mirror": {"described_as_mirror": "true"}}),
                  _repositories("o/a")),
                 (_answer("o/a"), [{**_repositories("o/a")[0], "is_mirror": True}])]
        for response, repositories in cases:
            with self.subTest(marked=repositories[0]["is_mirror"]):
                self.setUp()
                self.stopped_run_keeps(response, repositories)

    def stopped_run_keeps(self, response: list, repositories: list) -> None:
        """A run over `repositories` with the model's `response`, stopped at GitHub's merges,
        then a run that pays the model nothing."""
        class Rejecting(_GitHub):
            def __init__(self):
                super().__init__({"o/a": (ENGLISH, "s1")},
                                 merges={"o/a": ["2026-07-21T00:46:14Z"]})
                self.rejecting = True

            def post(self, url, headers=None, json=None, timeout=None):
                if self.rejecting:
                    return _Response(401, {"message": "Bad credentials"})
                return super().post(url, headers, json, timeout)

            def get(self, url, headers=None, timeout=None):
                if self.rejecting and url == readmes.RATE_LIMIT_URL:
                    return _Response(401, {"message": "Bad credentials"})
                return super().get(url, headers, timeout)
        github = Rejecting()
        model = _Model(lambda request, number: response)
        code, err = self.run_over(repositories, github, model)
        self.assertEqual((code, len(model.requests)), (1, 1), err)
        self.assertIn("GitHub accepts none of the configured GitHub access tokens", err)
        github.rejecting = False
        model = _Model(lambda request, number: response)
        code, err = self.run_over(repositories, github, model)
        self.assertEqual((code, model.requests), (0, []), err)
        # The line that keeps the response is no decision of the earlier invocation.
        self.assertNotIn("Retrying it later", err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["mirror_merges"]["latest_merge"]),
                         ("true", "2026-07-21T00:46:14Z"))
        # The request of the stopped invocation counts among the run's requests.
        self.assertEqual(self.index()[0]["model_requests"], "1")

    def test_a_response_kept_for_a_repository_that_a_finished_run_left_undecided_is_kept(self):
        """The model's response makes o/a a mirror, and GitHub fails the merges of o/a through
        every retry round. The run finishes without o/a within the ceiling. The next run, over
        the next month's sampling frame, reads only GitHub's facts for the same README and
        description rather than paying the model again."""
        self.real_ceiling()

        class Failing(_GitHub):
            broken = True

            def post(self, url, headers=None, json=None, timeout=None):
                if self.broken:
                    return _Response(200, {"data": None, "errors": [
                        {"type": "SERVICE_UNAVAILABLE", "message": "Something went wrong"}]})
                return super().post(url, headers, json, timeout)

        github = Failing({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")},
                         merges={"o/a": ["2026-07-21T00:46:14Z"]})

        def answer(request, number):
            name = _repository_of(request)
            return _answer(name, facts={"mirror": {"described_as_mirror": "true"}}
                           if name == "o/a" else None)

        model = _Model(answer)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model)
        self.assertEqual((code, self.undecided()), (0, ["o/a"]), err)
        self.assertEqual(sorted(_repository_of(r) for r in model.requests), ["o/a", "o/b"])
        github.broken = False
        model = _Model(answer)
        code, err = self.run_over(_repositories("o/a", "o/b"), github, model,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual((code, model.requests), (0, []), err)
        second = self.index()[-1]["run_id"]
        decision = next(d for rid, d in label_cache.all_decisions(self.cache)
                        if rid == second and d["id"] == 1)
        self.assertEqual((decision["label"], decision["mirror_merges"]["latest_merge"]),
                         ("true", "2026-07-21T00:46:14Z"))

    def test_a_kept_response_restored_under_another_purpose_check_is_checked_again(self):
        """A run under an earlier purpose check refused the quotation of o/a and kept the model's
        response when GitHub failed for the merges through every retry round. The next
        run restores the response and checks the quotation again under the current purpose
        check, as it checks a decision that it reuses (`Run.derive_again`)."""
        self.real_ceiling()

        class Failing(_GitHub):
            broken = True

            def post(self, url, headers=None, json=None, timeout=None):
                if self.broken:
                    return _Response(200, {"data": None, "errors": [
                        {"type": "SERVICE_UNAVAILABLE", "message": "Something went wrong"}]})
                return super().post(url, headers, json, timeout)

        github = Failing({"o/a": (ENGLISH, "s1")}, merges={"o/a": ["2026-07-21T00:46:14Z"]})
        answer = _answer("o/a", facts={"mirror": {"described_as_mirror": "true"}})
        with mock.patch.object(classify_repos, "quote_check", lambda: "an earlier check"), \
                mock.patch.object(classify_repos, "quoted", lambda quote, *sources: False):
            code, err = self.run_over(_repositories("o/a"), github,
                                      _Model(lambda request, number: answer))
        self.assertEqual((code, self.undecided()), (0, ["o/a"]), err)
        github.broken = False
        model = _Model(lambda request, number: answer)
        code, err = self.run_over(_repositories("o/a"), github, model,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual((code, model.requests), (0, []), err)
        decision = next(label_cache.all_decisions(self.cache))[1]
        self.assertEqual((decision["label"], decision["purpose_quote"],
                          decision["quote_check"]),
                         ("true", "A command line tool that formats configuration files.",
                          classify_repos.quote_check()))
        self.assertNotIn("purpose_quote_refused", decision)

    def test_without_a_github_access_token_every_worker_stops(self):
        """When GitHub refuses the last GitHub access token, no repository can be read. The run
        stops rather than failing each remaining repository on its own."""
        repositories = _repositories(*(f"o/r{i}" for i in range(6)))
        github = _GitHub({})
        github.get = lambda url, headers=None, timeout=None: _Response(401, {})
        code, err = self.run_over(repositories, github, _Model(_always_true), workers=3)
        self.assertEqual(code, 1)
        self.assertEqual(err.count("GitHub accepts none of the configured GitHub access tokens"), 1)
        self.assertEqual(self.index()[0]["outcome"], "incomplete")

    def test_a_spell_in_which_github_rejects_every_github_access_token_does_not_stop_the_run(
            self):
        """GitHub can reject valid GitHub access tokens for a while, also for their rate limit.
        The run checks the GitHub access tokens again after each wait and continues once GitHub
        accepts them. Otherwise, a spell of HTTP 401 would end a run that takes days."""
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        answer = github.get
        spell = {"left": 4}

        def get(url, headers=None, timeout=None):
            if spell["left"]:
                spell["left"] -= 1
                return _Response(401, {"message": "Bad credentials"})
            if url == readmes.RATE_LIMIT_URL:
                return _Response(200, {})
            return answer(url, headers, timeout)
        github.get = get
        model = _Model(_always_true)
        code, err = self.run_over(repositories, github, model)
        self.assertEqual(code, 0, err)
        self.assertIn("GitHub rejects every GitHub access token (HTTP 401). Checking them "
                      "again in 5 minutes", err)
        self.assertEqual(sorted(_repository_of(r) for r in model.requests), ["o/a", "o/b"])
        self.assertEqual((self.index()[0]["outcome"], self.index()[0]["labeled"]),
                         ("finished", "2"))

    def test_a_check_of_the_github_access_tokens_without_a_verdict_starts_the_run(self):
        """Only HTTP 401 rejects a GitHub access token. HTTP 503 or a failed connection as the
        run starts says nothing about the GitHub access tokens. A run that dropped each GitHub
        access token for such a response would end before its first request."""
        frame = self.tmp / "ghs_filtered.json"
        frame.write_text(json.dumps({
            "_metadata": {"input_source": {"github": True, "retrieval_id": "2026-10-05T101430Z",
                                           "query": {"contributorsMin": 2},
                                           "window": {"committed_min": "2026-03-22"}},
                          "stages": [{"key": "activity", "values": {"commits_min": 355}}]},
            "items": [{"id": 1, "name": "o/a", "description": "", "isMirror": False}]}))
        checks = iter([_Response(503, {}), _Response(200, {})])
        started = []

        async def classify(args, repositories, retrieval_id, thresholds, github, client,
                           key_credit=None, second_client=None):
            started.append([token.value for token in github.pool.tokens])
            return 0
        environment = {"GITHUB_TOKENS": "t1", "GITHUB_TOKEN": "", "OPENAI_API_KEY": "sk-k",
                       "AGENTCONTEXT_CREDENTIALS": str(self.tmp / "none.csv")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(readmes.requests, "get", lambda url, **kwargs: next(checks)), \
                mock.patch.object(readmes, "CHECK_WAITS", (0,)), \
                mock.patch.object(classify_repos, "classify", classify), \
                contextlib.redirect_stderr(err):
            code = classify_repos.main(["--sampling-frame", str(frame), "--cache-dir",
                                        str(self.cache), "--credentials",
                                        str(self.tmp / "none.csv"), "--limit", "1",
                                        "--model", "gpt-6-luna", "--summary-model",
                                        "gpt-6-luna", "--no-second-model"])
        self.assertEqual((code, started), (0, [["t1"]]), err.getvalue())
        self.assertRegex(err.getvalue(),
                         r"GitHub gave no verdict on GitHub access token 1 of 1 \([^)]*HTTP 503\)")

    def start_with_kimi(self, provider: str, get, second: tuple = ("--no-second-model",),
                        sends: bool = True) -> tuple[int, list, str]:
        """main() over a sampling frame of one repository with Kimi K3 on the endpoint
        `provider` in mxfp4 as the classifying model and the summary model, where `get` returns
        the bodies of the responses to GET requests to OpenRouter's API, without the second model
        unless `second` gives other options. The stand-in for `classify` refuses as `classify`
        does before its first request (`requests_unserved`), and without `sends` it completes a
        finished run without a request and records why OpenRouter serves no request
        (`args.unserved`). The result is the exit code, whether the run started, and the
        log."""
        frame = self.tmp / "ghs_filtered.json"
        frame.write_text(json.dumps({
            "_metadata": {"input_source": {"github": True, "retrieval_id": "2026-10-05T101430Z",
                                           "query": {"contributorsMin": 2},
                                           "window": {"committed_min": "2026-03-22"}},
                          "stages": [{"key": "activity", "values": {"commits_min": 355}}]},
            "items": [{"id": 1, "name": "o/a", "description": "", "isMirror": False}]}))
        started = []

        async def classify(args, repositories, retrieval_id, thresholds, github, client,
                           key_credit=None, second_client=None):
            if not sends:
                started.append(args.unserved)
                return 0
            if classify_repos.requests_unserved(args):
                return 1
            started.append(args.model)
            self.without_second_model = getattr(args, "second_model_unavailable", "")
            return 0
        environment = {"GITHUB_TOKENS": "t1", "GITHUB_TOKEN": "",
                       "OPENROUTER_API_KEY": "sk-or-v1-test",
                       "AGENTCONTEXT_CREDENTIALS": str(self.tmp / "none.csv")}
        endpoint = ["moonshotai/kimi-k3", "--provider", provider, "--quantization", "mxfp4",
                    "--hugging-face-id", "moonshotai/Kimi-K3"]
        err = io.StringIO()
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(readmes.requests, "get",
                                  lambda url, **kwargs: _Response(200, {})), \
                mock.patch.object(classify_repos.endpoints, "_get", get), \
                mock.patch.object(classify_repos, "classify", classify), \
                contextlib.redirect_stderr(err):
            code = classify_repos.main(["--sampling-frame", str(frame), "--cache-dir",
                                        str(self.cache), "--credentials",
                                        str(self.tmp / "none.csv"), "--limit", "1",
                                        "--model", *endpoint,
                                        "--summary-model", endpoint[0],
                                        "--summary-provider", provider,
                                        "--summary-quantization", "mxfp4",
                                        "--summary-hugging-face-id", "moonshotai/Kimi-K3",
                                        *second])
        return code, started, err.getvalue()

    def test_an_unserved_endpoint_leaves_the_refusal_to_the_first_request(self):
        """main gives the reason why OpenRouter serves no request of the classifying model to
        the labeling rather than exiting 1, and an invocation that completes a finished run
        from its labels, which sends no request, exits 0. With Chutes' mxfp4 endpoint of Kimi
        K3, which lacks "tools", the labeling starts with the reason."""
        code, started, err = self.start_with_kimi(
            "chutes/mxfp4", lambda url, headers: _openrouter_lists(url), sends=False)
        self.assertEqual(code, 0, err)
        [reason] = started
        self.assertIn("The endpoint chutes/mxfp4 of moonshotai/kimi-k3 lacks tools among its "
                      "supported parameters", reason)
        self.assertNotIn("ERROR", err)

    def test_a_pinned_endpoint_without_a_parameter_of_the_requests_stops_the_run_at_its_start(self):
        """OpenRouter's endpoint list gave Chutes' mxfp4 endpoint of Kimi K3 without "tools",
        which the requests of the classifying model send. With `require_parameters`, OpenRouter
        responds to each request with HTTP 404 "No endpoints found", which the run would wait
        out as a failure of the service."""
        requested = []

        def get(url, headers):
            requested.append(url)
            return _openrouter_lists(url)
        code, started, err = self.start_with_kimi("chutes/mxfp4", get)
        self.assertEqual((code, started), (1, []), err)
        self.assertIn("ERROR repository_labeling: The endpoint chutes/mxfp4 of moonshotai/kimi-k3 "
                      "lacks tools among its supported parameters. The endpoints in mxfp4 with "
                      "reasoning, tools, seed, max_tokens are wafer/us. OpenRouter routes no "
                      "request of the classifying model to the endpoint chutes/mxfp4 and "
                      "responds to each with HTTP 404. Pin an endpoint that serves them with "
                      "--provider and --quantization, or name another effort with "
                      "--reasoning-effort", err)
        self.assertEqual(requested,
                         ["https://openrouter.ai/api/v1/models/moonshotai/kimi-k3/endpoints"])

    def test_the_endpoint_has_to_generate_the_output_limit_of_the_attempts_after_a_failure(self):
        """The attempts at the higher temperature send their own, larger output limit. An endpoint
        that generates more tokens than the limit of the other requests but fewer than that of
        those attempts serves none of them, and the run stops at its start."""
        settings = classify_repos.endpoints.Settings("high", 100, 0.6, 0.95, 7, 1.0, 400)
        found = classify_repos.endpoints.PRODUCTION
        models = {"data": [{"id": found.model, "reasoning": {"supported_efforts": ["high"]}}]}
        body = {"data": {"id": found.model, "endpoints": [
            {"tag": found.provider, "quantization": found.quantization,
             "max_completion_tokens": 200,
             "supported_parameters": ["reasoning", "tools", "temperature", "top_p", "seed",
                                      "max_tokens"]}]}}

        def get(url, headers):
            return _openrouter_lists(url, {found.model: body}, models)
        refused, _, _ = classify_repos.endpoint_check(found, settings, found, "high", None, get)
        self.assertIn(f"The endpoint {found.provider} of {found.model} generates at most 200 "
                      f"tokens (max_completion_tokens), fewer than the output limit of 400 "
                      f"tokens of the requests", refused)
        body["data"]["endpoints"][0]["max_completion_tokens"] = 400
        refused, _, _ = classify_repos.endpoint_check(found, settings, found, "high", None, get)
        self.assertIsNone(refused)

    def test_an_endpoint_without_the_output_limit_stops_the_run_and_a_second_model_is_left_out(
            self):
        """The classifying requests send an output limit, which the endpoint list names
        `max_tokens`. With `require_parameters`, an endpoint without it among its supported
        parameters serves none of them, and neither does an endpoint that generates fewer tokens
        than the limit. The run then stops at its start. A second model on such an endpoint
        labels nothing in the invocation, which starts under the same classification
        parameters, because the second model is none of them."""
        without = json.loads(json.dumps(KIMI_ENDPOINTS))
        for entry in without["data"]["endpoints"]:
            entry["supported_parameters"].remove("max_tokens")
        code, started, err = self.start_with_kimi(
            "wafer/us", lambda url, headers: _openrouter_lists(url, {"moonshotai/kimi-k3":
                                                                     without}))
        self.assertEqual((code, started), (1, []), err)
        self.assertIn("The endpoint wafer/us of moonshotai/kimi-k3 lacks max_tokens among its "
                      "supported parameters", err)
        short = json.loads(json.dumps(KIMI_ENDPOINTS))
        short["data"]["endpoints"][1]["max_completion_tokens"] = 131_072
        code, started, err = self.start_with_kimi(
            "wafer/us", lambda url, headers: _openrouter_lists(url, {"moonshotai/kimi-k3":
                                                                     short}))
        self.assertEqual((code, started), (1, []), err)
        self.assertIn("The endpoint wafer/us of moonshotai/kimi-k3 generates at most 131,072 "
                      "tokens (max_completion_tokens), fewer than the output limit of 262,144 "
                      "tokens of the requests", err)
        glm = {"data": {"id": "z-ai/glm-5.3", "endpoints": [
            {"tag": "morph/fp8", "quantization": "fp8",
             "supported_parameters": ["reasoning", "tools", "temperature", "top_p", "seed"]}]}}

        def get(url, headers):
            return _openrouter_lists(url, {"z-ai/glm-5.3": glm})
        code, started, err = self.start_with_kimi("wafer/us", get, second=())
        self.assertEqual((code, started), (0, ["moonshotai/kimi-k3"]), err)
        self.assertTrue(self.without_second_model.startswith(
            "The endpoint morph/fp8 of z-ai/glm-5.3 lacks max_tokens"), self.without_second_model)
        self.assertIn("WARNING repository_labeling: The second model z-ai/glm-5.3 "
                      "(zai-org/GLM-5.3) on the endpoint morph/fp8 in fp8 through OpenRouter "
                      "labels nothing in this invocation. The endpoint morph/fp8 of "
                      "z-ai/glm-5.3 lacks max_tokens among its supported parameters. No endpoint "
                      "in fp8 has reasoning, tools, temperature, top_p, seed, max_tokens. "
                      "OpenRouter routes no request of the second model to the endpoint "
                      "morph/fp8 and responds to each with HTTP 404. The run reuses its earlier "
                      "labels, and the repositories that the model leaves without a decision "
                      "after the retry rounds and a failed response stay without one, for the "
                      "next run", err)
        glm["data"]["endpoints"][0]["supported_parameters"].append("max_tokens")
        code, started, err = self.start_with_kimi("wafer/us", get, second=())
        self.assertEqual((code, started, self.without_second_model),
                         (0, ["moonshotai/kimi-k3"], ""), err)
        self.assertNotIn("labels nothing in this invocation", err)

    def test_a_reasoning_effort_that_the_model_list_does_not_give_the_model_stops_the_run(self):
        """OpenRouter's model list gives each model the reasoning efforts that it takes, and the
        classification parameters of a run with another effort would name an effort that the
        model does not take. The run then stops at its start, and it starts with an effort that
        the list gives. The second model labels at its own effort, and a list that does not give
        it that effort leaves the second model out of the invocation."""
        code, started, err = self.start_with_kimi(
            "wafer/us", lambda url, headers: _openrouter_lists(url),
            second=("--no-second-model", "--reasoning-effort", "medium"))
        self.assertEqual((code, started), (1, []), err)
        self.assertIn("ERROR repository_labeling: OpenRouter's model list gives "
                      "moonshotai/kimi-k3 the reasoning efforts high, low, not medium, and the "
                      "classification parameters would name an effort that the model does not "
                      "take. Pin an endpoint that serves them with --provider and "
                      "--quantization, or name another effort with --reasoning-effort", err)
        code, started, err = self.start_with_kimi("wafer/us",
                                                  lambda url, headers: _openrouter_lists(url))
        self.assertEqual((code, started), (0, ["moonshotai/kimi-k3"]), err)
        models = json.loads(json.dumps(OPENROUTER_MODELS))
        models["data"][1]["reasoning"]["supported_efforts"] = ["max", "low"]
        code, started, err = self.start_with_kimi(
            "wafer/us", lambda url, headers: _openrouter_lists(url, models=models), second=())
        self.assertEqual((code, started), (0, ["moonshotai/kimi-k3"]), err)
        self.assertTrue(self.without_second_model)
        self.assertIn("labels nothing in this invocation. OpenRouter's model list gives "
                      "z-ai/glm-5.3 the reasoning efforts max, low, not high", err)

    def test_an_openai_run_without_an_openrouter_key_labels_without_the_second_model(self):
        """A run of an OpenAI model with an OpenAI summary model needs no OpenRouter API key but
        for the second model. Without one, the invocation starts without the second model and
        logs a WARNING, instead of exiting 1. The comparison with the previous run that follows
        gets the reason, so that it sends the second model no request either."""
        frame = self.tmp / "ghs_filtered.json"
        frame.write_text(json.dumps({
            "_metadata": {"input_source": {"github": True, "retrieval_id": "2026-10-05T101430Z",
                                           "query": {"contributorsMin": 2},
                                           "window": {"committed_min": "2026-03-22"}},
                          "stages": [{"key": "activity", "values": {"commits_min": 355}}]},
            "items": [{"id": 1, "name": "o/a", "description": "", "isMirror": False}]}))
        started, compared = [], []

        async def classify(args, repositories, retrieval_id, thresholds, github, client,
                           key_credit=None, second_client=None):
            started.append((args.model, getattr(args, "second_model_unavailable", ""),
                            key_credit))
            return 0

        def compare_with_previous_runs(*args, **options):
            compared.append(options["second_model_unavailable"])
            return 0
        environment = {"GITHUB_TOKENS": "t1", "GITHUB_TOKEN": "", "OPENROUTER_API_KEY": "",
                       "OPENAI_API_KEY": "sk-test",
                       "AGENTCONTEXT_CREDENTIALS": str(self.tmp / "none.csv")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(readmes.requests, "get",
                                  lambda url, **kwargs: _Response(200, {})), \
                mock.patch.object(classify_repos.endpoints, "_get",
                                  lambda url, headers: _openrouter_lists(url)), \
                mock.patch.object(classify_repos, "classify", classify), \
                mock.patch.object(classify_repos, "compare_with_previous_runs",
                                  compare_with_previous_runs), \
                contextlib.redirect_stderr(err):
            code = classify_repos.main(["--sampling-frame", str(frame), "--cache-dir",
                                        str(self.cache), "--output-dir", str(self.out),
                                        "--credentials", str(self.tmp / "none.csv"),
                                        "--model", "gpt-6-luna", "--summary-model",
                                        "gpt-6-luna"])
        self.assertEqual(code, 0, err.getvalue())
        [(model, unavailable, key_credit)] = started
        self.assertEqual((model, key_credit), ("gpt-6-luna", None))
        self.assertIn("OPENROUTER_API_KEY is unset", unavailable)
        self.assertEqual(compared, [unavailable])
        self.assertIn("WARNING repository_labeling: The second model z-ai/glm-5.3 "
                      "(zai-org/GLM-5.3) on the endpoint morph/fp8 in fp8 through OpenRouter "
                      "labels nothing in this invocation. OPENROUTER_API_KEY is unset",
                      err.getvalue())

    def a_frame_of_one(self, retrieval: str = "2026-10-05T101430Z") -> Path:
        """A sampling frame of the retrieval `retrieval` that names o/a."""
        frame = self.tmp / f"ghs_filtered-{retrieval}.json"
        frame.write_text(json.dumps({
            "_metadata": {"input_source": {"github": True, "retrieval_id": retrieval,
                                           "query": {"contributorsMin": 2},
                                           "window": {"committed_min": "2026-03-22"}},
                          "stages": [{"key": "activity", "values": {"commits_min": 355}}]},
            "items": [{"id": 1, "name": "o/a", "description": "", "isMirror": False}]}))
        return frame

    def labeling_through_main(self, frame: Path, model, steps: list, summary: tuple = (),
                              get=None, openrouter_key: str = "", github=None,
                              comparison: bool = False) -> tuple[int, str]:
        """main() over o/a and o/b, of the retrieval of `frame`, with `github` as GitHub, by
        default a fake that serves both READMEs, `model` as the classifying model, and a
        comparison that records its call in `steps`, as the labeling records its own. With
        `comparison`, the comparison of `compare_with_previous_runs` follows the record, and
        otherwise none. `summary` names the summary model, GPT-6 Luna by default, `get` returns
        the bodies of the responses to GET requests to OpenRouter's API, and `openrouter_key` is
        the OpenRouter API key. The result is the exit code and the log."""
        real_classify = classify_repos.classify
        real_comparison = classify_repos.compare_with_previous_runs
        repositories = _independent("o/a", "o/b")
        github = github or _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        bridge = _Bridge()

        async def classify(args, frame_repositories, retrieval_id, thresholds, client_of_main,
                           openai_client, key_credit=None, second_client=None):
            steps.append("label")
            pool = TokenPool([Credential("github", "a", "t")])
            return await real_classify(
                args, repositories, retrieval_id, THRESHOLDS,
                readmes.Client(pool, session=github, sleep=lambda s: None), model,
                bridge_for=lambda a: bridge)

        def compare_with_previous_runs(*args, **options):
            steps.append("compare")
            return real_comparison(*args, **options) if comparison else 0
        environment = {"GITHUB_TOKENS": "t1", "GITHUB_TOKEN": "",
                       "OPENROUTER_API_KEY": openrouter_key, "OPENAI_API_KEY": "sk-test",
                       "AGENTCONTEXT_CREDENTIALS": str(self.tmp / "none.csv")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(readmes.requests, "get",
                                  lambda url, **kwargs: _Response(200, {})), \
                mock.patch.object(classify_repos.endpoints, "_get",
                                  get or (lambda url, headers: _openrouter_lists(url))), \
                mock.patch.object(classify_repos, "classify", classify), \
                mock.patch.object(classify_repos, "compare_with_previous_runs",
                                  compare_with_previous_runs), \
                contextlib.redirect_stderr(err):
            code = classify_repos.main([
                "--sampling-frame", str(frame), "--cache-dir", str(self.cache),
                "--output-dir", str(self.out), "--credentials", str(self.tmp / "none.csv"),
                "--model", "gpt-6-luna", "--reasoning-effort", "low",
                *(summary or ("--summary-model", "gpt-6-luna", "--summary-effort", "low")),
                "--workers", "2", "--no-second-model", "--no-backup-provider"])
        return code, err.getvalue()

    def refuse_o_a(self, request, number):
        """The model's response to `request`, with o/a refused as malformed, as OpenAI refuses a
        README under its usage policy, so that the run leaves o/a without a decision."""
        if _repository_of(request) == "o/a":
            response = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})
            raise openai.BadRequestError("Invalid prompt: flagged", response=response,
                                         body={"code": "invalid_prompt", "message": "flagged"})
        return _answer(_repository_of(request))

    def test_a_completed_run_that_left_a_repository_undecided_is_followed_by_a_new_run(self):
        """A run over the sampling frame finished with a repository left without a decision,
        and the invocation ended before the outputs of the run were complete, as after a stop.
        The next invocation completes the run, and the runner records the labeling at its exit
        0, so that the selection and the cloning step follow without another invocation of the
        labeling. While no collection over the sample has begun, that invocation therefore
        starts the new run that tries the repository again, after the comparison of the
        completed run, and exits 0 with the repository labeled. Once a collection has begun,
        an invocation completes the run without it."""
        from agentcontext import resume
        steps = []
        for begun in (False, True):
            with self.subTest(collection_begun=begun):
                self.setUp()
                self.real_ceiling()
                frame = self.a_frame_of_one()
                steps.clear()
                code, err = self.labeling_through_main(frame, _Model(self.refuse_o_a), steps)
                self.assertEqual((code, steps), (0, ["label", "compare"]), err)
                # A stop after the index recorded the run as finished, before the outputs.
                (self.out / classify_repos.ANNOTATION_RECORD).unlink()
                if begun:
                    resume.save_collection(self.collection / resume.FINISHED_NAME,
                                           {"collection": "2026-10-06T090000Z-0123abcd",
                                            "input_sha256": "x", "taken_over": set()})
                steps.clear()
                model = _Model(_always_true)
                code, err = self.labeling_through_main(frame, model, steps)
                self.assertEqual(code, 0, err)
                if begun:
                    self.assertEqual(steps, ["label", "compare"])
                    self.assertEqual(model.requests, [])
                    self.assertEqual(len(self.index()), 1)
                    self.assertEqual([row["reason"] for row in self.rows("repos_excluded.csv")],
                                     ["undecided"])
                    continue
                self.assertEqual(steps, ["label", "compare", "label", "compare"])
                self.assertEqual([_repository_of(r) for r in model.requests], ["o/a"])
                self.assertEqual(len(self.index()), 2)
                self.assertEqual(self.rows("repos_excluded.csv"), [])
                self.assertIn("this invocation starts a new run, which tries them again", err)

    def test_the_labeling_skips_a_retry_while_openrouter_lists_the_endpoint_unserved(self):
        """While OpenRouter's lists show that the pinned endpoint of the summary model serves no
        request, as while its provider has taken the endpoint down, a new run exits 1 before its
        first request. An invocation that completes a finished run that left o/a without a
        decision, before a collection over the sample has begun, then skips the new run that
        would try o/a again, after the comparison, with a WARNING, and exits 0, so that the
        selection and the collection follow. So does every later invocation of the labeling while
        the lists show this. Once they show the endpoint again, an invocation tries o/a again in
        a new run. A labeling that has repositories to label for the first time, over the
        sampling frame of another retrieval, still exits 1 before its first request."""
        self.real_ceiling()
        listed = {DEEPSEEK["model"]: {"data": {"id": DEEPSEEK["model"], "endpoints": [
            {"tag": DEEPSEEK["provider"], "quantization": DEEPSEEK["quantization"],
             "max_completion_tokens": 393_216, "supported_parameters": ["reasoning"]}]}}}
        delisted = {DEEPSEEK["model"]: {"data": {"id": DEEPSEEK["model"], "endpoints": []}}}
        models = {"data": [{"id": DEEPSEEK["model"],
                            "reasoning": {"supported_efforts": ["high", "low"]}}]}
        lists = [listed]
        summary = ("--summary-model", DEEPSEEK["model"], "--summary-effort", "low")

        def invoke(frame: Path, model) -> tuple[int, str]:
            return self.labeling_through_main(
                frame, model, steps, summary,
                lambda url, headers: _openrouter_lists(url, lists[0], models), "sk-or-v1-test")
        frame = self.a_frame_of_one()
        steps = []
        code, err = invoke(frame, _Model(self.refuse_o_a))
        self.assertEqual((code, steps), (0, ["label", "compare"]), err)
        # A stop after the index recorded the run as finished, before the outputs.
        (self.out / classify_repos.ANNOTATION_RECORD).unlink()
        lists[0] = delisted
        reason = (f"OpenRouter lists no endpoint {DEEPSEEK['provider']} of {DEEPSEEK['model']}")
        # The second invocation completes the outputs, and the third one, as after a kill before
        # the runner recorded the exit of the second, finds the run compared and written.
        for invocation in ("outputs incomplete", "outputs complete"):
            with self.subTest(invocation=invocation):
                steps.clear()
                model = _Model(_always_true)
                code, err = invoke(frame, model)
                self.assertEqual((code, steps, model.requests), (0, ["label", "compare"], []),
                                 err)
                self.assertEqual(len(self.index()), 1)
                self.assertEqual([row["reason"] for row in self.rows("repos_excluded.csv")],
                                 ["undecided"])
                self.assertRegex(err, rf"WARNING repository_labeling: {re.escape(reason)}[^\n]*"
                                      rf"\. The invocation does not try again the 1 repository "
                                      rf"that the completed run left without a decision, so that "
                                      rf"the selection and the collection follow\. The next new "
                                      rf"run of the labeling, such as the run over the next "
                                      rf"monthly retrieval, tries it again\.")
                self.assertNotIn("ERROR", err)
                self.assertNotIn("this invocation starts a new run", err)
        lists[0] = listed
        steps.clear()
        model = _Model(_always_true)
        code, err = invoke(frame, model)
        self.assertEqual((code, steps), (0, ["label", "compare"]), err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/a"])
        self.assertEqual(len(self.index()), 2)
        self.assertEqual(self.rows("repos_excluded.csv"), [])
        lists[0] = delisted
        steps.clear()
        model = _Model(_always_true)
        code, err = invoke(self.a_frame_of_one("2026-11-02T101430Z"), model)
        self.assertEqual((code, steps, model.requests), (1, ["label"], []), err)
        self.assertIn(f"ERROR repository_labeling: {reason}", err)
        self.assertEqual(len(self.index()), 2)

    def test_label_changes_csv_keeps_each_comparison_of_the_invocation_before_a_retry(self):
        """The run over the second retrieval left o/a without a decision, because the API
        refused its changed README, and excluded o/b, whose README is now in Russian. Either a
        kill ended the invocation while its comparison with the run over the first retrieval
        wrote label_changes.csv, or the comparison finished and the record of the outputs was
        lost. The next invocation completes the run, compares it or writes its comparison from
        the cache of labels, and then tries o/a again in a new run, which excludes o/a, whose
        README is now in Russian too, and compares that run. label_changes.csv shows both
        comparisons, oldest first: the change of o/b, and then the change of o/a, which names as
        its previous run the run over the first retrieval, the latest that decided it. Written
        from the last comparison alone, the file would lose the change of o/b, and the changes of
        the retrieval with it."""
        written = classify_repos.write_csv

        def killed(path, columns, rows):
            if Path(path).name == "label_changes.csv":
                raise _Killed(path)
            written(path, columns, rows)
        for interrupted in ("comparison", "outputs"):
            with self.subTest(interrupted=interrupted):
                self.setUp()
                self.real_ceiling()
                github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
                steps = []
                code, err = self.labeling_through_main(self.a_frame_of_one(),
                                                       _Model(_always_true), steps,
                                                       github=github, comparison=True)
                self.assertEqual((code, steps), (0, ["label", "compare"]), err)
                github.readmes["o/a"] = (ENGLISH + " Now with tests.", "s3")
                github.readmes["o/b"] = (RUSSIAN, "s4")
                frame = self.a_frame_of_one("2026-11-05T101430Z")
                if interrupted == "comparison":
                    with mock.patch.object(classify_repos, "write_csv", killed), \
                            self.assertRaises(_Killed):
                        self.labeling_through_main(frame, _Model(self.refuse_o_a), steps,
                                                   github=github, comparison=True)
                    self.assertEqual(self.index()[1]["compared_with"], "")
                else:
                    code, err = self.labeling_through_main(frame, _Model(self.refuse_o_a), steps,
                                                           github=github, comparison=True)
                    self.assertEqual(code, 0, err)
                    self.assertNotEqual(self.index()[1]["compared_with"], "")
                    (self.out / classify_repos.ANNOTATION_RECORD).unlink()
                first, month = [row["run_id"] for row in self.index()]
                github.readmes["o/a"] = (RUSSIAN, "s5")
                steps.clear()
                code, err = self.labeling_through_main(frame, _Model(_always_true), steps,
                                                       github=github, comparison=True)
                self.assertEqual((code, steps), (0, ["label", "compare", "label", "compare"]),
                                 err)
                self.assertEqual([row["compared_with"] for row in self.index()],
                                 ["", first, month])
                self.assertEqual([(row["repo_name"], row["now"], row["previous_run"])
                                  for row in self.rows("label_changes.csv")],
                                 [("o/b", "excluded: readme_non_english", first),
                                  ("o/a", "excluded: readme_non_english", first)])

    def test_label_changes_csv_keeps_the_comparison_of_the_completed_run_in_a_later_invocation(
            self):
        """The run over the second retrieval left o/a without a decision, because the API
        refused its changed README, and excluded o/b, whose README is now in Russian, and an
        invocation compared it with the run over the first retrieval. A later invocation compares
        a new run that tries o/a again, which excludes o/a, whose README is now in Russian too.
        In one case, the invocation that compared the run over the second retrieval went on to
        the new run, and a kill ended it while the new run wrote its outputs. The next invocation
        completes and compares the new run, and the invocation after it, as after a new image
        before the collection, runs the labeling again and writes the comparison of the new run
        again from the cache of labels. In the other case, the invocation that compared the run
        over the second retrieval exited 0, and the labeling runs again, as after a new image
        before the collection, and tries o/a again in the new run. label_changes.csv shows the
        change of o/b before the change of o/a each time. Written from the comparisons of the
        later invocation alone, the file would lose the change of o/b."""
        written = classify_repos.write_csv

        def killed_in_comparison(path, columns, rows):
            if Path(path).name == "label_changes.csv":
                raise _Killed(path)
            written(path, columns, rows)
        for case in ("new run killed", "labeling again after exit 0"):
            with self.subTest(case=case):
                self.setUp()
                self.real_ceiling()
                github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
                steps = []
                code, err = self.labeling_through_main(self.a_frame_of_one(),
                                                       _Model(_always_true), steps,
                                                       github=github, comparison=True)
                self.assertEqual((code, steps), (0, ["label", "compare"]), err)
                github.readmes["o/a"] = (ENGLISH + " Now with tests.", "s3")
                github.readmes["o/b"] = (RUSSIAN, "s4")
                frame = self.a_frame_of_one("2026-11-05T101430Z")
                o_b = [("o/b", "excluded: readme_non_english")]
                both = [*o_b, ("o/a", "excluded: readme_non_english")]
                steps.clear()
                if case == "new run killed":
                    # A kill in the comparison, so that the next invocation completes the run
                    # and then starts the new run, after o/a's README changed.
                    with mock.patch.object(classify_repos, "write_csv", killed_in_comparison), \
                            self.assertRaises(_Killed):
                        self.labeling_through_main(frame, _Model(self.refuse_o_a), steps,
                                                   github=github, comparison=True)
                    github.readmes["o/a"] = (RUSSIAN, "s5")
                    annotations = []

                    def killed_in_new_run(path, columns, rows):
                        # The first write completes the outputs of the run over the second
                        # retrieval, and the second writes those of the new run.
                        if Path(path).name == "repos_annotated.csv":
                            annotations.append(path)
                            if len(annotations) == 2:
                                raise _Killed(path)
                        written(path, columns, rows)
                    steps.clear()
                    with mock.patch.object(classify_repos, "write_csv", killed_in_new_run), \
                            self.assertRaises(_Killed):
                        self.labeling_through_main(frame, _Model(_always_true), steps,
                                                   github=github, comparison=True)
                    self.assertEqual(steps, ["label", "compare", "label"])
                else:
                    code, err = self.labeling_through_main(frame, _Model(self.refuse_o_a), steps,
                                                           github=github, comparison=True)
                    self.assertEqual((code, steps), (0, ["label", "compare"]), err)
                    github.readmes["o/a"] = (RUSSIAN, "s5")
                self.assertEqual([(row["repo_name"], row["now"])
                                  for row in self.rows("label_changes.csv")], o_b)
                steps.clear()
                code, err = self.labeling_through_main(frame, _Model(_always_true), steps,
                                                       github=github, comparison=True)
                self.assertEqual((code, steps), (0, ["label", "compare"]), err)
                first, month, _ = [row["run_id"] for row in self.index()]
                self.assertEqual([row["compared_with"] for row in self.index()],
                                 ["", first, month])
                self.assertEqual([(row["repo_name"], row["now"])
                                  for row in self.rows("label_changes.csv")], both)
                if case == "new run killed":
                    steps.clear()
                    model = _Model(_always_true)
                    code, err = self.labeling_through_main(frame, model, steps, github=github,
                                                           comparison=True)
                    self.assertEqual((code, steps, model.requests),
                                     (0, ["label", "compare"], []), err)
                    self.assertEqual(len(self.index()), 3)
                    self.assertEqual([(row["repo_name"], row["now"])
                                      for row in self.rows("label_changes.csv")], both)

    def test_label_changes_csv_leaves_out_the_comparisons_of_an_earlier_retrieval(self):
        """Three monthly runs over three retrievals: the second excludes o/b, whose README is now
        in Russian, and the third excludes o/a, whose README is now in Russian too. The third
        run is compared with the second, which was compared with the first, and
        label_changes.csv of the third invocation shows only the change of o/a, because the
        second run is over another retrieval."""
        self.real_ceiling()
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        steps = []
        for retrieval, changed in (("2026-10-05T101430Z", ""), ("2026-11-05T101430Z", "o/b"),
                                   ("2026-12-05T101430Z", "o/a")):
            if changed:
                github.readmes[changed] = (RUSSIAN, f"{changed}-{retrieval}")
            steps.clear()
            code, err = self.labeling_through_main(self.a_frame_of_one(retrieval),
                                                   _Model(_always_true), steps, github=github,
                                                   comparison=True)
            self.assertEqual((code, steps), (0, ["label", "compare"]), err)
        first, second, _ = [row["run_id"] for row in self.index()]
        self.assertEqual([row["compared_with"] for row in self.index()], ["", first, second])
        self.assertEqual([(row["repo_name"], row["now"], row["previous_run"])
                          for row in self.rows("label_changes.csv")],
                         [("o/a", "excluded: readme_non_english", second)])

    def test_a_pinned_endpoint_that_serves_the_requests_starts_the_run(self):
        requested = []

        def get(url, headers):
            requested.append(url)
            return _openrouter_lists(url)
        code, started, err = self.start_with_kimi("wafer/us", get)
        self.assertEqual((code, started), (0, ["moonshotai/kimi-k3"]), err)
        self.assertNotIn("endpoint list", err)
        # The model list is read once, for the classifying model and the summary model.
        self.assertEqual(requested,
                         ["https://openrouter.ai/api/v1/models/moonshotai/kimi-k3/endpoints",
                          "https://openrouter.ai/api/v1/models",
                          "https://openrouter.ai/api/v1/models/moonshotai/kimi-k3/endpoints"])

    def start_with_deepseek(self, endpoints_listed: tuple, *options: str) -> tuple:
        """main() over a sampling frame of one repository with DeepSeek V4.1 Flash as the
        classifying model and the summary model, without the second model, where OpenRouter's
        endpoint list of the model names the fp8 endpoints `endpoints_listed`, each with every
        parameter of the requests. The stand-in for `classify` refuses as `classify` does before
        its first request (`requests_unserved`). The result is the exit code, the options of the
        run that started, or None, and the log."""
        frame = self.tmp / "ghs_filtered.json"
        frame.write_text(json.dumps({
            "_metadata": {"input_source": {"github": True, "retrieval_id": "2026-10-05T101430Z",
                                           "query": {"contributorsMin": 2},
                                           "window": {"committed_min": "2026-03-22"}},
                          "stages": [{"key": "activity", "values": {"commits_min": 355}}]},
            "items": [{"id": 1, "name": "o/a", "description": "", "isMirror": False}]}))
        listed = {DEEPSEEK["model"]: {"data": {"id": DEEPSEEK["model"], "endpoints": [
            {"tag": tag, "quantization": "fp8", "max_completion_tokens": 393_216,
             "supported_parameters": ["max_tokens", "reasoning", "seed", "temperature", "tools",
                                      "top_p"]} for tag in endpoints_listed]}}}
        models = {"data": [{"id": DEEPSEEK["model"],
                            "reasoning": {"supported_efforts": ["max", "high", "low", "none"]}}]}
        started = []

        async def classify(args, repositories, retrieval_id, thresholds, github, client,
                           key_credit=None, second_client=None):
            if classify_repos.requests_unserved(args):
                return 1
            started.append(args)
            return 0
        environment = {"GITHUB_TOKENS": "t1", "GITHUB_TOKEN": "",
                       "OPENROUTER_API_KEY": "sk-or-v1-test",
                       "AGENTCONTEXT_CREDENTIALS": str(self.tmp / "none.csv")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(readmes.requests, "get",
                                  lambda url, **kwargs: _Response(200, {})), \
                mock.patch.object(classify_repos.endpoints, "_get",
                                  lambda url, headers: _openrouter_lists(url, listed, models)), \
                mock.patch.object(classify_repos, "classify", classify), \
                contextlib.redirect_stderr(err):
            code = classify_repos.main(["--sampling-frame", str(frame), "--cache-dir",
                                        str(self.cache), "--credentials",
                                        str(self.tmp / "none.csv"), "--limit", "1",
                                        "--no-second-model", *options])
        return code, (started[0] if started else None), err.getvalue()

    def test_a_pinned_endpoint_missing_from_the_endpoint_list_starts_the_run_on_the_backup(self):
        """OpenRouter's endpoint list of DeepSeek V4.1 Flash names deepinfra/fp8, its backup
        provider, and not morph/fp8, its pinned endpoint, as while a provider takes its endpoint
        down. The run starts with both roles of the model on the backup provider, with a
        WARNING, rather than stopping at its start. With --no-backup-provider it stops."""
        code, args, err = self.start_with_deepseek(("deepinfra/fp8", "novita/fp8"))
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(args)
        self.assertEqual(sorted(args.started_on_backups), ["classifying model", "summary model"])
        self.assertIn("OpenRouter lists no endpoint morph/fp8 of deepseek/deepseek-v4.1-flash",
                      args.started_on_backups["classifying model"])
        self.assertIn("WARNING repository_labeling: OpenRouter lists no endpoint morph/fp8 of "
                      "deepseek/deepseek-v4.1-flash. The endpoints in fp8 with reasoning are "
                      "deepinfra/fp8, novita/fp8. The first summary that the pinned provider "
                      "morph/fp8 of the summary model fails goes to its backup provider "
                      "deepinfra/fp8 in fp8", err)
        self.assertNotIn("ERROR", err)
        code, args, err = self.start_with_deepseek(("deepinfra/fp8",), "--no-backup-provider")
        self.assertEqual((code, args), (1, None), err)
        self.assertIn("ERROR repository_labeling: OpenRouter lists no endpoint morph/fp8 of "
                      "deepseek/deepseek-v4.1-flash", err)

    def test_a_pinned_endpoint_missing_from_the_endpoint_list_stops_the_run_without_a_backup(
            self):
        """When the endpoint list names neither the pinned endpoint nor the backup provider, no
        provider of the weights serves the requests, and the run stops at its start."""
        code, args, err = self.start_with_deepseek(("novita/fp8",))
        self.assertEqual((code, args), (1, None), err)
        self.assertIn("ERROR repository_labeling: OpenRouter lists no endpoint morph/fp8 of "
                      "deepseek/deepseek-v4.1-flash", err)

    def test_an_endpoint_list_that_cannot_be_read_leaves_the_test_to_the_first_request(self):
        def get(url, headers):
            raise OSError("timed out")
        code, started, err = self.start_with_kimi("chutes/mxfp4", get)
        self.assertEqual((code, started), (0, ["moonshotai/kimi-k3"]), err)
        self.assertIn("WARNING repository_labeling: Could not read OpenRouter's endpoint list "
                      "of moonshotai/kimi-k3 (timed out). The first request tests the endpoint "
                      "chutes/mxfp4 instead", err)
        self.assertIn("WARNING repository_labeling: Could not read OpenRouter's model list "
                      "(timed out). The run sends the classifying model the reasoning effort "
                      "high without checking that the model takes it", err)

    def test_a_github_response_that_is_not_json_fails_the_repository_alone(self):
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/b": (ENGLISH, "s2")})
        original = github.get

        def get(url, headers=None, timeout=None):
            if "/o/a/" in url:
                response = _Response(200, None)
                response.json = lambda: (_ for _ in ()).throw(ValueError("not JSON"))
                return response
            return original(url, headers, timeout)
        github.get = get
        code, err = self.run_over(repositories, github, _Model(_always_true))
        self.assertEqual(code, 1)
        self.assertIn("Could not decide o/a (not JSON). Retrying it later", err)
        self.assertEqual(read_partial_ids(self.cache), {2})

    def test_a_finished_run_records_every_repositorys_outcome_and_the_run_that_decided_it(self):
        repositories, github = self.sampling_frame()
        self.run_over(repositories, github, _Model(_always_true))
        github.readmes["o/a"] = (ENGLISH + " Now with tests.", "s3")
        code, err = self.run_over(repositories, github, _Model(_always_true), new_run=True)
        self.assertEqual(code, 0, err)
        first, second = self.index()
        self.assertEqual((first["scope"], second["scope"]), ("sampling_frame", "sampling_frame"))
        rows = label_cache.read_labels(self.cache, second["run_id"], second["labels_sha256"])
        self.assertEqual([(r["repo_name"], r["outcome"], r["label"], r["reason"], r["decided_in"])
                          for r in rows],
                         [("o/a", "labeled", "true", "", second["run_id"]),
                          ("o/b", "excluded", "", "empty_readme", first["run_id"]),
                          ("o/c", "gone", "", "", ""),
                          ("o/d", "excluded", "", "no_readme_found", first["run_id"])])

    def test_a_stopped_run_is_resumed_only_by_a_run_over_the_same_repositories(self):
        repositories = _repositories("o/a", "o/b")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        failing = _Model(lambda request, number: [_message("not json")])
        code, err = self.run_over(repositories[:1], github, failing, limit=1, output_dir=None)
        self.assertEqual(code, 1)
        code, err = self.run_over(repositories, github, _Model(_always_true))
        self.assertEqual(code, 0, err)
        self.assertEqual([(r["scope"], r["outcome"]) for r in self.index()],
                         [("limit", "incomplete"), ("sampling_frame", "finished")])

    def test_a_stopped_run_over_a_list_is_resumed_only_by_a_run_over_that_list(self):
        """Two lists of repositories have one scope. A run over the second list, such as a check
        sample, that took up a stopped run over the first list, such as an evaluation sample,
        would contain the decisions of both lists and the labels of the second list alone."""
        repositories = _repositories("o/a", "o/b", "o/c")
        github = _GitHub({n: (ENGLISH, n) for n in ("o/a", "o/b", "o/c")})
        failing_b = _Model(lambda request, number: (
            [_message("not json")] if _repository_of(request) == "o/b"
            else _answer(_repository_of(request))))
        listed = self.tmp / "list.csv"
        code, err = self.run_over(repositories[:2], github, failing_b, repository_list=listed,
                                  output_dir=None)
        self.assertEqual(code, 1)
        code, err = self.run_over(repositories[2:], github, _Model(_always_true),
                                  repository_list=listed, output_dir=None)
        self.assertEqual(code, 0, err)
        self.assertEqual([(r["outcome"], r["repositories"]) for r in self.index()],
                         [("incomplete", "2"), ("finished", "1")])
        model = _Model(_always_true)
        code, err = self.run_over(repositories[:2], github, model, repository_list=listed,
                                  output_dir=None)
        self.assertEqual(code, 0, err)
        self.assertEqual([_repository_of(r) for r in model.requests], ["o/b"])
        self.assertEqual([(r["outcome"], r["repositories"]) for r in self.index()],
                         [("finished", "2"), ("finished", "1")])

    def test_a_run_over_part_of_the_sampling_frame_writes_no_outputs(self):
        """`--limit 0` is a run over part of the sampling frame too. Read as no limit,
        `--limit 0` would replace the outputs of the whole sampling frame with empty outputs."""
        repositories, github = self.sampling_frame()
        for limit in (1, 0):
            with self.subTest(limit=limit):
                self.setUp()
                with mock.patch.object(classify_repos, "write_outputs") as write:
                    code, err = self.run_over(repositories[:limit], github, _Model(_always_true),
                                              limit=limit, output_dir=None)
                self.assertEqual(code, 0, err)
                write.assert_not_called()
                self.assertEqual([(row["scope"], row["outcome"]) for row in self.index()],
                                 [("limit", "finished")])

    def labeling(self, repositories, github, client, compare, **options):
        """A run over the sampling frame followed by the comparison with the previous run, as
        `main` runs them, with `compare` in place of `changed_labels.compare`."""
        code, err = self.run_over(repositories, github, client, **options)
        if code == 0:
            with contextlib.redirect_stderr(io.StringIO()) as compared:
                code = classify_repos.compare_with_previous_runs(self.cache, self.out, 2, None,
                                                                 compare=compare)
            err += compared.getvalue()
        return code, err

    def a_run_left_uncompared(self, kill_on=None):
        """Two runs over two sampling frames. The second sampling frame changed the README of
        o/a, and the second run left o/e without a decision. The comparison of the second run
        exits 1, as when the API refuses its repeated labels for a spent quota. With `kill_on`,
        the second invocation instead ends while it writes the output of that name. Returns the
        repositories, GitHub, the runs that were compared, in order, and the comparison, which
        succeeds from then on."""
        self.real_ceiling()
        repositories = _repositories("o/a", "o/b", "o/c", "o/d", "o/e")
        github = _GitHub({"o/a": (ENGLISH, "s1"), "o/b": (" \n", "s2"), "o/e": (ENGLISH, "s5")},
                         {"o/d": {"id": 4, "full_name": "o/d"}})

        def fails_for_e(request, number):
            return [] if _repository_of(request) == "o/e" else _answer(_repository_of(request))
        compared, failing = [], {"now": False}

        def compare(args):
            """The comparison's effect on the index: it names the previous run, if any."""
            compared.append(args.run)
            previous = label_cache.previous_run(self.cache, args.run)
            if previous is None:
                return 0
            if failing["now"]:
                return 1
            label_cache.put_index_row({"run_id": args.run, "compared_with": previous["run_id"]},
                                      self.cache)
            return 0
        code, err = self.labeling(repositories, github, _Model(fails_for_e), compare)
        self.assertEqual(code, 0, err)
        github.readmes["o/a"] = (ENGLISH + " Now with tests.", "s3")
        failing["now"] = kill_on is None
        if kill_on is None:
            code, err = self.labeling(repositories, github, _Model(fails_for_e), compare,
                                      retrieval="2026-11-05T101430Z")
            self.assertEqual(code, 1, err)
        else:
            written = classify_repos.write_csv

            def killed(path, columns, rows):
                if Path(path).name == kill_on:
                    raise _Killed(path)
                written(path, columns, rows)
            with mock.patch.object(classify_repos, "write_csv", killed), \
                    self.assertRaises(_Killed):
                self.labeling(repositories, github, _Model(fails_for_e), compare,
                              retrieval="2026-11-05T101430Z")
        failing["now"] = False
        self.assertEqual([row["outcome"] for row in self.index()], ["finished", "finished"])
        return repositories, github, compared, compare

    def test_a_finished_run_whose_comparison_failed_is_compared_without_a_new_run(self):
        """The index records a run as finished before its outputs and its comparison. After a
        comparison that exits 1, the next invocation over the same sampling frame requests no
        README, adds no run to the cache of labels and compares the finished run. The outputs of
        the finished run stay as it wrote them, and the repository that it left without a
        decision stays undecided until the next run."""
        repositories, github, compared, compare = self.a_run_left_uncompared()
        first, second = [row["run_id"] for row in self.index()]
        self.assertEqual(compared, [first, second])
        outputs = {name: (self.out / name).read_bytes() for name in
                   ("repos_annotated.csv", "repos_excluded.csv", classify_repos.ANNOTATION_RECORD)}
        # The runner records the step only when it rewrote each output that the step declares,
        # and stops the Start otherwise.
        signatures = {name: runner._output_signature(self.out / name) for name in outputs}
        github.sent.clear()
        model = _Model(_always_true)
        code, err = self.labeling(repositories, github, model, compare,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        self.assertEqual((github.sent, model.requests), ([], []))
        self.assertEqual([row["run_id"] for row in self.index()], [first, second])
        self.assertEqual(compared, [first, second, second])
        self.assertEqual(self.index()[1]["compared_with"], first)
        self.assertEqual({name: (self.out / name).read_bytes() for name in outputs}, outputs)
        self.assertEqual([name for name, before in signatures.items()
                          if not runner._wrote_output(self.out / name, before)], [])
        self.assertIn(f"Run {second} finished over the same repositories", err)

    def test_a_finished_run_stopped_while_it_wrote_its_outputs_writes_them_without_a_new_run(
            self):
        """A stop while the run writes its outputs leaves the outputs of the previous run beside
        the new labels. The next invocation over the same sampling frame writes the outputs of
        the finished run from its labels, without a README request or a new run. It then removes
        the run's partial file and compares the run."""
        repositories, github, compared, compare = self.a_run_left_uncompared(
            kill_on="repos_excluded.csv")
        first, second = [row["run_id"] for row in self.index()]
        self.assertEqual(compared, [first])
        record = json.loads((self.out / classify_repos.ANNOTATION_RECORD).read_text())
        self.assertEqual(record["run_id"], first)
        self.assertTrue(label_cache.partial_path(self.cache, second).is_file())
        # The run wrote this output before the stop, from the labels as it made them.
        annotated = (self.out / "repos_annotated.csv").read_bytes()
        github.sent.clear()
        model = _Model(_always_true)
        code, err = self.labeling(repositories, github, model, compare,
                                  retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        self.assertEqual((github.sent, model.requests), ([], []))
        self.assertEqual([row["run_id"] for row in self.index()], [first, second])
        self.assertEqual(compared, [first, second])
        self.assertFalse(label_cache.partial_path(self.cache, second).exists())
        self.assertEqual((self.out / "repos_annotated.csv").read_bytes(), annotated)
        self.assertEqual([(r["repo_name"], r["engineered_project"])
                          for r in self.rows("repos_annotated.csv")], [("o/a", "true")])
        self.assertEqual([(r["repo_name"], r["reason"]) for r in self.rows("repos_excluded.csv")],
                         [("o/b", "empty_readme"), ("o/c", "not_found_or_no_access"),
                          ("o/d", "no_readme_found"), ("o/e", "undecided")])
        record = json.loads((self.out / classify_repos.ANNOTATION_RECORD).read_text())
        self.assertEqual((record["run_id"], record["retrieval_id"], record["undecided"],
                          record["models"]), (second, "2026-11-05T101430Z", 1, ["gpt-6-luna"]))

    def test_a_finished_run_is_completed_without_a_new_run_only_under_the_same_inputs(self):
        """The next invocation does not take up a finished run with `--new-run`, nor when it
        labels with another model or over other repositories, or when a new image changed a
        rule that derives one of the run's labels or changed the purpose check, which needs the
        README again. A new run labels the repositories instead, and the outputs carry no label
        of the earlier rules."""
        retrieval = "2026-11-05T101430Z"
        rules = {"criterion": mock.patch.dict(classify_repos.CRITERION_APPLIES,
                                              {"research_artifact": lambda facts: "true"}),
                 "purpose check": mock.patch.object(classify_repos, "quote_check",
                                                    lambda: "another purpose check")}
        for change in ("new run", "model", "repositories", *rules):
            with self.subTest(change=change):
                self.setUp()
                repositories, github, compared, compare = self.a_run_left_uncompared()
                runs = [row["run_id"] for row in self.index()]
                options = {"retrieval": retrieval}
                if change == "new run":
                    code, err = self.labeling(repositories, github, _Model(_always_true),
                                              compare, retrieval=retrieval)
                    self.assertEqual((code, self.index()[1]["compared_with"]), (0, runs[0]), err)
                    runs = [row["run_id"] for row in self.index()]
                    options["new_run"] = True
                elif change == "model":
                    options["model"] = "gpt-6.1-sol"
                elif change == "repositories":
                    repositories = repositories[:-1]
                github.sent.clear()
                with contextlib.ExitStack() as stack:
                    if change in rules:
                        stack.enter_context(rules[change])
                    code, err = self.labeling(repositories, github, _Model(_always_true),
                                              compare, **options)
                    labels = self.rows("repos_annotated.csv")
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.index()), len(runs) + 1)
                self.assertTrue(self.readme_requests(github, "o/a"))
                self.assertEqual(labels[0]["engineered_project"],
                                 "false" if change == "criterion" else "true")

    def select(self) -> Path:
        """The sample that the engineered project selection writes from the labels, beside
        them, as the pipeline writes it."""
        from agentcontext.steps import engineered_project_selection as selection
        sample = self.out / "repos_annotated_included.csv"
        selection.write(sample, selection.render(*selection.derive_sample(
            self.out / "repos_annotated.csv")))
        return sample

    def a_compared_run(self, names):
        """Two runs over two sampling frames of `names` with the same READMEs, the second
        compared with the first by the comparison of changed_labels.py. Returns the
        repositories, GitHub and the retrieval of the second sampling frame."""
        repositories = _independent(*names)
        github = _GitHub({name: (ENGLISH.replace("files", f"files of {name}"), name)
                          for name in names})
        for retrieval in ("2026-10-05T101430Z", "2026-11-05T101430Z"):
            code, err = self.labeling(repositories, github, _Model(_always_true), None,
                                      retrieval=retrieval)
            self.assertEqual(code, 0, err)
        first, second = self.index()
        self.assertEqual(second["compared_with"], first["run_id"])
        return repositories, github, "2026-11-05T101430Z"

    def test_a_change_to_a_module_that_decides_no_label_runs_the_labeling_again(self):
        """The runner records the labeling by the code of its script and of every module that
        `run_classification.MODULES` names, and a change to the cost record, which decides no
        label, therefore runs the labeling again."""
        import run_classification
        labeling = run_classification.STEPS[0]
        code_dir = self.tmp / "code"
        for rel in (labeling.args[0], *labeling.code_inputs):
            (code_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(Path(classify_repos.__file__).parent / rel, code_dir / rel)
        labeling = dataclasses.replace(labeling, code=str(code_dir), inputs=())
        recorded = runner.step_fingerprint(labeling)
        with open(code_dir / "costs.py", "a", encoding="utf-8") as f:
            f.write("\nRECORDED_TIERS = ()\n")
        self.assertNotEqual(runner.step_fingerprint(labeling), recorded)

    def test_a_refused_endpoint_stops_only_an_invocation_that_sends_a_request(self):
        """OpenRouter's lists can show that neither the pinned provider nor the backup provider
        of a model serves the requests, as while both are missing from the endpoint list
        (`endpoint_check`). The runner runs the labeling again after a change of its code, and
        an invocation that completes its finished and compared run from its labels sends no
        request. It exits 0 with a WARNING, so that the collection after the labeling runs. An
        invocation that labels exits 1 with the ERROR before its first request."""
        repositories, github, frame = self.a_compared_run(("o/a", "o/b"))
        runs = [row["run_id"] for row in self.index()]
        reason = "OpenRouter lists no endpoint morph/fp8 of deepseek/deepseek-v4.1-flash"
        github.sent.clear()
        model = _Model(_always_true)
        code, err = self.labeling(repositories, github, model, None, retrieval=frame,
                                  unserved=reason)
        self.assertEqual(code, 0, err)
        self.assertEqual((github.sent, model.requests), ([], []))
        self.assertIn(f"WARNING repository_labeling: {reason}. The invocation completes run "
                      f"{runs[-1]} from its labels without a request", err)
        self.assertNotIn("ERROR", err)
        code, err = self.labeling(repositories, github, model, None, retrieval=frame,
                                  unserved=reason, new_run=True)
        self.assertEqual(code, 1, err)
        self.assertEqual((github.sent, model.requests), ([], []))
        self.assertIn(f"ERROR repository_labeling: {reason}.", err)
        self.assertEqual([row["run_id"] for row in self.index()], runs)

    def test_a_change_that_decides_no_label_keeps_the_labels_the_sample_and_the_collection(
            self):
        """When the runner runs the labeling again after a change that decides no label, the
        labeling completes its finished and compared run without a README request. A new run
        would find o/b gone, which GitHub stopped serving, drop it from the sample and make the
        cloning step begin a new collection, which queries every head again and collects again
        every repository with a head that moved. The outputs, the sample and its digest, by which
        the runner records the cloning step, therefore stay, whether the collection is in
        progress or ended. When a labeling does label again, here with --new-run, and finds o/b
        gone, the collection continues over the sample without o/b."""
        from agentcontext import resume
        from agentcontext.steps import repository_cloning
        names = ("o/a", "o/b", "o/c")
        for ended in (False, True):
            with self.subTest(ended=ended):
                self.setUp()
                repositories, github, frame = self.a_compared_run(names)
                runs = [row["run_id"] for row in self.index()]
                sample = self.select()
                listed = sample.read_bytes()
                digest = runner._repositories_digest(sample, "repo_name")
                outputs = {name: (self.out / name).read_bytes() for name in
                           ("repos_annotated.csv", "repos_annotated.json", "repos_excluded.csv",
                            "label_changes.csv")}
                signatures = {name: runner._output_signature(self.out / name)
                              for name in outputs}
                finished = self.collection / resume.FINISHED_NAME
                with contextlib.redirect_stderr(io.StringIO()):
                    collection = resume.open_collection(finished, list(names))
                resume.append_finished(finished, "o/a", "o/a,row", None,
                                       collection=collection["collection"])
                if ended:
                    resume.close_collection(finished)
                del github.readmes["o/b"]
                github.sent.clear()
                model = _Model(_always_true)
                code, err = self.labeling(repositories, github, model, None, retrieval=frame)
                self.assertEqual(code, 0, err)
                self.assertEqual((github.sent, model.requests), ([], []))
                self.assertEqual([row["run_id"] for row in self.index()], runs)
                self.assertEqual({name: (self.out / name).read_bytes() for name in outputs},
                                 outputs)
                self.assertEqual([name for name, before in signatures.items()
                                  if not runner._wrote_output(self.out / name, before)], [])
                self.assertEqual(self.select().read_bytes(), listed)
                self.assertEqual(runner._repositories_digest(sample, "repo_name"), digest)
                code, err = self.labeling(repositories, github, _Model(_always_true), None,
                                          retrieval=frame, new_run=True)
                self.assertEqual(code, 0, err)
                self.assertIn(("o/b", "not_found_or_no_access"),
                              [(row["repo_name"], row["reason"])
                               for row in self.rows("repos_excluded.csv")])
                left = repository_cloning.parse_repo_csv(self.select(), "repo_name")
                self.assertEqual(left, ["o/a", "o/c"])
                with contextlib.redirect_stderr(io.StringIO()) as log:
                    continued = resume.open_collection(finished, left,
                                                       repository_cloning.labeling_gone(sample))
                self.assertEqual(continued["collection"], collection["collection"],
                                 log.getvalue())


class _Killed(Exception):
    """The end of an invocation by a kill, raised where the kill comes."""


def read_partial_ids(cache):
    partial = next(Path(cache).glob("*.partial.jsonl"))
    return set(classify_repos.read_partial(partial))


def write_partial(path, lines):
    """Write a partial file with `lines`, as the run writes them, and a last line that a stop
    cut inside the UTF-8 bytes of "ü". Returns the length of the longest line."""
    data = b"".join(json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + b"\n" for line in lines)
    cut = json.dumps({"id": 9999, "status": "decided", "decision": {"justification": "für"}},
                     ensure_ascii=False).encode("utf-8")
    Path(path).write_bytes(data + cut[:cut.index("ü".encode("utf-8")) + 1])
    return max(len(line) for line in data.split(b"\n"))


def traced_peak(reader):
    """What `reader()` returns and the peak of the memory that it allocated meanwhile."""
    import tracemalloc
    tracemalloc.start()
    try:
        result = reader()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class TestDataRoot(unittest.TestCase):
    """The classification's default data paths follow the data root that
    `AGENTCONTEXT_DATA_ROOT` names, as the container sets it. The check is here rather than in
    `tests/test_data_layout.py`, which CI runs without the packages of this stage."""

    def test_the_sampling_frame_read_by_default_is_under_the_data_root(self):
        import subprocess
        child = ("import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                 "print(importlib.import_module('1_classify_repos').SAMPLING_FRAME)")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENTCONTEXT_DATA_ROOT=tmp)
            result = subprocess.run([sys.executable, "-c", child,
                                     str(Path(classify_repos.__file__).parent)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(Path(result.stdout.strip()),
                             Path(tmp).resolve() / "sampling" / "ghs_filtered.json")


class TestTheCommandLine(unittest.TestCase):

    def test_an_abbreviated_option_is_a_usage_error(self):
        """The step takes long options by their full names only, as every parser of the
        pipeline's command lines does. `agentcontext.cli.ArgumentParser` gives the reason."""
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            classify_repos.build_parser().parse_args(["--service", "default"])
        self.assertEqual(stop.exception.code, 2)


class TestPieces(unittest.TestCase):

    def test_the_bridge_names_the_tool_of_the_checklist_among_the_tools(self):
        """A model that calls a tool by a wrong name gets the names of the tools that it may
        call. The tool of the checklist is the classification's own, beside the tools of the
        server, and a list of the names without it would leave a model that misnamed it without
        the name."""
        bridge = classify_repos.open_bridge(
            classify_repos.build_parser().parse_args(["--credentials", os.devnull]))
        self.assertEqual((bridge.offered, bridge.own_tools),
                         (classify_repos.OFFERED, (classify_repos.CHECKLIST_TOOL_NAME,)))

    def test_the_default_model_labels_and_summarizes_on_its_pinned_endpoint(self):
        """The maintainer chose DeepSeek V4.1 Flash on Morph's fp8 endpoint as the classifying
        and the summary model on 2026-10-31. Without options, a run labels and summarizes with
        that model on that endpoint, labels at the reasoning effort high of the model's settings,
        which the maintainer set on 2026-11-02, and summarizes at low. The endpoint options choose
        another endpoint, and an OpenAI model takes no endpoint and labels at medium."""
        parse = classify_repos.build_parser().parse_args
        pinned = classify_repos.endpoints.Endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8",
                                                   "fp8", "deepseek-ai/DeepSeek-V4.1-Flash")
        args = parse([])
        self.assertEqual((classify_repos.classifying_endpoint(args),
                          classify_repos.summary_endpoint(args),
                          classify_repos.run_settings(args).reasoning_effort,
                          args.summary_effort), (pinned, pinned, "high", "low"))
        self.assertEqual(classify_repos.run_settings(parse(["--model", "gpt-6-luna"])),
                         classify_repos.endpoints.Settings("medium"))
        args = parse(["--model", "gpt-6-luna", "--summary-model", "gpt-6-luna"])
        self.assertEqual((classify_repos.classifying_endpoint(args),
                          classify_repos.summary_endpoint(args)),
                         (classify_repos.endpoints.Endpoint("gpt-6-luna"),) * 2)
        other = ["--provider", "wafer/mxfp4", "--quantization", "mxfp4",
                 "--hugging-face-id", "deepseek-ai/DeepSeek-V4.1-Flash"]
        self.assertEqual(classify_repos.classifying_endpoint(parse(other)).provider,
                         "wafer/mxfp4")

    def test_the_pause_of_every_worker_grows_up_to_its_longest_and_a_response_ends_it(self):
        """The failed requests of one repository use its attempts, and the pause begins once the
        requests of two repositories have failed since the last response. Each pause is twice as
        long as the pause before it, up to the longest. A request sent before the latest pause
        began begins no further pause, and a response makes the next pause the shortest again. A
        failure uses no attempt during the first pauses since the last response, and uses one
        after them."""
        clock = SimpleNamespace(now=0.0)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            clock.now += seconds
        pause = classify_repos.ServicePause(sleep, clock=lambda: clock.now)
        error = RuntimeError("HTTP 429")
        first, longest = classify_repos.PAUSE_FIRST_SECONDS, classify_repos.PAUSE_LONGEST_SECONDS
        free = classify_repos.PAUSES_BEFORE_ATTEMPTS

        async def scenario():
            sent = await pause.ready()
            self.assertFalse(pause.failed("o/a", sent, error))
            self.assertTrue(pause.failed("o/b", sent, error))
            self.assertTrue(pause.failed("o/c", sent, error))
            for number in range(2, 12):
                sent = await pause.ready()
                self.assertEqual(pause.failed("o/a", sent, error), number <= free)
            await pause.ready()
            pause.succeeded()
            sent = await pause.ready()
            self.assertFalse(pause.failed("o/a", sent, error))
            self.assertTrue(pause.failed("o/b", sent, error))
            await pause.ready()
        with contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(scenario())
        grown = [min(first * 2 ** n, longest) for n in range(11)]
        self.assertEqual(waits, grown + [first])
        self.assertEqual(grown[-2:], [longest, longest])

    def test_a_pause_for_the_response_format_holds_every_worker_and_keeps_a_longer_pause(self):
        """`hold` pauses every worker for its seconds and counts among the pauses, so that a
        request sent before it began begins no pause of the service when it fails. It keeps a
        pause of the service that lasts longer."""
        clock = SimpleNamespace(now=0.0)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            clock.now += seconds
        pause = classify_repos.ServicePause(sleep, clock=lambda: clock.now)
        error = RuntimeError("HTTP 503")

        async def scenario():
            sent = await pause.ready()
            pause.hold(30)
            self.assertEqual(await pause.ready(), sent + 1)
            self.assertFalse(pause.failed("o/a", sent, error))
            self.assertTrue(pause.failed("o/b", sent, error))
            self.assertEqual(await pause.ready(), sent + 1)
            sent = await pause.ready()
            self.assertTrue(pause.failed("o/b", sent, error))
            pause.hold(10)
            await pause.ready()
        with contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(scenario())
        self.assertEqual(waits, [30, classify_repos.PAUSE_FIRST_SECONDS])

    def test_a_repository_that_left_begins_no_pause_and_the_pauses_end_at_their_total(self):
        """A finished repository no longer counts toward a pause, and the last repository with
        failing requests uses its attempts. Once the pauses since the last response have reached
        their total, the failure that would begin the next pause stops the run, and so does the
        next request of every other worker."""
        clock = SimpleNamespace(now=0.0)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            clock.now += seconds
        pause = classify_repos.ServicePause(sleep, clock=lambda: clock.now)
        error = RuntimeError("HTTP 503")
        total = classify_repos.PAUSE_TOTAL_SECONDS

        async def scenario():
            sent = await pause.ready()
            self.assertFalse(pause.failed("o/a", sent, error))
            pause.finished("o/a")
            self.assertFalse(pause.failed("o/b", sent, error))
            self.assertEqual(waits, [])
            with self.assertRaises(classify_repos.ServiceDown) as stopped:
                for _ in range(1000):
                    sent = await pause.ready()
                    pause.failed("o/c", sent, error)
            self.assertIn("the API failed every request through", str(stopped.exception))
            with self.assertRaises(classify_repos.ServiceDown):
                await pause.ready()
        with contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(scenario())
        self.assertGreaterEqual(sum(waits), total)
        self.assertLess(sum(waits[:-1]), total)

    def test_the_pause_for_github_takes_its_waits_in_turn_and_ends_with_a_response_or_a_stop(
            self):
        """GitHub's failures of one repository fail that repository, and the pause begins once
        two repositories have failed since GitHub's last response. The pauses take the waits in
        turn. A step that began before the latest pause began begins no further pause, and a
        response of GitHub makes the next pause the first again. After the last pause, a
        repository that waited for the pauses fails on its own and begins no pause again. The
        failure of a repository that did not wait stops the run, and so does the next step of
        every other worker."""
        clock = SimpleNamespace(now=0.0)
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            clock.now += seconds
        github = SimpleNamespace(responses=0)
        # GitHub returns no response to a request that depends on no repository either.
        pause = classify_repos.GitHubPause(lambda: github.responses, lambda: False, sleep,
                                           clock=lambda: clock.now)
        error = readmes.NoResponse("GitHub returned HTTP 502")
        pauses = classify_repos.GITHUB_PAUSES

        async def scenario():
            sent = await pause.ready()
            self.assertFalse(await pause.failed("o/a", sent, error))
            self.assertTrue(await pause.failed("o/b", sent, error))
            self.assertTrue(await pause.failed("o/c", sent, error))
            sent = await pause.ready()
            github.responses += 1
            self.assertFalse(await pause.failed("o/b", sent, error))
            self.assertTrue(await pause.failed("o/c", sent, error))
            for _ in pauses[1:]:
                sent = await pause.ready()
                self.assertTrue(await pause.failed("o/c", sent, error))
            sent = await pause.ready()
            self.assertFalse(await pause.failed("o/c", sent, error))
            self.assertFalse(await pause.failed("o/c", sent, error))
            with self.assertRaises(classify_repos.GitHubDown) as stopped:
                await pause.failed("o/d", sent, error)
            self.assertIn("GitHub returned no usable response through 7:20:00 of pauses",
                          str(stopped.exception))
            with self.assertRaises(classify_repos.GitHubDown):
                await pause.ready()
        with contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(scenario())
        self.assertEqual(waits, [pauses[0], *pauses])

    def test_a_repository_that_github_failed_before_begins_no_pause(self):
        """In the pass, GitHub fails o/a and o/b on their own, each while it responds for other
        repositories. The retry round holds the repositories without a decision, and there the
        two fail again with no response in between. GitHub responds to the request that depends
        on no repository after each failure, and the two begin no pause. Neither does o/c,
        which a caller names as failed in an earlier invocation of the run. Two repositories
        that GitHub has not failed before begin no pause either while GitHub responds to that
        request, as at the end of a pass, when nothing else is in flight."""
        clock = SimpleNamespace(now=0.0)
        waits, requested = [], []

        async def sleep(seconds):
            waits.append(seconds)
            clock.now += seconds

        def responds():
            requested.append(clock.now)
            return True
        github = SimpleNamespace(responses=0)
        pause = classify_repos.GitHubPause(lambda: github.responses, responds, sleep,
                                           clock=lambda: clock.now)
        pause.failed_before.add("o/c")
        error = readmes.NoResponse("GitHub returned HTTP 502")

        async def scenario():
            sent = await pause.ready()
            self.assertFalse(await pause.failed("o/a", sent, error))
            github.responses += 1
            self.assertFalse(await pause.failed("o/b", sent, error))
            github.responses += 1
            for name in ("o/a", "o/b", "o/c", "o/a"):
                sent = await pause.ready()
                self.assertFalse(await pause.failed(name, sent, error))
            self.assertEqual((waits, len(requested)), ([], 4))
            self.assertFalse(await pause.failed("o/d", sent, error))
            self.assertFalse(await pause.failed("o/e", sent, error))
            await pause.ready()
        with contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(scenario())
        self.assertEqual((waits, len(requested)), ([], 5))

    def test_a_repository_that_github_failed_before_counts_when_github_returns_nothing(self):
        """In the pass, GitHub fails o/a and o/b on their own, and it then fails every request,
        as in an outage that begins in the retry round. The round holds only the two
        repositories. After either failure, GitHub returns no response to the request that
        depends on no repository, and their failures count toward the pause as any other failure
        does. The repository o/a fails for the next round, and o/b begins the pause and goes
        through its step again after the pause. Left on their own, both would fail in every
        retry round of the outage and end without a decision."""
        clock = SimpleNamespace(now=0.0)
        waits, requested = [], []

        async def sleep(seconds):
            waits.append(seconds)
            clock.now += seconds

        def responds():
            requested.append(clock.now)
            return False
        github = SimpleNamespace(responses=0)
        pause = classify_repos.GitHubPause(lambda: github.responses, responds, sleep,
                                           clock=lambda: clock.now)
        error = readmes.NoResponse("GitHub returned HTTP 502")

        async def scenario():
            sent = await pause.ready()
            self.assertFalse(await pause.failed("o/a", sent, error))
            github.responses += 1
            self.assertFalse(await pause.failed("o/b", sent, error))
            github.responses += 1
            sent = await pause.ready()
            self.assertFalse(await pause.failed("o/a", sent, error))
            self.assertTrue(await pause.failed("o/b", sent, error))
            await pause.ready()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            asyncio.run(scenario())
        self.assertEqual((waits, len(requested)), ([classify_repos.GITHUB_PAUSES[0]], 2))
        self.assertIn("GitHub returned no usable response for 2 repositories since its last "
                      "response, the last for o/b", err.getvalue())

    def test_the_readers_of_a_partial_file_hold_one_line_at_a_time(self):
        """The partial file of a run over the whole sampling frame keeps every tool output, and
        the run reads the file at its end and at every resume. Read as a whole, the file's text
        takes four bytes per character once one character is outside ASCII, and its list of
        lines takes as much again. The file here is a hundred times as long as its longest line,
        and its lines keep the model's responses. Each reader's peak stays a small multiple of
        the longest line, and each reader returns what the file holds without the last line,
        which a stop cut inside a character."""
        output = "x" * 200_000
        lines = [{"repositories": "0" * 64}]
        for number in range(1, 151):
            lines.append({"id": number, "status": classify_repos.KEPT, "key": f"k{number}",
                          "response": {"tools": [{"output": output}]},
                          "spent": {"model_requests": 1}})
            lines.append({"id": number, "status": classify_repos.DECIDED,
                          "decision": {"id": number, "justification": "Ein Werkzeug 🛠 für "
                                                                      "Dateien."},
                          "spent": {"github_requests": 2}})
        lines.append({"id": 150, "status": classify_repos.FAILED, "reason": "github",
                      "name": "o/r150"})
        lines.append({"id": 151, "status": classify_repos.KEPT, "key": "k151",
                      "response": {"tools": []}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-10-18T000000Z.partial.jsonl"
            longest = write_partial(path, lines)
            self.assertGreater(path.stat().st_size, 100 * longest)
            for reader, expected in (
                    (lambda: sorted(classify_repos.read_partial(path)), list(range(1, 150))),
                    (lambda: [d["id"] for d in classify_repos.partial_decisions(path)],
                     list(range(1, 151))),
                    (lambda: classify_repos.kept_responses(path, classify_repos.FINISHED),
                     {"k151": {"tools": []}}),
                    (lambda: label_cache.spent_in(path, ("model_requests", "github_requests")),
                     {"model_requests": 150, "github_requests": 300}),
                    (lambda: classify_repos.github_failures(path), {"o/r150"})):
                result, peak = traced_peak(reader)
                self.assertEqual(result, expected)
                self.assertLess(peak, 10 * longest)

    def test_the_purpose_checks_version_follows_its_code_and_not_its_docstrings(self):
        """A label made under another version of the purpose check has its README requested
        again and is written again as carried. A docstring, a comment or a blank line in the
        purpose check therefore keeps its version, and a change to its code gives a new version."""
        current = classify_repos.quote_check()
        reworded = _edited_script(self, '"""Whether one of the sources contains the quotation',
                                  '"""Whether a description or a README contains the quotation')
        commented = _edited_script(self, "    parts = _parts(quote)\n",
                                   "    # The quotation is split first.\n\n"
                                   "    parts = _parts(quote)\n")
        changed = _edited_script(self, "allowed = len(wanted) // WORDS_PER_CHANGED_WORD\n",
                                 "allowed = len(wanted) // WORDS_PER_CHANGED_WORD + 1\n")
        self.assertEqual(reworded.quote_check(), current)
        self.assertEqual(commented.quote_check(), current)
        self.assertNotEqual(changed.quote_check(), current)

    def test_a_line_of_many_links_is_read_in_time(self):
        """A list of links joined by `<br>`, by commas or by parentheses is one line without
        whitespace, and so is a minified stylesheet with a `background-image:` in every rule.
        An AsciiDoc link pattern that scanned from each link to the end of the line would hold
        every worker of the run for each check of a quotation."""
        lines = {separator: separator.join(f"https://github.com/user{n}/project{n}"
                                           for n in range(12800))
                 for separator in ("<br>", ",")}
        for separator in (")(", "),("):
            lines[separator] = "(" + separator.join(f"https://github.com/user{n}/project{n}"
                                                    for n in range(12800)) + ")"
        lines["name(url)"] = "\u3001".join(f"user{n}(https://github.com/user{n}/project{n})"
                                           for n in range(12800))
        lines["stylesheet"] = "".join(f".i{n}{{background-image:none;width:16px}}"
                                      for n in range(8000))
        lines["underscores"] = "a" + "_" * 60000 + "b"
        lines["asterisks"] = "a" + "*" * 60000 + " b"
        lines["brackets"] = "[.a]" * 20000
        lines["shifts"] = "cout << a, b " * 8000
        lines["carets"] = "^a" * 40000
        lines["attributes"] = "{a}" * 40000
        for name, line in lines.items():
            with self.subTest(line=name):
                started = time.monotonic()
                classify_repos.plain(f"# People\n\nThe people who took part.\n\n{line}\n")
                classify_repos._resolved(f":a: 1\n\n{line}\n")
                self.assertLess(time.monotonic() - started, 2.0)

    def test_a_quotation_matches_the_readme_without_its_markup(self):
        readme = ("# Munin\n\n**Munin** is a [stats](https://x.example) bot for an *alliance*\u2014"
                  "it  imports   `ships`.<br>\n\n> Install it with pip.")
        for quote, held in (("Munin is a stats bot for an alliance - it imports ships.", True),
                            ("munin is a stats bot ... imports ships", True),
                            ("\u201cMunin is a stats bot\u201d", True),
                            ("imports ships ... Munin is a stats bot", False),
                            ("Munin is a database-backed system", False),
                            ("...", False)):
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)
        self.assertTrue(classify_repos.quoted("A stats bot", "A stats bot.", "Nothing."))

    def test_a_quotation_runs_from_a_sentence_into_its_list(self):
        """dcSpark/cardano-multiplatform-lib's README says "It handles:" and lists what the
        library handles, and the model quoted the sentence with its first item word for word.
        Kimi K3 quoted the list with its markers, and a quotation can also join the items into
        one line with their markers. The purpose check leaves the marker of a list item, such as
        a hyphen, an asterisk, a plus or a number, out of the README and of the quotation, and
        the version of the purpose check changes with the patterns that find the marker."""
        readme = ("This is a library, written in Rust, that can be deployed to multiple "
                  "platforms. It handles:\n- Serialization & deserialization of core data "
                  "structures\n- Useful utility functions for dApps & wallets\n\nSteps:\n"
                  "1. Install the crate\n2) Build it\n* Run the tests\n+ Publish it\n")
        for quote in ("It handles: Serialization & deserialization of core data structures",
                      "It handles: Serialization & deserialization of core data structures "
                      "Useful utility functions for dApps & wallets",
                      "Steps: Install the crate Build it Run the tests Publish it",
                      "This is a library, written in Rust, that can be deployed to multiple "
                      "platforms. It handles:\n- Serialization & deserialization of core data "
                      "structures\n- Useful utility functions for dApps & wallets",
                      "It handles: - Serialization & deserialization of core data structures "
                      "- Useful utility functions for dApps & wallets",
                      "Steps:\n1. Install the crate\n2) Build it"):
            with self.subTest(quote=quote):
                self.assertTrue(classify_repos.quoted(quote, "", readme))
        self.assertFalse(classify_repos.quoted("It handles: - Parsing of transactions", "",
                                               readme))
        current = classify_repos.quote_check()
        for name, pattern in (("_LIST_MARKER", r"(?m)^-[ \t]+"), ("_LONE_MARKER", r" - ")):
            with mock.patch.object(classify_repos, name, re.compile(pattern)):
                self.assertNotEqual(classify_repos.quote_check(), current)

    def test_a_quotation_matches_entities_reference_links_and_markup_beside_punctuation(self):
        """Quotations that the READMEs contain match: Openfire's shortcut link, Evennia's reference
        link, Spotless's entities and ScreenToGif's emphasis before a comma."""
        readme = ("About\n-----\n[Openfire] is a real time collaboration (RTC) server.\n"
                  "**Evennia** is a library for creating [online text games][wiki] (MUD, MOO).\n"
                  "Spotless can format &lt;java | kotlin&gt; using &lt;gradle | maven&gt;.\n"
                  "It records your **screen**, webcam or ( _sketchboard_ ).\n\n"
                  "[wiki]: https://wiki.example/mud")
        for quote in ("Openfire is a real time collaboration (RTC) server.",
                      "Evennia is a library for creating online text games (MUD, MOO).",
                      "Spotless can format <java | kotlin> using <gradle | maven>.",
                      "It records your screen, webcam or (sketchboard)."):
            with self.subTest(quote=quote):
                self.assertTrue(classify_repos.quoted(quote, "", readme))

    def test_a_quotation_of_the_rendered_sentence_matches_past_inline_markup(self):
        """A quotation of the rendered sentence matches these README lines with their inline
        markup. The same lines refuse a quotation that their rendered sentence does not contain."""
        qor = ("Media is a [Golang](http://golang.org/) library that supports the upload of "
               "*files*/*images*/*videos* to a filesystem or cloud storage as well as *linked "
               "videos* (i.e. YouTube, Vimeo, etc.). The plugin includes:\n")
        yourls = ("**YOURLS** is a set of PHP scripts that will allow you to run <strong>Y</strong>"
                  "our <strong>O</strong>wn <strong>URL</strong> <strong>S</strong>hortener, on "
                  "**your** server. You'll have full control over your data, detailed stats, "
                  "analytics, plugins, and more. It's free and open-source.\n")
        fitlins = ("FitLins is a tool for estimating linear models, defined by the\n"
                   "`BIDS Stats-Models`_ specification proposal, to `BIDS`_-formatted datasets.\n")
        osparc = ("The aim of o<sup>2</sup>S<sup>2</sup>PARC is to establish a comprehensive, "
                  "freely accessible, intuitive, and interactive online platform for simulating "
                  "peripheral nerve system neuromodulation/ stimulation and its impact on organ "
                  "physiology in a precise and predictive manner.\n")
        streamshub = ("StreamsHub Console is a web application designed to facilitate interactions "
                      "with Apache Kafka<sup>\u00ae</sup> instances, optionally leveraging the "
                      "[Strimzi](https://strimzi.io) Cluster Operator for Kafka<sup>\u00ae</sup> "
                      "instances running on Kubernetes.\n")
        wallpaper = ("Bring **Wallpaper Engine**-style live wallpapers to Linux! This project "
                     "allows you to run animated wallpapers from Steam\u2019s Wallpaper Engine "
                     "right on your desktop.\n")
        freegsnke = ('FreeGSNKE (pronounced "free-gee-snake") is a **Python**-based code for '
                     "**simulating the evolution of free-boundary tokamak plasma equilibria**.\n")
        geonature = ("**Application de saisie, de gestion, de synth\u00e8se et de diffusion\n"
                     "d\\'observations faune et flore.**\n")
        boilerplate = "\U0001f6a7 Boilerplate for \U0001f680 Fiber"
        cases = (("Media is a Golang library that supports the upload of files/images/videos to a "
                  "filesystem or cloud storage as well as linked videos (i.e. YouTube, Vimeo, "
                  "etc.).", qor, True),
                 ("Media is a Golang library that supports the upload of files/images/videos to a "
                  "database or a tape.", qor, False),
                 ("Media is a http://golang.org/ library.", qor, False),
                 ("YOURLS is a set of PHP scripts that will allow you to run Your Own URL "
                  "Shortener, on your server.", yourls, True),
                 ("YOURLS is a set of PHP scripts that runs Your Own Link Shortener.", yourls,
                  False),
                 ("FitLins is a tool for estimating linear models, defined by the BIDS "
                  "Stats-Models specification proposal, to BIDS-formatted datasets.", fitlins,
                  True),
                 ("FitLins is a tool for estimating neural networks, defined by the BIDS "
                  "Stats-Models proposal, to BIDS-formatted images.", fitlins, False),
                 ("The aim of o2S2PARC is to establish a comprehensive, freely accessible, "
                  "intuitive, and interactive online platform for simulating peripheral nerve "
                  "system neuromodulation/ stimulation and its impact on organ physiology in a "
                  "precise and predictive manner.", osparc, True),
                 ("The aim of o2S2PARC is to replace the kidney.", osparc, False),
                 ("StreamsHub Console is a web application designed to facilitate interactions "
                  "with Apache Kafka\u00ae instances, optionally leveraging the Strimzi Cluster "
                  "Operator for Kafka\u00ae instances running on Kubernetes.", streamshub, True),
                 ("Bring Wallpaper Engine-style live wallpapers to Linux! This project allows you "
                  "to run animated wallpapers from Steam's Wallpaper Engine right on your "
                  "desktop.", wallpaper, True),
                 ("Bring Wallpaper Engine-style live wallpapers to Windows!", wallpaper, False),
                 ("a Python-based code for simulating the evolution of free-boundary tokamak "
                  "plasma equilibria", freegsnke, True),
                 ("a Rust-based code for simulating tokamak plasma", freegsnke, False),
                 ("Application de saisie, de gestion, de synth\u00e8se et de diffusion "
                  "d'observations faune et flore.", geonature, True),
                 ("Application de gestion de la flore.", geonature, False),
                 ("Boilerplate for Fiber", boilerplate, True),
                 ("Boilerplate for Express", boilerplate, False),
                 # The same sentence with its markup, as a model can copy it.
                 ("**Wallpaper Engine**-style live wallpapers", wallpaper, True),
                 ("<strong>Y</strong>our <strong>O</strong>wn <strong>URL</strong> "
                  "<strong>S</strong>hortener", yourls, True))
        for quote, readme, held in cases:
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)

    def test_a_quotation_matches_an_asciidoc_readme_as_rendered(self):
        """The AsciiDoc READMEs of smallrye/smallrye-health, asciidoctor/asciidoctorj and
        smallrye/smallrye-jwt link through attributes, and the README of wildfly/quickstart
        writes the name of its release through attributes that a conditional block sets. A
        quotation of the rendered sentence matches, and so does the sentence as written. A value
        that a block with a condition that does not hold sets is not in the rendered sentence, and
        neither is the name of an attribute in place of its value."""
        health = (":microprofile-health: https://github.com/eclipse/microprofile-health/\n\n"
                  "SmallRye Health is an implementation of {microprofile-health}[Eclipse "
                  "MicroProfile Health].\n")
        asciidoctorj = (":uri-repo: https://github.com/asciidoctor/asciidoctorj\n\n"
                        "{uri-repo}[AsciidoctorJ] is the official library for running "
                        "{uri-asciidoctor}[Asciidoctor] on the JVM.\n")
        jwt = (":microprofile-jwt: https://github.com/eclipse/microprofile-jwt-auth/\n"
               ":mp-jwt-name: Eclipse MicroProfile JWT RBAC\n\n"
               "SmallRye JWT is a library for implementing the {microprofile-jwt}[{mp-jwt-name}]. "
               "Currently it is focused on supporting the MP-JWT 2.1 spec.\n")
        quickstart = ("ifdef::ProductRelease[]\n:productName: JBoss EAP\n"
                      ":productNameFull: Red Hat JBoss Enterprise Application Platform\n"
                      "endif::[]\n"
                      "ifndef::ProductRelease,EAPXPRelease[]\n:productName: WildFly\n"
                      ":productNameFull: WildFly Application Server\nendif::[]\n"
                      ":javaVersion: Jakarta EE 11\nifdef::EAPXPRelease[]\n"
                      ":javaVersion: Eclipse MicroProfile\nendif::[]\n\n"
                      "The quickstarts demonstrate {javaVersion} and a few additional technologies "
                      "from the {productNameFull} stack. They provide small, specific, working "
                      "examples that can be used as a reference for your own project.\n")
        included = ("SmallRye Health is an implementation of {microprofile-health}[Eclipse "
                    "MicroProfile Health]. See {uri-docs}/health[the guide].\n")
        cases = (("SmallRye Health is an implementation of Eclipse MicroProfile Health.", health,
                  True),
                 ("SmallRye Health is an implementation of Eclipse MicroProfile Health.", included,
                  True),
                 ("See the guide.", included, True),
                 ("See the manual.", included, False),
                 ("SmallRye Health is an implementation of Jakarta EE Health.", health, False),
                 ("SmallRye Health is an implementation of microprofile-health.", health, False),
                 ("AsciidoctorJ is the official library for running Asciidoctor on the JVM.",
                  asciidoctorj, True),
                 ("AsciidoctorJ is the official library for running Ruby on the CLR.",
                  asciidoctorj, False),
                 ("SmallRye JWT is a library for implementing the Eclipse MicroProfile JWT RBAC.",
                  jwt, True),
                 ("SmallRye JWT is a library for implementing the mp-jwt-name.", jwt, False),
                 ("The quickstarts demonstrate Jakarta EE 11 and a few additional technologies "
                  "from the WildFly Application Server stack. They provide small, specific, "
                  "working examples that can be used as a reference for your own project.",
                  quickstart, True),
                 ("The quickstarts demonstrate {javaVersion} and a few additional technologies "
                  "from the {productNameFull} stack.", quickstart, True),
                 ("The quickstarts demonstrate Eclipse MicroProfile and a few additional "
                  "technologies from the WildFly Application Server stack.", quickstart, False),
                 ("The quickstarts demonstrate Jakarta EE 11 and a few additional technologies "
                  "from the Red Hat JBoss Enterprise Application Platform stack.", quickstart,
                  False))
        for quote, readme, held in cases:
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)

    def test_the_asciidoc_attributes_follow_the_conditions_of_their_blocks(self):
        """`ifdef::a,b[]` holds when one attribute is set and `ifdef::a+b[]` when both are, and
        `ifndef` holds when `ifdef` does not. A block that does not hold sets nothing, also
        inside a block that holds, and GitHub's renderer sets `env-github`."""
        cases = ((":a: 1\nifdef::a[]\n:x: yes\nendif::[]\n{x}", "yes"),
                 ("ifdef::a[]\n:x: no\nendif::[]\n{x}", "{x}"),
                 (":a: 1\nifndef::a[]\n:x: no\nendif::[]\n{x}", "{x}"),
                 ("ifndef::a[]\n:x: yes\nendif::[]\n{x}", "yes"),
                 (":a: 1\nifdef::a,b[]\n:x: yes\nendif::[]\n{x}", "yes"),
                 (":a: 1\nifdef::a+b[]\n:x: no\nendif::[]\n{x}", "{x}"),
                 (":a: 1\nifndef::a,b[]\n:x: no\nendif::[]\n{x}", "{x}"),
                 ("ifdef::env-github[]\n:x: yes\nendif::[]\n{x}", "yes"),
                 ("ifndef::env-github[]\n:x: no\nendif::[]\n{x}", "{x}"),
                 ("ifdef::a[]\nifndef::b[]\n:x: no\nendif::[]\nendif::[]\n{x}", "{x}"),
                 (":flag:\nifdef::flag[]\n:x: yes\nendif::[]\n{x}", "yes"),
                 (":y: 2\n:x: {y} and {y}\n{x}", "2 and 2"))
        for text, last_line in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_repos._resolved(text).splitlines()[-1], last_line)

    def test_a_short_readme_cannot_make_its_attributes_grow_without_limit(self):
        """A README can nest references to attributes, or refer to a long value on many lines. Its
        rendered form then stays within a limit, and the quotation still matches. A value keeps at
        most the size at which GitHub's renderer cuts it."""
        nested = ":a0: " + "x" * 10 + "\n"
        for level in range(1, 8):
            nested += f":a{level}: " + f"{{a{level - 1}}}" * 10 + "\n"
        nested += "Foo is a parser. {a7}\n"
        wide = ":a: " + "x" * 4096 + "\nFoo is a parser.\n" + "{a}\n" * 30_000
        for readme in (nested, wide):
            with self.subTest(readme=readme[:40]):
                self.assertLessEqual(len(classify_repos._resolved(readme)), 4 * len(readme))
                self.assertIs(classify_repos.quoted("Foo is a parser", "", readme), True)
        self.assertEqual(len(classify_repos._attributes(":a: " + "x" * 5000)["a"]), 4096)

    def test_the_asciidoc_attributes_are_read_from_a_readme_with_crlf_line_ends(self):
        """A README with CRLF line ends sets its attributes and opens its conditional blocks as
        the same README with LF line ends does, without the carriage return in a value."""
        link = ":u: https://x.y/\r\n:n: Eclipse JWT\r\n\r\nFoo implements the {u}[{n}].\r\n"
        block = "%s::nope[]\r\n:name: %s\r\nendif::[]\r\n\r\n{name} is a parser\r\n"
        for quote, readme, held in (("Foo implements the Eclipse JWT.", link, True),
                                    ("Wrong is a parser", block % ("ifdef", "Wrong"), False),
                                    ("Right is a parser", block % ("ifndef", "Right"), True)):
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)

    def test_the_patterns_of_inline_markup_stop_at_their_bounds(self):
        """A `<br>` between words leaves a space, and a number after a word without a space is no
        number of a list. The marks of AsciiDoc's superscript do not span a space, the joiners of a
        pictograph go with it, a backslash before a letter stays, and an attribute link does not
        span a line."""
        cases = (("Foo Bar", "Foo<br>Bar", True),
                 ("foobar", "Foo<br>Bar", False),
                 ("the tool1. runs", "the toolruns", False),
                 ("a b c", "a ^b c^ d", False),
                 ("Boilerplate for Fiber", "Boilerplate for \U0001f468\u200d\U0001f4bb Fiber",
                  True),
                 ("c:new folder", "c:\\new folder", False),
                 ("foo x y z", "foo {a}[x\ny] z", False))
        for quote, source, held in cases:
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", source), held)

    def test_a_quotation_matches_past_other_inline_markup_of_markdown_and_asciidoc(self):
        """Strikethrough, a span with a role, superscript and subscript, a cross reference with
        its text and an escaped punctuation mark read as the rendered text. An underscore inside a
        word and a sign between spaces are not markup, and a quotation of another word stays
        refused."""
        readme = ("It replaces the ~~old~~-style parser and the [.underline]#fast#-path cache.\n"
                  "It computes H~2~O and E=mc^2^ tables, as <<usage,the usage section>> says.\n"
                  "It reads snake_case names and 3 * 4 grids, and writes foo\\_bar \\[sic\\].\n"
                  "See <<usage,the usage section>> first.\n"
                  "Water is H~2~O. Energy is mc^2^. Read the [.underline]#fast#-path notes. "
                  "Skip the ~~old~~-style notes.\n")
        for quote, held in (("It replaces the old-style parser and the fast-path cache.", True),
                            ("It computes H2O and E=mc2 tables, as the usage section says.", True),
                            ("It reads snake case names and 3 4 grids", True),
                            ("It reads snake_case names", True),
                            ("writes foo_bar [sic]", True),
                            ("See the usage section first.", True),
                            ("Water is H2O.", True),
                            ("Energy is mc2.", True),
                            ("Read the fast-path notes.", True),
                            ("Skip the old-style notes.", True),
                            ("See the install section first.", False),
                            ("It replaces the new-style parser", False),
                            ("It computes CO2 tables", False)):
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)

    def test_a_quotation_that_joins_a_numbered_list_into_one_line_matches(self):
        """agglayer/provers's README lists what the service does as a numbered list, and the model
        quoted the items in one line with their numbers. Another item stays refused, and so does a
        number that ends a sentence when the source does not contain it."""
        readme = ("Agglayer is the Rust-based service designed to: \n"
                  "1. Receive updates from Agglayer-connected chains \n"
                  "2. Verify their validity \n"
                  "3. Send them to the L1 for final settlement. \n")
        for quote, held in (("Agglayer is the Rust-based service designed to: 1. Receive updates "
                             "from Agglayer-connected chains 2. Verify their validity 3. Send "
                             "them to the L1 for final settlement.", True),
                            ("Agglayer is the Rust-based service designed to: 1) Receive updates "
                             "from Agglayer-connected chains 2) Verify their validity", True),
                            ("Agglayer is the Rust-based service designed to: 1. Receive updates "
                             "from Ethereum 2. Burn their tokens", False)):
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)
        self.assertTrue(classify_repos.quoted("It supports Python 3. Install it with pip.", "",
                                              "It supports Python 3. Install it with pip."))
        self.assertFalse(classify_repos.quoted("It supports Python 3. Install it with pip.", "",
                                               "It supports Python 2. Install it with pip."))

    def test_a_quotation_that_the_sources_do_not_contain_stays_refused(self):
        """The quotation of frappe/agent is the purpose sentence of the README of frappe/press, a
        page of another repository, and the README of frappe/agent does not contain it. The
        quotation of VickScarlet/remake is the title of a README in Chinese that the decision read
        through a link, which is neither the description, the README nor a purpose sentence. A
        two-column table of MetPX/sarracenia alternates the cells of each row
        (classify-repos-spec.md)."""
        agent = ("# Agent\n\n## CLI\n\nAgent has a CLI\n([ref](https://github.com/frappe/agent/"
                 "blob/master/agent/cli.py)). You can\naccess this by activating the env:\n")
        press = [{"tool": "read_linked_page", "is_error": False, "linked": True,
                  "output": json.dumps({"url": "https://github.com/frappe/press",
                                        "other_repository": "frappe/press",
                                        "purpose_sentence": "It provides a CLI interface for "
                                                            "Press to communicate with the sites "
                                                            "and benches."})}]
        self.assertEqual(classify_repos.quoted_purpose(
            "It provides a CLI interface for Press to communicate with the sites and benches.",
            "Works with https://github.com/frappe/press", agent, press), (False, ""))
        remake = ("# Remake\n\nEnglish | [\u7b80\u4f53\u4e2d\u6587](./README-zh_CN.md)\n\n"
                  "## Environment\n\nPackage Manager: `pnpm`\nRuntime: `bun`\n")
        self.assertEqual(classify_repos.quoted_purpose(
            "\u4eba\u751f\u91cd\u5f00\u6a21\u62df\u5668",
            "\u3084\u308a\u76f4\u3059\u3093\u3060\u3002", remake, []), (False, ""))
        sarracenia = ("| MetPX-sr3 (Sarracenia v3) is a data duplication | MetPX-sr3 (Sarracenia "
                      "v3) est un engin de copie et de |\n"
                      "| or distribution pump that leverages               | distribution de "
                      "donn\u00e9es qui utilise des |\n")
        self.assertFalse(classify_repos.quoted(
            "MetPX-sr3 (Sarracenia v3) is a data duplication or distribution pump that leverages",
            "", sarracenia))

    def test_the_purpose_checks_version_follows_its_patterns_for_markup(self):
        """A stored decision is checked again under a new version of the purpose check, so the
        version changes with each pattern and table that removes inline markup, with the limits on
        attributes, and with the code that replaces references to attributes."""
        current = classify_repos.quote_check()
        for name in ("_ATTRIBUTE_LINK", "_ASCIIDOC_CROSS_REFERENCE", "_ATTRIBUTE_LINE",
                     "_ATTRIBUTE_REFERENCE", "_ESCAPED_MARK", "_INLINE_TAG", "_ITEM_NUMBER",
                     "_EMPHASIS", "_ASCIIDOC_ROLE", "_ASCIIDOC_SCRIPT", "_PICTOGRAPH"):
            with self.subTest(pattern=name), \
                    mock.patch.object(classify_repos, name, re.compile("another pattern")):
                self.assertNotEqual(classify_repos.quote_check(), current)
        with mock.patch.dict(classify_repos.GITHUB_ATTRIBUTES, {"env": "github"}):
            self.assertNotEqual(classify_repos.quote_check(), current)
        for name in ("MAX_ATTRIBUTE_VALUE", "MAX_RESOLVED_GROWTH"):
            with self.subTest(constant=name), mock.patch.object(classify_repos, name, 1):
                self.assertNotEqual(classify_repos.quote_check(), current)
        for old, new in (('values = dict(GITHUB_ATTRIBUTES)\n', 'values = {}\n'),
                         ('    values = _attributes(text)\n', '    values = {}\n'),
                         ('    if size > limit:\n', '    if size < limit:\n')):
            with self.subTest(code=old):
                self.assertNotEqual(_edited_script(self, old, new).quote_check(), current)

    def test_a_quotation_matches_past_stray_brackets_roles_and_split_hyphens(self):
        """Quotations that the READMEs of PySyft, libzim, LinApple, python-constraint, umap and
        TestDisk contain match. libcgroup's README states no purpose, and the check refuses the
        model's quotation of a purpose."""
        readme = ("Supports syft <0.10 only. See [docs].\n\n"
                  "LinApple is an Apple ][, Apple ][+, Apple //e emulator for Linux.\n"
                  "PySyft lets data scientists submit computations.\n"
                  "It's a software [library](https://en.wikipedia.org/wiki/Library_(computing)) "
                  "to read ZIM files.\nThe :code:`python-constraint` module offers solvers for "
                  "`CSPs <https://en.wikipedia.org/wiki/CSP>`_ in Python.\n"
                  "A simple, user-\nspace page fault handler.\n\n"
                  "**TestDisk** checks partitions.\n\nIt works with FAT.\n\n"
                  "**PhotoRec** recovers files.\n<!-- a comment -->\n"
                  "Starters for use with link:https://docs.example/scs[Spring Cloud Services].\n"
                  "**Bao** (from Mandarin \u201cb\u01ceoh\u00f9\u201d) is a lightweight "
                  "hypervisor.")
        for quote, held in (("PySyft lets data scientists submit computations.", True),
                            ("It's a software library to read ZIM files.", True),
                            ("The python-constraint module offers solvers for CSPs in Python.",
                             True),
                            ("LinApple is an Apple ][, Apple ][+, Apple //e emulator for Linux.",
                             True),
                            ("A simple, user-space page fault handler.", True),
                            ("TestDisk checks partitions. PhotoRec recovers files.", True),
                            ("**TestDisk** checks partitions.\n\n**PhotoRec** recovers files.",
                             True),
                            ("PhotoRec recovers files. TestDisk checks partitions.", False),
                            ("Starters for use with Spring Cloud Services.", True),
                            ("Bao is a lightweight hypervisor.", True),
                            ("User-space tools for managing Linux cgroups.", False)):
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)

    def test_a_quotation_matches_past_links_with_a_sentence_end_and_a_changed_word(self):
        """Quotations of framework.joomla.org, TAPA, account-payment, product-apim-tooling,
        Vogen and Rello/analytics match what their READMEs contain. Rello's quotation differs in
        one word of fourteen."""
        readme = ("This application is the code powering the [Joomla! Framework]"
                  "(https://framework.joomla.org) website.\n\n"
                  "TAPA (**Ta**sk-**Pa**rallel) is a framework for designing\nFPGA accelerators.\n"
                  "### 1. [Import Export CLI](https://x.example/cli)\n"
                  "### 2. [APIM Traffic Tool](https://x.example/traffic)\n"
                  "[due](due/) | 19.0.1.0.0 | | Register only due payments\n"
                  "[return](return/) | 19.0.1.0.0 | | Manage the return of your payments\n"
                  "Vogen (_pronounced \"Voh Jen\"_) is a Source Generator. It turns your "
                  "primitives \n(ints, decimals etc.) into \nvalue objects.\n"
                  "Connect data from different sources in one place and turn it into shareable "
                  "insights.")
        for quote, held in (("This application is the code powering the [Joomla! Framework]"
                             "(https://framework.joomla.org) website.", True),
                            ("TAPA (**Ta**sk-**Parallel) is a framework for designing FPGA "
                             "accelerators.", True),
                            ("Import Export CLI; APIM Traffic Tool", True),
                            ("“Register only due payments”; “Manage the return of "
                             "your payments”", True),
                            ("Manage the return of your payments; Register only due payments",
                             False),
                            ("Vogen is a Source Generator. It turns your primitives (ints, "
                             "decimals etc.) into value objects.", True),
                            ("Connect data from different sources in one place and turn them into "
                             "shareable insights.", True),
                            ("Connect data from many sources in one place and turn them into "
                             "shareable insights.", False),
                            ("TAPA is a tool for designing FPGA accelerators.", False)):
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)

    def test_a_quotation_matches_past_a_word_left_out_escaped_spaces_and_org_links(self):
        """The quotations from the READMEs of pingcap/ticdc, mike-works/sql-fundamentals,
        lueschem/edi and rndusr/stig match. The quotation of ticdc leaves out "them" and that of
        sql-fundamentals "Mike.Works". The README.rst of edi writes escaped spaces, which
        reStructuredText removes, and the README.org of stig an Org link, which Org shows as its
        description. A quotation of ten words that leaves out three words of the README does not
        match, and neither does libcgroup's quotation of a purpose that its README does not
        state."""
        ticdc = ("TiCDC pulls change logs from TiDB clusters and pushes them to downstream "
                 "systems, such as MySQL, TiDB, Kafka, Pulsar, and Object Storages (e.g., S3). "
                 "Beginning from v8.5.4, we use this repository to build TiCDC instead of the old "
                 "repository [tiflow](https://github.com/pingcap/tiflow).")
        sql = ('This is the example project used for the <a title="Mike.Works" '
               'href="https://mike.works">Mike.Works</a> <a title="SQL Fundamentals" '
               'href="https://mike.works/course/sql-fundamentals-ad811af">SQL Fundamentals</a> '
               'and <a title="Professional SQL" href="https://mike.works/course/professional-sql'
               '-c9c7184">Professional SQL</a> courses.')
        edi = ("edi is a generic tool that helps you to streamline your *e*\\ mbedded "
               "*d*\\ evelopment *i*\\ nfrastructure.\nBy itself edi does not do a lot.")
        stig = ("* stig\n  [[https://pypi.python.org/pypi/stig][file:https://img.shields.io/pypi/"
                "v/stig.svg]]\n\n  stig is a TUI (text user interface) and CLI (command line "
                "interface) client\n  for the [[http://www.transmissionbt.com/][BitTorrent client "
                "Transmission]].\n")
        libcgroup = ("[![Build Status](https://github.com/libcgroup/libcgroup/workflows/"
                     "Continuous%20Integration/badge.svg)](https://github.com/libcgroup/libcgroup/"
                     "actions)\n\nThe entire libcgroup README is available [here](README).")
        cases = (("TiCDC pulls change logs from TiDB clusters and pushes to downstream systems, "
                  "such as MySQL, TiDB, Kafka, Pulsar, and Object Storages (e.g., S3).", ticdc,
                  True),
                 ("This is the example project used for the SQL Fundamentals and Professional "
                  "SQL courses.", sql, True),
                 ("edi is a generic tool that helps you to streamline your embedded development "
                  "infrastructure.", edi, True),
                 ("stig is a TUI (text user interface) and CLI (command line interface) client "
                  "for the BitTorrent client Transmission.", stig, True),
                 ("Our tool reads the logs of every cluster and writes them out.",
                  "Our tool reads the change logs of every cluster and writes them out.", True),
                 ("Our tool reads the change logs of every cluster and quickly writes them out.",
                  "Our tool reads the change logs of every cluster and writes them out.", True),
                 ("Our tool reads the logs of cluster and writes out.",
                  "Our tool reads the change logs of every cluster and writes them out.", False),
                 ("libcgroup is a library that provides an interface to the Linux kernel control "
                  "groups (cgroups).", libcgroup, False))
        for quote, readme, held in cases:
            with self.subTest(quote=quote):
                self.assertIs(classify_repos.quoted(quote, "", readme), held)
        self.assertEqual(classify_repos.plain("[[https://x.example/]] and [[file:a.org][A]]"),
                         "https://x.example/ and a")

    def test_a_sampling_frame_without_githubs_ids_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "ghs_filtered.json"
        path.write_text(json.dumps({"_metadata": {"input_source": {
            "downloaded_at": "2026-03-16T18:07:01Z"}}, "items": [{"id": 5, "name": "o/r"}]}))
        with self.assertRaises(SystemExit) as stop:
            classify_repos.read_sampling_frame(path)
        self.assertIn("Its ids are GHS's, not GitHub's", str(stop.exception))
        items = [{"id": 5, "name": "o/r", "defaultBranch": "main", "lastCommitSHA": REVISION,
                  "createdAt": "2020-04-01T12:00:00Z", "releases": 2,
                  "lastCommit": "2026-09-01T08:00:00", "description": "  A formatter.  ",
                  "isMirror": True}]
        source = {"retrieval_id": "2026-10-05T101430Z", "github": {"found": 1},
                  "query": {"contributorsMin": "2"}}
        path.write_text(json.dumps({"_metadata": {"input_source": source}, "items": items}))
        with self.assertRaises(SystemExit) as stop:
            classify_repos.read_sampling_frame(path)
        self.assertIn("does not record the thresholds of the fork heuristic (the "
                      "contributorsMin of its query and its sampling window)", str(stop.exception))
        source["window"] = {"committed_min": "2026-03-22"}
        stages = [{"stage": 8, "key": "activity", "values": {"commits_min": 355.0,
                                                             "watchers_min": 5.0}}]
        path.write_text(json.dumps({"_metadata": {"input_source": source, "stages": stages},
                                    "items": items}))
        self.assertEqual(classify_repos.read_sampling_frame(path),
                         ([{"id": 5, "name": "o/r", "default_branch": "main",
                            "revision": REVISION, "created_at": "2020-04-01T12:00:00Z",
                            "activity": {"releases": 2, "created": "2020-04-01",
                                         "last commit": "2026-09-01"},
                            "description": "A formatter.", "is_mirror": True}],
                          "2026-10-05T101430Z", THRESHOLDS))
        for field, message in (("isMirror", "have no field for GitHub's mirror flag, isMirror"),
                               ("description", "have no field for GitHub's description")):
            del items[0][field]
            path.write_text(json.dumps({"_metadata": {"input_source": source, "stages": stages},
                                        "items": items}))
            with self.subTest(field=field), self.assertRaises(SystemExit) as stop:
                classify_repos.read_sampling_frame(path)
            self.assertIn(message, str(stop.exception))

    def test_the_sampling_frame_that_the_filter_writes_from_rows_without_an_id_is_read(self):
        """GitHub's GraphQL schema declares a repository's `databaseId` as a nullable `Int`,
        and a label is keyed by that id. Run as in the sampling stage, the filter writes no
        repository without an id into the sampling frame, even for two repositories created in
        the same second."""
        sampling = Path(__file__).resolve().parents[1] / "1_sampling"
        with mock.patch.object(sys, "path", [str(sampling), *sys.path]):
            loader = importlib.import_module("_script_loader")
            filter_ghs = loader.load_script("4_filter_ghs.py")
        fields = {"createdAt": "2014-01-01T10:00:00Z", "pushedAt": "2026-10-01T00:00:00Z",
                  "updatedAt": "2026-10-01T00:00:00Z", "isArchived": False,
                  "isDisabled": False, "isLocked": False, "isFork": False, "isPrivate": False,
                  "isMirror": False, "license": "MIT License", "primaryLanguage": "Python",
                  "stargazers": 10, "defaultBranch": "main", "description": "A tool.",
                  "headCommittedAt": "2026-10-01T00:00:00Z"}
        rows = []
        for ghs_id, (name, github_id) in enumerate((("o/a", 11), ("o/b", None), ("o/c", None)),
                                                   1):
            ghs = {"id": ghs_id, "name": name, "commits": 1000, "watchers": 100,
                   "totalPullRequests": 5, "totalIssues": 5, "topics": []}
            repository = {**fields, "id": github_id, "name": name, "headSha": name[-1] * 40}
            rows.append(filter_ghs.merge(ghs, {"ghsName": name, "status": "found",
                                               "repository": repository}))
        config = filter_ghs.FilterConfig(filter_licenses=False, filter_languages=False,
                                         filter_educational=False, commits_min=1,
                                         watchers_min=1)
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        kept, results = filter_ghs.run_steps(rows, config, set(), created_max="2025-04-12")
        retrieval = {"retrieval_id": "2026-10-12T104432Z", "started_at": "2026-10-12T10:44:32Z",
                     "end_date": "2026-10-12", "end_date_source": "ghs",
                     "created_max": "2025-04-12", "committed_min": "2026-04-12",
                     "query": json.dumps({"contributorsMin": 2}), "ghs_version": "1",
                     "github_started_at": "2026-10-12T11:00:00Z",
                     "github_finished_at": "2026-10-12T12:00:00Z", "github_found": "3",
                     "github_not_found": "0", "github_unresolved": "0"}
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "ghs_filtered.json"
        filter_ghs.save_output(path, filter_ghs.sampling_frame_metadata(
            retrieval, config, results, len(rows), len(kept)), kept)
        repositories, retrieval_id, thresholds = classify_repos.read_sampling_frame(path)
        self.assertEqual([(r["id"], r["name"]) for r in repositories], [(11, "o/a")])
        self.assertEqual((retrieval_id, thresholds),
                         ("2026-10-12T104432Z", forks.Thresholds(2, "2026-04-12")))

    def test_the_prompt_names_the_criteria_and_fields_of_the_response_format(self):
        """The prompt's criteria and its output fields are prose, and the response format is
        code. A criterion added to only one of them would get a field in the response without an
        instruction, or an instruction without a field in the response."""
        prompt = classify_repos.PROMPT.read_text(encoding="utf-8")
        step = prompt.split("## Step 1")[1].split("\n## ")[0]
        criteria = re.findall(r"^\* `([a-z_]+)`:", step, re.M)
        self.assertEqual(tuple(criteria), classify_repos.EXCLUSIONS)
        bullets = dict(zip(criteria, re.split(r"^\* `[a-z_]+`:", step, flags=re.M)[1:]))
        for key, facts in classify_repos.CRITERION_FACTS.items():
            for fact in facts:
                if fact != "applies":
                    with self.subTest(criterion=key, fact=fact):
                        self.assertIn(f"`{fact}`:", bullets[key])
        before = step.split("\n* ")[0]
        for field in classify_repos.MAINTENANCE_AND_USE:
            with self.subTest(field=field):
                self.assertIn(f"`{field}`:", before)
        output = prompt.split("## Output format")[1]
        fields = re.findall(r"^\* `([a-z_]+)`:", output, re.M)
        schema = classify_repos.CHECKLIST_SCHEMA
        self.assertEqual(fields, list(schema["properties"]))
        # The values that the prompt allows for `engineering`, which the response format has to
        # allow too.
        engineering = re.search(r"^\* `engineering`: (.+)$", output, re.M).group(1)
        self.assertEqual(schema["properties"]["engineering"]["enum"],
                         re.findall(r'`"([a-z]+)"`', engineering))

    def test_links_of_a_readme_inside_the_repository_and_out(self):
        text = ("[g](docs/guide.md) <https://a.example/x>, https://b.example/. [up](../X.md#y) "
                "[root](/LICENSE) <a href=\"https://c.example/\">c</a> [top](#top) "
                "[m](mailto:a@b.example) [esc](../../outside)")
        links = classify_repos.readme_links(text, "o/r", "docs/README.md", REVISION)
        self.assertEqual(sorted(links), [
            "https://a.example/x", "https://b.example/", "https://c.example/",
            f"https://github.com/o/r/blob/{REVISION}/LICENSE",
            f"https://github.com/o/r/blob/{REVISION}/X.md",
            f"https://github.com/o/r/blob/{REVISION}/docs/docs/guide.md"])
        self.assertEqual(classify_repos.resolve_link("../X.md", links, "o/r", "docs/README.md",
                                                     REVISION),
                         f"https://github.com/o/r/blob/{REVISION}/X.md")
        self.assertIsNone(classify_repos.resolve_link("../../outside", links, "o/r",
                                                      "docs/README.md", REVISION))
        self.assertIn("https://github.com/o/r/blob/HEAD/LICENSE",
                      classify_repos.readme_links(text, "o/r", "docs/README.md"))

    def test_an_issue_is_linked_in_any_form_of_its_address(self):
        """A README can link the issue that the model reads over http, with the owner and the
        name in another letter case, with a trailing slash, a query or a fragment, as a pull
        request with the same number, or relative to the README's page on GitHub, as
        "../../issues/98" of a README at the root does. Each link names the issue, and a link to
        another issue or to the issue of another repository does not."""
        url = "https://github.com/o/r/issues/98"
        for written, path in (("http://github.com/O/R/issues/98/", "README.md"),
                              ("https://www.github.com/o/r/issues/98?x=1#top", "README.md"),
                              ("https://github.com/o/r/pull/98", "README.md"),
                              ("../../issues/98", "README.md"),
                              ("../../../issues/98", "docs/README.md")):
            with self.subTest(written=written):
                links = classify_repos.readme_links(f"See [why]({written}).", "o/r", path,
                                                    REVISION)
                self.assertTrue(classify_repos.linked_page(url, links, "o/r"))
        for written in ("https://github.com/o/r/issues/99", "https://github.com/o/s/issues/98"):
            with self.subTest(written=written):
                links = classify_repos.readme_links(f"See [why]({written}).", "o/r",
                                                    "README.md", REVISION)
                self.assertFalse(classify_repos.linked_page(url, links, "o/r"))
        links = classify_repos.readme_links("See [why](../../issues/98).", "o/r", "README.md",
                                            REVISION)
        self.assertEqual(classify_repos.resolve_link("../../issues/98", links, "o/r",
                                                     "README.md", REVISION), url)

    def test_asciidoc_links_and_cross_references_are_links_of_the_readme(self):
        """oxidecomputer/omicron's README.adoc points to its contribution guide with "Please see
        xref:CONTRIBUTING.md[]". If only Markdown and HTML links counted, the model could not
        read that guide."""
        text = ("Please see xref:CONTRIBUTING.md[]. The link:docs/guide.adoc[guide] and "
                "link:https://x.example/doc[the site].")
        links = classify_repos.readme_links(text, "o/r", "README.adoc", REVISION)
        self.assertEqual(sorted(links), [
            "https://github.com/o/r/blob/" + REVISION + "/CONTRIBUTING.md",
            "https://github.com/o/r/blob/" + REVISION + "/docs/guide.adoc",
            "https://x.example/doc"])
        self.assertEqual(classify_repos.resolve_link("CONTRIBUTING.md", links, "o/r",
                                                     "README.adoc", REVISION),
                         "https://github.com/o/r/blob/" + REVISION + "/CONTRIBUTING.md")

    def test_restructuredtext_and_org_links_are_links_of_the_readme(self):
        """A README.rst writes a link as "`guide <CONTRIBUTING.rst>`_" or as a target
        definition, and an Org README writes "[[file:CONTRIBUTING.org][Contributing]]". Without
        these forms, read_linked_page would refuse such a link as no link of the README. The
        model may pass such a link with its markup too."""
        blob = f"https://github.com/o/r/blob/{REVISION}/"
        rst = ("See the `guide <CONTRIBUTING.rst>`_, the HISTORY_ and `Other <other_>`_.\n\n"
               ".. _HISTORY: HISTORY.rst\n.. _site: https://x.example/doc\n__ docs/install.rst\n"
               "See also <<NEWS.adoc#,the news>>.\n")
        links = classify_repos.readme_links(rst, "o/r", "README.rst", REVISION)
        self.assertEqual(sorted(links), [blob + "CONTRIBUTING.rst", blob + "HISTORY.rst",
                                         blob + "NEWS.adoc", blob + "docs/install.rst",
                                         "https://x.example/doc"])
        org = ("[[file:CONTRIBUTING.org][Contributing]], [[./docs/usage.org::*Usage][usage]], "
               "[[https://y.example/][the site]] and [[#local][a heading]]")
        links = classify_repos.readme_links(org, "o/r", "README.org", REVISION)
        self.assertEqual(sorted(links), [blob + "CONTRIBUTING.org", blob + "docs/usage.org",
                                         "https://y.example/"])
        for written in ("[[file:CONTRIBUTING.org][Contributing]]", "file:CONTRIBUTING.org",
                        "<CONTRIBUTING.org>", "link:CONTRIBUTING.org[Contributing]",
                        "link:./CONTRIBUTING.org", "CONTRIBUTING.org[Contributing]"):
            with self.subTest(written=written):
                self.assertEqual(classify_repos.resolve_link(written, links, "o/r", "README.org",
                                                             REVISION), blob + "CONTRIBUTING.org")

    def test_a_link_of_the_repository_at_another_ref_stands_for_the_readmes_link(self):
        """The model can pass a github.com link of the repository at a branch in place of a link
        of the README. The classification reads every file or directory of the repository at
        the README's commit, also for an absolute link of the README at a branch. GitHub returns
        404 for a file at a branch that was renamed, as for android/android-test's link to
        `blob/master/CONTRIBUTING.md` after master became main."""
        text = ("[contributing](CONTRIBUTING.md), https://github.com/o/r/tree/main/docs and "
                "https://github.com/o/r/blob/master/GUIDE.md")
        links = classify_repos.readme_links(text, "o/r", "README.md", REVISION)
        at_revision = f"https://github.com/o/r/blob/{REVISION}/"
        cases = {"https://github.com/o/r/blob/master/GUIDE.md": at_revision + "GUIDE.md",
                 "https://github.com/o/r/raw/master/GUIDE.md": at_revision + "GUIDE.md",
                 "https://github.com/o/r/blob/master/CONTRIBUTING.md":
                 at_revision + "CONTRIBUTING.md",
                 "https://github.com/O/R/tree/v2/CONTRIBUTING.md/": at_revision + "CONTRIBUTING.md",
                 "https://github.com/o/r/tree/main/docs": at_revision + "docs",
                 "https://github.com/o/r/blob/master/docs": at_revision + "docs",
                 "docs": at_revision + "docs",
                 "https://github.com/o/r/blob/master/SECRET.md": None,
                 "https://github.com/other/r/blob/master/CONTRIBUTING.md": None}
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, "o/r", "README.md",
                                                             REVISION), expected)
        self.assertEqual(classify_repos.resolve_link(
            "https://github.com/o/r/blob/master/GUIDE.md", links, "o/r", "README.md"),
            "https://github.com/o/r/blob/HEAD/GUIDE.md")

    def test_a_relative_link_to_a_file_at_a_branch_is_a_link_of_the_readme(self):
        """skuschel/postpic's README.md writes "[CONTRIBUTING.md](../master/CONTRIBUTING.md)", and
        GitHub renders it as /skuschel/postpic/blob/master/CONTRIBUTING.md. The link stands for
        that path at the README's commit, as a github.com link at a branch does (rule 5). A
        README in a directory climbs one level more to reach the same page."""
        name = "skuschel/postpic"
        at_revision = f"https://github.com/{name}/blob/{REVISION}/"
        text = ("Read [CONTRIBUTING.md](../master/CONTRIBUTING.md) and "
                "[g](../../blob/dev/docs/g.md).")
        links = classify_repos.readme_links(text, name, "README.md", REVISION)
        self.assertEqual(sorted(links), [f"https://github.com/{name}/blob/dev/docs/g.md",
                                         f"https://github.com/{name}/blob/master/CONTRIBUTING.md"])
        cases = {"../master/CONTRIBUTING.md": at_revision + "CONTRIBUTING.md",
                 f"https://github.com/{name}/blob/master/CONTRIBUTING.md":
                 at_revision + "CONTRIBUTING.md",
                 "CONTRIBUTING.md": at_revision + "CONTRIBUTING.md",
                 "../../blob/dev/docs/g.md": at_revision + "docs/g.md",
                 "../master/SECRET.md": None, "SECRET.md": None}
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, name, "README.md",
                                                             REVISION), expected)
        nested = classify_repos.readme_links("[c](../../master/CONTRIBUTING.md)", name,
                                             "docs/README.md", REVISION)
        self.assertEqual(nested, {f"https://github.com/{name}/blob/master/CONTRIBUTING.md"})
        self.assertEqual(classify_repos.readme_links("[c](../master/CONTRIBUTING.md)", name,
                                                     "README.md"),
                         {f"https://github.com/{name}/blob/master/CONTRIBUTING.md"})
        self.assertEqual(classify_repos.resolve_link(
            "../master/CONTRIBUTING.md", {f"https://github.com/{name}/blob/master/CONTRIBUTING.md"},
            name, "README.md"), f"https://github.com/{name}/blob/HEAD/CONTRIBUTING.md")

    def test_a_relative_link_out_of_the_repository_names_the_page_that_github_renders(self):
        """FasterXML/jackson-core's README.md writes "../../../jackson#participation-contributing",
        "../../../jackson-1" and "../../wiki", and GitHub renders them as
        /FasterXML/jackson#participation-contributing, /FasterXML/jackson-1 and
        /FasterXML/jackson-core/wiki. The model may pass the link as the README writes it or as
        the address of the page."""
        name = "FasterXML/jackson-core"
        text = ("See [Participation, Contributing](../../../jackson#participation-contributing), "
                "[Jackson-1](../../../jackson-1) and the [Wiki](../../wiki).")
        jackson = "https://github.com/FasterXML/jackson"
        links = classify_repos.readme_links(text, name, "README.md", REVISION)
        self.assertEqual(sorted(links), [jackson, jackson + "-1", jackson + "-core/wiki"])
        cases = {"../../../jackson#participation-contributing": jackson,
                 "../../../jackson": jackson,
                 jackson: jackson,
                 jackson + "#participation-contributing": jackson,
                 "../../../jackson-1": jackson + "-1",
                 "../../wiki": jackson + "-core/wiki"}
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, name, "README.md",
                                                             REVISION), expected)
        nested = classify_repos.readme_links("[j](../../../../jackson)", name, "docs/README.md",
                                             REVISION)
        self.assertEqual(nested, {jackson})
        self.assertEqual(classify_repos.readme_links("[o](../../../../other/repo)", name,
                                                     "README.md", REVISION),
                         {"https://github.com/other/repo"})
        self.assertEqual(classify_repos.readme_links("[r](../..)", name, "README.md", REVISION),
                         {jackson + "-core"})

    def test_a_relative_link_that_names_no_page_is_no_link_of_the_readme(self):
        """A relative link above GitHub's root, a branch without a path and a path from the
        repository's root that leaves it name no page that the classification can check. A
        path from the repository's root stays there, because `resolve_link` writes the path of a
        github.com link of the repository in that form, and such a path must not reach another
        repository."""
        for written in ("../../../../..", "../../../../../other/repo", "../", "../master",
                        "/../master/CONTRIBUTING.md", "/../../../other", "#top", "."):
            with self.subTest(written=written):
                self.assertEqual(classify_repos.readme_links(f"[l]({written})", "o/r", "README.md",
                                                             REVISION), set())
        for written in ("..file.md", "/..file.md"):
            with self.subTest(written=written):
                self.assertEqual(classify_repos.readme_links(f"[f]({written})", "o/r", "README.md",
                                                             REVISION),
                                 {f"https://github.com/o/r/blob/{REVISION}/..file.md"})

    def test_an_address_without_its_trailing_slash_stands_for_the_readmes_address(self):
        """tfranzel/drf-spectacular's README.rst writes
        "`documentation <https://drf-spectacular.readthedocs.io/>`_". The model can pass the
        address without its trailing slash, and the tool reads the address that the README
        writes. The same holds for a homepage, which GitHub keeps as its owner wrote it, also
        without a scheme."""
        name = "tfranzel/drf-spectacular"
        site = "https://drf-spectacular.readthedocs.io/"
        text = (f"For more information visit the `documentation <{site}>`_.\n"
                "See https://x.example/docs and https://y.example/guide/.")
        links = classify_repos.readme_links(text, name, "README.rst", REVISION)
        self.assertIn(site, links)
        links |= classify_repos.described_links("", [
            {"tool": "get_repository_metadata",
             "output": json.dumps({"homepage": "https://home.example/"})}])
        cases = {site: site, site.rstrip("/"): site, "HTTPS://DRF-Spectacular.ReadTheDocs.io": site,
                 "drf-spectacular.readthedocs.io": site, "drf-spectacular.readthedocs.io/": site,
                 "https://x.example/docs/": "https://x.example/docs",
                 "https://y.example/guide": "https://y.example/guide/",
                 "https://home.example": "https://home.example/",
                 "home.example": "https://home.example/"}
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, name, "README.rst",
                                                             REVISION), expected)

    def test_an_address_without_its_fragment_stands_for_the_readmes_address(self):
        """OhMyGuus/BetterCrewLink's README.md links
        https://github.com/OhMyGuus/BetterCrewlink-mobile only with fragments, such as #web and
        #ios--macos. A request does not contain the fragment, so that the address without it, or
        with another fragment, names the same page. The tool reads an address that the README
        writes, the first in sorted order, so that every run makes the same request and records
        the same address."""
        name = "OhMyGuus/BetterCrewLink"
        mobile = "https://github.com/OhMyGuus/BetterCrewlink-mobile"
        text = (f"[iOS]({mobile}#ios--macos), [Switch]({mobile}#web), "
                f"[Installation]({mobile}#android--chromebook).\n"
                "[a](https://x.example/a) [b](https://y.example/b#section)")
        links = classify_repos.readme_links(text, name, "README.md", REVISION)
        cases = {mobile: mobile + "#android--chromebook",
                 mobile + "/": mobile + "#android--chromebook",
                 mobile + "#web": mobile + "#web",
                 mobile + "#another": mobile + "#android--chromebook",
                 "https://x.example/a#section": "https://x.example/a",
                 "https://y.example/b": "https://y.example/b#section"}
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, name, "README.md",
                                                             REVISION), expected)

    def test_a_restructuredtext_reference_names_the_target_of_its_definition(self):
        """pism/pism's README.rst writes "`Contributing to PISM <pism-contributing_>`_" and
        ".. _pism-contributing: http://www.pism.io/docs/contributing", and GitHub renders the
        reference as a link to that address. The model may pass the reference with or without
        its text or the definition whole. reStructuredText matches a name regardless of its case
        and its whitespace, as GitHub renders "MPI_" of the same README as a link to the target
        of ".. _mpi:". The first definition of a name counts, and the target of a later one stays
        a link of the README. A target that is a mail address reads as no link, as in the
        definition of uaf-pism, and so does the indirect target of the definition of indirect.
        The text of a reference may contain angle brackets, as tfranzel/drf-spectacular's
        README.rst writes "`Pydantic (>=2.0) <https://github.com/pydantic/pydantic>`_", which
        GitHub renders as a link to that address."""
        name = "pism/pism"
        contributing = "http://www.pism.io/docs/contributing"
        mpi = "http://www.mcs.anl.gov/research/projects/mpi/"
        blob = f"https://github.com/{name}/blob/{REVISION}/"
        for line_end in ("\n", "\r\n"):
            text = line_end.join((
                "See `Contributing to PISM <pism-contributing_>`_, MPI_ and the HISTORY_.",
                "See `the notes (>=2.0) <docs/notes.rst>`_.", "",
                f".. _pism-contributing: {contributing}",
                f".. _mpi: {mpi}",
                ".. _Slack-PISM: https://uaf-pism.slack.com/join",
                ".. _`two words`: docs/two.rst",
                ".. _HISTORY: HISTORY.rst",
                ".. _history: OTHER.rst",
                ".. _uaf-pism: mailto:uaf-pism@alaska.edu",
                ".. __: docs/anonymous.rst",
                "__ docs/short.rst",
                ".. _indirect: pism-contributing_", ""))
            links = classify_repos.readme_links(text, name, "README.rst", REVISION)
            definitions = classify_repos.reference_definitions(text, name, "README.rst", REVISION)
            self.assertEqual(definitions, {"pism-contributing": contributing, "mpi": mpi,
                                           "slack-pism": "https://uaf-pism.slack.com/join",
                                           "two words": blob + "docs/two.rst",
                                           "history": blob + "HISTORY.rst"})
            self.assertEqual(sorted(links), sorted([
                contributing, mpi, "https://uaf-pism.slack.com/join", blob + "docs/two.rst",
                blob + "HISTORY.rst", blob + "OTHER.rst", blob + "docs/anonymous.rst",
                blob + "docs/short.rst", blob + "docs/notes.rst"]))
            cases = {"pism-contributing_": contributing,
                     "MPI_": mpi,
                     "<pism-contributing_>": contributing,
                     "`Contributing to PISM <pism-contributing_>`_": contributing,
                     f".. _pism-contributing: {contributing}": contributing,
                     contributing: contributing,
                     "slack-pism_": "https://uaf-pism.slack.com/join",
                     "`two   Words`_": blob + "docs/two.rst",
                     "HISTORY_": blob + "HISTORY.rst",
                     "`the history <HISTORY.rst>`_": blob + "HISTORY.rst",
                     "`the history <HISTORY.rst>`__": blob + "HISTORY.rst",
                     "`the history (>=2.0) <HISTORY.rst>`_": blob + "HISTORY.rst",
                     "`the notes (>=2.0) <docs/notes.rst>`_": blob + "docs/notes.rst",
                     "docs/notes.rst": blob + "docs/notes.rst",
                     "uaf-pism_": None, "unknown_": None, "__": None, "indirect_": None,
                     "`Contributing to PISM`_": None, "`text <unknown_>`_": None,
                     "`text <https://elsewhere.example/>`_": None,
                     ".. _pism-contributing: https://elsewhere.example/": None}
            for link, expected in cases.items():
                with self.subTest(link=link, line_end=line_end):
                    self.assertEqual(classify_repos.resolve_link(
                        link, links, name, "README.rst", REVISION, definitions), expected)

    def test_a_restructuredtext_definition_can_write_its_target_on_the_next_line(self):
        """celery/celery's README.rst writes the target of ".. _`Next steps`:" indented on the
        next line, and GitHub renders "`Next steps`_" as a link to it. The first definition of a
        name counts, and the target of a later one stays a link of the README. A line without
        indentation after ".. _section:" is the text of the document, so that this definition
        names no link, and neither does a chained target or an indirect target."""
        name = "celery/celery"
        next_steps = "https://docs.celeryq.dev/en/stable/getting-started/next-steps.html"
        blob = f"https://github.com/{name}/blob/{REVISION}/"
        for line_end in ("\n", "\r\n"):
            text = line_end.join(("See `Next steps`_ and guide_.", "",
                                  ".. _`Next steps`:", f"    {next_steps}", "",
                                  ".. _guide:", "   docs/guide.rst", "",
                                  ".. _GUIDE:", "   docs/second.rst", "",
                                  ".. _section:", "Section", "",
                                  ".. _chained:", ".. _after: docs/after.rst", "",
                                  ".. _indirect:", "   guide_", "", "Text", ""))
            links = classify_repos.readme_links(text, name, "README.rst", REVISION)
            definitions = classify_repos.reference_definitions(text, name, "README.rst", REVISION)
            self.assertEqual(definitions, {"next steps": next_steps,
                                           "guide": blob + "docs/guide.rst",
                                           "after": blob + "docs/after.rst"})
            self.assertEqual(sorted(links), sorted([next_steps, blob + "docs/guide.rst",
                                                    blob + "docs/second.rst",
                                                    blob + "docs/after.rst"]))
            cases = {"`Next steps`_": next_steps, "guide_": blob + "docs/guide.rst",
                     "docs/guide.rst": blob + "docs/guide.rst",
                     "docs/second.rst": blob + "docs/second.rst",
                     f".. _guide:{line_end}   docs/guide.rst": blob + "docs/guide.rst",
                     "after_": blob + "docs/after.rst", "section_": None, "chained_": None,
                     "indirect_": None}
            for link, expected in cases.items():
                with self.subTest(link=link, line_end=line_end):
                    self.assertEqual(classify_repos.resolve_link(
                        link, links, name, "README.rst", REVISION, definitions), expected)

    def test_a_file_that_the_readme_links_comes_before_a_host_of_that_name(self):
        """MrBear2-Rus/Bear2Wave's README links "(CONTRIBUTING.md)" and
        "(https://contributing.md/)", and GitHub renders the first as the repository's file. A
        link without a scheme reads as a host only in the letter case of the address, so that a
        file that the README only mentions does not name that site. A github.com address without
        its scheme names the path at the README's commit, as with its scheme (rule 5)."""
        blob = f"https://github.com/o/r/blob/{REVISION}/"
        cases = (("[c](CONTRIBUTING.md) https://contributing.md/", "CONTRIBUTING.md",
                  blob + "CONTRIBUTING.md"),
                 ("[c](CONTRIBUTING.md) https://contributing.md/", "contributing.md",
                  "https://contributing.md/"),
                 ("[x](https://contributing.md/)", "CONTRIBUTING.md", None),
                 ("[x](https://contributing.md/)", "contributing.md", "https://contributing.md/"),
                 ("[c](CONTRIBUTING.md) https://CONTRIBUTING.md", "CONTRIBUTING.md",
                  blob + "CONTRIBUTING.md"),
                 ("[c](lib.rs) [s](https://lib.rs/)", "lib.rs", blob + "lib.rs"),
                 ("[g](https://github.com/o/r/blob/main/GUIDE.md)",
                  "github.com/o/r/blob/main/GUIDE.md", blob + "GUIDE.md"),
                 ("[g](https://github.com/o/r/blob/main/GUIDE.md)",
                  "github.com/o/r/blob/main/GUIDE.md/", blob + "GUIDE.md"))
        for text, link, expected in cases:
            with self.subTest(text=text, link=link):
                links = classify_repos.readme_links(text, "o/r", "README.md", REVISION)
                self.assertEqual(classify_repos.resolve_link(link, links, "o/r", "README.md",
                                                             REVISION), expected)

    def test_an_address_with_an_empty_query_stands_for_the_readmes_address(self):
        """common-workflow-language/cwlviewer's README writes
        "<https://doi.org/10.7490/f1000research.1114375.1?>", and GitHub renders a link with the
        "?". `readme_links` reads the address without the "?", and the model may pass it as the
        README writes it. An empty query does not name a page with another query."""
        page = "https://doi.org/10.7490/x.1"
        links = classify_repos.readme_links(f"<{page}?> https://q.example/a?x=1", "o/r",
                                            "README.md", REVISION)
        self.assertEqual(classify_repos.resolve_link(f"{page}?", links, "o/r", "README.md",
                                                     REVISION), page)
        self.assertIsNone(classify_repos.resolve_link("https://q.example/a?", links, "o/r",
                                                      "README.md", REVISION))

    def test_a_link_from_the_model_reaches_only_what_the_readme_links(self):
        """read_linked_page fetches the address that `resolve_link` returns. A link that names
        another host, another repository or another page than the README's links is refused,
        whatever its form, and every address that the function returns is one that the README
        writes or the file of the repository at the README's commit."""
        name = "o/r"
        text = ("[site](https://site.example/docs/) [up](../../../other#part) "
                "[branch](../main/GUIDE.md) [query](https://q.example/a?x=1)\n\n"
                ".. _target: https://t.example/page\n")
        links = classify_repos.readme_links(text, name, "README.rst", REVISION)
        definitions = classify_repos.reference_definitions(text, name, "README.rst", REVISION)
        refused = (
            # Another host, in forms that a parser can read differently.
            "https://elsewhere.example/docs/", "https://site.example.elsewhere.example/docs",
            "https://site.example@elsewhere.example/docs", "https://elsewhere.example/#site",
            "https://elsewhere.example/site.example/docs", "https://site.example:8443/docs",
            "https://elsewhere.example\\@site.example/docs", "//elsewhere.example/docs",
            "elsewhere.example", "//site.example.elsewhere.example/docs",
            # Another scheme, and no scheme of the web.
            "http://site.example/docs/", "ftp://site.example/docs/", "file:///etc/passwd",
            # Another page of the host: another path, a parent, a dot segment, another query.
            "https://site.example/", "https://site.example", "https://site.example/docs/other",
            "https://site.example/DOCS/", "https://site.example/docs/../secret",
            "https://site.example/docs?x=1", "https://q.example/a", "https://q.example/a?x=2",
            # Another repository, by a relative link or by its address.
            "../../../secret", "../../../../someone/else", "../../../other/../secret",
            "https://github.com/o/secret", "https://github.com/o/other/blob/main/x.md",
            "https://github.com/someone/r", "../../../other/issues/3",
            "https://github.com/o/r/blob/main/../../../secret",
            "https://github.com/o/r/blob/main/../../../other", "/../../../other",
            # An encoded or a backslashed climb, and whitespace inside the link.
            "..%2f..%2f..%2fother", "..\\..\\..\\other", "https://site.example/docs/ x",
            "https://site.example/docs/\nhttps://elsewhere.example/",
            # Another file of the repository, also through a relative link to a branch.
            "../main/SECRET.md", "https://github.com/o/r/blob/main/SECRET.md",
            # A definition whole with another target, and a name with no definition.
            ".. _target: https://elsewhere.example/", "`text <https://elsewhere.example/>`_",
            "elsewhere_", "[target]: https://elsewhere.example/")
        for link in refused:
            with self.subTest(link=link):
                self.assertIsNone(classify_repos.resolve_link(link, links, name, "README.rst",
                                                              REVISION, definitions))
        accepted = {"https://site.example/docs": "https://site.example/docs/",
                    "../../../other": "https://github.com/o/other",
                    "https://github.com/o/other#part": "https://github.com/o/other",
                    "../main/GUIDE.md": f"https://github.com/o/r/blob/{REVISION}/GUIDE.md",
                    "https://q.example/a?x=1#part": "https://q.example/a?x=1",
                    "target_": "https://t.example/page",
                    "//site.example/docs": "https://site.example/docs/",
                    "../../../other?x=1": "https://github.com/o/other"}
        for link, expected in accepted.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, name, "README.rst",
                                                             REVISION, definitions), expected)

    def test_link_reference_definitions_are_links_of_the_readme(self):
        """A Markdown README can define its links apart from the text, as herumi/xbyak's
        readme.md writes "[Install]: doc/install.md" and goldmansachs/reladomo's
        "[contrib]:CONTRIBUTING.md" with CRLF line ends. The model may pass the target, the
        definition whole or the label as a reference. A footnote, an anchor and a line of an
        indented code block define no link."""
        blob = f"https://github.com/o/r/blob/{REVISION}/"
        text = ("See the [install guide][Install], [usage] and [Contributing].\n\n"
                "[Install]: doc/install.md\n"
                "  [usage]:  <doc/usage.md>  \"Usage\"\n"
                "[contributing]:\n   /CONTRIBUTING.md 'How to contribute'\n"
                "[contributing]: OTHER.md\n"
                "[contrib]:CHANGELOG.md\r\n"
                "[^1]: NOTES.md\n"
                "[//]: # (a comment)\n"
                "    [code]: not/a/definition.md\n")
        links = classify_repos.readme_links(text, "o/r", "docs/README.md", REVISION)
        self.assertEqual(sorted(links), [blob + "CONTRIBUTING.md", blob + "docs/CHANGELOG.md",
                                         blob + "docs/doc/install.md",
                                         blob + "docs/doc/usage.md"])
        definitions = classify_repos.reference_definitions(text, "o/r", "docs/README.md",
                                                           REVISION)
        self.assertEqual(definitions, {"install": blob + "docs/doc/install.md",
                                       "usage": blob + "docs/doc/usage.md",
                                       "contributing": blob + "CONTRIBUTING.md",
                                       "contrib": blob + "docs/CHANGELOG.md"})
        cases = {"doc/install.md": blob + "docs/doc/install.md",
                 "[Install]": blob + "docs/doc/install.md",
                 "[install guide][Install]": blob + "docs/doc/install.md",
                 "[usage][]": blob + "docs/doc/usage.md",
                 "[ Contributing ]": blob + "CONTRIBUTING.md",
                 "[Install]: doc/install.md": blob + "docs/doc/install.md",
                 "[contrib]": blob + "docs/CHANGELOG.md",
                 "[unknown]": None, "[^1]": None, "NOTES.md": None}
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, "o/r", "docs/README.md",
                                                             REVISION, definitions), expected)

    def test_a_web_address_with_balanced_parentheses_is_a_link_of_the_readme(self):
        """lambdaloop/anipose and PyLabRobot/pylabrobot link their papers at cell.com, with
        addresses that contain balanced parentheses, inline and in angle brackets."""
        anipose = "https://www.cell.com/cell-reports/fulltext/S2211-1247(21)01179-7"
        pylabrobot = "https://www.cell.com/device/fulltext/S2666-9986(23)00170-9"
        wiki = "https://en.wikipedia.org/wiki/Library_(computing)"
        text = (f"Check out the [Anipose paper]({anipose}) for more information. Read [the "
                f"paper](<{pylabrobot}>) in Device. A library ({wiki}).")
        links = classify_repos.readme_links(text, "o/r", "README.md", REVISION)
        self.assertEqual(sorted(links), sorted([anipose, pylabrobot, wiki]))
        for link in (anipose, pylabrobot, wiki):
            with self.subTest(link=link):
                self.assertEqual(classify_repos.resolve_link(link, links, "o/r", "README.md",
                                                             REVISION), link)

    def test_the_longest_readme_of_the_calibration_sample_is_not_cut(self):
        """Rule 1: the classification cuts nothing while the request stays under 272,000
        tokens."""
        text = "x" * 406_750
        self.assertEqual(classify_repos.readme_input("o/a", "README.md", text, 20_000, "A tool.",
                                                     {"releases": 3}),
                         f'<repository>o/a</repository>\n<description>A tool.</description>\n'
                         f'<activity>releases: 3</activity>\n'
                         f'<readme path="README.md">\n{text}\n</readme>')

    def test_a_readme_over_the_request_is_cut_and_ends_with_a_note(self):
        available = int(272_000 * 3.57) - 20_000
        text = classify_repos.readme_input("o/a", "README.md", "x" * available, 20_000)
        self.assertEqual(len(text), available)
        self.assertTrue(text.startswith('<repository>o/a</repository>\n'
                                        '<description>GitHub shows no description for this '
                                        'repository.</description>\n<activity>The sampling '
                                        'frame records no activity for this repository.'
                                        '</activity>\n<readme path="README.md">\nxxx'))
        self.assertTrue(text.endswith("xx\n[The README is cut here, so that the request stays "
                                      "within its size limit.]\n</readme>"))


PARAMETER_DIGESTS = (Path(classify_repos.__file__).resolve().parent / "config"
                     / "parameter_digests.csv")


class TestTheReleaseDefinesEachCriterion(unittest.TestCase):
    """The published justification gives the key of each criterion that decided a label
    (`published_justification`), and the release README defines each key
    (`release.CRITERION_MEANINGS`). A criterion added to `classify_repos.CRITERIA` without an entry
    in `release.CRITERION_MEANINGS` would appear in the published justification as a key that the
    release README does not define."""

    def test_every_criterion_has_a_meaning_in_the_release_readme(self):
        from agentcontext.steps import release
        self.assertEqual(set(classify_repos.CRITERIA), set(release.CRITERION_MEANINGS))

    def test_the_release_reads_the_criteria_that_each_verdict_names(self):
        """The release refuses a published verdict that names a criterion that its README does
        not define, and it reads the keys out of the sentence that `published_justification`
        writes, for one criterion, for several and for unclear ones."""
        from agentcontext.steps import release
        for applied, unclear in ((["inactive"], []), (["inactive", "mirror"], []),
                                 (["inactive", "moved", "collection"], []),
                                 ([], ["work_in_progress"]), ([], ["inactive", "no_purpose"]),
                                 ([], [])):
            decision = {"label": "false" if applied else "unclear" if unclear else "true",
                        "applied_criteria": applied, "unclear_criteria": unclear,
                        "justification": "The README says that the project is in maintenance "
                                         "mode, and the criteria apply."}
            with self.subTest(applied=applied, unclear=unclear):
                self.assertEqual(release.verdict_criteria(
                    classify_repos.published_justification(decision)), applied or unclear)


class TestTheRecordedParameters(unittest.TestCase):
    """`config/parameter_digests.csv` records each digest of the classification parameters, in
    order, with the commits that made it and what changed. A run reuses a label only under the
    digest that it was made under, so a run under a new digest labels every repository in the
    sampling frame again."""

    def test_the_parameters_of_the_code_are_the_last_recorded_digest(self):
        """The digest comes from the tool definitions and the output schemas that the real MCP
        server lists, which the bridge starts as a run starts it (`open_bridge`), with the
        defaults of a run. The server reads no credentials, and no tool is called."""
        args = classify_repos.build_parser().parse_args(["--credentials", os.devnull])

        async def digest():
            async with classify_repos.open_bridge(args) as bridge:
                return label_cache.digest(classify_repos.run_parameters(
                    args, classify_repos.PROMPT.read_text(encoding="utf-8"),
                    bridge.function_tools, bridge.output_schemas))
        current = asyncio.run(digest())
        with open(PARAMETER_DIGESTS, newline="", encoding="utf-8") as f:
            recorded = list(csv.DictReader(f))
        self.assertEqual(current, recorded[-1]["digest"],
                         f"The classification parameters of the code have the digest {current}, "
                         f"and the last entry of {PARAMETER_DIGESTS.name} is "
                         f"{recorded[-1]['digest']}. A change of the classification parameters "
                         f"labels every repository in the sampling frame again and pays the model "
                         f"for each. If the change is intended, add a row with the digest, the "
                         f"commits that made it and what changed.")

    # The digests that config/parameter_digests.csv records for the parameters with the provider
    # morph/fp8 of each model: those of a run with the defaults, of a run with
    # --endpoint-default-temperature and of a run of GLM-5.3. The run of the NAS of 2026-11-03
    # labels under the first.
    WITH_PROVIDER = {(): "95c7ac80cbecb584b2a140355a7ed6dbc3661c166986258facc44acd58c2dc4f",
                     ("--endpoint-default-temperature",):
                         "50d38fc7a8bc0e8328f4769b68789f1f683d1321db9d2ea3c88f7d63b4b8e876",
                     ("--model", "z-ai/glm-5.3"):
                         "5c4bdfbdf27536c26e92c8de59e2369fd2fe53952740c85e0acf3f4e2739d982"}
    # What the prompt gained after those parameters, on the maintainer's decision of 2026-11-06.
    PROMPT_ADDITIONS = (" maintained only as a courtesy to users who have not migrated,",
                        " A pinned issue of a maintainer that sends all issues or contributions "
                        "to the repository on another host describes the repository as a mirror "
                        "of that repository.",
                        " A README that calls the repository an implementation of a paper "
                        "describes the software that the paper presents, also when the project "
                        "plans real use.")
    # The digest of the code that resolved a link from the model under those parameters.
    EARLIER_LINK_RESOLUTION = "c8b9ff44b30d4808cb4776d7c6d36ef189489265850652cc55382463cd88d0ef"

    def test_the_parameters_recorded_with_the_provider_have_the_key_of_those_without_it(self):
        """The parameters of the code with the prompt before the additions of 2026-11-06, with
        the digest of the code that resolved a link before and with the provider morph/fp8 put
        back into the blocks of the classifying model and the summary model are the parameters
        that the classification recorded before 2026-11-04, and their key in a cache of labels is
        the digest of the same parameters without the provider (`label_cache.ParameterKeys`). The
        additions and the code that resolves a link are the only changes of the parameters since
        then, and a run under them labels every repository again."""
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        prompt = classify_repos.PROMPT.read_text(encoding="utf-8")
        for addition in self.PROMPT_ADDITIONS:
            self.assertEqual(prompt.count(addition), 1, addition)
            prompt = prompt.replace(addition, "")

        async def parameters():
            args = classify_repos.build_parser().parse_args(["--credentials", os.devnull])
            async with classify_repos.open_bridge(args) as bridge:
                return {options: classify_repos.run_parameters(
                    classify_repos.build_parser().parse_args(["--credentials", os.devnull,
                                                              *options]),
                    prompt, bridge.function_tools,
                    bridge.output_schemas) for options in self.WITH_PROVIDER}
        for options, value in asyncio.run(parameters()).items():
            with self.subTest(options=options):
                self.assertNotIn("provider", value["classifying_model"])
                self.assertNotIn("provider", value["summary"])
                without = {**value, "pages": {**value["pages"], "link_resolution":
                                              self.EARLIER_LINK_RESOLUTION}}
                earlier = {**without, "classifying_model": {**value["classifying_model"],
                                                            "provider": "morph/fp8"},
                           "summary": {**value["summary"], "provider": "morph/fp8"}}
                sha = label_cache.record("parameters", earlier, cache)
                self.assertEqual(sha, self.WITH_PROVIDER[options])
                self.assertEqual(label_cache.ParameterKeys(cache)(sha),
                                 label_cache.digest(without))

    def test_every_entry_names_its_digest_commits_and_change_once(self):
        with open(PARAMETER_DIGESTS, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            recorded = list(reader)
        self.assertEqual(reader.fieldnames, ["digest", "commits", "change"])
        self.assertTrue(recorded)
        for row in recorded:
            with self.subTest(digest=row["digest"]):
                self.assertRegex(row["digest"], r"^[0-9a-f]{64}$")
                self.assertRegex(row["commits"], r"^[0-9a-f]{8,40}( [0-9a-f]{8,40})*$")
                self.assertTrue(row["change"].strip())
        self.assertEqual(len({row["digest"] for row in recorded}), len(recorded))


def _contained(sentence: str, text: str) -> bool:
    """Another check of a page's purpose sentence, for the test of the digest."""
    return sentence in text


def _no_repository(*arguments) -> str:
    """A replacement that returns an empty string for any arguments, for the tests of the digest."""
    return ""


class TestWhatTheParametersRecord(unittest.TestCase):
    """The classification parameters record everything that the model reads besides the
    repository's name, description, activity and README, so that a label is reused only for the
    same input. The parameters here come from the tools that the real MCP server lists, read in
    process."""

    def setUp(self):
        self.listed = asyncio.run(tools.build_server().list_tools())

    def parameters(self, output_schemas=None, options=()):
        """The classification parameters of the code with the defaults of a run and the command
        line `options`, offering the model the tools that a run offers it."""
        offered = [mcp_bridge.function_tool(tool, classify_repos.SUPPLIED.get(tool.name, ()))
                   for tool in self.listed if tool.name in classify_repos.OFFERED]
        if output_schemas is None:
            output_schemas = {tool.name: tool.output_schema for tool in self.listed}
        args = classify_repos.build_parser().parse_args(["--credentials", os.devnull,
                                                         *options])
        return classify_repos.run_parameters(
            args, classify_repos.PROMPT.read_text(encoding="utf-8"), offered, output_schemas)

    def digest(self, output_schemas=None, options=()):
        return label_cache.digest(self.parameters(output_schemas, options))

    def test_the_digest_follows_the_model_its_weights_and_its_reasoning_effort(self):
        """An open-weight model has fixed weights that a Hugging Face id names, and its providers
        serve it in different quantizations, so a label is reused only from the same model,
        weights, quantization, reasoning effort, temperature, top_p and seed. The provider that
        serves the weights is no classification parameter, by the maintainer's decision of
        2026-11-04, and another provider of the same weights in the same quantization has the
        same parameters. The default model with its pinned endpoint named has the parameters of
        the default model named alone. An OpenAI model records neither a temperature, a top_p nor
        a seed. The service tier sets the price only."""
        deepseek = ["--model", "deepseek/deepseek-v4.1-flash", "--provider", "morph/fp8",
                    "--quantization", "fp8", "--hugging-face-id",
                    "deepseek-ai/DeepSeek-V4.1-Flash"]
        current = self.digest(options=deepseek)
        self.assertEqual(current, self.digest())
        self.assertEqual(self.parameters(options=deepseek)["classifying_model"],
                         {"model": "deepseek/deepseek-v4.1-flash",
                          "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash",
                          "quantization": "fp8",
                          "reasoning_effort": "high", "temperature": 0.6, "top_p": 0.95,
                          "seed": 7, "max_output_tokens": 65_536})
        # An OpenAI model's requests send no temperature, top_p, seed or output limit.
        self.assertEqual(self.parameters(options=["--model", "gpt-6-luna"])["classifying_model"],
                         {"model": "gpt-6-luna", "reasoning_effort": "medium"})
        # GLM-5.3 labels the evaluation sample with the settings of its own record.
        self.assertEqual(self.parameters(options=["--model", "z-ai/glm-5.3"])["classifying_model"],
                         {"model": "z-ai/glm-5.3", "hugging_face_id": "zai-org/GLM-5.3",
                          "quantization": "fp8",
                          "reasoning_effort": "high", "temperature": 1.0, "top_p": 0.95,
                          "seed": 7, "max_output_tokens": 262_144})
        for provider in ("wafer/us", "deepinfra/fp8"):
            with self.subTest(provider=provider):
                changed = list(deepseek)
                changed[changed.index("--provider") + 1] = provider
                self.assertEqual(self.digest(options=changed), current)
        for option, value in (("--model", "z-ai/glm-5.3-flash"),
                              ("--quantization", "mxfp4"),
                              ("--hugging-face-id", "deepseek-ai/DeepSeek-V4.1-Flash-0911"),
                              ("--reasoning-effort", "low"), ("--temperature", "0.3"),
                              ("--top-p", "1.0"), ("--seed", "8"),
                              ("--endpoint-default-temperature", None)):
            with self.subTest(option=option):
                changed = list(deepseek)
                if option in changed:
                    changed[changed.index(option) + 1] = value
                else:
                    changed += [option, *([value] if value is not None else [])]
                self.assertNotEqual(self.digest(options=changed), current)
        self.assertNotEqual(self.digest(options=["--model", "gpt-6.1-sol"]), self.digest())
        self.assertEqual(self.digest(options=["--openai-service-tier", "default"]), self.digest())

    def test_the_parameters_record_the_output_limit_and_not_the_labels_after_a_failure(self):
        """The output limit decides labels. The higher temperature and the second model decide only
        the labels after a failed response of the classifying model, which record them
        (`Run.applies`), so that a run without the second model, such as a check of one model, and a
        run with other settings of either label the rest under the same parameters. A run at the
        endpoint's defaults records neither a temperature, a top_p nor a seed."""
        current = self.digest()
        production = classify_repos.endpoints.PRODUCTION
        for changed, same in (({"max_output_tokens": 131_072}, False),
                              ({"higher_temperature": 1.2}, True)):
            with self.subTest(changed=changed):
                other = dataclasses.replace(production, settings=dataclasses.replace(
                    production.settings, **changed))
                with mock.patch.dict(classify_repos.endpoints.PINNED,
                                     {production.model: other}):
                    self.assertEqual(self.digest() == current, same)
        glm = classify_repos.endpoints.SECOND_MODEL
        with mock.patch.object(classify_repos.endpoints, "SECOND_MODEL", dataclasses.replace(
                glm, settings=dataclasses.replace(glm.settings, temperature=0.9))):
            self.assertEqual(self.digest(), current)
        with mock.patch.object(classify_repos, "SECOND_MODEL_REASONS", ("output_limit",)):
            self.assertEqual(self.digest(), current)
        self.assertEqual(self.digest(options=["--no-second-model"]), current)
        block = self.parameters(options=["--endpoint-default-temperature"])["classifying_model"]
        self.assertEqual({key: block.get(key) for key in ("temperature", "top_p", "seed",
                                                         "max_output_tokens",
                                                         "reasoning_effort")},
                         {"temperature": None, "top_p": None, "seed": None,
                          "max_output_tokens": 65_536, "reasoning_effort": "high"})

    def test_the_digest_follows_the_weights_of_the_summary_model_and_not_its_provider(self):
        """The summary model is recorded with its weights and its quantization. Its provider is
        no classification parameter, by the maintainer's decision of 2026-11-04."""
        summary = ["--summary-model", "deepseek/deepseek-v4.1-flash", "--summary-provider",
                   "morph/fp8", "--summary-quantization", "fp8", "--summary-hugging-face-id",
                   "deepseek-ai/DeepSeek-V4.1-Flash"]
        current = self.digest(options=summary)
        self.assertNotEqual(current, self.digest(options=["--summary-model", "gpt-6-luna"]))
        self.assertEqual(self.digest(options=[*summary[:3], "wafer/us", *summary[4:]]),
                         current)
        self.assertNotEqual(self.digest(options=[*summary[:5], "mxfp4", *summary[6:]]),
                            current)

    def test_the_digest_follows_the_check_of_a_pages_purpose_sentence(self):
        """A purpose quoted from the purpose sentence of a page counts as stated, so the minimum
        number of words of the sentence, the line of the summary that holds it, the code that
        compares its words with the page and the fields of the page tool's result decide labels
        as the prompt does."""
        current = self.digest()
        with mock.patch.object(tools, "PURPOSE_MIN_WORDS", tools.PURPOSE_MIN_WORDS + 1):
            self.assertNotEqual(self.digest(), current)
        with mock.patch.object(tools, "PURPOSE_LINE", re.compile("purpose[ :]*(.*)$", re.M)):
            self.assertNotEqual(self.digest(), current)
        with mock.patch.object(tools, "in_page", _contained):
            self.assertNotEqual(self.digest(), current)
        # The code that names the repository of a page, because a purpose quoted from a page of
        # another repository does not count as stated.
        for name in ("page_repository", "other_repository"):
            with self.subTest(code=name), mock.patch.object(tools, name, _no_repository):
                self.assertNotEqual(self.digest(), current)
        # The code that decides whether a link names a page, because only the purpose sentence
        # of such a page counts.
        with mock.patch.object(classify_repos, "linked_page", _no_repository):
            self.assertNotEqual(self.digest(), current)
        schemas = {tool.name: tool.output_schema for tool in self.listed}
        page = schemas["read_linked_page"]
        schemas["read_linked_page"] = {**page, "properties": {
            key: value for key, value in page["properties"].items() if key != "purpose_sentence"}}
        self.assertNotEqual(self.digest(schemas), current)

    def test_the_digest_follows_the_description_of_the_repositorys_owner(self):
        """The metadata gives the profile description of the owner, such as the description of
        KDE's organization, which calls every repository of KDE a read-only mirror. The output
        schema and the code that reads it are classification parameters, because the model
        decides from them."""
        schemas = self.parameters()["read_first_results"]["output_schemas"]
        self.assertIn("owner_description",
                      schemas["get_repository_metadata"]["properties"])
        current = self.digest()
        with mock.patch.object(tools, "owner_description", _no_repository):
            self.assertNotEqual(self.digest(), current)

    def test_the_digest_follows_the_limits_of_the_results_that_the_input_holds(self):
        """The input holds the activity of the issues and pull requests, and the number of
        recent pull requests and issues that it lists changes what the model reads."""
        current = self.digest()
        with mock.patch.object(tools, "ACTIVITY_ITEMS", tools.ACTIVITY_ITEMS + 1):
            self.assertNotEqual(self.digest(), current)

    def test_the_digest_follows_the_output_schemas_of_the_results_that_the_input_holds(self):
        """A field that the server no longer returns for the outline, the metadata or the issues
        and pull requests is a field that the model no longer reads."""
        schemas = {tool.name: tool.output_schema for tool in self.listed}
        current = self.digest(schemas)
        for tool in ("get_file_tree", "get_repository_metadata", "get_issues_and_pull_requests"):
            with self.subTest(tool=tool):
                fewer = json.loads(json.dumps(schemas))
                fewer[tool]["properties"].pop("note")
                self.assertNotEqual(self.digest(fewer), current)

    def test_the_digest_follows_the_code_that_writes_the_results_and_not_its_docstrings(self):
        """A reworded docstring of the code that writes the metadata keeps the digest, and a
        change to what the code writes gives a new one."""
        current = self.digest()
        reworded = _edited_script(self, '"""What GitHub records about `repo_name`, from',
                                  '"""What GitHub records about the repository `repo_name`, from',
                                  module=tools)
        changed = _edited_script(self, 'description=body.get("description") or ""',
                                 'description=(body.get("description") or "").strip()',
                                 module=tools)
        with mock.patch.object(tools, "repository_metadata", reworded.repository_metadata):
            self.assertEqual(self.digest(), current)
        with mock.patch.object(tools, "repository_metadata", changed.repository_metadata):
            self.assertNotEqual(self.digest(), current)

    def test_the_digest_follows_the_code_that_resolves_a_link_and_not_its_docstrings(self):
        """Which links of the README the model may read, and at which address, decides what
        read_linked_page returns. A reworded docstring of that code keeps the digest, and a
        change to which links it reads gives a new one."""
        current = self.digest()
        reworded = _edited_script(self, '"""Every link of a README as an absolute URL:',
                                  '"""Each link of a README as an absolute URL:')
        changed = _edited_script(self, "return links | set(reference_definitions(",
                                 "return set() | links | set(reference_definitions(")
        for module, same in ((reworded, True), (changed, False)):
            with self.subTest(same=same), mock.patch.object(classify_repos, "link_resolution",
                                                            module.link_resolution):
                self.assertIs(self.digest() == current, same)

    def test_the_digest_follows_how_a_link_stands_for_a_link_of_the_readme(self):
        """Which address of the README a link from the model stands for, and which form of a
        reStructuredText reference reaches its definition, decides the page that the tool reads,
        and so each of these functions and patterns changes the digest."""
        current = self.digest()
        for old, new in (('(found.group(2) or "").rstrip("/")', '(found.group(2) or "")'),
                         ("return min((known for known in links if _page_of(known) == page), "
                          "default=None)",
                          "return max((known for known in links if _page_of(known) == page), "
                          "default=None)"),
                         ('r"^(https?://[^/?#\\s]+)(/[^?#\\s]*)?(\\?[^#\\s]*)?(?:#\\S*)?$"',
                          'r"^(https?://[^/?#\\s]+)(/[^?#\\s]*)?(\\?[^#\\s]*)?$"'),
                         ('r"^`[^`]*<([^<>`\\s]+)>`__?$"', 'r"^`[^`]*<([^<>`\\s]+)>`_$"'),
                         ('r"^(?:([^\\s`<>\\[\\]]+)|`([^`<>]+)`)_$"',
                          'r"^([^\\s`<>\\[\\]]+)_$"'),
                         ('r"[ \\t]*\\r?$", re.MULTILINE)', 'r"[ \\t]*$", re.MULTILINE)')):
            module = _edited_script(self, old, new)
            with self.subTest(old=old), mock.patch.object(classify_repos, "link_resolution",
                                                          module.link_resolution):
                self.assertNotEqual(self.digest(), current)

    def test_no_text_for_the_model_names_a_tool_that_it_is_not_offered(self):
        """The model is offered `read_linked_page` alone, and a text that names another tool of
        the server would tell it to call a tool that it is not offered. The note that the input
        contains the result of a tool names the tool that the model called, in place of
        `{tool}`."""
        parameters = self.parameters()
        offered = {tool["name"] for tool in parameters["tools"]}
        others = {tool.name for tool in self.listed} - offered
        self.assertEqual(offered, {"read_linked_page"})
        texts = {"prompt": parameters["prompt"], "tools": json.dumps(parameters["tools"]),
                 "checklist_tool": json.dumps(parameters["checklist_tool"]),
                 "summary prompt": parameters["summary"]["prompt"],
                 **{key: parameters[key] for key in ("input", "read_first_input",
                                                     "no_description", "no_activity",
                                                     "truncated")},
                 **parameters["messages"]}
        for key, text in texts.items():
            for name in others:
                with self.subTest(text=key, tool=name):
                    self.assertNotIn(name, text)

    def test_the_digest_follows_every_message_that_replaces_the_result_of_a_tool_call(self):
        """The refusal of a link, the limits of linked pages and rounds, the note that the input
        contains the result of the called tool, and the message for a tool that the server
        lacks."""
        current = self.digest()
        for module, constant in ((classify_repos, "REFUSED_LINK"),
                                 (classify_repos, "READ_FIRST_CALL"),
                                 (classify_repos, "LINKED_PAGE_LIMIT"),
                                 (classify_repos, "TOOL_ROUND_LIMIT"),
                                 (mcp_bridge, "NO_SUCH_TOOL")):
            with self.subTest(constant=constant), mock.patch.object(
                    module, constant, getattr(module, constant) + " Reworded."):
                self.assertNotEqual(self.digest(), current)


if __name__ == "__main__":
    unittest.main()
