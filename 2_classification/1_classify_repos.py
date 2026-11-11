#!/usr/bin/env python3
"""Label every repository in the sampling frame as an engineered software project or not,
reusing a label while the README, the model and the classification parameters that decided it
are unchanged.

For each repository in the sampling frame, `../data/sampling/ghs_filtered.json`, the script reads
the README through GitHub's README API (`readmes.py`) with the ETag of the repository's latest
decision. GitHub then returns 304 for an unchanged README, and the decision is reused without a
request to the model. A README that the classification cannot use, because it is missing, empty
or not in English by the language check, is an exclusion. Any other README goes to the model, by
default DeepSeek V4.1 Flash through OpenRouter's Responses API (`endpoints.PRODUCTION`), or an
OpenAI model through OpenAI's, with the prompt in `prompts/classification-prompt.md` as
instructions and the repository's description on GitHub, its activity as the sampling frame
records it and its README as input. Before the first request, the classification reads three results from the MCP
server `classification_tools.py` through `mcp_bridge.py` and puts them in the input after the
README: the activity of the repository's issues and pull requests, what GitHub records about the
repository and an outline of its files. The model may call the server's fourth tool, which
returns a summary of a page that the description, the homepage or the README links to.

The model's response is a checklist:

- the README's words of prose by language, from which the classification decides whether the
  README is mainly English
- the fields of each exclusion criterion, "true", "false" or "unclear", with the evidence
- the upstream, if the repository is described as a fork
- the purpose, quoted from the description, the README or the purpose sentence of a page
- whether the engineering evidence is clear ("true") or not ("unclear")

The classification derives the label from the checklist. The label is "false" when a criterion
applies, when the README is not mainly in English, when no purpose is quoted, or when a fork has
not become a project of its own (`forks.py`). It is "unclear" when none of these applies and one
of them is unclear, and otherwise the model's "true" or "unclear". What the classification
reads, and the limits on it, follow the reading rules of 0_README.md, which the comments name by
number.

Each model labels with the settings of its record (`endpoints.settings_of`): DeepSeek V4.1 Flash at
temperature 0.6, top_p 0.95 and seed 7 with an output limit of 65,536 tokens, and GLM-5.3 at
temperature 1.0, top_p 0.95 and seed 7 with an output limit of 262,144 tokens, both at reasoning
effort high. The attempts of DeepSeek V4.1 Flash at its higher temperature send the output limit
of 262,144 tokens too. A failed response is a response
that stopped at the output limit before the model called submit_classification or in the arguments
of the call, a final response that did not call submit_classification with its schema, or a
response to a request that offered only submit_classification in which the model called another
tool (SECOND_MODEL_REASONS). After a failed response at a repository, the next attempts at that
repository use the higher temperature of the model's record, 1.0 for DeepSeek V4.1 Flash
(`Classifier.label`). A repository that the classifying model still leaves without a decision after
the retry rounds is labeled by the second model, GLM-5.3 on its pinned endpoint
(`endpoints.SECOND_MODEL`), with the same inputs and prompt, unless `--no-second-model` is given,
the model is the second model itself or the second model cannot label in the invocation
(`Run.label_with_second_model`). Each decision records the temperature and the top_p of the request
that decided it under `sampling`. A decision of the higher temperature or of the second model
records that temperature or that model with its settings, the reason and the failed requests of the
classifying model under `higher_temperature` or `second_model`, and a run reuses it only under the
same settings (`Run.applies`).

Each model of OpenRouter has a backup provider of the same weights in the same quantization
(`endpoints.BACKUPS`). Where a request that the API's service failed at the pinned provider would
use an attempt, the same request goes to the backup provider, and once the pinned provider has
failed the requests of two repositories through the pauses that use no attempt, the backup
provider serves every request of the model until the pinned provider responds again to the
request that tests it every `endpoints.PINNED_CHECK_SECONDS` (`Classifier.again`,
`endpoints.Providers`). The provider
is no classification parameter, and each decision names the provider of each of its responses
under `served_by`. `--no-backup-provider` sends every request to the pinned provider.

Every decision is appended to the run's partial file in the cache of labels (`label_cache.py`)
as it is made. A stopped run therefore resumes where it stopped, after it has derived the
decisions of its earlier invocations again under the current rules (`Run.recheck`). When every
repository has its decision, or when the repositories left without one after the retry rounds
are within the ceiling of `undecided_ceiling`, the run writes the new decisions as its file of
the cache and writes the outputs:

  repos_annotated.csv   repo_name, github_id, default_branch, engineered_project,
                        justification, one row per labeled repository, in the order of the
                        sampling frame. The engineered project selection keeps the rows
                        labeled "true" and refuses two of them with one GitHub id
  repos_excluded.csv    repo_name, repo_url, reason, detected_language, one row per repository
                        that was not labeled, with the reason "undecided" for a repository
                        left without a decision, which the next invocation tries again in a
                        new run while no collection over the sample has begun
  repos_annotated.json  the inputs of the labels beside it: the run, the models that labeled, the
                        classification parameters and the retrieval, which the release names,
                        under "undecided" the repositories left without a decision, under
                        "higher_temperature_labels" and "second_model_labels" the repositories
                        that the classifying model labeled at its higher temperature or the
                        second model labeled, with the reason, and under
                        "second_model_failures" the repositories that the second model left
                        without a decision too

A run over part of the sampling frame, `--repository-list` or `--limit`, writes its decisions to
the cache and writes the outputs only to the directory that `--output-dir` names.

Each invocation adds the tokens of its responses, by the model and the service tier that OpenAI
billed or the provider that served an OpenRouter model, to the run's tokens file in the cache. A run
that ends, finished or stopped, writes its cost record beside it in USD, at the prices of
`config/model_prices.csv`, and logs the total (`costs.py`).

Usage:
  python3 1_classify_repos.py
  python3 1_classify_repos.py --limit 20 --output-dir /tmp/trial
  python3 1_classify_repos.py --model z-ai/glm-5.3 \
      --repository-list ../data/classification/evaluation/<id>/evaluation_sample.csv
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import dataclasses
import hashlib
import html
import inspect
import io
import json
import os
import posixpath
import re
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from string import Template

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import classification_tools as tools  # noqa: E402
import costs  # noqa: E402
import endpoints  # noqa: E402
import forks  # noqa: E402
import label_cache  # noqa: E402
import mcp_bridge  # noqa: E402
import readmes  # noqa: E402
from label_cache import EXCLUDED, LABELED, CacheError  # noqa: E402

from agentcontext import (CLASSIFICATION_DIR, SAMPLING_DIR, STAGE_DIR, caches,  # noqa: E402
                          credentials, guards, logs)
from agentcontext.github_api import NoTokens, TokenPool, graphql_errors  # noqa: E402

COMPONENT = "repository_labeling"

SAMPLING_FRAME = SAMPLING_DIR / "ghs_filtered.json"
PROMPT = HERE / "prompts" / "classification-prompt.md"
# The classifying model by default, DeepSeek V4.1 Flash on its pinned endpoint
# (`endpoints.PRODUCTION`). The runner reads the name from this line for its pre-checks.
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
# Repositories decided at once by default, the maintainer's choice that classify-repos-spec.md
# records with its reason.
WORKERS = 32
# Rules 13 and 14 of the section "The reading rules" of 0_README.md: rounds of tool calls that the
# model may take for one repository, and linked pages that it may read. The labels of the
# calibration sample reached neither limit, and a model that keeps calling tools stops after 7
# requests. A call past a limit returns the message that the limit is reached, and the repository
# is decided with what the model has.
MAX_TOOL_ROUNDS = 5
LINKED_PAGES = 5
# Rule 1: a request is sized to stay under the 272,000 input tokens above which OpenAI charges
# the long context price for its models. Tokens are estimated at 3.57 characters a token, a low
# percentile of OpenAI's token counts over the READMEs of the calibration sample that rule 1
# gives, so that the estimate is more likely too high than too low for OpenAI's tokenizer. The
# tokenizer of another model can count more tokens for the same request.
REQUEST_TOKENS = 272_000
CHARS_PER_TOKEN = 3.57
# Attempts at one repository's label after the SDK's own retries of a rate limit or a server
# error: a response that is not the JSON that the format requires, or a request that failed
# outright. After a failed response (SECOND_MODEL_REASONS), the next attempts at the
# repository, in this round and the later ones, use the higher temperature of the model's
# settings (`Classifier.label`).
ATTEMPTS = 3
RETRY_SECONDS = 2
# A repository left without a decision because GitHub or the model returned no usable response
# is tried again after the others, in up to RETRY_ROUNDS more rounds, each after
# RETRY_ROUND_SECONDS. A repository that failed on a dropped connection to GitHub or OpenAI is
# then decided on a later attempt.
RETRY_ROUNDS = 3
RETRY_ROUND_SECONDS = 60
# Every worker pauses when FORMAT_STREAK final responses in a row, over FORMAT_STREAK_REPOSITORIES
# repositories or more, fail the response format (`FormatStreak`), and the run stops when the
# endpoint goes on failing the format through FORMAT_PAUSES pauses. An endpoint that does not hold
# the model to the schema of CHECKLIST_TOOL fails every response, and the run would pay for each
# attempt at each repository.
# A working endpoint fails a few responses of a run, and the responses of the other workers that
# follow the format end each streak. A response that stopped at its output limit does not count,
# because it ended before the model could follow the format. While no streak has paused the
# workers, the responses of a repository that failed an earlier round of the run do not count
# either, because a retry round holds nothing but such repositories, and neither do those of a
# repository that the model failed in the finished run that the run follows, because the run that
# tries these repositories again reuses every other decision. Neither do the responses of the
# second model, which labels nothing but such repositories. Their failures would otherwise pause
# and then stop a run on a working endpoint.
FORMAT_STREAK = 20
FORMAT_STREAK_REPOSITORIES = 5
# The pauses of a streak take the waits of the pauses for the API's service, PAUSE_FIRST_SECONDS
# first and each further pause twice as long, up to PAUSE_LONGEST_SECONDS (`ServicePause.hold`).
# The streak goes on counting during the pauses. Once the streak has reached FORMAT_STREAK, the
# first failed response to a request sent after the latest pause of every worker began, for the
# service or for the streak, begins the next pause, and after FORMAT_PAUSES pauses it stops the
# run (`FormatIgnored`). A failed response to a request that was in flight when the latest pause
# began counts toward the streak and begins no pause, so that the requests in flight lengthen the
# pauses only once. The failed responses keep their consequences: the higher temperature for the
# next attempts at their repositories and the second model after the retry rounds.
# A response in the format ends the streak, so that FORMAT_STREAK failed responses in a row have
# to follow before the next pause, but the pauses count on and the waits keep growing until the
# endpoint has recovered: until responses in the format for FORMAT_STREAK_REPOSITORIES
# repositories have followed each other without a failed response between them. Only then does
# the next pause begin with the shortest wait (`FormatStreak.passed`), while a pause that holds
# runs to its end. An endpoint that fails most responses returns one in the format now and then,
# and if each such response made the next pause the first again, the run would not stop. It would
# pay for the failed attempts at every repository, and the labels would come from the higher
# temperature or the second model. A run of responses in the format for as many repositories as
# the streak needs to tell an endpoint from a repository is rare at such an endpoint and common at
# a working one.
# Once a streak has paused every worker and until the endpoint has recovered, the failed
# responses of the repositories that failed an earlier round count as well, and the pass of the
# second model takes over the pauses (`Run.label_with_second_model`). An outage that begins at the
# end of a round then pauses the retry rounds and the pass of the second model too, instead of
# using up their attempts within minutes and leaving the repositories to the second model or
# without a decision.
# GROUNDING: a policy, as PAUSES_BEFORE_ATTEMPTS is for the pauses of the service. The waits of
# FORMAT_PAUSES pauses add up to about two hours, so that an unattended run of several days rides
# out an endpoint that fails the format for that long instead of stopping. The request after a
# pause costs only the requests that the workers send, whereas a stop costs the hours until a
# person starts the run again. An endpoint that ignores the schema, which fails every response
# also after the pauses, stops after about one more request per worker and pause.
FORMAT_PAUSES = 7
# The waits in seconds before a tool call that GitHub or the summary model failed is made again
# in the same conversation (`Classifier.tool`). The next request continues from the model's last
# response through `previous_response_id`, whereas a repository that fails is labeled again from
# its first request, and the model is paid for that request again. After the last wait, a failure
# of GitHub fails the repository, and the pause for GitHub then covers it (`GitHubPause`). A
# failure of the summary model fails the repository for a retry round. GROUNDING: a policy. The
# MCP server's client has made its own attempts at the request before the call fails. Waits of
# minutes keep a worker on one conversation through a short failure without holding it for the
# hours of an outage, which the pause for GitHub waits out.
TOOL_WAITS = (60, 5 * 60, 15 * 60)
# A failure of GitHub rather than of a repository: no usable response for a README, for GitHub's
# facts or for a tool call through TOOL_WAITS. Once two repositories have failed this way since
# GitHub's last response, every worker pauses before its next repository, after each of
# GITHUB_PAUSES in turn. A repository that GitHub failed is then tried again after the pause
# instead of failing for a retry round (`GitHubPause`), and GitHub's next response makes the next
# pause the first again. A repository that waited for every pause and fails again fails on its
# own and begins no pause. A repository that GitHub failed earlier in the run also fails on its
# own, unless GitHub then fails the README request that it served last
# (`readmes.Client.responds`). In that case, its failure counts as any other. The failure of a
# repository that did not wait for the pauses stops the run, and the next run resumes it
# (`GitHubDown`).
# GROUNDING: the policy of the sampling stage's query, `ROUND_WAITS` of
# ../1_sampling/2_query_github.py, which `readmes.RECHECK_WAITS` follows. Without the pauses, an
# outage of GitHub in the last minutes of a pass would outlast the retry rounds and leave more
# repositories without a decision than the ceiling allows, and the run would end incomplete.
GITHUB_PAUSES = readmes.RECHECK_WAITS
# A failure of the API's service rather than of a repository: a rate limit or a lack of flex
# capacity (HTTP 429), a server error (HTTP 5xx), a timeout or a broken connection, after the
# SDK's own retries. Once the requests of two repositories have failed this way since the last
# response, every worker pauses, first for PAUSE_FIRST_SECONDS and after each further pause twice
# as long, up to PAUSE_LONGEST_SECONDS (`ServicePause`). Such a failure uses no attempt at a
# repository's label during the first PAUSES_BEFORE_ATTEMPTS pauses since the last response, and
# uses one after them. The requests of the last repositories of a pass or of a retry round can
# fail every time, and with no other repository to respond, pausing for them would keep the run
# waiting without end. Once the pauses since the last response have taken PAUSE_TOTAL_SECONDS,
# the run stops with its decisions kept, and the next run resumes it (`ServiceDown`). The run
# stays on its service tier, which sets the price. GROUNDING: a policy. An outage of minutes
# costs pauses of minutes, and an outage of hours leaves the workers waiting with a request every
# PAUSE_LONGEST_SECONDS. A longer outage stops the run instead of using up the attempts of every
# repository in the sampling frame. A repository with requests that fail while the requests of
# other repositories succeed uses its attempts as usual.
PAUSE_FIRST_SECONDS = 60
PAUSE_LONGEST_SECONDS = 4 * 3600
PAUSES_BEFORE_ATTEMPTS = 3
PAUSE_TOTAL_SECONDS = 24 * 3600
# A refusal for spent credit (`endpoints.credit_refusal`): OpenRouter's HTTP 402, or its HTTP 403
# for a spent budget or key limit, and OpenAI's `insufficient_quota` or
# `billing_hard_limit_reached`. The requests to that service pause, which for the classifying
# model's service is every worker, and the failure uses no attempt at a label and fails no
# repository. A response of that service ends the pause, and a response of the other service of
# a run with the models on two services ends none. The run checks the credit every
# CREDIT_CHECK_SECONDS, for OpenRouter through GET /api/v1/key, which spends nothing, and then,
# as for OpenAI, by sending one request again (`CreditPause`). Once the credit is back, the run
# continues by itself. After
# CREDIT_WAIT_SECONDS without credit, the run stops with its decisions kept, and the next run
# resumes it (`CreditDown`). GROUNDING: a policy. A refused request costs nothing: "Add more
# credits and retry the request" (openrouter.ai/docs/api-reference/errors), so that a check
# every ten minutes costs nothing and the run continues within ten minutes of a top-up. Three
# days cover a weekend: credit spent on a Friday evening is topped up on the Monday morning, and
# the wait lasts through that Monday's working hours.
CREDIT_CHECK_SECONDS = 600
CREDIT_WAIT_SECONDS = 72 * 3600
SDK_RETRIES = 6
# The timeout of the SDK's client for the requests to the API (`endpoints.client`). httpx applies
# it to connecting and to each read of the response, not to the whole request: httpcore passes
# the read timeout to each read of the socket (`_receive_event` of `httpcore/_async/http11.py`).
# A read that waits longer ends the request with a timeout, which the SDK retries up to
# SDK_RETRIES times. OpenRouter "sends you the HTTP `200 OK` status and headers as soon as the
# provider accepts the request", and "Non-streaming requests send the status at the same point"
# (openrouter.ai/docs/api_reference/errors-and-debugging). Its pages state bytes during the
# generation only for streams: "For SSE (Server-Sent Events) streams, OpenRouter occasionally
# sends comments to prevent connection timeouts" (openrouter.ai/docs/api-reference/streaming).
# The requests here are not streamed. The bodies of OpenRouter's responses to such requests
# begin with blank lines, the more the longer the model generated, as the responses of DeepSeek
# V4.1 Flash on morph/fp8 that a reproduction captured with their bodies on 2026-11-03 show. Each
# read can then end within the timeout, and a request can hold its worker for far longer than
# REQUEST_TIMEOUT.
REQUEST_TIMEOUT = 300
# The flex tier responds more slowly, and OpenAI's guide for it states that "The default timeout
# is 10 minutes" and that a request may need more
# (developers.openai.com/api/docs/guides/flex-processing).
FLEX_TIMEOUT = 900
SERVICE_TIERS = ("auto", "default", "flex")
# The tier of a request that names none, such as the request of a page summary: "When not set,
# the default behavior is 'auto'" (openai 3.13.0, `types/responses/response.py`).
UNNAMED_TIER = "auto"
# A progress line after this many finished repositories, or after logs.PROGRESS_SECONDS without
# one, as the cloning step writes it.
PROGRESS_EVERY = 100

# Every field of the model's response is "true", "false" or "unclear", and "unclear" when what the
# model was given or retrieved shows neither. Where nothing shows whether people other than the
# authors use a repository, a boolean would make the model's response state that they do not.
TRUE, FALSE, UNCLEAR = "true", "false", "unclear"
FIELD_VALUES = (TRUE, FALSE, UNCLEAR)


def _all(*values: str) -> str:
    """Kleene's conjunction: false when one value is false, unclear when one is unclear."""
    return FALSE if FALSE in values else UNCLEAR if UNCLEAR in values else TRUE


def _any(*values: str) -> str:
    """Kleene's disjunction: true when one value is true, unclear when one is unclear."""
    return TRUE if TRUE in values else UNCLEAR if UNCLEAR in values else FALSE


def _not(value: str) -> str:
    return {TRUE: FALSE, FALSE: TRUE, UNCLEAR: UNCLEAR}[value]


# The exclusion criteria in the model's response, in the order of the prompt's first step, each
# with its fields. A criterion with an exception has separate fields, as `no_source_code` has for
# its two cases, and the classification decides from them whether the criterion applies
# (CRITERION_APPLIES). Given the whole criterion,
# the model can decide it by its first half alone, as for OpenSSL, a mirror with a README that
# names the exception. An unclear field leaves the criterion unclear only where the criterion
# depends on that field.
CRITERION_FACTS = {
    "inactive": ("applies",),
    "moved": ("applies",),
    "mirror": ("described_as_mirror", "invites_pull_requests_here"),
    "collection": ("applies",),
    "example_or_template": ("example_or_template", "project_generator"),
    "website": ("applies",),
    "documentation_or_teaching": ("applies",),
    "research_artifact": ("dataset", "research_code"),
    "localization": ("applies",),
    "configuration_only": ("applies",),
    "no_source_code": ("contains_no_source_code", "main_part_not_public"),
    "work_in_progress": ("unfinished", "in_use"),
    "personal_or_single_script": ("toy_project", "own_use", "single_script"),
}
EXCLUSIONS = tuple(CRITERION_FACTS)
# Whether the software is maintained and used by others: one field of the response, shared by the
# criteria that make an exception for such software, namely research code, software that its
# author wrote for their own use or a single script. The criterion leaves out the evidence of
# software engineering practices, because step 2 judges that evidence.
MAINTENANCE_AND_USE = ("maintained", "used_by_others")


def _not_maintained_and_used(f: dict) -> str:
    return _not(_all(f["maintained"], f["used_by_others"]))


CRITERION_APPLIES = {
    # A repository described as a mirror is a mirror unless its README invites pull requests in
    # the repository. For example, the reviewer labels mlichvar/chrony and openstack/charm-cinder
    # as mirrors from their descriptions alone. `derive` lifts the criterion when pull requests
    # that no GitHub App opened were merged in the repository since the start of the sampling
    # window (`mirror_merges`, MERGES_COUNTED).
    "mirror": lambda f: _all(f["described_as_mirror"],
                             FALSE if f["invites_pull_requests_here"] == TRUE else TRUE),
    "example_or_template": lambda f: _all(f["example_or_template"],
                                          _not(f["project_generator"])),
    "research_artifact": lambda f: _any(f["dataset"], _all(
        f["research_code"], _not_maintained_and_used(f))),
    # Software described as a toy, an experiment, a learning exercise or something made just for
    # fun is excluded in any case, as PierfrancescoSoffritti/doodles, "Web Experiments I do for
    # fun", which its author maintains and others use. Software that its author wrote for their
    # own use and a single script are excluded unless the software is maintained and used by
    # others.
    "personal_or_single_script": lambda f: _any(f["toy_project"], _all(
        _any(f["own_use"], f["single_script"]), _not_maintained_and_used(f))),
    "work_in_progress": lambda f: _all(f["unfinished"], _not(f["in_use"])),
    # The two cases of the criterion: a repository without the source code of the software that
    # it is about, and software with the source code of its main part not public, as the app of
    # drawthingsai/draw-things-community, which its README places in a private monorepo. One
    # field for both would hide which case the model found.
    "no_source_code": lambda f: _any(f["contains_no_source_code"], f["main_part_not_public"]),
}
# The criteria that the classification applies itself: the README's language, a fork that has
# not become a project of its own (forks.py), and a purpose quote that is in neither the
# description nor the README.
NOT_ENGLISH, FORK, NO_PURPOSE = "not_english", "fork", "no_purpose"
CRITERIA = (NOT_ENGLISH, *EXCLUSIONS, FORK, NO_PURPOSE)
# The model's judgment of the evidence of software engineering practices, with the exclusion
# criteria left aside. "false" comes only from a criterion.
ENGINEERING = (TRUE, UNCLEAR)
# The repository's name goes into the input rather than the instructions, so that every
# repository's first request starts with the same instructions and tools, the tool of the
# checklist among them, which the API can then read from its prompt cache.
INPUT = ('<repository>{name}</repository>\n<description>{description}</description>\n'
         '<activity>{activity}</activity>\n<readme path="{path}">\n{text}\n</readme>')
NO_DESCRIPTION = "GitHub shows no description for this repository."
NO_ACTIVITY = "The sampling frame records no activity for this repository."
# The activity of a repository as the sampling frame records it, as pairs of the field in the
# sampling frame and the name under which the model reads it. The creation and the last commit are
# the values that GitHub returned to the sampling stage's query of GitHub, and the counts are the
# values that GHS recorded. The stars and the forks are left out, because they measure
# popularity, which no criterion of the prompt names as evidence of maintenance or use. The share
# of the sample with a configuration artifact also rises with the stars, and labels that followed
# the stars would raise the adoption rate that the dataset publishes.
ACTIVITY_FIELDS = (("commits", "commits"), ("contributors", "contributors"),
                   ("releases", "releases"), ("totalIssues", "issues"),
                   ("openIssues", "open issues"), ("totalPullRequests", "pull requests"),
                   ("openPullRequests", "open pull requests"), ("createdAt", "created"),
                   ("lastCommit", "last commit"))
TRUNCATED = "\n[The README is cut here, so that the request stays within its size limit.]"
# What a quotation of a README leaves out: the target of a link or an image, inline, with
# parentheses in it, or by reference; square brackets; an HTML tag, comment or autolink; a role
# of reStructuredText; the characters of emphasis, code, headings, block quotes and tables; the
# backslash of an escaped punctuation mark; and pictographs. The check also replaces a reference
# to an AsciiDoc attribute by its value, decodes HTML entities, compares curly quotes and dashes as
# straight quotes and hyphens, joins a word that a hyphen and a line break split, and drops a space
# that markup left beside punctuation. Without these steps, the check would refuse quotations that
# the READMEs contain, such as Openfire's "[Openfire] is a real time collaboration (RTC) server",
# Spotless's "&lt;antlr | c | ...&gt;", and PySyft's purpose, which a tag starting at "<0.10"
# earlier in the README would otherwise remove.
_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
# An AsciiDoc link, link:target[text] or https://target[text], as spring-cloud-services-starters
# writes it. A target contains no `<` or `>` and ends before the next link begins, and a URL
# begins after whitespace, a bracket, a parenthesis or a tag, or at the start of a line. A list of
# links joined by `<br>`, by commas or by parentheses is one line without whitespace, and so is a
# minified stylesheet with a `background-image:` in every rule. A pattern that scanned from each
# link to the end of such a line would hold every worker for each check of a quotation.
_ASCIIDOC_LINK = re.compile(r"(?:(?:link|xref|image):+(?:(?!(?:link|xref|image):)[^\s\[\]<>])*"
                            r"|(?<![^\s\[\]<>(])(?:https?|ftp)://"
                            r"(?:(?!(?:https?|ftp)://)[^\s\[\]<>])*)"
                            r"\[([^\]]*)\]")
# An AsciiDoc link with a target that begins with an attribute, {name}[text] or {name}/path[text],
# which reads as its text, as asciidoctor/asciidoctorj's README writes "{uri-repo}[AsciidoctorJ] is
# the official library". The text reads so also when the README does not set the attribute itself,
# as when it includes the file that sets it. The target ends before the next attribute begins, so
# that a line of many attributes without a link is read in time.
_ATTRIBUTE_LINK = re.compile(r"\{[\w-]+\}[^\s\[\]<>{]*\[([^\]\n]*)\]")
# An AsciiDoc cross reference with its text, <<anchor,text>>, which reads as its text.
_ASCIIDOC_CROSS_REFERENCE = re.compile(r"<<[^<>,\n]+,\s*([^<>\n]+)>>")
# A line of an AsciiDoc document that sets an attribute, ":name: value" or ":name:", or that opens
# or closes a conditional block around such lines, as wildfly/quickstart's README sets
# ":productNameFull:" for each of its releases. A reference to an attribute, {name}, reads as its
# value, so that the README's "{productNameFull} stack" reads "WildFly Application Server stack".
_ATTRIBUTE_LINE = re.compile(r"^(?::([\w-]+):(?:[ \t]+(.*?))?|(ifdef|ifndef)::([\w,+-]+)\[\]"
                             r"|endif::[\w,+-]*\[\])[ \t\r]*$", re.M)
_ATTRIBUTE_REFERENCE = re.compile(r"\{([\w-]+)\}")
# The attribute that a README tests for GitHub with "ifdef::env-github[]". GitHub's renderer sets
# it to an empty value: "'env-github' => ''" in lib/github/markups.rb of github/markup.
GITHUB_ATTRIBUTES = {"env-github": ""}
# The longest value of an attribute. GitHub renders a README in the secure mode of Asciidoctor
# (":safe => :secure" in lib/github/markups.rb of github/markup), which cuts each value at this
# size: "attr_overrides['max-attribute-value-size'] = 4096" in lib/asciidoctor/document.rb of
# asciidoctor/asciidoctor. Attributes that refer to each other therefore cannot grow without limit.
MAX_ATTRIBUTE_VALUE = 4096
# How many times its own length a text can become through its references. A longer result is read
# as written, because a value within the limit can still be referred to any number of times.
MAX_RESOLVED_GROWTH = 4
# A parenthetical that a quotation may leave out. For example, bao-hypervisor's README names "Bao
# (from Mandarin Chinese ..., meaning “to protect”)", and the model quotes the name without the
# parenthetical.
_PARENTHETICAL = re.compile(r" ?\([^()]*\)")
# An Org link, [[target][text]] or [[target]], which Org displays as its text or else as its
# target, as rndusr/stig's README.org writes "the [[http://www.transmissionbt.com/][BitTorrent
# client Transmission]]".
_ORG_LINK = re.compile(r"\[\[([^\[\]]+)\](?:\[([^\[\]]*)\])?\]")
_REFERENCE_LABEL = re.compile(r"\]\[[^\[\],]*\]")
# A backslash before a space or a tab, which reStructuredText removes from the output together
# with the space: lueschem/edi's README.rst writes "*e*\ mbedded", which renders as "embedded".
# A backslash before a line break stays, because Markdown renders it as a line break.
_ESCAPED_SPACE = re.compile(r"\\[ \t]")
# A backslash before a punctuation mark, which Markdown leaves out of the output:
# PnX-SI/GeoNature's README writes "d\'observations", which renders as "d'observations".
_ESCAPED_MARK = re.compile(r"\\([!-/:-@\[-`{-~])")
_TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>|<(?:https?|mailto|ftp):[^<>\s]*>",
                  re.S)
# An HTML tag of inline text. A browser shows the text around it without a break, so that the tag
# leaves no space: YOURLS/YOURLS's README writes "<strong>Y</strong>our <strong>O</strong>wn", which
# reads "Your Own", and ITISFoundation/osparc-simcore's "o<sup>2</sup>S<sup>2</sup>PARC" reads
# "o2S2PARC". Every other tag stands between words, as <br> and <td> do.
_INLINE_TAG = re.compile(r"</?(?:a|abbr|b|bdi|bdo|big|cite|code|data|del|dfn|em|font|i|ins|kbd|mark"
                         r"|nobr|q|s|samp|small|span|strike|strong|sub|sup|time|tt|u|var)"
                         r"(?:\s[^<>]*)?/?>", re.I)
_ROLE = re.compile(r":[A-Za-z][A-Za-z-]*:(?=`)")
_HYPHEN_BREAK = re.compile(r"(\w)- (?=\w)")
# The marker of a list item at the start of a line: a hyphen, an asterisk or a plus, or a number
# with a period or a parenthesis, followed by a space.
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+")
# A hyphen, a plus or a bullet that stands between words, as the marker of a list item does in a
# quotation that joins the items into one line, such as "It handles: - Serialization &
# deserialization of core data structures - Useful utility functions". The same sign stands for
# a dash in prose, which the comparison leaves out of the source and the quotation alike.
_LONE_MARKER = re.compile(r"(?<!\S)[-+\u2022](?!\S)")
# The number of a list item that stands between words in a quotation that joins the items into one
# line, as agglayer/provers's quotation "designed to: 1. Receive updates ... 2. Verify their
# validity", where the README lists the items with these numbers. `quoted` also tries the quotation
# without them, because the same number and period can end a sentence, as in "supports Python 3."
_ITEM_NUMBER = re.compile(r"(?<=\s)\d{1,3}[.)]\s+(?=\S)")
_SPACE_BEFORE = re.compile(r" ([,.;:!?)])")
_SPACE_AFTER = re.compile(r"\( ")
_SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[*_`#>\[]*[A-Z])")
# The characters of emphasis, code and strikethrough beside a word or a punctuation mark, which
# the rendered sentence shows without a space in their place: TAPA's README writes
# "**Ta**sk-**Pa**rallel", which the model quotes as "**Ta**sk-**Parallel",
# Almamu/linux-wallpaperengine's "**Wallpaper Engine**-style", qor/media's
# "*files*/*images*/*videos*" and poldracklab/fitlins's "`BIDS`_-formatted". An underscore inside a
# word, as in snake_case, is not markup, and neither is such a character between spaces.
_EMPHASIS = re.compile(r"(?:[*`#]+|~~)(?=\S)|(?<=\S)(?:[*`#]+|~~)"
                       r"|(?<!\w)_+(?=\S)|(?<=[^\s_])_+(?!\w)")
# The role of an AsciiDoc span, as in "[.underline]#text#", which reads as the text.
_ASCIIDOC_ROLE = re.compile(r"\[[^\[\]\n]*\](?=#[^\s#])")
# AsciiDoc's superscript and subscript, "^text^" and "~text~", which read as the text.
_ASCIIDOC_SCRIPT = re.compile(r"([\^~])([^\s\^~]+)\1")
# A pictograph, with what joins it to another or selects its form. A quotation can leave one out,
# as the description of gofiber/boilerplate writes a rocket between "Boilerplate for" and "Fiber",
# and the model quotes "Boilerplate for Fiber".
_PICTOGRAPH = re.compile("[\u200d\u20e3\u2300-\u23ff\u25a0-\u27bf\u2b00-\u2bff\ufe0f"
                         "\U0001f000-\U0001faff]")
_MARKS = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                        "\u2013": " - ", "\u2014": " - ", "*": " ", "_": " ", "`": " ", "#": " ",
                        ">": " ", "|": " ", "\\": " ", "[": "", "]": ""})
# What separates the parts of a quotation: an ellipsis, or a semicolon between phrases that the
# model quotes from different places, as when it joins the names of OCA/account-payment's addons.
_FRAGMENTS = re.compile(r"\[?(?:\.\.\.|\u2026)\]?|;")
# One word in ten of a part of a quotation may differ from the source: a word replaced, left out
# or added. For example, Rello/analytics's README contains "turn it into shareable insights", and
# the model quotes "turn them into". pingcap/ticdc's README contains "pushes them to downstream
# systems", and the model leaves out "them".
WORDS_PER_CHANGED_WORD = 10
# The tool parameters that the classification fills in and does not show to the model.
SUPPLIED = {"get_file_tree": ("repo_name", "revision", "max_chars"),
            "read_linked_page": ("repo_name",), "get_repository_metadata": ("repo_name",),
            "get_issues_and_pull_requests": ("repo_name",)}
# The fields of a page's summary that the classification records and does not show to the model.
SUMMARY_FIELDS = ("input_tokens", "output_tokens", "model")
# The fields of a page's summary that the classification adds to the tokens of the run by the tier
# that OpenAI billed or the provider that served an OpenRouter model (`Classifier.bill`), and that
# the model does not read. A decision names the provider of a summary of an OpenRouter model
# (`Classifier.page`).
SUMMARY_BILLING = ("cached_tokens", "cache_write_tokens", "service_tier")
# The tools of which the classification fills in every argument. It reads their results before
# the first request and puts them in the input, so that the model decides from them without a
# request for the calls, and it offers the model `read_linked_page` alone, the one tool with an
# argument of the model's choice.
READ_FIRST = ("get_issues_and_pull_requests", "get_repository_metadata", "get_file_tree")
OFFERED = ("read_linked_page",)
# The results of READ_FIRST after the README, the outline last, because it is shortened to the
# room that the request has left (rule 1).
READ_FIRST_INPUT = ('\n<issues_and_pull_requests>{issues}</issues_and_pull_requests>\n'
                    '<metadata>{metadata}</metadata>\n<outline>{outline}</outline>')
# Calls of get_file_tree for one outline. A second and a third call are made when the JSON fields
# and escapes make the result longer than its room, and each call shortens the outline by the
# excess.
OUTLINE_CALLS = 3
_FIELD_VALUE = {"type": "string", "enum": list(FIELD_VALUES)}


def _criterion(facts: tuple) -> dict:
    """The schema of one exclusion criterion in the response: each of its fields, and the
    evidence."""
    fields = [*facts, "evidence"]
    return {"type": "object",
            "properties": {**{fact: _FIELD_VALUE for fact in facts},
                           "evidence": {"type": "string"}},
            "required": fields, "additionalProperties": False}
# The fields in the order in which the model writes them: the checklist before the purpose and
# `engineering`, so that the model decides the exclusion criteria before it weighs the evidence
# of software engineering practices.
# The words of prose of the README by language, as the model counts them. The classification
# decides from the counts whether the README is mainly English (`mainly_english`), because a
# model that judges the language itself can call a README mainly English when most of its prose
# is Chinese, as with the README of bbossgroups/bboss-elasticsearch, which the language check of
# Lingua also takes for English.
_WORDS_BY_LANGUAGE = {"type": "array", "items": {
    "type": "object", "properties": {"language": {"type": "string"}, "words": {"type": "integer"}},
    "required": ["language", "words"], "additionalProperties": False}}
_FIELDS = {"repo_name": {"type": "string"},
           "readme_words_by_language": _WORDS_BY_LANGUAGE,
           "maintenance_and_use": _criterion(MAINTENANCE_AND_USE),
           "exclusions": {"type": "object",
                          "properties": {key: _criterion(facts)
                                         for key, facts in CRITERION_FACTS.items()},
                          "required": list(EXCLUSIONS), "additionalProperties": False},
           "fork_of": {"type": "string"},
           "purpose_quote": {"type": "string"},
           "purpose": {"type": "string"},
           "engineering": {"type": "string", "enum": list(ENGINEERING)},
           "justification": {"type": "string"}}
CHECKLIST_SCHEMA = {"type": "object", "properties": _FIELDS, "required": list(_FIELDS),
                    "additionalProperties": False}
# The tool that the model calls with its response, with the fields of the checklist as its strict
# parameters, beside the tools that read the repository. The requests set no response format,
# because an endpoint of OpenRouter that holds the whole output to a response format lets no model
# call a tool while the format is set. The request after the last round of tool calls
# offers this tool alone, and no request sets `tool_choice`: OpenRouter's endpoint list gives
# `"required": false` under `supports_tool_choice` for DeepSeek V4.1 Flash on morph/fp8 and for
# GLM-5.3 on gmicloud/fp8 (read 2026-11-04), so a request that forced the call would find no
# endpoint.
CHECKLIST_TOOL_NAME = "submit_classification"
CHECKLIST_TOOL = {"type": "function", "name": CHECKLIST_TOOL_NAME,
                  "description": ("Submit the classification of the repository, with the fields "
                                  "of the output format in the instructions. Call it once, as "
                                  "your final response, after reading what the fields need."),
                  "parameters": CHECKLIST_SCHEMA, "strict": True}

ANNOTATED_COLUMNS = ["repo_name", "github_id", "default_branch", "engineered_project",
                     "justification"]
# The record of the inputs of the labels in repos_annotated.csv, beside it, which the release step
# reads.
ANNOTATION_RECORD = "repos_annotated.json"
# The keys of that record that list the repositories of repos_annotated.csv that the classifying
# model labeled at its higher temperature and those that the second model labeled, each with its
# name, its GitHub id, the model that labeled it, the classifying model and the kind of the
# classifying model's failed response, and the key that lists the repositories that the second
# model left without a decision too, each with its name, its GitHub id, the second model, the
# classifying model and that kind.
HIGHER_TEMPERATURE_LABELS = "higher_temperature_labels"
SECOND_MODEL_LABELS = "second_model_labels"
SECOND_MODEL_FAILURES = "second_model_failures"
# The key of that record that lists the repositories of repos_annotated.csv with a label that a
# backup provider served wholly or in part (`backup_served`), each with its name, its GitHub id,
# the model that labeled it, the backup providers and whether they served it wholly.
BACKUP_PROVIDER_LABELS = "backup_provider_labels"
EXCLUDED_COLUMNS = ["repo_name", "repo_url", "reason", "detected_language"]
# The status of a repository in a run: an earlier decision reused as it is, an earlier decision
# carried over to a new README or to changed code (`Run.carry`), a new decision, gone, or failed.
REUSED, CARRIED, DECIDED, GONE, FAILED = "reused", "carried", "decided", "gone", "failed"
# The statuses with which a repository leaves the run, which a resumed run does not decide again.
FINISHED = (REUSED, CARRIED, DECIDED, GONE)
# The status of a line of a partial file that keeps the model's response for a repository while
# GitHub's facts about it are still to be read (`assess`). A resumed run or comparison restores the
# response.
KEPT = "kept"
# What left a repository without a decision: GitHub's response for the README, or the model's
# response.
GITHUB_FAILED, MODEL_FAILED = "github", "model"
# What GitHub's failure for a repository raises: no usable response after the client's attempts,
# or a response that is not the JSON that GitHub documents, which the fork heuristic raises as
# `forks.Unexpected` and the other readers as ValueError or KeyError.
GITHUB_ERRORS = (readmes.NoResponse, forks.Unexpected, ValueError, KeyError)
# The reason under which repos_excluded.csv lists a repository that the run left without a
# decision after its retry rounds, which is also its outcome in the run's labels.
UNDECIDED = label_cache.UNDECIDED
# The reason under which repos_excluded.csv lists a repository labeled as an engineered project
# that shares its head commit with another one that the sample keeps instead (`first_of_copies`).
COPY = "copy"


def undecided_ceiling(repositories: int) -> int:
    """How many repositories a run over `repositories` may leave without a decision after its
    retry rounds and still finish: as many as the cloning step may leave as clone failures and
    still count as complete (`guards.clone_failure_ceiling`). Such a repository is left out of
    the sample as a clone failure is left out of the captured repositories. The next invocation
    tries it again in a new run while no collection over the sample has begun, and `--new-run`
    after that (`complete_finished_run`). Without the ceiling, a README that the API refuses for
    its content, or a repository for which GitHub fails every time, would end every run
    incomplete and keep the stages after the classification from running. More repositories
    without a decision point to a cause that someone has to check, and the run then ends
    incomplete."""
    return guards.clone_failure_ceiling(repositories)


class QuotationRefused(Exception):
    """The model's purpose quote is in neither the description, the README, the purpose sentence
    of a page that the description, the homepage field or the README links to, nor the purpose
    sentence of an issue or a pull request that only the input's list of issues and pull requests
    named (`quotation_fields`). `decision` is the decision with the purpose unclear, which the
    classification takes when no attempt succeeds."""

    def __init__(self, message: str, decision: dict):
        super().__init__(message)
        self.decision = decision


# The refusals (HTTP 400) that a new attempt does not receive again. The MCP server applies the
# same list to the summary model's errors.
TRANSIENT_REFUSALS = tools.TRANSIENT_REFUSALS


# The kinds of a failed response: a response that stopped at the output limit before the model
# called submit_classification or in the arguments of the call, a final response that did not call
# submit_classification with its schema, and a response to a request that offered only
# submit_classification in which the model called another tool. After a failed response, the
# classifying model's next attempts at the repository use its higher temperature, and the second
# model labels a repository that the classifying model left without a decision after the retry
# rounds when one of its attempts ended in a failed response. A failure of GitHub, of a tool or of
# the API's service, and a refusal of the input, is no failed response, and another temperature or
# the second model would get the same failure.
OUTPUT_LIMIT, RESPONSE_FORMAT, TOOL_CALL = "output_limit", "response_format", "tool_call"
SECOND_MODEL_REASONS = (OUTPUT_LIMIT, RESPONSE_FORMAT, TOOL_CALL)
# The tokens of a model's failed attempts at one repository, and the counts of those attempts,
# of those of them at the higher temperature and of their requests, which a failure records
# (`ModelFailure.usage`). The count at the higher temperature shows whether the second model
# labeled a repository after attempts at that temperature or without any.
FAILURE_TOKENS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
FAILURE_COUNTS = ("attempts", "higher_temperature_attempts", "requests", *FAILURE_TOKENS)
# The characters of the start and of the end of the arguments of a call of CHECKLIST_TOOL that a
# failed attempt keeps, with the length of the arguments, in its entry of the `failed` line of its
# repository (`kept_arguments`, `ModelFailure.attempts`). GROUNDING: a policy. The start shows
# whether the arguments are JSON at all, and the end whether they were cut off. The bound keeps a
# failed line small, also with arguments that a model wrote until its output limit.
KEPT_ARGUMENT_CHARS = 200
# The characters of the end of arguments of CHECKLIST_TOOL that are not JSON, which the text of
# the failure names, with the error of `json.loads` and the length of the arguments
# (`checked_response`). The text reaches the log, and the `failed` line, which the log does not
# show, keeps more of the arguments (KEPT_ARGUMENT_CHARS). GROUNDING: a policy. The end shows
# whether the arguments were cut off, and the bound keeps the line of the log short.
LOGGED_ARGUMENT_CHARS = 80
# The characters of the start and of the end of the text that the model wrote in the messages of
# a response without a call of CHECKLIST_TOOL, which a failed attempt keeps, with the length of
# the text, in its entry of the `failed` line of its repository and in the record of the failed
# response that a later decision names (`kept_message`, `ModelFailure.model_message`).
# GROUNDING: a policy. The start shows what the model wrote instead of the call, such as a
# checklist in another format or prose, and the end how the text ended. The bound keeps a failed
# line and a decision small, as KEPT_ARGUMENT_CHARS does.
KEPT_MESSAGE_CHARS = 200
# The characters of the start of that text, which the text of the failure names, with the length
# of the text (`response_details`). GROUNDING: a policy. The start shows what the model wrote
# instead of the call, and the bound keeps the line of the log short, as LOGGED_ARGUMENT_CHARS
# does.
LOGGED_MESSAGE_CHARS = 80
# The fields of the record of a label of the higher temperature or of the second model that
# describe why it was made rather than how. Two such records with the same other fields are of
# the same settings (`fallback_settings`).
FALLBACK_OUTCOME = ("reason", "error", "message", "classifying_model_failures")
# The field of a decision that a model made at its own settings after a failed response at the
# repository, as a model without a higher temperature does, with the kind, the error and the
# start and the end of the text of the messages of the model's first failed response at the
# repository in the run (`new_history`). It records why the decision followed failed attempts,
# as the record of the higher temperature does for a decision at that temperature. It names no
# setting, and no run compares it before it reuses the decision (`Run.applies`).
FIRST_FAILED_RESPONSE = "first_failed_response"


class ModelFailure(Exception):
    """The model left a repository without a usable response after every attempt. `final` is
    true when the API refused the request as malformed. The API refuses such a request the same
    way every time, and the repository is not tried again in the run. `kind` is one of
    SECOND_MODEL_REASONS for a failed response. When `Classifier.label` raises
    it, `kind` is that of the last attempt, `usage` counts the requests and tokens of the failed
    attempts (FAILURE_COUNTS), and `attempts` lists each failed attempt with the temperature and
    the top_p of its requests, its kind and its error, from which a resumed run continues
    (`failure_histories`). `arguments` is the length, the start and the end of the arguments of
    the call of CHECKLIST_TOOL in a response that failed with such a call (`kept_arguments`),
    which the entry of its attempt keeps, or None. `model_message` is the length, the start and
    the end of the text of the messages of a response that failed without such a call
    (`kept_message`), which the entry of its attempt and the record of its failed response
    (`new_history`) keep under `message`, or None."""

    def __init__(self, message: str, final: bool = False, kind: str = "",
                 usage: dict | None = None, attempts: list | None = None,
                 arguments: dict | None = None, model_message: dict | None = None):
        super().__init__(message)
        self.final, self.kind, self.usage, self.attempts = final, kind, usage, attempts
        self.arguments, self.model_message = arguments, model_message


class ServiceEnded(ModelFailure):
    """A response that the API's service ended: a response with an error or with the status
    "failed" that did not stop at its output limit, with a call of CHECKLIST_TOOL or without one
    (`service_ended`), or a Response that holds an error and no output items, whatever the code
    of the error (`error_body`). The failure is one of the API's service, as a server error is
    (`Classifier.create`). It is no failed response (SECOND_MODEL_REASONS) and does not count
    toward FormatStreak."""


class SummaryFailed(ModelFailure):
    """The summary model failed a page in a tool call through every wait of TOOL_WAITS. The
    repository fails for a retry round rather than using an attempt, which would pay the
    conversation's first request again for a summary that still fails."""


class ModelRefused(Exception):
    """The API refuses every request of the run for the classifying model or the summary model:
    it rejects the API key (HTTP 401), denies the project or the key access (HTTP 403) or rejects
    the model. A top-up does not end such a refusal. The run stops, as it does when GitHub accepts
    no GitHub access token, and a later run resumes it. Otherwise each repository in the
    sampling frame would fail on its own, each after its attempts and retry rounds. A refusal for
    spent credit pauses the run instead (`CreditPause`)."""


class ModelNotOffered(ModelRefused):
    """The API does not offer the classifying model or the summary model and reports this with
    the code `model_not_found`. The run stops as on any refusal. The comparison with the previous
    run repeats labels with the models of the compared run, and the API can retire those models
    after that run. The comparison then leaves the causes that it cannot repeat undetermined
    (`changed_labels.py`)."""


class FormatIgnored(ModelRefused):
    """The endpoint of the classifying model returns final responses that do not follow the
    response format, FORMAT_STREAK in a row over FORMAT_STREAK_REPOSITORIES repositories or more,
    and goes on returning them through FORMAT_PAUSES pauses of every worker without recovering
    (`FormatStreak`): no call of CHECKLIST_TOOL, or arguments outside its schema. Such an endpoint
    does not hold the model to the schema, and every later attempt would be paid for and fail as
    well. The run stops, and a run on another endpoint labels the rest. The endpoint of the
    second model, in a pass that took over the pauses of the classifying model's streak, ends
    the pass instead (`Run.decide_with_second_model`)."""


class FormatStreak:
    """The final responses in a row that failed the response format, over every worker of the
    run, the repositories of those responses and the pauses of every worker that the streak began
    (FORMAT_PAUSES). A response that follows the format ends the streak. The pauses count on
    until responses in the format for FORMAT_STREAK_REPOSITORIES repositories in a row show that
    the endpoint has recovered (`passed`), and until then the streak is `open`. A response
    that stopped at its output limit does not count (`Classifier.label_readme`), and neither do
    the responses of a repository that an earlier attempt round of the run left without a
    decision while the streak is not open (`Classifier.failed_earlier`)."""

    def __init__(self):
        self.count, self.repositories, self.last = 0, set(), ""
        self.recovered()

    def recovered(self) -> None:
        """Start the count of pauses and the waits again, so that the next pause is the first,
        with the shortest wait. A pause that holds runs to its end (`ServicePause.hold`)."""
        self.pauses, self.wait = 0, PAUSE_FIRST_SECONDS
        # The repositories of the responses in the format since the latest failed response, and
        # the final responses that failed or followed the format since the first pause.
        self.passing: set = set()
        self.failed_since, self.passed_since = 0, 0

    @property
    def open(self) -> bool:
        """Whether the streak has paused every worker and the endpoint has not recovered since."""
        return self.pauses > 0

    def passed(self, name: str) -> None:
        """Count a response in the format for repository `name`. It ends the streak, and once
        such responses for FORMAT_STREAK_REPOSITORIES repositories have followed each other
        without a failed response between them, the endpoint has recovered (`recovered`)."""
        self.count, self.repositories, self.last = 0, set(), ""
        if not self.open:
            return
        self.passed_since += 1
        self.passing.add(name)
        if len(self.passing) >= FORMAT_STREAK_REPOSITORIES:
            self.recovered()

    def pause(self) -> float:
        """Count a pause of every worker for the streak, and return its seconds:
        PAUSE_FIRST_SECONDS for the first pause and twice the seconds of the previous pause for
        each further one, up to PAUSE_LONGEST_SECONDS."""
        seconds = self.wait
        self.pauses += 1
        self.wait = min(self.wait * 2, PAUSE_LONGEST_SECONDS)
        return seconds

    def restart(self) -> None:
        """Start the count of the failed responses again without counting a response in the
        format, after the provider that returned them stopped serving (`Classifier.format_failed`).
        The pauses of an open streak count on."""
        self.count, self.repositories, self.last = 0, set(), ""

    def take_over(self, other: "FormatStreak") -> None:
        """Continue the pauses of `other`, an open streak of another classifier, with its waits:
        the pass of the second model after a streak of the classifying model's endpoint that has
        not recovered (`Run.label_with_second_model`). The count of failed responses starts
        again, because they are the second model's."""
        self.pauses, self.wait = other.pauses, other.wait

    def failed(self, name: str, reason: str) -> bool:
        """Count a response for repository `name` that failed the format for `reason`, and
        return whether the streak has reached FORMAT_STREAK responses over
        FORMAT_STREAK_REPOSITORIES repositories."""
        self.count += 1
        self.repositories.add(name)
        self.last = reason
        self.passing.clear()
        if self.open:
            self.failed_since += 1
        return (self.count >= FORMAT_STREAK
                and len(self.repositories) >= FORMAT_STREAK_REPOSITORIES)


def refused(error) -> ModelRefused:
    """The refusal of every request of the run that the API's `error` states, as
    `ModelNotOffered` when the API does not offer the model."""
    reason = refusal(error)
    kind = ModelNotOffered if reason == tools.MODEL_NOT_OFFERED else ModelRefused
    return kind(f"{reason} ({error})")


# Why the API refuses every request of the run, read from the error of one request, or "" when the
# error concerns the repository's request alone. The MCP server applies the same test to the
# summary model's errors.
refusal = tools.refusal


# Whether a request failed for the API's service rather than for its repository. The MCP server
# applies the same test to the summary model's errors (`classification_tools.service_failure`).
service_failure = tools.service_failure


class ServiceDown(Exception):
    """The API's service failed every request through PAUSE_TOTAL_SECONDS of pauses since its
    last response. The run stops with its decisions kept, as on a refusal of every request, and
    the next run resumes it. If the run waited on, it would send a request every
    PAUSE_LONGEST_SECONDS without end."""


async def wait_unless_ended(pause, seconds: float) -> None:
    """Wait `seconds` with the sleep of `pause`, or less when `pause` ends. `ServicePause` and
    `GitHubPause` wait with this function. `pause` has `sleep`, `ended` and `waiting`, the tasks
    that wait, which `end_wait` cancels."""
    task = asyncio.current_task()
    pause.waiting.add(task)
    try:
        await pause.sleep(max(seconds, 0))
    except asyncio.CancelledError:
        if not pause.ended:
            raise
        # The end of the pause cut the wait short, and the task goes on.
        task.uncancel()
    finally:
        pause.waiting.discard(task)


def end_wait(pause, reason: str) -> None:
    """End the waits of `pause` for a run that stopped for `reason` (`wait_unless_ended`)."""
    if pause.ended:
        return
    pause.ended = reason
    for task in list(pause.waiting):
        task.cancel()


class PauseEnded(ServiceDown):
    """The run stopped for another reason while a worker waited out a pause for the API's
    service (`ServicePause.end`)."""


class ServicePause:
    """The pause of every worker of a run while the API's service fails (PAUSE_FIRST_SECONDS).

    `ready` waits out the pause before each request, `succeeded` records a response, `failed` a
    request that failed for the service, `finished` a repository that the classifier left, with a
    label or without one, and `hold` begins a pause for a streak of final responses that failed
    the response format (`FormatStreak`). The pause of the service begins when the requests of
    two repositories that are still being labeled have failed since the last response. Failed
    requests of one repository, while the requests of other repositories succeed, therefore use
    that repository's attempts. A repository that used every attempt in the run (`exhausted`)
    begins no pause in a later retry round, because only repositories without a decision are left
    then, and no response of another repository would end the pauses. A request sent before the
    latest pause began begins no further pause when it fails, so that the requests in flight
    together lengthen the wait only once.
    """

    def __init__(self, sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock
        self.until, self.wait, self.pauses = 0.0, PAUSE_FIRST_SECONDS, 0
        # The pauses since the last response, and the seconds that they took.
        self.idle, self.paused = 0, 0.0
        self.failing: set[str] = set()
        self.exhausted: set[str] = set()
        self.down = ""
        # Why the run stopped (`end`), after which no worker waits out a pause, and the tasks
        # that wait (`wait_unless_ended`).
        self.ended = ""
        self.waiting: set = set()

    async def ready(self) -> int:
        """Wait while a pause holds, and return the number of pauses so far, which a failure of
        the request sent next reports to `failed`. Raises `ServiceDown` once the pauses have
        passed their total, and `PauseEnded` when the run stops during the wait."""
        while (left := self.until - self.clock()) > 0:
            if self.ended:
                raise PauseEnded(self.ended)
            await wait_unless_ended(self, left)
        if self.down:
            raise ServiceDown(self.down)
        return self.pauses

    def end(self, reason: str) -> None:
        """End the pause of a run that stopped for `reason`: each worker that waits out a pause
        raises `PauseEnded` instead of waiting for hours, and a worker without a pause sends its
        request, so that the repository that it labels is not paid for again."""
        end_wait(self, reason)

    def succeeded(self) -> None:
        """Record a response. The service works, and the next pause is the shortest."""
        self.failing.clear()
        self.wait, self.idle, self.paused = PAUSE_FIRST_SECONDS, 0, 0.0

    def finished(self, repository: str) -> None:
        """Record that the classifier left `repository`. Its failures no longer begin a pause
        together with the failures of another repository."""
        self.failing.discard(repository)

    def hold(self, seconds: float) -> None:
        """Pause every worker for `seconds` for a streak of final responses that failed the
        response format (`FormatStreak`). The pause counts among the pauses that `ready` returns,
        so that a request in flight when it began begins no further pause when it fails, for the
        service or for the format. The waits of the service and their total stay as they are,
        because the service responds, and a pause of the service that lasts longer is kept."""
        self.pauses += 1
        self.until = max(self.until, self.clock() + seconds)

    def wait_for(self, other: "ServicePause") -> None:
        """Hold every worker until the pause that holds the workers of `other` ends, without a
        pause of its own: the pass of the second model after a streak of the classifying model's
        endpoint that has not recovered (`Run.label_with_second_model`)."""
        self.until = max(self.until, other.until)

    def failed(self, repository: str, sent_after: int, error, backup: bool = False) -> bool:
        """Record that the service failed the request for `repository` sent after `sent_after`
        pauses, and return whether every worker pauses for it, so that the same request is sent
        again after the pause and the failure uses no attempt at the repository's label. Raises
        `ServiceDown` in place of a pause that would begin after the pauses since the last
        response took PAUSE_TOTAL_SECONDS. With `backup`, a backup provider takes a request that
        would otherwise use an attempt (`Classifier.again`), and the pause that would begin after
        PAUSES_BEFORE_ATTEMPTS pauses since the last response does not begin."""
        if repository in self.exhausted:
            return False
        self.failing.add(repository)
        if len(self.failing) < 2:
            return False
        if backup and sent_after == self.pauses and self.idle >= PAUSES_BEFORE_ATTEMPTS:
            return False
        if sent_after == self.pauses:
            if self.paused >= PAUSE_TOTAL_SECONDS:
                self.down = (f"the API failed every request through {logs.hms(self.paused)} of "
                             f"pauses since its last response, the last with "
                             f"{type(error).__name__} ({error})")
                raise ServiceDown(self.down)
            self.pauses += 1
            self.idle += 1
            self.until = self.clock() + self.wait
            self.paused += self.wait
            counted = ("" if self.idle <= PAUSES_BEFORE_ATTEMPTS else
                       f". After {PAUSES_BEFORE_ATTEMPTS} pauses without a response, each "
                       f"failure uses an attempt at the repository's label")
            logs.warning(COMPONENT, f"The API failed the requests of {len(self.failing):,} "
                                    f"repositories since its last response, the last with "
                                    f"{type(error).__name__} ({error}). Pausing every worker for "
                                    f"{logs.hms(self.wait)}, then sending each request "
                                    f"again{counted}.")
            self.wait = min(self.wait * 2, PAUSE_LONGEST_SECONDS)
        return self.idle <= PAUSES_BEFORE_ATTEMPTS


class CreditDown(ModelRefused):
    """The service refused the requests for spent credit through CREDIT_WAIT_SECONDS. The run
    stops with its decisions kept, as on a refusal, and the next run resumes it."""


async def any_credit(service: str) -> bool:
    """The check of a run without a way to read the credit: one request tests it."""
    return True


class CreditPause:
    """The pause of the requests to a service while the service refuses them for spent credit
    (CREDIT_CHECK_SECONDS), a pause of its own for each service.

    `refused` begins the pause of the refusal's service, with a WARNING that names the service
    and what to do, and states in the runner's state that the step waits for credit
    (`logs.step_status`). The request that the service refused is sent again after the pause,
    so the refusal uses no attempt at a label and fails no repository. `ready(service)` waits
    out the pause of `service` before each request to that service. Once its tool calls are
    made, the next request of every worker is one of the classifying model, so a pause of the
    classifying model's service holds every worker, also in a run with the summary model on that
    service. A pause of the summary model's service, when it is another service, holds the
    requests of the summary model alone, and the requests of the classifying model go on.

    Every CREDIT_CHECK_SECONDS, `check(service)` tests the credit: it returns False while the
    service shows that the credit is spent, such as an OpenRouter API key with no credit left,
    and True otherwise. On True, `ready` returns True to one worker, whose request to the
    service tests the credit, and the other requests to the service wait for it. A refused
    request costs nothing. `succeeded(service)` records a response of `service`, which ends its
    pause, and `probe_ended` a request that tested the credit and failed for another reason. A
    response of another service ends no pause, because it shows nothing about the credit that
    was spent. After CREDIT_WAIT_SECONDS without credit, `ready` raises `CreditDown`.
    """

    def __init__(self, sleep, clock=time.monotonic, check=any_credit, classifying: str = ""):
        """`classifying` is the service of the classifying model, whose pause holds every
        worker. Without it, the WARNING of every pause says that every worker waits."""
        self.sleep, self.clock, self.check = sleep, clock, check
        self.classifying = classifying
        # The refusal that began the pause of each service, the clock when it began and the
        # clock of its next check, by service, and the services with a request that tests the
        # credit.
        self.refusals: dict[str, endpoints.CreditRefusal] = {}
        self.since: dict[str, float] = {}
        self.next_check: dict[str, float] = {}
        self.probing: set[str] = set()
        # The status of each pause that holds, in the order in which the pauses began.
        self.statuses: dict[str, dict] = {}
        self.down = ""
        # Why the run stopped (`end`), after which no pause waits for credit.
        self.ended = ""
        self.changed: asyncio.Event | None = None

    def _changed(self) -> None:
        """Wake the workers that wait for a pause, so that they look at it again."""
        if self.changed is not None:
            self.changed.set()
        self.changed = None

    async def _wait(self, seconds: float) -> None:
        """Wait `seconds`, or less when a pause changes."""
        if self.changed is None:
            self.changed = asyncio.Event()
        sleeper = asyncio.ensure_future(self.sleep(max(seconds, 0)))
        waiter = asyncio.ensure_future(self.changed.wait())
        _, pending = await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    def _state(self) -> None:
        """State in the runner's state the wait of the pause that began first among the pauses
        that hold, which stops the run first, or no wait when no pause holds."""
        logs.step_status(next(iter(self.statuses.values()), None))

    async def ready(self, service: str) -> bool:
        """Wait while the credit of `service` is spent, and return whether the request to it
        sent next tests the credit. Raises `CreditDown` once a pause has taken
        CREDIT_WAIT_SECONDS."""
        while (refusal := self.refusals.get(service)) is not None:
            if self.down or self.ended:
                raise CreditDown(self.down or self.ended)
            now, since = self.clock(), self.since[service]
            # The longest wait comes first, also while a request tests the credit, so that a
            # test that never reports its outcome cannot hold the other workers past it.
            if now - since >= CREDIT_WAIT_SECONDS:
                self.down = (f"{refusal.reason} through {logs.hms(now - since)} of waiting for "
                             f"credit. To continue, {refusal.top_up}. The next invocation "
                             f"resumes the run")
                logs.step_status(None)
                self._changed()
                raise CreditDown(self.down)
            probing = service in self.probing
            if probing or now < self.next_check[service]:
                wait = CREDIT_CHECK_SECONDS if probing else self.next_check[service] - now
                await self._wait(min(wait, since + CREDIT_WAIT_SECONDS - now))
                continue
            self.next_check[service] = now + CREDIT_CHECK_SECONDS
            if await self.check(service):
                self.probing.add(service)
                return True
            logs.info(COMPONENT, f"{endpoints.SERVICE_NAMES[service]} shows no credit after "
                                 f"{logs.hms(now - since)} of waiting. Checking again in "
                                 f"{logs.hms(CREDIT_CHECK_SECONDS)}.")
        if self.down:
            raise CreditDown(self.down)
        return False

    def refused(self, refusal: endpoints.CreditRefusal, probe: bool) -> None:
        """Record that the service of `refusal` refused a request for spent credit. `probe` is
        the return of `ready` before the request."""
        service = refusal.service
        if probe:
            self.probing.discard(service)
        if self.ended:
            # The run has stopped: the worker raises `CreditDown` at its next `ready` instead of
            # waiting.
            self.refusals.setdefault(service, refusal)
            return
        if service in self.refusals:
            if probe:
                waited = logs.hms(self.clock() - self.since[service])
                logs.info(COMPONENT, f"{refusal.reason} after {waited} of waiting. Checking "
                                     f"again in {logs.hms(CREDIT_CHECK_SECONDS)}.")
            self._changed()
            return
        self.refusals[service], self.since[service] = refusal, self.clock()
        self.next_check[service] = self.since[service] + CREDIT_CHECK_SECONDS
        # A new pause begins without a request that tests the credit. A response can end the
        # earlier pause while `check` waits for the service, and the request that the check then
        # lets test the credit finds no pause to end when it succeeds.
        self.probing.discard(service)
        # The status before the line, because the runner copies the status into its state at
        # each line of the log, and the next line comes CREDIT_CHECK_SECONDS later.
        self.statuses[service] = {"waiting_for": "credit",
                                  "service": endpoints.SERVICE_NAMES[service],
                                  "reason": refusal.reason, "to_continue": refusal.top_up,
                                  "since": label_cache.iso(label_cache.utc_now()),
                                  "stops_after_seconds": CREDIT_WAIT_SECONDS}
        self._state()
        waits = ("Every worker waits for credit" if service == (self.classifying or service)
                 else "The requests of the summary model wait for credit, and the requests of "
                      "the classifying model go on")
        logs.warning(COMPONENT, f"{refusal.reason}. {waits}. To continue, {refusal.top_up}. "
                                f"The run checks the credit every "
                                f"{logs.hms(CREDIT_CHECK_SECONDS)} and continues by itself. "
                                f"After {logs.hms(CREDIT_WAIT_SECONDS)} without credit, it stops "
                                f"with its decisions kept, and the next run resumes it.")

    def end(self, reason: str) -> None:
        """End the pauses of a run that stopped for `reason`, and every pause after them: each
        worker that waits for credit raises `CreditDown`, and so does a worker whose request is
        refused for spent credit later, so that the run can end instead of waiting for credit
        that no request uses. A request in flight without a refusal goes on, so that the
        repository that it labels is not paid for again by the next run."""
        if self.ended:
            return
        self.ended = reason
        if self.refusals:
            logs.step_status(None)
        self._changed()

    def probe_ended(self, service: str, probe: bool) -> None:
        """Record that a request to `service` that tested the credit failed for another reason.
        The next check comes after CREDIT_CHECK_SECONDS."""
        if probe:
            self.probing.discard(service)
            self._changed()

    def succeeded(self, service: str) -> None:
        """Record a response of `service`. Its credit is back, and its pause ends. After the run
        stopped, no pause is left to end (`end`), and the log names none."""
        refusal = self.refusals.pop(service, None)
        if refusal is None:
            return
        # The refusal ends also after a stop, so that the worker of the response finishes its
        # repository.
        self.probing.discard(service)
        if not self.ended:
            self.statuses.pop(service, None)
            self._state()
            logs.info(COMPONENT, f"{endpoints.SERVICE_NAMES[service]} responds again after "
                                 f"{logs.hms(self.clock() - self.since[service])} of waiting "
                                 f"for credit. The run continues.")
        self._changed()


class GitHubPauseEnded(Exception):
    """The run stopped for another reason while a worker waited out a pause for GitHub
    (`GitHubPause.end`)."""


class GitHubDown(Exception):
    """GitHub returned no usable response through the pauses of GITHUB_PAUSES since its last
    response, and then failed a repository that had not waited for them. The run stops with its
    decisions kept, as when GitHub accepts no GitHub access token, and the next run resumes it.
    If the run went on, it would fail every repository left in the pass, each after the client's
    attempts."""


class GitHubPause:
    """The pause of every worker of a run while GitHub fails (GITHUB_PAUSES).

    `ready` waits out the pause before a worker takes a repository through its step, and returns
    the number of pauses so far, which a failure of the step reports to `failed`. `failed`
    records a repository that GitHub failed and returns whether the repository waits for the
    pause and goes through its step again. `responses()` counts GitHub's responses, and a
    response since the last failure makes the next pause the first of GITHUB_PAUSES again. The
    pause begins when two repositories have failed since GitHub's last response. A repository
    that GitHub fails while it responds for the other repositories therefore fails for a retry
    round as usual. A step that began before the latest pause began begins no further pause when
    it fails. A repository that waited for every pause since GitHub's last response and fails
    after the last pause fails on its own and begins no pause.

    A repository that GitHub failed earlier in the run, in this invocation or in an earlier one,
    or in the finished run that the run follows (`failed_before`), fails again on its own while
    GitHub returns a usable response to the request of `responds`, the README request that
    GitHub served last. A retry round or a resumed run can hold only such repositories, with no
    response of GitHub for another repository to clear their failures. If their failures
    counted, the repositories for which GitHub fails every request would pause every worker
    through GITHUB_PAUSES and stop every run at the same repositories. When GitHub fails the
    request of `responds` too, GitHub is failing rather than the repository. The failure then
    counts toward the pause as any other, so that the run waits out an outage of GitHub that
    begins in a retry round. The failure of a repository that did not wait for the pauses stops
    the run (`GitHubDown`), and so does the next step of every other worker.
    """

    def __init__(self, responses, responds, sleep, clock=time.monotonic):
        self.responses, self.responds = responses, responds
        self.sleep, self.clock = sleep, clock
        self.seen = responses()
        # The pauses so far, and the pauses since GitHub's last response.
        self.until, self.pauses, self.idle = 0.0, 0, 0
        self.failing: set[str] = set()
        self.waited: set[str] = set()
        # The repositories that GitHub failed in the run, by name, which a pause does not hold
        # for. The caller adds the repositories that GitHub failed in the run's earlier
        # invocations and in the finished run that the run follows.
        self.failed_before: set[str] = set()
        self.down = ""
        # Why the run stopped (`end`), after which no worker waits out a pause, and the tasks
        # that wait (`wait_unless_ended`).
        self.ended = ""
        self.waiting: set = set()

    async def ready(self) -> int:
        """Wait while a pause holds, and return the number of pauses so far. Raises
        `GitHubDown` once a failure after the last pause stopped the run, and
        `GitHubPauseEnded` when the run stops for another reason during the wait."""
        while (left := self.until - self.clock()) > 0:
            if self.ended:
                raise GitHubPauseEnded(self.ended)
            await wait_unless_ended(self, left)
        if self.down:
            raise GitHubDown(self.down)
        return self.pauses

    def end(self, reason: str) -> None:
        """End the pause of a run that stopped for `reason`: each worker that waits out a pause,
        which can last hours, raises `GitHubPauseEnded` instead."""
        end_wait(self, reason)

    async def failed(self, repository: str, sent_after: int, error) -> bool:
        """Record that GitHub failed `repository` in a step that began after `sent_after` pauses,
        and return whether the repository waits for the pause and goes through its step again.
        Raises `GitHubDown` in place of a pause that would begin after the last of
        GITHUB_PAUSES, and NoTokens when `responds` finds no GitHub access token that GitHub
        accepts."""
        if repository in self.failed_before and await asyncio.to_thread(self.responds):
            self.failing.discard(repository)
            return False
        responses = self.responses()
        if responses != self.seen:
            self.seen = responses
            self.failing.clear()
            self.waited.clear()
            self.idle = 0
        self.failing.add(repository)
        if len(self.failing) < 2:
            self.failed_before.add(repository)
            return False
        # A pause holds every worker. GitHub can fail two repositories in a row while it serves
        # the other repositories, as at the end of a pass, when no other request is in flight to
        # show its responses. These repositories then fail on their own, as a repository that
        # failed before does, instead of holding the run through every wait of GITHUB_PAUSES.
        # The check above handles a repository that failed before. The state is read again after
        # the call of `responds`, because another worker can begin a pause during that call.
        if (sent_after == self.pauses and self.idle < len(GITHUB_PAUSES)
                and repository not in self.failed_before
                and await asyncio.to_thread(self.responds)):
            self.failed_before.add(repository)
            self.failing.discard(repository)
            return False
        if sent_after == self.pauses:
            if self.idle == len(GITHUB_PAUSES):
                if repository in self.waited:
                    self.failed_before.add(repository)
                    self.failing.discard(repository)
                    return False
                self.down = (f"GitHub returned no usable response through "
                             f"{logs.hms(sum(GITHUB_PAUSES))} of pauses since its last response, "
                             f"the last for {repository} with {type(error).__name__} ({error})")
                raise GitHubDown(self.down)
            wait = GITHUB_PAUSES[self.idle]
            self.pauses += 1
            self.idle += 1
            self.until = self.clock() + wait
            logs.warning(COMPONENT, f"GitHub returned no usable response for "
                                    f"{len(self.failing):,} repositories since its last "
                                    f"response, the last for {repository} with "
                                    f"{type(error).__name__} ({error}). Pausing every worker for "
                                    f"{logs.hms(wait)}, then trying the failed repositories "
                                    f"again.")
        self.waited.add(repository)
        return True


def read_sampling_frame(path: Path) -> tuple[list[dict], str, forks.Thresholds]:
    """The repositories in the sampling frame, each with its GitHub id, name, default branch,
    the commit that GitHub named as its HEAD, the time at which GitHub created it, its
    description and whether GitHub marks it as a mirror, the id of the retrieval that the sampling frame was filtered from, and the thresholds
    that a fork's own development has to reach (`forks.thresholds_of`).

    The function refuses a sampling frame filtered before the sampling stage queried GitHub,
    because such a sampling frame has GHS's database ids rather than GitHub's, and a label is
    keyed by GitHub's id. It also refuses a sampling frame that does not record the thresholds
    that it was filtered with, and a sampling frame without GitHub's descriptions, which the
    model reads, or without GitHub's mirror flags, which the mirror criterion reads.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"The sampling frame {path} cannot be read ({error})") from error
    source = (data.get("_metadata") or {}).get("input_source") or {}
    if not source.get("github") or not source.get("retrieval_id"):
        raise SystemExit(f"The sampling frame {path} names no retrieval with GitHub's responses. "
                         f"Its ids are GHS's, not GitHub's. Run the sampling stage")
    thresholds = forks.thresholds_of(data.get("_metadata") or {})
    if thresholds is None:
        raise SystemExit(f"The sampling frame {path} does not record the thresholds of the fork "
                         f"heuristic (the contributorsMin of its query and its sampling window). "
                         f"Run the sampling stage")
    lacking = sum("description" not in item for item in data["items"])
    if lacking:
        raise SystemExit(f"{lacking:,} repositories in the sampling frame {path} have no field "
                         f"for GitHub's description. Run the sampling stage")
    lacking = sum("isMirror" not in item for item in data["items"])
    if lacking:
        raise SystemExit(f"{lacking:,} repositories in the sampling frame {path} have no field "
                         f"for GitHub's mirror flag, isMirror. Run the sampling stage")
    repositories = [{"id": int(item["id"]), "name": item["name"],
                     "default_branch": item.get("defaultBranch") or "",
                     "revision": item.get("lastCommitSHA") or "",
                     "created_at": item.get("createdAt") or "",
                     "activity": activity_of(item),
                     "description": str(item["description"] or "").strip(),
                     "is_mirror": item["isMirror"] is True}
                    for item in data["items"]]
    return repositories, source["retrieval_id"], thresholds


def _unlinked(text: str) -> str:
    """The text with its entities decoded and each link replaced by its text."""
    text = _LINK.sub(r"\1", html.unescape(text))
    text = _ORG_LINK.sub(lambda link: link.group(2) or link.group(1), text)
    text = _ATTRIBUTE_LINK.sub(r"\1", _ASCIIDOC_LINK.sub(r"\1", text))
    return _REFERENCE_LABEL.sub("]", _ASCIIDOC_CROSS_REFERENCE.sub(r"\1", text))


def _flattened(text: str) -> str:
    """The text without its other markup and the markers of list items between its words,
    lowercase, and with its whitespace collapsed."""
    text = _ROLE.sub("", _TAG.sub(" ", _INLINE_TAG.sub("", text)))
    text = _ESCAPED_MARK.sub(r"\1", _ESCAPED_SPACE.sub("", text))
    text = _ASCIIDOC_SCRIPT.sub(r"\2", _EMPHASIS.sub("", _ASCIIDOC_ROLE.sub("", text)))
    text = _LONE_MARKER.sub(" ", _PICTOGRAPH.sub(" ", text).translate(_MARKS))
    text = _HYPHEN_BREAK.sub(r"\1-", " ".join(text.lower().split()))
    return _SPACE_AFTER.sub("(", _SPACE_BEFORE.sub(r"\1", text))


def _replaced(text: str, values: dict, limit: int) -> str:
    """The text with each reference to an attribute in `values` replaced by its value, or the text
    as written when the result would be longer than `limit`."""
    size = len(text) + sum(len(values[reference[1]]) - len(reference[0])
                           for reference in _ATTRIBUTE_REFERENCE.finditer(text)
                           if reference[1] in values)
    if size > limit:
        return text
    return _ATTRIBUTE_REFERENCE.sub(lambda reference: values.get(reference[1], reference[0]), text)


def _attributes(text: str) -> dict:
    """The attributes that an AsciiDoc text sets, by name, each with references in its value
    replaced. A line inside a conditional block with a condition that does not hold sets nothing,
    because the rendered README does not read it. `ifdef::a,b[]` holds when one of the attributes
    is set and `ifdef::a+b[]` when both are, and `ifndef` holds when `ifdef` does not."""
    values = dict(GITHUB_ATTRIBUTES)
    conditions = []
    for line in _ATTRIBUTE_LINE.finditer(text):
        name, value, directive, names = line.groups()
        if name:
            if all(conditions):
                values[name] = _replaced(value or "", values,
                                         MAX_ATTRIBUTE_VALUE)[:MAX_ATTRIBUTE_VALUE]
        elif directive:
            holds = (all(part in values for part in names.split("+")) if "+" in names
                     else any(part in values for part in names.split(",")))
            conditions.append(holds if directive == "ifdef" else not holds)
        elif conditions:
            conditions.pop()
    return values


def _resolved(text: str) -> str:
    """The text with each reference to an attribute that it sets replaced by the attribute's value,
    as smallrye/smallrye-jwt's README writes "{microprofile-jwt}[{mp-jwt-name}]" and sets
    ":mp-jwt-name: Eclipse MicroProfile JWT RBAC"."""
    values = _attributes(text)
    return _replaced(text, values, MAX_RESOLVED_GROWTH * len(text))


def plain(text: str) -> str:
    """The text as a quotation of it is compared: without markup and the markers of list items,
    lowercase, and with its whitespace collapsed. A quotation that runs from a sentence into its
    list, as "It handles: Serialization & deserialization of core data structures" of
    dcSpark/cardano-multiplatform-lib, would otherwise keep the marker "-" between its words.
    `quoted` leaves the markers out of the quotation too, because a model can quote such a list
    with its markers, as Kimi K3 and `gpt-6.1-sol` quoted the list of that README."""
    return _flattened(_unlinked(_LIST_MARKER.sub("", text or "")))


def _parts(quote: str) -> list:
    """The parts of a quotation around an ellipsis or a semicolon, each sentence a part of its
    own. The links are replaced before the sentences are split, so that a link such as
    "[Joomla! Framework](...)" stays in one part."""
    parts = [_flattened(sentence).strip(" \"'.,;:!?-()") for fragment in _FRAGMENTS.split(quote)
             for sentence in _SENTENCES.split(fragment)]
    return [part for part in parts if part]


def _words(text: str) -> list:
    """The words of a flattened text, each with its end in the text, compared without the
    punctuation around them."""
    return [(word.group().strip(" \"'.,;:!?-()"), word.end())
            for word in re.finditer(r"\S+", text)]


def _found(part: str, source: str, start: int) -> int:
    """Where `part` ends in `source` from `start` on, or -1: word for word, or with one word in
    WORDS_PER_CHANGED_WORD replaced, left out or added.

    The search finds the earliest end of a passage of the source that the part matches within
    that many changed words (Sellers' algorithm over words). It computes each column only down to
    the last row still within the budget (Ukkonen's cutoff), so that the time for a long README
    grows on average with its words times the budget rather than with its words times the length
    of the part."""
    found = source.find(part, start)
    if found >= 0:
        return found + len(part)
    wanted = [word for word, _ in _words(part)]
    allowed = len(wanted) // WORDS_PER_CHANGED_WORD
    if not allowed:
        return -1
    # changes[row]: the fewest changed words with which the first `row` words of the part end at
    # the current word of the source. A passage may begin at any word, so row 0 stays 0.
    changes = list(range(len(wanted) + 1))
    active = allowed + 1
    for word, end in _words(source[start:]):
        diagonal = current = 0
        for row in range(1, active + 1):
            if wanted[row - 1] == word:
                current = diagonal
            else:
                current = 1 + min(diagonal, current, changes[row])
            diagonal, changes[row] = changes[row], current
        while changes[active] > allowed:
            active -= 1
        if active == len(wanted):
            return start + end
        active += 1
    return -1


def quoted(quote: str, *sources: str) -> bool:
    """Whether one of the sources contains the quotation, with its parts in their order, because
    the model quotes sentences of different paragraphs together, as in the purposes of TestDisk
    and PhotoRec. The quotation and the source are also compared without their parentheticals,
    because the model can leave one out, as it left out "(_pronounced "Voh Jen"_)" after "Vogen"
    in Vogen's README. A source that sets AsciiDoc attributes is compared as written and as
    rendered (`_resolved`), because the model can quote either form of the sentence. A quotation
    that joins the items of a numbered list into one line is also compared without their numbers
    (`_ITEM_NUMBER`)."""
    quote = _unlinked(_LIST_MARKER.sub("", quote))
    parts = _parts(quote)
    if not parts:
        return False
    texts = [plain(text) for source in sources
             for text in dict.fromkeys((source or "", _resolved(source or "")))]
    without = [_PARENTHETICAL.sub("", text) for text in texts]
    pairs = [(parts, text) for text in texts + without]
    pairs += [(_parts(_PARENTHETICAL.sub("", quote)), text) for text in without]
    if _ITEM_NUMBER.search(quote):
        pairs += [(_parts(_ITEM_NUMBER.sub("", quote)), text) for text in texts]
    for wanted, source in pairs:
        start = 0
        for part in wanted:
            start = _found(part, source, start)
            if start < 0:
                break
        else:
            if wanted:
                return True
    return False


def page_sentences(tools_record: list, linked: bool = True) -> list[tuple[str, str]]:
    """The purpose sentences of the pages that the model read in a decision, each with the
    page's address. The server of the tools returns only a sentence that it found in the page's
    text (`classification_tools.in_page`). The pages are those that the description, the README
    or the homepage links (`linked_page`), or with `linked` false, the issues and pull requests
    that only the input's list named. A page of another repository on GitHub, which the server
    names (`classification_tools.other_repository`), is left out, because its sentence says what
    another project is, as the README of sommerper/filetype-color, which file-icons/atom credits,
    says what that package is. A page of the repository itself under another name, which GitHub
    resolves to it, counts."""
    found = []
    for call in tools_record or []:
        if call.get("tool") != "read_linked_page" or call.get("is_error"):
            continue
        try:
            output = json.loads(call.get("output") or "")
        except ValueError:
            continue
        if not isinstance(output, dict) or not str(output.get("purpose_sentence") or "").strip():
            continue
        if str(output.get("other_repository") or "") or bool(call.get("linked")) != linked:
            continue
        found.append((str(output.get("url") or ""), str(output["purpose_sentence"])))
    return found


def quoted_purpose(quote: str, description: str, text: str,
                   tools_record: list) -> tuple[bool, str]:
    """Whether the description, the README or the purpose sentence of a page that one of them or
    the homepage links contains the purpose quote, and the address of the page when only the
    page's sentence does. The description of openjdk/lilliput is the address of the project's
    page, and neither it nor the README says what Lilliput is, which the page's sentence does."""
    if quoted(quote, description, text):
        return True, ""
    for url, sentence in page_sentences(tools_record):
        if quoted(quote, sentence):
            return True, url
    return False, ""


def quotation_fields(quote: str, description: str, text: str, tools_record: list) -> dict:
    """The fields of a decision for the purpose quote. The quote is `purpose_quote`, with its
    `purpose_source`, when `quoted_purpose` finds it. It is `purpose_quote_unlinked` when only the
    purpose sentence of an issue or a pull request of the repository contains it and no link
    names that issue or pull request, because such a sentence states no purpose. The models
    quoted the sentence of issue #98 of bbloomf/jgabc, which only the input's list of issues and
    pull requests named, and the README of bbloomf/jgabc states no purpose either. Otherwise it
    is `purpose_quote_refused`, which leaves the purpose unclear (`derive`) and starts a new
    attempt."""
    fields = {"purpose_quote": "", "purpose_source": ""}
    if not quote:
        return fields
    found, source = quoted_purpose(quote, description, text, tools_record)
    if found:
        return {"purpose_quote": quote, "purpose_source": source}
    unlinked = any(quoted(quote, sentence)
                   for _, sentence in page_sentences(tools_record, linked=False))
    return {**fields, "purpose_quote_unlinked" if unlinked else "purpose_quote_refused": quote}


def quote_check() -> str:
    """The SHA-256 of the purpose check's code, patterns and tolerance. A label records it, and
    `Run.derive_again` checks the quotation of a reused label again when the recorded digest
    differs from the current digest. The code is hashed without its docstrings, comments and
    layout (`forks.code_digest`), because under a new digest a run requests the README of every
    reused label again and writes each label again as a carried decision."""
    parts = [forks.code_digest(inspect.getsource(function)) for function in
             (_unlinked, _flattened, _replaced, _attributes, _resolved, plain, _parts, _words,
              _found, quoted, page_sentences, quoted_purpose, quotation_fields)]
    parts += [f"{pattern.pattern}\n{pattern.flags}" for pattern in
              (_LINK, _ASCIIDOC_LINK, _ATTRIBUTE_LINK, _ASCIIDOC_CROSS_REFERENCE, _ATTRIBUTE_LINE,
               _ATTRIBUTE_REFERENCE, _PARENTHETICAL, _ORG_LINK, _REFERENCE_LABEL, _ESCAPED_SPACE,
               _ESCAPED_MARK, _TAG, _INLINE_TAG, _ROLE, _HYPHEN_BREAK, _LIST_MARKER, _LONE_MARKER,
               _ITEM_NUMBER, _SPACE_BEFORE, _SPACE_AFTER, _SENTENCES, _EMPHASIS, _ASCIIDOC_ROLE,
               _ASCIIDOC_SCRIPT, _PICTOGRAPH, _FRAGMENTS)]
    parts += [repr(sorted(_MARKS.items())), repr(sorted(GITHUB_ATTRIBUTES.items())),
              str(MAX_ATTRIBUTE_VALUE), str(MAX_RESOLVED_GROWTH), str(WORDS_PER_CHANGED_WORD)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def activity_of(item: dict) -> dict:
    """The activity of a repository as the sampling frame records it, by the name that the model
    reads it under, with dates to the day."""
    activity = {}
    for field, name in ACTIVITY_FIELDS:
        value = item.get(field)
        if value is None or value == "":
            continue
        activity[name] = str(value)[:10] if field in ("createdAt", "lastCommit") else value
    return activity


def activity_text(activity: dict) -> str:
    """The activity as the model reads it, or a line stating that there is none."""
    if not activity:
        return NO_ACTIVITY
    return "; ".join(f"{name}: {value:,}" if isinstance(value, int) else f"{name}: {value}"
                     for name, value in activity.items())


def read_repository_ids(path: Path) -> list[int]:
    """The GitHub ids in the `id` column of a CSV, in its order."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "id" not in (reader.fieldnames or []):
            raise SystemExit(f"{path} has no id column")
        return [int(row["id"]) for row in reader]


def read_first_code() -> str:
    """The SHA-256 of the code of the MCP server that writes the results of READ_FIRST: the
    outline with its text, the metadata and the activity of the issues and pull requests. The
    classification parameters record it, because a change to this code changes what the input
    holds. The code is hashed without its docstrings, comments and layout (`forks.code_digest`),
    as for `quote_check`, because a new digest makes the next run label every repository again."""
    parts = [forks.code_digest(inspect.getsource(function)) for function in
             (tools._extension, tools.outline, tools.file_tree, tools._discussion, tools.activity,
              tools.repository_metadata, tools.owner_description)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def purpose_sentence_check() -> dict:
    """How the server of the tools takes a page's purpose sentence from the summary and checks it
    against the page: the fewest words, the pattern of the summary's line and the SHA-256 of the
    code and the pattern that compare the words (`classification_tools.in_page`), and of the
    code that names the page of another repository, whose sentence counts for no purpose
    (`classification_tools.other_repository`, `page_sentences`). A change to them changes which
    purpose a page states, and with it the label that the purpose decides. The code is hashed
    without its docstrings, comments and layout (`forks.code_digest`), as for `quote_check`."""
    parts = [forks.code_digest(inspect.getsource(function)) for function in
             (tools.purpose_of, tools._words_of, tools.in_page, tools.page_repository,
              tools.github_link, tools.other_repository)]
    parts.append(f"{tools._PAGE_MARKUP.pattern}\n{tools._PAGE_MARKUP.flags}")
    return {"min_words": tools.PURPOSE_MIN_WORDS,
            "line": [tools.PURPOSE_LINE.pattern, int(tools.PURPOSE_LINE.flags)],
            "code": hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()}


def classification_parameters(prompt: str, function_tools: list,
                              classifying: endpoints.Endpoint, settings: endpoints.Settings,
                              summary: endpoints.Endpoint, summary_effort: str,
                              output_schemas: dict) -> dict:
    """The classification parameters: everything that the models are given besides the README,
    and the models with their weights, their quantizations and the settings that they are called
    with. Together with the README, the parameters decide a label, and a label is therefore
    reused only under the same parameters.

    The classifying model is recorded under `classifying_model` with its Hugging Face id and its
    quantization (`endpoints.Endpoint.parameters`) and the settings of its requests: the
    reasoning effort, the temperature, the top_p, the seed and the output limit that are set
    (`endpoints.Settings.parameters`). The provider that serves a model, the pinned provider or
    its backup provider, is no classification parameter (the maintainer's decision of
    2026-11-04), and each decision records the provider of each of its responses instead
    (`Classifier.create`). The higher temperature and the second model decide only
    the labels after a failed response of the classifying model, and the parameters leave them
    out: each such label records them, and a run reuses it only under the same higher
    temperature or second model (`Run.applies`). A run without the second model, such as a check
    of one model, therefore has the parameters of a run with it. The summary model is recorded
    with its Hugging Face id, its quantization and its reasoning effort under `summary`. The
    label evaluation compares two models under the parameters apart from `classifying_model`
    (`label_cache.shared_parameters`).

    `output_schemas` holds the output schema of each tool as the MCP server lists it. Of the tools
    of READ_FIRST, which the model is not offered, the parameters record the output schemas, the
    constants that limit their results or select what they contain and the digest of the code
    that writes them, because the input holds their results. They also record every message with
    which the classification or the bridge replaces the result of a tool call, and the digest of
    the code that decides which page a link from the model names (`link_resolution`)."""
    return {"prompt": prompt, "tools": function_tools, "checklist_tool": CHECKLIST_TOOL,
            "classifying_model": {**classifying.parameters(), **settings.parameters()},
            "max_tool_rounds": MAX_TOOL_ROUNDS,
            "linked_pages": LINKED_PAGES, "request_tokens": REQUEST_TOKENS,
            "chars_per_token": CHARS_PER_TOKEN, "input": INPUT, "read_first": list(READ_FIRST),
            "read_first_input": READ_FIRST_INPUT, "no_description": NO_DESCRIPTION,
            "no_activity": NO_ACTIVITY, "activity_fields": [list(f) for f in ACTIVITY_FIELDS],
            "truncated": TRUNCATED,
            "read_first_results": {
                "output_schemas": {tool: output_schemas[tool] for tool in READ_FIRST},
                "activity_items": tools.ACTIVITY_ITEMS, "comment_chars": tools.COMMENT_CHARS,
                "pinned_issues": tools.PINNED_ISSUES, "activity_query": tools.ACTIVITY_QUERY,
                "practices": {practice: [pattern.pattern, int(pattern.flags)]
                              for practice, pattern in tools.PRACTICES.items()},
                "outline_file_types": tools.OUTLINE_FILE_TYPES, "outline_calls": OUTLINE_CALLS,
                "code": read_first_code()},
            "messages": {"refused_link": REFUSED_LINK, "read_first_call": READ_FIRST_CALL,
                         "linked_page_limit": LINKED_PAGE_LIMIT,
                         "tool_round_limit": TOOL_ROUND_LIMIT,
                         "no_such_tool": mcp_bridge.NO_SUCH_TOOL},
            "summary": {"prompt": tools.SUMMARY_PROMPT.read_text(encoding="utf-8"),
                        **summary.parameters(), "reasoning_effort": summary_effort,
                        "max_input_chars": tools.MAX_SUMMARY_INPUT_CHARS,
                        "output_schema": output_schemas["read_linked_page"],
                        "purpose_sentence": purpose_sentence_check()},
            "pages": {"max_bytes": tools.MAX_PAGE_BYTES, "max_redirects": tools.MAX_REDIRECTS,
                      "timeout": list(tools.PAGE_TIMEOUT), "types": list(tools.PAGE_TYPES),
                      "github_kinds": list(tools.GITHUB_KINDS),
                      "link_resolution": link_resolution()}}


def run_parameters(args, prompt: str, function_tools: list, output_schemas: dict) -> dict:
    """The classification parameters of a run with the command line `args`: its classifying
    model with the settings of its requests (`run_settings`), its summary model, `prompt` and the
    tools that the bridge lists (`classification_parameters`)."""
    return classification_parameters(prompt, function_tools, classifying_endpoint(args),
                                     run_settings(args), summary_endpoint(args),
                                     args.summary_effort, output_schemas)


def room(used_tokens: int) -> int:
    """The characters that a request may still contain when `used_tokens` of it are taken
    (rule 1)."""
    return int((REQUEST_TOKENS - used_tokens) * CHARS_PER_TOKEN)


def readme_input(name: str, path: str, text: str, other_chars: int, description: str = "",
                 activity: dict | None = None) -> str:
    """The repository's name, the description, the activity and the README as the model reads
    them, each enclosed in its tags. The README is cut, with a line stating the cut, where the
    request would cross REQUEST_TOKENS together with `other_chars` characters of the rest of the
    request, such as the instructions, the tool definitions with the tool of the checklist and
    the results that follow the README (rule 1)."""
    fixed = {"name": name, "description": description.strip() or NO_DESCRIPTION,
             "activity": activity_text(activity or {}), "path": path}
    whole = INPUT.format(**fixed, text=text)
    available = room(0) - other_chars
    if len(whole) <= available:
        return whole
    keep = max(0, available - len(INPUT.format(**fixed, text="")) - len(TRUNCATED))
    return INPUT.format(**fixed, text=text[:keep] + TRUNCATED)


# A web address, which may contain balanced parentheses, as lambdaloop/anipose's README links
# "https://www.cell.com/cell-reports/fulltext/S2211-1247(21)01179-7".
_ABSOLUTE = re.compile(r"https?://(?:[^\s<>\"'`()\[\]{}]|\([^\s<>\"'`()\[\]{}]*\))+",
                       re.IGNORECASE)
# The target of a Markdown link, in angle brackets or with balanced parentheses, and of an HTML
# `href`.
_TARGET = re.compile(r"\]\(\s*(?:<([^<>\n]*)>|((?:[^\s()<>]|\([^\s()<>]*\))+))"
                     r"(?:\s+[\"'][^)]*)?\s*\)|href=[\"']([^\"']+)[\"']", re.IGNORECASE)
# A CommonMark link reference definition, "[label]: target", indented by up to three spaces, with
# the target in angle brackets or without them, on the line of the label or the next, and an
# optional title, as herumi/xbyak's readme.md writes "[Install]: doc/install.md". A line may end
# in a carriage return, as in goldmansachs/reladomo's README.md. A label that starts with "^"
# names a footnote, which GitHub writes as "[^1]: text".
_DEFINITION = re.compile(r"^ {0,3}\[(?!\^)((?:[^\[\]\\\r\n]|\\.)+)\]:[ \t]*\r?\n?[ \t]*"
                         r"(?:<([^<>\r\n]*)>|([^\s<>]\S*))"
                         r"(?:[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?[ \t]*\r?$",
                         re.MULTILINE)
# The target of an AsciiDoc link or cross reference, as in oxidecomputer/omicron's README.adoc,
# "Please see xref:CONTRIBUTING.md[]", or of a cross reference to another document, as in
# "<<CONTRIBUTING.adoc#,contributing guide>>".
_ASCIIDOC_TARGET = re.compile(r"\b(?:link|xref):([^\s\[\]]+)\[|<<([^<>#,\s]+\.\w+)#[^<>]*>>")
# The target of a reStructuredText reference with the target inside it, as in
# "`guide <CONTRIBUTING.rst>`_", and of a target definition, as in ".. _HISTORY: HISTORY.rst" or
# the anonymous "__ HISTORY.rst". A target that ends in an underscore names another reference. The
# text of a reference may contain angle brackets, as tfranzel/drf-spectacular's README.rst writes
# "`Pydantic (>=2.0) <https://github.com/pydantic/pydantic>`_", and a named definition may write
# its target indented on the next line (`_RST_DEFINITION`).
_RST_TARGET = re.compile(r"`[^`]*<([^<>`\s]+[^<>`\s_])>`__?"
                         r"|^[ \t]*\.\.[ \t]+_(?:`[^`]+`|[^:`\n]+):"
                         r"(?:[ \t]+|[ \t]*\r?\n[ \t]+)(\S+[^\s_])[ \t]*\r?$"
                         r"|^[ \t]*(?:\.\.[ \t]+__:|__)[ \t]+(\S+[^\s_])[ \t]*\r?$", re.MULTILINE)
# A reStructuredText target definition with its name and its target, which a reference names, as
# ".. _pism-contributing: http://www.pism.io/docs/contributing" of pism/pism's README.rst, which
# "`Contributing to PISM <pism-contributing_>`_" names. A name may stand in backticks, and the
# anonymous ".. __: HISTORY.rst" has no name. The target may stand indented on the next line, as
# celery/celery's README.rst writes ".. _`Next steps`:". A line without indentation after
# ".. _section:" is the text of the document, so that this definition names no link, and neither
# does a chained target, such as ".. _first:" directly above ".. _second: docs/guide.rst", or an
# indirect target, such as ".. _first: second_".
_RST_DEFINITION = re.compile(r"^[ \t]*\.\.[ \t]+_(?!_:)(`[^`]+`|[^:`\n]+):"
                             r"(?:[ \t]+|[ \t]*\r?\n[ \t]+)(\S+[^\s_])"
                             r"[ \t]*\r?$", re.MULTILINE)
# The target of an Org link, as in "[[file:CONTRIBUTING.org][Contributing]]", without the
# `file:` prefix and the search option after `::`.
_ORG_TARGET = re.compile(r"\[\[(?:file:)?([^\[\]]+?)(?:::[^\[\]]*)?\](?:\[[^\[\]]*\])?\]")


def _from_repository(target: str, name: str, path: str, revision: str = "") -> str | None:
    """A relative link of the README at `path` as the github.com link of the file or directory
    that it points to at `revision`, which the tools read through GitHub (rule 5), or of the page
    on GitHub that it points to from the README's page: a page of the repository, such as an
    issue or a file at a branch, or of another repository. None for an anchor, another scheme, a
    path from the repository's root that leaves it, a branch without a path, or a path above
    GitHub's root."""
    target = target.split("#")[0].split("?")[0].strip()
    if not target or ":" in target.split("/")[0]:
        return None
    if target.startswith("//"):
        return "https:" + target
    rooted = target.startswith("/")
    joined = target.lstrip("/") if rooted else posixpath.join(posixpath.dirname(path), target)
    normal = posixpath.normpath(joined)
    if normal == ".":
        return None
    if normal != ".." and not normal.startswith("../"):
        return f"https://github.com/{name}/blob/{revision or 'HEAD'}/{normal}"
    if rooted:
        return None
    # GitHub resolves a relative link from the README's page under /<owner>/<repo>/blob/<ref>/,
    # so that of a README at the root, "../../issues/98" names the repository's issue 98,
    # "../master/CONTRIBUTING.md" the file at the branch master, and "../../../jackson" the
    # repository jackson of the same owner.
    page = posixpath.normpath(posixpath.join(name, "blob", revision or "HEAD", joined))
    if page == "." or page.split("/")[0] == "..":
        return None
    if page == f"{name}/blob" or (page.startswith(f"{name}/blob/") and page.count("/") == 3):
        return None
    return f"https://github.com/{page}"


# An issue or a pull request of a repository on GitHub.
_OWN_DISCUSSION = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/\d+$")
# The address of an issue or a pull request of a repository on GitHub in any form that a link
# can give it: over http or https, with or without www, in any letter case, and with or without a
# trailing slash, a query or a fragment.
_DISCUSSION = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s?#]+/[^/\s?#]+)/"
                         r"(?:issues|pull)/(\d+)/?(?:[?#]\S*)?$", re.IGNORECASE)


def _discussion(link: str) -> tuple[str, str] | None:
    """The repository, in lowercase, and the number of the issue or the pull request at `link`,
    or None for another page. An issue and a pull request of a repository share one sequence of
    numbers, and GitHub redirects each kind of link to the other."""
    found = _DISCUSSION.match(link.strip())
    return (found.group(1).lower(), found.group(2)) if found else None


def _absolute(target: str, name: str, path: str, revision: str = "") -> str | None:
    """The target of a link of the README as an absolute URL: a web address as it is, and a
    relative link as `_from_repository` reads it."""
    target = target.strip()
    if target.lower().startswith(("http://", "https://")):
        return target
    return _from_repository(target, name, path, revision)


def _reference_label(label: str) -> str:
    """A label of a link reference as CommonMark matches it: case folded, with its whitespace
    collapsed."""
    return " ".join(label.split()).casefold()


def reference_definitions(text: str, name: str, path: str,
                          revision: str = "") -> dict[str, str]:
    """The link reference definitions and the reStructuredText target definitions of a README,
    each label (`_reference_label`) with its target as an absolute URL, as `readme_links` reads
    it. reStructuredText matches a reference name as CommonMark matches a label. The first
    definition of a label counts, as in CommonMark, and a label whose first target is no link,
    such as an anchor or a mail address, has no entry."""
    targets = {}
    for definition in _DEFINITION.finditer(text or ""):
        label = _reference_label(definition.group(1))
        if label not in targets:
            target = next(group for group in definition.groups()[1:] if group is not None)
            targets[label] = _absolute(target, name, path, revision)
    for definition in _RST_DEFINITION.finditer(text or ""):
        label = _reference_label(definition.group(1).strip("`"))
        if label not in targets:
            targets[label] = _absolute(definition.group(2), name, path, revision)
    return {label: link for label, link in targets.items() if link is not None}


def readme_links(text: str, name: str, path: str, revision: str = "") -> set[str]:
    """Every link of a README as an absolute URL: its web addresses, and its relative links as
    the github.com links of what they point to at `revision`. The links are read from Markdown,
    with its link reference definitions, HTML, AsciiDoc, reStructuredText and Org."""
    text = text or ""
    links = {match.rstrip(".,;:!?*_") for match in _ABSOLUTE.findall(text)}
    targets = [next(group for group in match.groups() if group is not None)
               for pattern in (_TARGET, _ASCIIDOC_TARGET, _RST_TARGET, _ORG_TARGET)
               for match in pattern.finditer(text)
               if any(group is not None for group in match.groups())]
    for target in targets:
        if (absolute := _absolute(target, name, path, revision)) is not None:
            links.add(absolute)
    return links | set(reference_definitions(text, name, path, revision).values())


def described_links(description: str, tools_record: list) -> set[str]:
    """The links that the repository's description and its homepage name: the web addresses in
    the description and the homepage that the metadata of READ_FIRST gives, which the input
    shows the model. The owner names them beside the README's links, as openjdk/lilliput's
    description is the address of the project's page, which states its purpose."""
    links = {match.rstrip(".,;:!?*_") for match in _ABSOLUTE.findall(description or "")}
    for call in tools_record:
        if call.get("tool") != "get_repository_metadata" or call.get("is_error"):
            continue
        try:
            metadata = json.loads(call.get("output") or "")
        except ValueError:
            continue
        homepage = str(metadata.get("homepage") or "").strip() if isinstance(metadata, dict) \
            else ""
        # GitHub keeps a homepage as its owner wrote it, also without a scheme, such as
        # angryip.org, which a browser opens over https.
        if homepage and not _ABSOLUTE.match(homepage) and _HOST.match(homepage):
            homepage = f"https://{homepage}"
        if _ABSOLUTE.fullmatch(homepage):
            links.add(homepage)
    return links


# A web address without its scheme: a host name with a dot, then an optional path.
_HOST = re.compile(r"^([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?:[/?#]\S*)?$")


# A link that the model passes in the markup of the README: an AsciiDoc link or cross
# reference, with or without its text, an Org link, a target followed by AsciiDoc's link text, a
# reStructuredText target in angle brackets, or an Org target with its `file:` prefix.
_WRITTEN_TARGET = re.compile(r"^(?:(?:link|xref):([^\s\[\]]+)(?:\[.*\])?"
                             r"|\[\[(?:file:)?([^\[\]]+?)(?:::[^\[\]]*)?\](?:\[[^\[\]]*\])?\]"
                             r"|([^\s\[\]<>]+)\[[^\[\]]*\]|<([^<>\s]+)>"
                             r"|file:([^\s:]+)(?:::.*)?)$")
# A link that the model passes as a Markdown reference to a link reference definition of the
# README: "[label]", "[label][]" or "[text][label]".
_WRITTEN_REFERENCE = re.compile(r"^\[([^\[\]]+)\](?:\[([^\[\]]*)\])?$")
# A link that the model passes in the markup of a reStructuredText reference with its target or
# the name of a target definition inside it, "`guide <CONTRIBUTING.rst>`_" or
# "`Contributing to PISM <pism-contributing_>`_". The text may contain angle brackets.
_WRITTEN_RST_TARGET = re.compile(r"^`[^`]*<([^<>`\s]+)>`__?$")
# A link that the model passes as a reStructuredText reference to a target definition of the
# README by its name: "pism-contributing_" or "`a name`_".
_WRITTEN_RST_REFERENCE = re.compile(r"^(?:([^\s`<>\[\]]+)|`([^`<>]+)`)_$")
# A file or a directory of a repository on GitHub at a branch, a tag or a commit.
_GITHUB_PATH = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s]+/[^/\s]+)/"
                          r"(?:blob|tree|raw)/[^/\s]+/([^?#\s]+)", re.IGNORECASE)
# A web address: the scheme and the host, the path, the query and the fragment.
_WEB_ADDRESS = re.compile(r"^(https?://[^/?#\s]+)(/[^?#\s]*)?(\?[^#\s]*)?(?:#\S*)?$",
                          re.IGNORECASE)


def _repository_path(link: str, name: str) -> str | None:
    """The path of a github.com link to a file or directory of the repository `name` at any
    branch, tag or commit, or None for another link."""
    found = _GITHUB_PATH.match(link)
    if not found or found.group(1).lower() != name.lower():
        return None
    return posixpath.normpath(found.group(2).strip("/"))


def _page_of(link: str) -> tuple[str, str, str] | None:
    """The page that a web address names, or None for another link: the scheme and the host in
    lowercase, the path without its trailing slashes, and the query, with an empty query as
    none. A request does not contain the fragment. A path with and without its trailing slash
    count as one page, because the tool reads the address that the README writes for it
    (`_known_link`). `readme_links` reads a web address that ends in "?" without it, and the
    model may pass the address as the README writes it."""
    found = _WEB_ADDRESS.match(link)
    if not found:
        return None
    query = found.group(3) or ""
    return (found.group(1).lower(), (found.group(2) or "").rstrip("/"),
            "" if query == "?" else query)


def _known_link(link: str, links: set[str]) -> str | None:
    """`link` if it is among `links`, or else the first of `links` in sorted order that names the
    same page (`_page_of`). The result is always a member of `links`, so that a link from the
    model reaches no address that the README, the description or the homepage does not write. A
    README can link one page with several fragments, as OhMyGuus/BetterCrewLink's does, and the
    order makes the address the same in every run."""
    if link in links:
        return link
    page = _page_of(link)
    if page is None:
        return None
    return min((known for known in links if _page_of(known) == page), default=None)


def resolve_link(link: str, links: set[str], name: str, path: str, revision: str = "",
                 definitions: dict[str, str] | None = None) -> str | None:
    """The absolute URL that a link from the model names, if the README contains the link or the
    link names an issue or a pull request of the repository, which the input's issues and pull
    requests list.

    The model may pass the link in the markup of the README, a link reference definition or a
    reStructuredText target definition whole, or a reference to a definition by its label or
    name, which `definitions` maps to the definition's target (`reference_definitions`). A
    github.com link to a file or a directory of the repository at any branch, tag or commit
    stands for the README's link to the same path. The URL for such a path is at `revision`, as
    for a relative link (rule 5), because a branch that the README names can have been renamed
    since, and GitHub returns 404 for a file at a branch that no longer exists. Any other link
    stands for the README's link to the same page, also without its trailing slash or its
    fragment, and the URL is the address that the README writes (`_known_link`). A link without
    a scheme names a file that the README links before it names a web address of that host."""
    link = (link or "").strip()
    if definition := _DEFINITION.match(link):
        link = next(group for group in definition.groups()[1:] if group is not None).strip()
    elif reference := _WRITTEN_REFERENCE.match(link):
        label = _reference_label(reference.group(2) or reference.group(1))
        link = (definitions or {}).get(label, link)
    elif rst_definition := _RST_DEFINITION.match(link):
        link = rst_definition.group(2)
    elif rst_target := _WRITTEN_RST_TARGET.match(link):
        link = rst_target.group(1)
    elif written := _WRITTEN_TARGET.match(link):
        link = next(group for group in written.groups() if group is not None).strip()
    if rst_reference := _WRITTEN_RST_REFERENCE.match(link):
        label = _reference_label(rst_reference.group(1) or rst_reference.group(2))
        link = (definitions or {}).get(label, link)
    absolute = _from_repository(link, name, path, revision) or link
    own = _OWN_DISCUSSION.match(absolute)
    if own and own.group(1).lower() == name.lower():
        return absolute
    wanted = _repository_path(absolute, name)
    if wanted is not None and any(_repository_path(known, name) == wanted for known in links):
        return _from_repository("/" + wanted, name, path, revision)
    # A homepage without its scheme is among the links with https (`described_links`), and the
    # model may pass it as the metadata writes it, which would otherwise read as a relative link.
    # A file that the README links comes first, because GitHub renders "[c](CONTRIBUTING.md)" as
    # that file also when the README writes https://contributing.md/. The host has to stand in the
    # letter case of the address, because a file that the README only mentions, such as
    # CONTRIBUTING.md, does not name that site. A github.com address names the path at the
    # README's commit, as it does with its scheme.
    host = _HOST.match(link)
    if host and (known := _known_link(f"https://{link}", links)) is not None \
            and known.startswith(f"https://{host.group(1)}"):
        own_path = _repository_path(known, name)
        return known if own_path is None else \
            _from_repository("/" + own_path, name, path, revision)
    return _known_link(absolute, links) if wanted is None else None


def linked_page(url: str, links: set[str], name: str) -> bool:
    """Whether the description, the README or the homepage links the page at `url`, which
    `resolve_link` resolved, rather than only the issues and pull requests of the input listing
    it as an issue or a pull request of the repository. Only a linked page's purpose sentence
    states the purpose (`page_sentences`), because a model can quote an issue that no link names,
    such as unpinned issue #98 of bbloomf/jgabc about why its author wrote the software. A link
    names the issue or the pull request in any form of its address that `_discussion` reads, and
    a relative link of the README names it as `_from_repository` resolves the link."""
    own = _discussion(url)
    if own is None or own[0] != name.lower():
        return True
    return any(_discussion(link) == own for link in links)


def link_resolution() -> str:
    """The SHA-256 of the code and the patterns that read the links of a README and resolve a
    link from the model to the page that read_linked_page reads. The classification parameters
    record it, because a change to this code changes which pages the model can read and which
    address the summary is made from. The code is hashed without its docstrings, comments and
    layout (`forks.code_digest`), as for `quote_check`, because a new digest makes the next run
    label every repository again."""
    parts = [forks.code_digest(inspect.getsource(function)) for function in
             (_from_repository, _absolute, _reference_label, reference_definitions, readme_links,
              _repository_path, _page_of, _known_link, resolve_link, described_links, _discussion,
              linked_page)]
    parts += [f"{pattern.pattern}\n{pattern.flags}" for pattern in
              (_ABSOLUTE, _HOST, _TARGET, _DEFINITION, _ASCIIDOC_TARGET, _RST_TARGET,
               _RST_DEFINITION, _ORG_TARGET, _OWN_DISCUSSION, _DISCUSSION, _WRITTEN_TARGET,
               _WRITTEN_REFERENCE, _WRITTEN_RST_TARGET, _WRITTEN_RST_REFERENCE, _GITHUB_PATH,
               _WEB_ADDRESS)]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# The output of read_linked_page for a link that `resolve_link` refuses. It names the link forms
# that `readme_links` reads, because a README can link a page in a form that it does not read.
REFUSED_LINK = ("The link is neither among the links that the tool reads from the description, "
                "the homepage and the README, which are their web addresses and the targets of "
                "the README's Markdown, HTML, AsciiDoc, reStructuredText and Org links, nor an "
                "issue or a pull request of the repository. Pass the link as the description, the "
                "homepage or the README writes it, relative or absolute, or the link of an issue "
                "or a pull request as the input's issues and pull requests give it.")
# The output for a call of a tool of READ_FIRST, which the model is not offered because the input
# holds its result. It names the tool that the model called.
READ_FIRST_CALL = "The input contains the result of {tool}."
# The output of read_linked_page past the limit of linked pages, and of every call in a round past
# the limit of rounds, after which the next request offers CHECKLIST_TOOL alone (rules 13 and 14).
LINKED_PAGE_LIMIT = "The limit of {limit} linked pages per repository is reached."
TOOL_ROUND_LIMIT = ("The limit of {limit} rounds of tool calls is reached. Call "
                    f"{CHECKLIST_TOOL_NAME} with the content that you have.")


def recorded_results(decision: dict) -> dict:
    """The calls of READ_FIRST that `decision` recorded for the input of its first request, by
    tool. A decision records them as calls of round 0. A decision made before the classification
    read these results itself recorded none, and its tool calls are the model's."""
    return {call["tool"]: call for call in decision.get("tools") or []
            if call.get("round") == 0 and call.get("tool") in READ_FIRST}


def usage_of(response) -> dict:
    """A response's token counts."""
    usage = getattr(response, "usage", None)
    details = getattr(usage, "input_tokens_details", None)
    output = getattr(usage, "output_tokens_details", None)
    return {"input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "reasoning_tokens": getattr(output, "reasoning_tokens", 0) or 0}


def billed_usage(response) -> dict:
    """A response's token counts with the input tokens that the API wrote to its prompt cache,
    which OpenAI bills at a rate of their own. The index and the decisions record the counts of
    `usage_of`, which leave these tokens among the input tokens."""
    details = getattr(getattr(response, "usage", None), "input_tokens_details", None)
    return {**usage_of(response),
            "cache_write_tokens": getattr(details, "cache_write_tokens", 0) or 0}


def stopped_at_limit(response, limit: int | None) -> bool:
    """Whether `response` stopped at its output limit: "the response returns `status:
    "incomplete"` with `incomplete_details.reason: "max_output_tokens"`" (openrouter.ai/docs/
    guides/best-practices/reasoning-tokens), or its output tokens reached `limit`, the limit
    that its request sent."""
    details = getattr(response, "incomplete_details", None)
    if (getattr(response, "status", None) == "incomplete"
            and getattr(details, "reason", None) == "max_output_tokens"):
        return True
    output = getattr(getattr(response, "usage", None), "output_tokens", 0)
    return limit is not None and isinstance(output, int) and output >= limit


def checklist_calls(response) -> list:
    """The calls of CHECKLIST_TOOL in `response`, of which the last is the final response."""
    return [item for item in response.output
            if item.type == "function_call" and item.name == CHECKLIST_TOOL_NAME]


def argument_text(arguments) -> str:
    """The arguments of a call of CHECKLIST_TOOL as the text that the model wrote."""
    return arguments if isinstance(arguments, str) else str(arguments or "")


def kept_arguments(arguments) -> dict:
    """The length of the arguments of a call of CHECKLIST_TOOL, and their first and their last
    KEPT_ARGUMENT_CHARS characters, which overlap for arguments of fewer than twice as many."""
    text = argument_text(arguments)
    return {"chars": len(text), "start": text[:KEPT_ARGUMENT_CHARS],
            "end": text[-KEPT_ARGUMENT_CHARS:]}


def message_text(response) -> str:
    """The text that the model wrote in the messages of `response` instead of a call of
    CHECKLIST_TOOL: the text of each `output_text` part of its output items of the type
    `message`, joined in order as the SDK's `Response.output_text` joins them. A response with a
    call of CHECKLIST_TOOL, or without such a part, has none, and the text is empty."""
    output = getattr(response, "output", None)
    if not isinstance(output, list) or checklist_calls(response):
        return ""
    return "".join(part.text for item in output if getattr(item, "type", None) == "message"
                   for part in getattr(item, "content", None) or []
                   if getattr(part, "type", None) == "output_text"
                   and isinstance(getattr(part, "text", None), str))


def kept_message(response) -> dict | None:
    """The length of the text of the messages of `response` (`message_text`), and its first and
    its last KEPT_MESSAGE_CHARS characters, which overlap for a text of fewer than twice as many,
    or None for a response without such text."""
    text = message_text(response)
    if not text:
        return None
    return {"chars": len(text), "start": text[:KEPT_MESSAGE_CHARS],
            "end": text[-KEPT_MESSAGE_CHARS:]}


def printable(value) -> str:
    """`value` as text for one line of the log: as it is when each of its characters is
    printable, and otherwise with Python's escapes, such as `\\n` for a line break, so that a
    value from the API cannot break the line."""
    text = str(value)
    return text if text.isprintable() else repr(text)


def response_details(response) -> str:
    """The status of `response`, the reason of its `incomplete_details`, the types of its output
    items and its output tokens, from which the log shows how a response without a call of
    CHECKLIST_TOOL ended. A response with an empty output or without one, such as a Response
    that holds only an error (`error_body`), has "no output items". A response with the text of
    a message instead of the call (`message_text`) also names the length of the text and its
    first LOGGED_MESSAGE_CHARS characters, escaped so that a line break in them cannot break the
    line of the log."""
    status = getattr(response, "status", None)
    reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
    output = getattr(response, "output", None)
    items = "no output items"
    if isinstance(output, list) and output:
        types = ", ".join(printable(getattr(item, "type", None)) for item in output)
        items = f"the output items [{types}]"
    tokens = getattr(getattr(response, "usage", None), "output_tokens", None)
    ended = (f"the status {printable(status) if status else 'none'}"
             f"{f' for the reason {printable(reason)}' if reason else ''}, {items}")
    counted = f"{f'{tokens:,}' if isinstance(tokens, int) else 'no count of the'} output tokens"
    text = message_text(response)
    if not text:
        return f"{ended} and {counted}"
    return (f"{ended}, {counted} and a message of {len(text):,} characters that begins with "
            f"{text[:LOGGED_MESSAGE_CHARS]!r}")


def service_ended(response, limit: int | None = None) -> str:
    """Why the API's service ended `response`, or "" when it did not. The service ended a
    response with an error or with the status "failed", whether or not the response has a call of
    CHECKLIST_TOOL. A response that stopped at its output limit, `limit` for a request that sent
    one (`stopped_at_limit`), is the model's failed response, and so is a response with the
    status "incomplete" for another reason, such as "content_filter": the content of the
    conversation decides such a reason, and the model would write the same content again in
    response to the same request."""
    if stopped_at_limit(response, limit):
        return ""
    if getattr(response, "error", None):
        return named_error(response)
    if getattr(response, "status", None) == "failed":
        return "the status failed and no error"
    return ""


def named_error(response) -> str:
    """The code and the message of the error of `response` for the log, and the `error_type` of
    a Response of OpenRouter with the status "failed", in which "the precise reason is
    preserved" when the code of the error collapses to `server_error` (openrouter.ai/docs/
    api_reference/errors-and-debugging)."""
    error = response.error
    code = getattr(error, "code", "") or "without a code"
    kind = getattr(response, "error_type", None)
    return (f"the error {printable(code)} ({printable(getattr(error, 'message', '') or error)})"
            f"{f' with the error_type {printable(kind)}' if kind else ''}")


def error_body(response) -> ServiceEnded | None:
    """The failure of a Response that holds an error and no output items, or None for any other
    response. OpenRouter sends the status 200 "as soon as the provider accepts the request", and
    "a non-streaming request receives an error body" for a failure after that point. On the
    Responses API, "the non-streaming JSON body" of such a failure carries "the canonical
    `error_type` at the top level of the response object", as in `{"id": "resp_abc123",
    "status": "failed", "error": { "code": "server_error", "message": "Invalid credentials" },
    "error_type": "authentication"}`, because "many distinct internal types collapse to
    `server_error`" in the code of the error (openrouter.ai/docs/api_reference/
    errors-and-debugging). The SDK parses a body without `output`, as in that example, into a
    Response without output items, and a body with an empty `output` into a Response with an
    empty output. The service ended the response in both forms (`ServiceEnded`), whatever the
    code of its error. No such body is a refusal for spent credit, because a key or an account
    out of credits gets a request error with "the same status code as `error.code`" (the same
    page)."""
    output = getattr(response, "output", None)
    if not getattr(response, "error", None) or (isinstance(output, list) and output):
        return None
    return ServiceEnded(f"the API ended the response with {named_error(response)}, "
                        f"{response_details(response)}")


def checked_response(response, name: str, limit: int | None = None) -> dict:
    """The arguments of the model's call of CHECKLIST_TOOL for repository `name` as a JSON
    object, or ModelFailure when the final response does not follow the response format: no call
    of the tool, arguments that are no JSON object, a response for another repository, a field of
    a criterion, of `maintenance_and_use` or `engineering` without one of its values, or a count
    of the README's words by language that is not a list of languages with numbers of words.
    A response that stopped at its output limit, `limit` for a request that sent one
    (`stopped_at_limit`), without the call or in its arguments, so that they are not JSON, fails
    as OUTPUT_LIMIT, and any other response as RESPONSE_FORMAT. The text of each failure names how
    the response ended (`response_details`), and that of arguments that are not JSON also names
    the error of `json.loads`, the length of the arguments and their last LOGGED_ARGUMENT_CHARS
    characters, escaped so that they stay on one line of the log. A failure of a response with the
    call keeps the start and the end of its arguments (`kept_arguments`), and a failure of a
    response without the call the start and the end of the text of its messages, when it has
    such text (`kept_message`)."""
    calls = checklist_calls(response)
    limited = stopped_at_limit(response, limit)
    details = response_details(response)
    if not calls and limited:
        raise ModelFailure(f"the model's response stopped at its output limit before it called "
                           f"{CHECKLIST_TOOL_NAME} ({details})", kind=OUTPUT_LIMIT,
                           model_message=kept_message(response))
    if not calls:
        raise ModelFailure(f"the model's response does not call {CHECKLIST_TOOL_NAME} "
                           f"({details})", kind=RESPONSE_FORMAT,
                           model_message=kept_message(response))
    text = argument_text(calls[-1].arguments)
    kept = kept_arguments(text)

    def failed(message: str, kind: str = RESPONSE_FORMAT) -> ModelFailure:
        return ModelFailure(f"{message} ({details})", kind=kind, arguments=kept)
    try:
        parsed = json.loads(text)
    except ValueError as error:
        cut = (f"{printable(error)}, {len(text):,} characters ending in "
               f"{text[-LOGGED_ARGUMENT_CHARS:]!r}")
        # Arguments that stop with the response at its output limit were cut off by the limit,
        # as a response without the call was, and an endpoint that holds the model to the
        # schema returns such responses too (FORMAT_STREAK).
        if limited:
            raise failed(f"the model's response stopped at its output limit in the arguments of "
                         f"{CHECKLIST_TOOL_NAME}, which are not JSON ({cut})",
                         OUTPUT_LIMIT) from error
        raise failed(f"the arguments of {CHECKLIST_TOOL_NAME} are not JSON ({cut})") from error
    # JSON that is no object, such as `[]`, would otherwise raise an AttributeError below and
    # stop the run as an unexpected error instead of failing this attempt.
    if not isinstance(parsed, dict):
        raise failed("the model's response is not a JSON object")
    engineering = parsed.get("engineering")
    if engineering not in ENGINEERING:
        raise failed(f"the model's response sets engineering to {engineering!r}")
    if str(parsed.get("repo_name", "")).lower() != name.lower():
        raise failed(f"the model's response is for {parsed.get('repo_name')!r}")
    exclusions = parsed.get("exclusions")
    if not isinstance(exclusions, dict) or set(exclusions) != set(EXCLUSIONS) or not all(
            isinstance(exclusions[key], dict)
            and all(exclusions[key].get(fact) in FIELD_VALUES for fact in facts)
            for key, facts in CRITERION_FACTS.items()):
        raise failed("the model's response lacks a field of an exclusion criterion")
    words = parsed.get("readme_words_by_language")
    if not isinstance(words, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("language"), str)
            and type(entry.get("words")) is int and entry["words"] >= 0 for entry in words):
        raise failed(f"the model's response sets readme_words_by_language to {words!r}")
    use = parsed.get("maintenance_and_use")
    if not isinstance(use, dict) or not all(use.get(field) in FIELD_VALUES
                                            for field in MAINTENANCE_AND_USE):
        raise failed("the model's response lacks a field of maintenance_and_use")
    return parsed


def as_input(item):
    """An output item of a response as an input item of the next request, as the response gave
    it: a reasoning item with its `encrypted_content`, a function call or a message."""
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if isinstance(item, list):
        return [as_input(value) for value in item]
    if hasattr(item, "__dict__"):
        return {key: as_input(value) for key, value in vars(item).items()}
    return item


def new_history() -> dict:
    """The failed attempts of the classifying model at one repository in a run, before any:
    their counts and tokens (FAILURE_COUNTS), and the first and the last failed response, each
    with its kind as `reason`, its `error` and, for a response with the text of a message
    instead of the call of CHECKLIST_TOOL, the length, the start and the end of the text as
    `message` (`kept_message`), or None (`Classifier.history`)."""
    return {"counts": dict.fromkeys(FAILURE_COUNTS, 0), "first": None, "last": None}


class Classifier:
    """The model and its tools, one repository at a time."""

    def __init__(self, client, bridge, model: str, prompt: str, settings: endpoints.Settings,
                 parameters_sha: str, sleep=None, service_tier: str | None = None, clock=None,
                 summary_model: str = "", endpoint: endpoints.Endpoint | None = None,
                 summary: endpoints.Endpoint | None = None, credit_check=any_credit,
                 higher: endpoints.Settings | None = None,
                 backup: endpoints.Endpoint | None = None, role: str = "the classifying model"):
        """`settings` are those of the requests (`endpoints.Settings`), and `higher` those of
        the attempts at a repository after a failed response, or None for a classifier that
        keeps `settings` (`label`). `summary_model` is the model with which the
        server of the tools summarizes pages, and the classification counts the tokens of the
        summaries under it. Without it, they count under the model that the response of a
        summary reports. `endpoint` and `summary` are where the two models are called, OpenAI's
        API by default, and `credit_check` tests the credit of a service while it refuses the
        requests for spent credit (`CreditPause`). `backup` is the backup provider of the model
        at `endpoint` (`endpoints.backup_of`), or None, and `role` names the model in the log
        lines of the backup provider (`endpoints.Providers`)."""
        self.client, self.bridge, self.model = client, bridge, model
        self.endpoint = endpoint or endpoints.endpoint(model)
        self.settings, self.higher = settings, higher
        self.summary_model = summary_model
        self.summary = summary or endpoints.endpoint(summary_model or tools.SUMMARY_MODEL)
        self.template = Template(prompt)
        self.parameters_sha = parameters_sha
        # The tier sets the price and the speed of a response and changes nothing that the model
        # is given. It is therefore not among the classification parameters, and each decision
        # records it. OpenRouter has no tier of OpenAI's, and the provider that serves a request
        # sets its price.
        self.service_tier = None if self.endpoint.openrouter else service_tier
        # The waits and the clock of the run, which its pauses and retry rounds share.
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        # One pause for the repositories decided at once, which share the API's service.
        self.pause = ServicePause(self.sleep, self.clock)
        # The pinned provider and the backup provider of the model, and which of them serves the
        # next request (`again`).
        self.providers = endpoints.Providers(self.endpoint, backup, role, self.clock)
        # The pauses for spent credit, one for the classifying model's service and one for the
        # summary model's when it is another service.
        self.credit = CreditPause(self.sleep, self.clock, credit_check,
                                  classifying=self.endpoint.service)
        # The parsed responses that wait for GitHub's facts, kept until the facts are read
        # (`assess`), including the responses that a resumed run or comparison restores from its
        # partial file.
        self.kept: dict[str, dict] = {}
        # The responses of each repository and its failed responses, over every attempt of every
        # retry round of the run (`label`).
        self.tallies: dict[tuple, dict] = {}
        self.format_streak = FormatStreak()
        # The pauses of every worker before the latest request of each repository was sent
        # (`ServicePause.ready`), by its name, from which `label_readme` tells whether a final
        # response that failed the format was in flight when the latest pause began
        # (FORMAT_PAUSES).
        self.sent_after: dict[str, int] = {}
        # The provider that returned the latest response for each repository, by its name, which
        # `format_failed` names and from which it tells a streak of the backup provider.
        self.responders: dict[str, endpoints.Endpoint] = {}
        # The repositories that an earlier call of `label` in the run left without a decision, in
        # this invocation or an earlier one (`model_failures`), those that the model failed in
        # the finished run that the run follows (`failed_in_finished_run`), and every repository
        # that the second model labels, by the key of the repository that `label` is given, the
        # GitHub id in a run. Their responses count toward FormatStreak only while it is open,
        # because a retry round, the main round of a resumed run or of a run that tries again
        # the repositories that a finished run left without a decision, and the pass of the
        # second model can hold nothing but such repositories, and their failures would pause
        # and then stop a run on a working endpoint.
        self.failed_earlier: set = set()
        # The failed attempts at each repository in the run, by the key of the repository that
        # `label` is given, the GitHub id in a run, over every round and every invocation
        # (`new_history`), from which `label` takes the higher temperature and the records of a
        # label of the higher temperature or of the second model. A resumed run reads them from
        # its partial file (`failure_histories`). The GitHub id stays when GitHub returns the
        # README under the name of a renamed repository, which the response names.
        self.history: dict = {}
        self.requests = 0
        self.usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
                      "reasoning_tokens": 0, "summary_input_tokens": 0,
                      "summary_output_tokens": 0}
        # The tokens of the responses by the role and the model, the tier that the request named
        # and the tier that the response reports, which OpenAI bills (`label_cache.TOKEN_KEY`).
        # The run adds them to its tokens file as an invocation ends (`take_billed`).
        self.billed: dict[tuple, dict] = {}

    def end_pauses(self, reason: str) -> None:
        """End the pause for the API's service and the pause for spent credit of a run or a
        comparison that stopped for `reason` (`ServicePause.end`, `CreditPause.end`)."""
        self.pause.end(reason)
        self.credit.end(reason)

    def add(self, counts: dict, spent: dict | None) -> None:
        """Add tokens to the run's counts and to `spent`, the repository's counts."""
        for key, value in counts.items():
            self.usage[key] += value
            if spent is not None:
                spent[key] = spent.get(key, 0) + value

    def bill(self, role: str, model: str, requested_tier: str, service_tier: str,
             counts: dict) -> None:
        """Count one response under its role, its model, the tier that its request named and the
        tier that it reports. OpenAI bills a request at the tier that served it, and a response
        reports that tier: "This response value may be different from the value set in the
        parameter" (openai 3.13.0, `types/responses/response.py`)."""
        row = self.billed.setdefault((role, model, requested_tier, service_tier),
                                     dict.fromkeys(label_cache.TOKEN_COUNTS, 0))
        row["requests"] += 1
        for name in label_cache.TOKEN_COUNTS[1:]:
            row[name] += int(counts.get(name) or 0)

    def take_billed(self) -> dict:
        """The tokens that `bill` counted since the last call, which the counts then leave out,
        so that the tokens file gets the tokens of each response once."""
        billed, self.billed = self.billed, {}
        return billed

    def again(self, target: endpoints.Endpoint, test: bool, repository: str, sent_after: int,
              error) -> tuple[bool, endpoints.Endpoint | None]:
        """Whether the request for `repository` that the API's service failed with `error` at
        `target` is sent again rather than using an attempt, and the endpoint that it goes to at
        once, or None for the endpoint that `endpoints.Providers.next` gives after the pause.

        Without a backup provider, for a failure of the backup provider, and for OpenRouter's
        refusal of a request because the requests in flight fill their budget
        (`endpoints.in_flight`), the failure is one of the service as `ServicePause.failed`
        handles it. That budget belongs to the API key and not to the provider, and the backup
        provider, with higher prices, would reserve more of it. A failure of the pinned provider
        goes to the backup provider at once when the request tested the pinned provider or the
        backup provider serves every request already. Otherwise it pauses every worker during
        the first PAUSES_BEFORE_ATTEMPTS pauses since the last response, and where it would use
        an attempt after them, or as the failure of one repository while the requests of other
        repositories succeed, the same request goes to the backup provider instead. Once the
        pinned provider has failed the requests of two repositories or more since its last
        response (`endpoints.Providers.failed`) and a failure would use an attempt, the backup
        provider serves every request of the model, and the pause begins again with the shortest
        wait for the failures of the backup provider. Two repositories are the sign of an outage
        that `ServicePause` takes, and the pauses come first while the requests of both are in
        flight."""
        providers = self.providers
        backup = (providers.available
                  if target == providers.pinned and not endpoints.in_flight(error) else None)
        if backup is not None and (test or providers.switched):
            return True, backup
        if backup is not None:
            providers.failed(repository)
        if self.pause.failed(repository, sent_after, error, backup=backup is not None):
            return True, None
        if backup is None:
            return False, None
        if len(providers.failing) >= 2:
            pauses = (f" through {self.pause.idle:,} pauses" if self.pause.idle else "")
            providers.switch(f"failed the requests of {len(providers.failing):,} repositories"
                             f"{pauses} since its last response, the last with "
                             f"{type(error).__name__} ({error})")
            self.pause.succeeded()
        return True, backup

    async def create(self, instructions: str, input_value, previous: str | None = None,
                     spent: dict | None = None, checklist_only: bool = False,
                     repository: str = "", settings: endpoints.Settings | None = None,
                     tally: dict | None = None, served: list | None = None):
        """One request to the Responses API for `repository`, with `settings`, the classifier's
        own by default. The instructions go with every request, because a response does not keep
        them for the next request. The request offers the tools of the bridge and
        CHECKLIST_TOOL, or with `checklist_only` CHECKLIST_TOOL alone, after the last round of
        tool calls. Through OpenRouter, the request routes without fallbacks to the provider
        that serves the model's requests, the pinned provider or its backup provider
        (`endpoints.Providers`, `endpoints.Endpoint.routing`), names no service tier, and
        `input_value` is the whole conversation, because OpenRouter's Responses API keeps none
        and rejects `previous_response_id` (`Classifier.follow`). It sends the temperature and
        the top_p of `settings` as `temperature` and `top_p`, the seed in the body, which
        OpenRouter passes to the endpoint, and the output limit as `max_output_tokens`, each
        when it is set, to either provider.

        The request waits out a pause of every worker. After a failure of the service that pauses
        the workers, a response that the service ended among them (`service_ended`, `error_body`),
        or a refusal for spent credit, the same request is sent again with the same previous
        response or conversation (`ServicePause`, `CreditPause`), and where such a failure of the
        pinned provider would use an attempt, the same request goes to the backup provider
        (`again`). If the conversation started again from its first request instead, a later
        request would pay the model again for every request before it and the summary model for
        its pages. Each response that carries usage, the tokens that the API bills, adds its
        tokens to `spent`, counts under `responses` of `tally`, the repository's requests
        (`label_readme`), and for an OpenRouter model adds the provider that served it to
        `served`, with `backup` set for the backup provider, also a response that the service
        ended. A Response that holds only an error carries no usage and counts as no
        response."""
        settings = settings or self.settings
        tools = ([CHECKLIST_TOOL] if checklist_only
                 else [*self.bridge.function_tools, CHECKLIST_TOOL])
        arguments = {"model": self.model, "reasoning": {"effort": settings.reasoning_effort},
                     "instructions": instructions, "input": input_value, "tools": tools}
        if previous:
            arguments["previous_response_id"] = previous
        sampled = settings.sampled()
        if self.endpoint.openrouter:
            for name in ("temperature", "top_p"):
                if name in sampled:
                    arguments[name] = sampled[name]
            if settings.max_output_tokens is not None:
                arguments["max_output_tokens"] = settings.max_output_tokens
        elif self.service_tier:
            arguments["service_tier"] = self.service_tier
        forced = None
        while True:
            probe = await self.credit.ready(self.endpoint.service)
            sent_after = await self.pause.ready()
            target, test = (forced, False) if forced is not None else self.providers.next()
            forced = None
            if target.openrouter:
                arguments["extra_body"] = {**target.routing(),
                                           **({"seed": sampled["seed"]} if "seed" in sampled
                                              else {})}
            # A request that tests the pinned provider during a switch goes without the SDK's
            # retries (`endpoints.TEST_RETRIES`), so that a test that fails holds its worker for
            # one request before the same request goes to the backup provider.
            client = (self.client.with_options(max_retries=endpoints.TEST_RETRIES) if test
                      else self.client)
            try:
                response = await client.responses.create(**arguments)
            except json.JSONDecodeError as error:
                # A response with status 200 and a body that does not parse, such as a page of
                # the API's edge. Because a JSONDecodeError is a ValueError, the run would read it
                # as a failure of GitHub, but it is a failed attempt of the model.
                self.credit.probe_ended(self.endpoint.service, probe)
                raise ModelFailure(f"the API's response is not a Response ({error})") from error
            except Exception as error:
                spent_credit = endpoints.credit_refusal(error)
                if spent_credit is not None:
                    self.credit.refused(spent_credit, probe)
                    continue
                self.credit.probe_ended(self.endpoint.service, probe)
                if service_failure(error):
                    resend, forced = self.again(target, test, repository, sent_after, error)
                    if resend:
                        continue
                raise
            # For a response with status 200 and another content type, the SDK returns the
            # text, and for JSON without output items or an error, a Response without output
            # items. Reading their output would stop the run.
            failure = error_body(response)
            if failure is None and not isinstance(getattr(response, "output", None), list):
                self.credit.probe_ended(self.endpoint.service, probe)
                raise ModelFailure(f"the API's response is a {type(response).__name__} "
                                   f"without output items or an error")
            # A Response, also one with an error, shows that the service accepted the request.
            # With spent credit, the service refuses the request with an HTTP error instead, and
            # the Response therefore ends the pause for credit. A response counts when it carries
            # usage, the tokens that the API bills: it counts toward the requests and the
            # responses of its repository and adds its tokens. A Response that holds only an
            # error carries no usage, with an empty output or without one, and counts as no
            # response, as an HTTP error does.
            self.credit.succeeded(self.endpoint.service)
            if getattr(response, "usage", None) is not None:
                self.requests += 1
                if tally is not None:
                    tally["responses"] += 1
                self.add(usage_of(response), spent)
                if self.endpoint.openrouter:
                    # Without fallbacks, the provider that the request went to served it, and it
                    # sets the price.
                    requested = reported = target.provider
                    if served is not None:
                        served.append({"provider": target.provider,
                                       **({"backup": True}
                                          if target == self.providers.backup else {})})
                else:
                    requested = self.service_tier or UNNAMED_TIER
                    reported = getattr(response, "service_tier", "") or ""
                self.bill(label_cache.CLASSIFYING_MODEL, self.model, requested, reported,
                          billed_usage(response))
            if failure is None:
                ended = service_ended(response, settings.max_output_tokens
                                      if self.endpoint.openrouter else None)
                if not ended:
                    break
                calls = checklist_calls(response)
                failure = ServiceEnded(
                    f"the API ended the response with {ended}, {response_details(response)}",
                    arguments=kept_arguments(calls[-1].arguments) if calls else None,
                    model_message=kept_message(response))
            # A response that the service ended fails as a server error does: it pauses every
            # worker together with the failures of another repository, goes to the backup
            # provider where it would use an attempt, and otherwise uses an attempt without
            # raising the temperature.
            resend, forced = self.again(target, test, repository, sent_after, failure)
            if resend:
                continue
            raise failure
        # A response of the pinned provider ends a switch to the backup provider. The pause
        # follows the provider that serves the requests, and a response of the backup provider to
        # one repository's request while the pinned provider serves leaves its failures counted.
        self.providers.responded(target)
        if target == self.providers.serving:
            self.pause.succeeded()
        self.sent_after[repository] = sent_after
        self.responders[repository] = target
        return response

    def opening(self, first_input: str):
        """The input of a repository's first request: the text itself for OpenAI, and the first
        item of the conversation for OpenRouter."""
        if self.endpoint.openrouter:
            return [{"type": "message", "role": "user", "content": first_input}]
        return first_input

    async def follow(self, instructions: str, conversation, response, outputs: list,
                     spent: dict, repository: str, checklist_only: bool = False,
                     settings: endpoints.Settings | None = None, tally: dict | None = None,
                     served: list | None = None):
        """The request after a round of tool calls, with the outputs of the round and
        `settings`, and the conversation that it sent. For OpenAI, the request continues from the
        response through `previous_response_id`. For OpenRouter, it sends the whole conversation
        with every output item of the response, its reasoning items included, as the response
        gave them, to either provider. `spent`, `tally` and `served` count the responses as in
        `create`."""
        if not self.endpoint.openrouter:
            followed = await self.create(instructions, outputs, previous=response.id,
                                         spent=spent, checklist_only=checklist_only,
                                         repository=repository, settings=settings, tally=tally,
                                         served=served)
            return followed, conversation
        conversation = [*conversation, *(as_input(item) for item in response.output), *outputs]
        followed = await self.create(instructions, conversation, spent=spent,
                                     checklist_only=checklist_only, repository=repository,
                                     settings=settings, tally=tally, served=served)
        return followed, conversation

    async def tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """One tool call through the bridge, its output and whether it is an error.

        A failure that the server marks (`classification_tools.failure`) is no evidence about the
        repository and is not returned to the model. When GitHub refuses every GitHub access
        token of the server, the call raises `NoTokens`, and when the API refuses the summary
        model, it raises `ModelRefused`. Both stop the run. After a failure of GitHub, of this
        machine's network or of the summary model, the call is made again after each of
        TOOL_WAITS, so that the conversation goes on from the model's last response. After the
        last wait, a failure of GitHub or of the network raises `readmes.NoResponse`, a failure of
        the summary model raises `SummaryFailed`, and both fail the repository. When the summary
        model's service refuses the summary for spent credit, the requests to that service wait
        for the credit (`CreditPause`), and the call is made again without using a wait. Only a
        response of that service ends the wait. A call without a result within the bridge's time
        fails the attempt, because passed to the model, it would let a label be cached without
        the page or the outline."""
        waits = iter(TOOL_WAITS)
        probe = False
        while True:
            try:
                output, error = await self.bridge.call(name, arguments)
            except mcp_bridge.ToolTimeout as timeout:
                self.credit.probe_ended(self.summary.service, probe)
                raise ModelFailure(str(timeout)) from timeout
            failed = tools.failure_of(output) if error else None
            if failed is None:
                if probe:
                    self.credit.succeeded(self.summary.service)
                return output, error
            kind, message = failed
            if kind == tools.SUMMARY_NO_CREDIT:
                self.credit.refused(endpoints.CreditRefusal(self.summary.service, message),
                                    probe)
                probe = await self.credit.ready(self.summary.service)
                continue
            self.credit.probe_ended(self.summary.service, probe)
            probe = False
            if kind == tools.NO_GITHUB_ACCESS_TOKEN:
                raise NoTokens(f"the MCP server of the tools has no GitHub access token left "
                               f"({message})")
            if kind == tools.SUMMARY_REFUSED:
                refusal_kind = (ModelNotOffered if message.startswith(tools.MODEL_NOT_OFFERED)
                                else ModelRefused)
                raise refusal_kind(f"the API refuses the requests of the summary model "
                                   f"({message})")
            wait = next(waits, None)
            if wait is None:
                break
            logs.warning(COMPONENT, f"The tool {name} failed for "
                                    f"{arguments.get('repo_name') or 'a repository'} "
                                    f"({message}). Calling it again in {logs.hms(wait)}.")
            await self.sleep(wait)
        if kind in (tools.GITHUB_FAILED, tools.NETWORK_FAILED):
            raise readmes.NoResponse(f"the tool {name} failed, because {message}")
        raise SummaryFailed(f"the tool {name} failed, because {message}")

    async def outline(self, name: str, revision: str, available: int) -> tuple[str, bool, dict]:
        """The outline's result in at most `available` characters, which the server shortens
        the outline to (rule 1), and the arguments of the last call."""
        max_chars = max(available, 1)
        for _ in range(OUTLINE_CALLS):
            arguments = {"repo_name": name, "revision": revision, "max_chars": max_chars}
            output, error = await self.tool("get_file_tree", arguments)
            excess = len(output) - available
            if error or excess <= 0 or max_chars == 1:
                break
            max_chars = max(max_chars - excess, 1)
        return output, error, arguments

    async def page(self, arguments: dict, spent: dict) -> tuple[str, bool, dict]:
        """A page's summary as the model reads it, without the summary's tokens and model, which
        are counted and returned. For a summary model that OpenRouter serves, the returned fields
        also name the provider that served the summary under `provider`, from the service tier
        that the server reports, with `backup` set when it was not the pinned provider of the
        summary model (`classification_tools.Summarizer`)."""
        output, error = await self.tool("read_linked_page", arguments)
        try:
            result = json.loads(output)
        except ValueError:
            return output, error, {}
        if not isinstance(result, dict):
            return output, error, {}
        made = {key: result.pop(key) for key in SUMMARY_FIELDS if key in result}
        billing = {key: result.pop(key) for key in SUMMARY_BILLING if key in result}
        self.add({"summary_input_tokens": int(made.get("input_tokens") or 0),
                  "summary_output_tokens": int(made.get("output_tokens") or 0)}, spent)
        # A page that could not be read has no summary, and no request was made for it. A
        # summary through OpenRouter names the provider that served it as the tier, the pinned
        # provider or the backup provider, which sets its price.
        if made.get("model") or made.get("input_tokens") or made.get("output_tokens"):
            reported = str(billing.get("service_tier") or "")
            if self.summary.openrouter:
                provider = reported or self.summary.provider
                self.bill(label_cache.SUMMARY_MODEL,
                          self.summary_model or str(made.get("model") or ""), provider, provider,
                          {**made, **billing})
                made = {**made, "provider": provider,
                        **({"backup": True} if provider != self.summary.provider else {})}
            else:
                self.bill(label_cache.SUMMARY_MODEL,
                          self.summary_model or str(made.get("model") or ""), UNNAMED_TIER,
                          reported, {**made, **billing})
        return json.dumps(result, ensure_ascii=False), error, made

    async def read_first(self, name: str, revision: str, path: str, text: str,
                         description: str, activity: dict | None, other_chars: int,
                         state: dict, recorded: dict | None = None) -> str:
        """The input of a repository's first request: its name, description, activity and
        README (`readme_input`) and the results of READ_FIRST, each call recorded in
        `state["tools"]` as round 0. The README is cut before the other results would cross the
        request's limit, and the outline is read last, to the room that the request has left
        (rule 1).

        `recorded` holds the calls of READ_FIRST that a decision recorded (`recorded_results`),
        which `changed_labels.py` repeats. Their results are taken as the decision recorded them,
        and only a result that the decision lacks is read anew, because a result read anew holds
        GitHub's current data, such as the current description in the metadata."""
        recorded = recorded or {}
        results = {}
        for tool in READ_FIRST[:2]:
            if tool in recorded:
                call = dict(recorded[tool])
            else:
                arguments = {"repo_name": name}
                output, error = await self.tool(tool, arguments)
                call = {"round": 0, "tool": tool, "arguments": arguments, "is_error": error,
                        "output": output}
            state["tools"].append(call)
            results[tool] = call["output"]
        empty = READ_FIRST_INPUT.format(issues=results["get_issues_and_pull_requests"],
                                        metadata=results["get_repository_metadata"], outline="")
        head = readme_input(name, path, text, other_chars + len(empty), description, activity)
        if "get_file_tree" in recorded:
            call = dict(recorded["get_file_tree"])
        else:
            output, error, arguments = await self.outline(
                name, revision, room(0) - other_chars - len(head) - len(empty))
            call = {"round": 0, "tool": "get_file_tree", "arguments": arguments,
                    "is_error": error, "output": output}
        state["tools"].append(call)
        return head + READ_FIRST_INPUT.format(issues=results["get_issues_and_pull_requests"],
                                              metadata=results["get_repository_metadata"],
                                              outline=call["output"])

    async def run_tools(self, calls: list, name: str, revision: str, path: str,
                        links: set[str], definitions: dict[str, str], state: dict,
                        round_number: int, spent: dict) -> list:
        """The outputs of one round of tool calls, each recorded in `state["tools"]`. `links`
        and `definitions` are the links of the README and its link reference definitions by
        label, which `resolve_link` reads. A call with arguments that are not a JSON object, or
        of a tool that the model is not offered, gets an output that says so, and the model
        continues: such a response is no failed response."""
        outputs = {}
        for call in calls:
            try:
                arguments = json.loads(call.arguments or "{}")
            except ValueError:
                arguments = None
            # A strict function tool's arguments are an object. Anything else, which the API could
            # send only against its schema, counts as no arguments instead of raising an error
            # where a tool reads an argument.
            if not isinstance(arguments, dict):
                arguments = {}
            made, linked = {}, {}
            if call.name in READ_FIRST:
                # The API offers the model only OFFERED, and the input holds these results.
                output, error = (READ_FIRST_CALL.format(tool=call.name), True)
            elif call.name == "read_linked_page":
                url = resolve_link(str(arguments.get("url", "")), links, name, path, revision,
                                   definitions)
                if url is None:
                    output, error = (REFUSED_LINK, True)
                elif state["linked_pages"] >= LINKED_PAGES:
                    output, error = (LINKED_PAGE_LIMIT.format(limit=LINKED_PAGES), True)
                    state["limits"].add("linked_pages")
                else:
                    state["linked_pages"] += 1
                    arguments = {"url": url, "repo_name": name}
                    output, error, made = await self.page(arguments, spent)
                    linked = {"linked": linked_page(url, links, name)}
            else:
                output, error = await self.tool(call.name, arguments)
            state["tools"].append({"round": round_number, "tool": call.name,
                                   "arguments": arguments, "is_error": error,
                                   "output": output, **linked,
                                   **{f"summary_{key}": value for key, value in made.items()}})
            outputs[call.call_id] = output
        return [{"type": "function_call_output", "call_id": call.call_id,
                 "output": outputs[call.call_id]} for call in calls]

    async def label_readme(self, name: str, revision: str, path: str, text: str,
                           description: str = "", activity: dict | None = None,
                           recorded: dict | None = None, tally: dict | None = None,
                           spent: dict | None = None,
                           settings: endpoints.Settings | None = None,
                           repository_key=None) -> dict:
        """One attempt at a repository's label with the requests of `settings`, the classifier's
        own by default: the label and the criteria that it follows from, the model's response,
        the tool calls, the model that the API reports, the temperature and the top_p of the
        requests under `sampling` and the tokens spent. `recorded` holds the results of
        READ_FIRST that the input takes from a decision (`read_first`). `tally` counts the
        responses of the attempt under `responses`, each response that the API bills, also one
        that the service ended before the same request was sent again (`create`), and its failed
        responses under `failed_responses` (SECOND_MODEL_REASONS). The decision's `requests` are
        the responses of the attempt. `spent` receives the tokens of the attempt, also of an
        attempt that fails. `repository_key` names the repository in `failed_earlier`, `name` by
        default. For an OpenRouter model, the decision lists under `served_by` the provider that
        served each of its responses, in order, with `backup` set for the backup provider
        (`create`), and each summary of a page names its provider (`page`)."""
        settings = settings or self.settings
        tally = tally if tally is not None else {"responses": 0, "failed_responses": 0}
        served: list = []
        instructions = self.template.safe_substitute(max_tool_rounds=MAX_TOOL_ROUNDS,
                                                     linked_pages=LINKED_PAGES)
        links = readme_links(text, name, path, revision)
        definitions = reference_definitions(text, name, path, revision)
        state = {"tools": [], "linked_pages": 0, "limits": set()}
        # The repository's own tokens, because the run's totals also include the tokens of the
        # repositories decided at the same time.
        spent = spent if spent is not None else {}
        spent.update({key: 0 for key in self.usage})
        other_chars = (len(instructions) + len(json.dumps(self.bridge.function_tools))
                       + len(json.dumps(CHECKLIST_TOOL)))
        conversation = self.opening(await self.read_first(name, revision, path, text,
                                                          description, activity, other_chars,
                                                          state, recorded))
        links |= described_links(description, state["tools"])
        # The responses before the attempt, from which the decision counts its requests.
        before = tally["responses"]
        response = await self.create(instructions, conversation, spent=spent, repository=name,
                                     settings=settings, tally=tally, served=served)
        rounds = 0
        while True:
            calls = [item for item in response.output if item.type == "function_call"]
            # The call of CHECKLIST_TOOL is the final response, and a response without a call
            # fails the response format (`checked_response`).
            if not calls or any(call.name == CHECKLIST_TOOL_NAME for call in calls):
                break
            if "tool_rounds" in state["limits"]:
                tally["failed_responses"] += 1
                raise ModelFailure(f"the model called another tool in a request that offers "
                                   f"only {CHECKLIST_TOOL_NAME} ({response_details(response)})",
                                   kind=TOOL_CALL, model_message=kept_message(response))
            if rounds == MAX_TOOL_ROUNDS:
                # A round past the limit: each call gets the message that the limit is reached,
                # and the next request offers CHECKLIST_TOOL alone.
                state["limits"].add("tool_rounds")
                outputs = []
                for call in calls:
                    output = TOOL_ROUND_LIMIT.format(limit=MAX_TOOL_ROUNDS)
                    state["tools"].append({"round": rounds + 1, "tool": call.name,
                                           "arguments": {}, "is_error": True, "output": output})
                    outputs.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": output})
                response, conversation = await self.follow(instructions, conversation, response,
                                                           outputs, spent, name,
                                                           checklist_only=True,
                                                           settings=settings, tally=tally,
                                                           served=served)
                continue
            rounds += 1
            outputs = await self.run_tools(calls, name, revision, path, links, definitions,
                                           state, rounds, spent)
            response, conversation = await self.follow(instructions, conversation, response,
                                                       outputs, spent, name, settings=settings,
                                                       tally=tally, served=served)
        # The pauses of every worker before the final request was sent, or None when another
        # attempt at a repository of the same name took them (FORMAT_PAUSES).
        sent_after = self.sent_after.pop(name, None)
        responder = self.responders.pop(name, None)
        try:
            parsed = checked_response(response, name,
                                      settings.max_output_tokens if self.endpoint.openrouter
                                      else None)
        except ModelFailure as error:
            tally["failed_responses"] += 1
            # A response that stopped at its output limit ended before the model could follow
            # the format, and an endpoint that holds the model to the schema returns such
            # responses too. The response of a repository that failed an earlier round counts
            # only while a streak is open (FORMAT_STREAK, FORMAT_PAUSES).
            if error.kind != OUTPUT_LIMIT and (
                    self.format_streak.open
                    or (name if repository_key is None else repository_key)
                    not in self.failed_earlier):
                self.format_failed(name, error, sent_after, responder)
            raise
        self.format_streak.passed(name)
        engineering, exclusions = parsed["engineering"], parsed["exclusions"]
        use = parsed["maintenance_and_use"]
        shared = {field: use[field] for field in MAINTENANCE_AND_USE}
        # The model's facts for each criterion, from which `derive` decides it.
        checklist = {key: {**{fact: exclusions[key][fact] for fact in facts},
                           "evidence": str(exclusions[key].get("evidence", ""))}
                     for key, facts in CRITERION_FACTS.items()}
        purpose = str(parsed.get("purpose") or "").strip()
        # The purpose counts as stated only when the model quotes it from the description, the
        # README or the purpose sentence of a page that one of them or the homepage links. For
        # example, munin/munin's README contains requirements and installation steps, and the
        # model can state a purpose for it that none of these sections names.
        quotation = quotation_fields(str(parsed.get("purpose_quote") or "").strip(), description,
                                     text, state["tools"])
        refused = quotation.get("purpose_quote_refused", "")
        decision = {"model_label": engineering,
                "readme_words_by_language": [
                    {"language": entry["language"].strip(), "words": entry["words"]}
                    for entry in parsed["readme_words_by_language"]],
                "maintenance_and_use": {**shared, "evidence": str(use.get("evidence", ""))},
                "exclusions": checklist,
                "fork_of": str(parsed.get("fork_of") or "").strip(),
                "purpose_quote": quotation["purpose_quote"],
                "purpose_source": quotation["purpose_source"], "purpose": purpose,
                "quote_check": quote_check(),
                "description": description, "activity": activity or {},
                "service_tier": getattr(response, "service_tier", "") or "",
                "justification": str(parsed.get("justification", "")),
                "response_model": getattr(response, "model", "") or "",
                "sampling": settings.sampled() if self.endpoint.openrouter else {},
                **({"served_by": served} if self.endpoint.openrouter else {}),
                "tools": state["tools"], "limits_reached": sorted(state["limits"]),
                "usage": {"requests": tally["responses"] - before, "tool_rounds": rounds,
                          "linked_pages": state["linked_pages"], **spent}}
        decision.update(quotation)
        derive(decision)
        if refused:
            raise QuotationRefused("the model's purpose quote is in neither the description, "
                                   "the README nor the purpose sentence of a page that the "
                                   "description, the homepage field or the README links to",
                                   decision)
        return decision

    def format_failed(self, name: str, error: ModelFailure, sent_after: int | None,
                      responder: endpoints.Endpoint | None = None) -> None:
        """Count the final response for repository `name` that failed the response format with
        `error` toward FormatStreak, a response of `responder`, the provider that returned it,
        the classifier's endpoint by default. Once the streak has reached FORMAT_STREAK responses over
        FORMAT_STREAK_REPOSITORIES repositories, the failure of a request sent after `sent_after`
        pauses of every worker, when no pause began after it was sent, pauses every worker
        (`ServicePause.hold`), and after FORMAT_PAUSES pauses without a recovery of the endpoint
        it raises FormatIgnored. The failure of a request that was in flight when the latest pause
        began counts toward the streak and begins no pause.

        A streak that a response of the backup provider completes retires the backup provider
        instead (`endpoints.Providers.retire`): the pinned provider serves every request again,
        the count of the streak starts again, and no pause begins. The backup provider does not
        hold the model to the schema, and the pinned provider, which the pilots checked, may only
        be down."""
        streak = self.format_streak
        if not streak.failed(name, str(error)) or sent_after != self.pause.pauses:
            return
        responder = responder or self.endpoint
        providers = self.providers
        if providers.available is not None and responder == providers.backup:
            providers.retire(f"returned {streak.count:,} final responses in a row, for "
                             f"{len(streak.repositories):,} repositories, that failed the "
                             f"response format, the last because {error}")
            streak.restart()
            return
        failed = (f"{streak.count:,} final responses in a row of {responder.described()}, "
                  f"for {len(streak.repositories):,} repositories, failed the response format, "
                  f"the last because {error}")
        if streak.pauses == FORMAT_PAUSES:
            raise FormatIgnored(
                f"{failed}. Through {FORMAT_PAUSES:,} pauses of every worker, "
                f"{streak.failed_since:,} of the {streak.failed_since + streak.passed_since:,} "
                f"final responses after the first pause failed the format, without responses in "
                f"the format for {FORMAT_STREAK_REPOSITORIES:,} repositories in a row. The "
                f"endpoint does not call {CHECKLIST_TOOL_NAME} with its schema. Label with "
                f"another endpoint or model") from error
        seconds = streak.pause()
        self.pause.hold(seconds)
        logs.warning(COMPONENT, f"{failed}. Pausing every worker for {logs.hms(seconds)} (pause "
                                f"{streak.pauses:,} of {FORMAT_PAUSES:,}). Responses in the format "
                                f"for {FORMAT_STREAK_REPOSITORIES:,} repositories in a row make "
                                f"the next pause the first again, and after pause "
                                f"{FORMAT_PAUSES:,}, the failed response that would begin another "
                                f"pause ends the labeling with the endpoint.")

    async def label(self, name: str, revision: str, path: str, text: str,
                    description: str = "", activity: dict | None = None,
                    recorded: dict | None = None, repository_key=None) -> dict:
        """The repository's label, up to ATTEMPTS attempts, or ModelFailure after the last.
        `recorded` holds the results of READ_FIRST that the input takes from a decision
        (`read_first`). `repository_key` names the repository in `history` and
        `failed_earlier`, its GitHub id in a run (`Run.decide`), and `name` by default.

        The SDK has already retried a rate limit, a timeout and a server error SDK_RETRIES
        times. A failure of the service while the requests of another repository fail too uses no
        attempt during the first PAUSES_BEFORE_ATTEMPTS pauses since the last response, and the
        same request is sent again in the conversation after the pause of every worker
        (`Classifier.create`, `ServicePause`). A response that the service ended is such a
        failure (`ServiceEnded`), and when it uses an attempt, the next attempt keeps the
        temperature. A request that the API refuses as malformed fails the same way every time
        and ends the repository at once, unless a new attempt does not receive the refusal again
        (TRANSIENT_REFUSALS). A summary that the summary model failed through every wait of a
        tool call also ends the repository (`SummaryFailed`). When no attempt succeeded and at
        least one gave a purpose quote that neither the description nor the README contains, the
        purpose is unclear. For example, libcgroup's README states no
        purpose, and the model can write a new purpose in every attempt. The attempt whose
        decision is then taken is no failed attempt.

        The attempts use the classifier's settings until one of them ends in a failed response
        (SECOND_MODEL_REASONS), and with `higher` set, every later attempt at the repository in
        the run uses `higher`, the settings of the higher temperature. The run's record of the
        failed attempts (`history`) decides it, so that a retry round and a resumed run continue
        at the higher temperature. A decision of the higher temperature records it under
        `higher_temperature` with the kind and the error of the failed response that raised the
        temperature, the start and the end of the text of its messages when it has such text
        instead of the call of CHECKLIST_TOOL (`kept_message`), and the counts and tokens of the
        failed attempts at the repository in the run. A decision at the classifier's own settings
        after a failed response at the repository, such as a decision of a classifier without
        `higher`, records the kind and the error of the classifier's first failed response there
        and the start and the end of the text of its messages, when it has such text, under
        FIRST_FAILED_RESPONSE, so that the text is kept at either settings. A stop in the middle
        of a repository's attempts starts the repository again from its first attempt, at the
        temperature that the record of its earlier rounds gives.

        The ModelFailure after the last attempt has the kind of the last attempt's failure, one of
        SECOND_MODEL_REASONS or none, the counts and tokens of the failed attempts as its usage
        (FAILURE_COUNTS) and each failed attempt with its settings, its kind and its error, for a
        response with a call of CHECKLIST_TOOL the start and the end of its arguments
        (`kept_arguments`), and for a response with the text of a message instead of the call the
        start and the end of the text (`kept_message`). Each failed attempt writes its error to
        the log at INFO, so that the log names every failed response and not only the last of a
        repository or of a streak.
        """
        import openai
        last, kind, quoted_from_nowhere = "", "", None
        attempt = 0
        # The responses of every attempt of every round, and those that failed the response
        # format or the tool calls, which the decision records beside its attempts
        # (`label_readme`), for the README at `revision`, so that the two labels of a repository
        # in the comparison are counted separately. A refusal for spent credit or a pause of the
        # service uses no attempt.
        tally = self.tallies.setdefault((name, revision),
                                        {"responses": 0, "failed_responses": 0})
        failures = dict.fromkeys(FAILURE_COUNTS, 0)
        attempts = []
        key = name if repository_key is None else repository_key
        history = self.history.setdefault(key, new_history())

        def current() -> endpoints.Settings:
            """The settings of the next attempt."""
            return self.higher if self.higher and history["first"] else self.settings

        def counted(decision: dict, made: int, settings: endpoints.Settings) -> dict:
            """`decision` with its `made` attempts, its responses and, after a failed response
            at the repository, the record of the higher temperature for a decision at that
            temperature, or the first failed response for a decision at the classifier's own
            settings (FIRST_FAILED_RESPONSE)."""
            decision["usage"].update(attempts=made, **tally)
            if settings is not self.settings:
                decision["higher_temperature"] = {
                    **settings.recorded(), **history["first"],
                    "classifying_model_failures": dict(history["counts"])}
            elif history["first"]:
                decision[FIRST_FAILED_RESPONSE] = dict(history["first"])
            return decision

        try:
            while attempt < ATTEMPTS:
                spent, responses, settings = {}, tally["responses"], current()
                arguments = model_message = None
                try:
                    return counted(await self.label_readme(name, revision, path, text,
                                                           description, activity, recorded,
                                                           tally, spent, settings,
                                                           key),
                                   attempt + 1, settings)
                except QuotationRefused as error:
                    last, kind = str(error), ""
                    quoted_from_nowhere = (error.decision, settings, None)
                except SummaryFailed:
                    raise
                except ModelFailure as error:
                    last, kind, arguments = str(error), error.kind, error.arguments
                    model_message = error.model_message
                except openai.APIStatusError as error:
                    block = endpoints.blocked(error)
                    if block:
                        # The same README is blocked in every attempt and every retry round,
                        # and the next repository is not.
                        raise ModelFailure(f"{block} ({error})", final=True) from error
                    if refusal(error):
                        raise refused(error) from error
                    if (isinstance(error, openai.BadRequestError)
                            and getattr(error, "code", None) not in TRANSIENT_REFUSALS):
                        raise ModelFailure(f"the API refused the request ({error})",
                                           final=True) from error
                    last, kind = f"{type(error).__name__} ({error})", ""
                except openai.OpenAIError as error:
                    last, kind = f"{type(error).__name__} ({error})", ""
                counts = {"attempts": 1,
                          "higher_temperature_attempts": int(settings is not self.settings),
                          "requests": tally["responses"] - responses,
                          **{count: int(spent.get(count) or 0) for count in FAILURE_TOKENS}}
                for count, value in counts.items():
                    failures[count] += value
                    history["counts"][count] += value
                if quoted_from_nowhere is not None and quoted_from_nowhere[2] is None:
                    quoted_from_nowhere = (*quoted_from_nowhere[:2], counts)
                attempts.append({**(settings.sampled() if self.endpoint.openrouter else {}),
                                 "kind": kind, "error": last,
                                 **({"arguments": arguments} if arguments else {}),
                                 **({"message": model_message} if model_message else {})})
                logs.info(COMPONENT, f"Attempt {attempt + 1} of {ATTEMPTS} at {name} by "
                                     f"{self.model} failed ({last}).")
                if kind in SECOND_MODEL_REASONS:
                    failure = {"reason": kind, "error": last,
                               **({"message": model_message} if model_message else {})}
                    history["first"] = history["first"] or failure
                    history["last"] = failure
                attempt += 1
                if attempt < ATTEMPTS:
                    await self.sleep(RETRY_SECONDS * 2 ** (attempt - 1))
            if quoted_from_nowhere is not None:
                logs.warning(COMPONENT, f"None of the {ATTEMPTS} attempts at {name} succeeded, and "
                                        f"the model's purpose quote is in neither the description, "
                                        f"the README nor the purpose sentence of a page that the "
                                        f"description, the homepage field or the README links "
                                        f"to. Recorded the purpose as unclear.")
                # The attempt of the decision that is taken is no failed attempt.
                decision, settings, taken = quoted_from_nowhere
                for count, value in taken.items():
                    history["counts"][count] -= value
                return counted(decision, ATTEMPTS, settings)
            self.pause.exhausted.add(name)
            raise ModelFailure(last, kind=kind, usage=failures, attempts=attempts)
        except BaseException:
            self.failed_earlier.add(key)
            raise
        finally:
            self.pause.finished(name)


# The merged pull requests that distinguish a mirror from a repository developed on GitHub: the
# 100 most recently updated, the most that one page of GitHub's GraphQL API returns, with their
# authors. The model reads notes on pull requests closed without a merge, such as mlichvar/chrony's
# "This was merged with some fixes", as development on GitHub. A count of all merges, recent or
# not, would count AviatrixSystems/terraform-provider-aviatrix as developed on GitHub, although its
# README states that changes made in the repository are overwritten.
MERGES_QUERY = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, last: 100, orderBy: {field: UPDATED_AT, direction: ASC}) {
      nodes { mergedAt author { __typename } }
    }
  }
}"""
# Which merges `latest_merge` counts, recorded with them, so that a decision that counted the
# merges otherwise reads them again. A pull request that a GitHub App opened, such as Dependabot's
# or Renovate's version updates, is no development in the repository: johnsonjh/duma,
# blacklight/platypush and input-output-hk/lace, which their READMEs call mirrors, merged only
# such pull requests since the start of the window of 2026-04-12. A pull request counts when
# GitHub no longer has its author.
MERGES_COUNTED = "pull requests that no GitHub App opened"


def latest_merge(client, name: str) -> str:
    """When the latest of the recently updated merged pull requests of `name` that no GitHub App
    opened was merged, or "" when none was (`MERGES_COUNTED`). When GitHub returns no usable
    response, the function raises `readmes.NoResponse`, which fails the repository for the run to
    try again. A response with errors and without the repository is no result, because taken for
    a response without merges, it would make the repository a mirror. A response with a `data` or
    a repository that is not an object, or with errors in place of the pull requests
    (`classification_tools.unresolved_fields`), is no result either."""
    owner, repository = name.split("/", 1)
    response = client.query(MERGES_QUERY, {"owner": owner, "name": repository})
    if response.status_code != 200:
        raise readmes.NoResponse(f"GitHub returned HTTP {response.status_code} for the merged "
                                 f"pull requests of {name}")
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    found = data.get("repository") if isinstance(data, dict) else None
    errors = [error.get("message") or error.get("type") for error in graphql_errors(body)]
    if not isinstance(found, dict):
        raise readmes.NoResponse(f"GitHub's response for the merged pull requests of {name} has "
                                 f"no repository ({', '.join(map(str, errors)) or 'no error'})")
    if tools.unresolved_fields(body, found):
        raise readmes.NoResponse(f"GitHub's response for the merged pull requests of {name} "
                                 f"{tools.IN_PLACE_OF_FIELDS} the pull requests "
                                 f"({', '.join(map(str, errors))})")
    dates = [node.get("mergedAt") or "" for node in
             (found.get("pullRequests") or {}).get("nodes") or []
             if node and (node.get("author") or {}).get("__typename") != "Bot"]
    return max(dates, default="")


# A language that the model's count names as English: "English", "English (US)", "British
# English", "en", "en-US", "en_GB", "en-Latn-US", "en (US)", "eng" or "eng-US". English named
# inside another language's entry, as in "Non-English", "Chinese (with English terms)" or
# "Chinese/English", is not.
_ENGLISH = re.compile(r"^(?:(?:american|british|us|uk|australian|canadian|indian|simple)\s+)?"
                      r"english(?:\s*\([^)]*\))?$|"
                      rf"^(?:en|eng)(?:{readmes.LANGUAGE_SUBTAG})*(?:\s*\([^)]*\))?$")
# What the model's count can name in place of a natural language, such as the words of code. The
# classification leaves such entries out of the count.
NO_LANGUAGE = ("", "code", "none", "n/a")
# Words of an unknown, another or a mixed language: prose in no named language, which counts as
# not English.
UNNAMED_LANGUAGE = ("unknown", "other", "mixed")


def mainly_english(words: list, measured: dict | None = None) -> str:
    """Whether the README is mainly English by the model's count of its words of prose by
    language: true when English has more than half of the words of natural languages, and true
    for a README without such words, which is in no language, as `readmes.english` reads a
    README without prose. False when the model names real languages and no English, also when
    Lingua found the README's prose English (`measured`, from `readmes.language`), because the
    model read the words: Lingua measures English for the README of
    SpriteStudio/SS6PlayerForUnrealEngine4, with English names beside its Japanese prose, and the
    maintainer labeled that repository false. Unclear when no entry names English although Lingua
    found the prose English and an entry has a name that the classification does not read as a
    language (`readmes.names_language`), because the classification cannot tell which language
    such a name stands for, or every entry is of no named language. An entry with 0 words names
    no language. Unclear too when the words of no named language (UNNAMED_LANGUAGE) alone take
    the majority from English, because every README that reaches the model passed Lingua's check
    of English, and words that the model could not place do not override that measurement."""
    counted = [(entry["language"].strip().lower(), entry["words"]) for entry in words]
    counted = [(language, number) for language, number in counted if language not in NO_LANGUAGE]
    total = sum(number for _, number in counted)
    english = sum(number for language, number in counted if _ENGLISH.match(language))
    unnamed = sum(number for language, number in counted if language in UNNAMED_LANGUAGE)
    if not total:
        return TRUE
    named = [language for language, number in counted
             if number and language not in UNNAMED_LANGUAGE]
    if (not english and (measured or {}).get("detected") == "en"
            and not (named and all(map(readmes.names_language, named)))):
        return UNCLEAR
    if 2 * english > total:
        return TRUE
    return UNCLEAR if 2 * english > total - unnamed else FALSE


def derive(decision: dict) -> None:
    """Set the label of `decision` and the criteria that apply or are unclear, from the model's
    response that it contains.

    A criterion with an exception is decided from its fields. A mirror counts as developed on
    GitHub when pull requests that no GitHub App opened were merged in it since the sampling window
    began. The merges do not decide `moved`, which follows the model's response. A purpose quote
    that the check cannot find leaves
    the purpose unclear, and a fork that has not become its own project is excluded. The label
    follows the prompt's precedence, under which the model weighs engineering evidence against a
    criterion that applies (rule 2). `Run.carry` also calls this function for a reused label.
    """
    shared = {field: decision["maintenance_and_use"][field] for field in MAINTENANCE_AND_USE}
    checklist = decision["exclusions"]
    for key in CRITERION_FACTS:
        rule = CRITERION_APPLIES.get(key)
        if rule is not None:
            checklist[key]["applies"] = rule({**shared, **criterion_facts(decision, key)})
    # Pull requests that no GitHub App opened and that were merged in the repository since the
    # window began show that the repository is developed on GitHub and takes pull requests, and
    # they lift `mirror`. They do not lift `moved`: a README that states the move, such as
    # GoogleChromeLabs/chromium-bidi's "the source was moved to Chromium", outweighs pull requests
    # merged before the move.
    merged_here = (decision.get("mirror_merges") or {}).get("merged_since_window_start")
    if merged_here:
        checklist["mirror"]["applies"] = FALSE
    # A purpose that the model states and the check cannot find leaves the purpose unclear, not
    # false. The README of libcgroup states no purpose, but a README in a form that the check does
    # not read states one and would then be labeled false. A purpose stated only in an issue or a
    # pull request that no link names is no stated purpose (`quotation_fields`).
    # A README without prose is in no language, and the language check reads it as English
    # (`readmes.english`). The model's count cannot decide it either, as for vAmigaWeb's README
    # of images and links, for which the model counted words of a language.
    measured = decision.get("language") or {}
    no_prose = "prose_chars" in measured and not measured["prose_chars"]
    decision["readme_mainly_english"] = mainly_english(decision["readme_words_by_language"],
                                                       measured)
    verdicts = {NOT_ENGLISH: FALSE if no_prose else _not(decision["readme_mainly_english"]),
                **{key: checklist[key]["applies"] for key in EXCLUSIONS},
                NO_PURPOSE: (UNCLEAR if decision.get("purpose_quote_refused") else
                             FALSE if decision.get("purpose_quote") else TRUE)}
    applied = [key for key, verdict in verdicts.items() if verdict == TRUE]
    unclear = [key for key, verdict in verdicts.items() if verdict == UNCLEAR]
    if decision.get("fork") and not decision["fork"].get("independent"):
        applied.append(FORK)
    decision["applied_criteria"], decision["unclear_criteria"] = applied, unclear
    decision["label"] = FALSE if applied else UNCLEAR if unclear else decision["model_label"]


def published_justification(decision: dict) -> str:
    """The model's justification, followed by the classification's verdict and the criteria that
    decided it. For a label that the classification's checks of GitHub decided without the model,
    the reason follows too: a fork that has not become a project of its own, or a mirror that
    merged no pull request since the sampling window began, with GitHub's mirror flag when the
    model did not describe it as a mirror (`criterion_facts`). The model justifies only its fields.
    Without the verdict, the published justification of mrhenry/polyfill-library would state that
    no criterion applies, and the justification of openjdk/jdk11u would state that its disabled
    issues make it `false`."""
    applied, unclear = decision.get("applied_criteria", []), decision.get("unclear_criteria", [])
    label = decision.get("label", "")
    if applied:
        notes = [f"The classification labels it false, because {_criteria(applied)} "
                 f"{'applies' if len(applied) == 1 else 'apply'}."]
    elif unclear:
        notes = [f"The classification labels it unclear, because {_criteria(unclear)} "
                 f"{'is' if len(unclear) == 1 else 'are'} unclear."]
    else:
        notes = [f"The classification labels it {label}, because no criterion applies and the "
                 f"model judged the evidence of software engineering practices {label}."]
    if FORK in decision.get("applied_criteria", []):
        upstream = (decision.get("fork") or {}).get("upstream") or decision.get("fork_of", "")
        notes.append("The fork heuristic finds that it has not become a project of its own apart "
                     f"from its upstream, {upstream}.")
    merges = decision.get("mirror_merges") or {}
    if "mirror" in decision.get("applied_criteria", []) and merges:
        marked = (decision.get("is_mirror")
                  and decision["exclusions"]["mirror"].get("described_as_mirror") != TRUE)
        notes.append("It counts as a mirror, because "
                     + ("GitHub marks it as one and " if marked else "")
                     + "no pull request was merged in it since the sampling window began on "
                     f"{merges['window_start']}, other than pull requests that GitHub Apps "
                     "opened.")
    return " ".join([decision.get("justification", ""), *notes]).strip()


def _criteria(keys: list) -> str:
    """The criteria named in a sentence, such as "the criteria inactive and mirror"."""
    if len(keys) == 1:
        return f"the criterion {keys[0]}"
    return f"the criteria {', '.join(keys[:-1])} and {keys[-1]}"


async def github_facts(decision: dict, client, name: str, revision: str,
                       thresholds: forks.Thresholds, repository_of) -> None:
    """Record in `decision` the merges of a repository that the model's response makes a mirror
    and the fork heuristic's verdict on the upstream that it names, under `thresholds` and at
    `revision`. `repository_of` returns GitHub's response for the repository."""
    decision["fork_thresholds"] = thresholds.as_dict()
    decision.pop("mirror_merges", None)
    decision.pop("fork", None)
    if mirror_may_apply(decision):
        found = await repository_of()
        merged = await asyncio.to_thread(latest_merge, client, found.get("full_name") or name)
        decision["mirror_merges"] = {
            "latest_merge": merged, "window_start": thresholds.window_start,
            "merged_since_window_start": (merged[:10] >= thresholds.window_start
                                          if merged else False),
            "counted": MERGES_COUNTED}
    if decision["fork_of"]:
        verdict = await asyncio.to_thread(forks.independence, client, await repository_of(),
                                          revision, decision["fork_of"], thresholds)
        decision["fork"] = {**verdict, "heuristic": forks.HEURISTIC}


def criterion_facts(decision: dict, key: str) -> dict:
    """The fields of criterion `key` in `decision` as its rule reads them: the model's, with
    `described_as_mirror` true for a repository that GitHub marks as a mirror (`is_mirror`). Such
    a repository is a copy that GitHub pulls from a repository elsewhere, as gcc-mirror/gcc is of
    the GCC repository on gcc.gnu.org, whether or not its description or README says so. The
    exceptions of the mirror criterion still apply: A mirror is not excluded when it invites pull
    requests here, as bcgit/bc-java does, or when pull requests that no GitHub App opened were
    merged in it since the sampling window began."""
    facts = {fact: decision["exclusions"][key][fact] for fact in CRITERION_FACTS[key]}
    if key == "mirror" and decision.get("is_mirror"):
        facts["described_as_mirror"] = TRUE
    return facts


def mirror_may_apply(decision: dict) -> bool:
    """Whether the mirror criterion applies or may apply by the model's facts in `decision`.
    GitHub's merges then decide it (`github_facts`)."""
    shared = {field: decision["maintenance_and_use"][field] for field in MAINTENANCE_AND_USE}
    return CRITERION_APPLIES["mirror"]({**shared, **criterion_facts(decision, "mirror")}) != FALSE


def queries_github(decision: dict) -> bool:
    """Whether the label of `decision` needs a fact of GitHub (`github_facts`): the merges of a
    repository that the model's facts make a mirror or possibly a mirror, or the fork heuristic's
    verdict on a named upstream."""
    return mirror_may_apply(decision) or bool(decision["fork_of"])


def facts_missing(decision: dict) -> bool:
    """Whether the current rules need a fact of GitHub that `decision` does not record: the
    merges of a repository that they make a mirror, or the fork heuristic's verdict on a named
    upstream."""
    return ((mirror_may_apply(decision) and "mirror_merges" not in decision)
            or (bool(decision["fork_of"]) and "fork" not in decision))


def lazily(function, *arguments):
    """An awaitable that runs `function` in a thread on its first await and returns the same
    result on later awaits."""
    parsed = []

    async def once():
        if not parsed:
            parsed.append(await asyncio.to_thread(function, *arguments))
        return parsed[0]
    return once


async def assess(classifier: Classifier, client, readme, thresholds: forks.Thresholds,
                 activity: dict, description: str, language: dict, keep=None,
                 recorded: dict | None = None, is_mirror: bool = False,
                 second_model: dict | None = None, repository_key=None) -> dict:
    """The model's label for a README, with the description and the activity from the
    sampling frame in the input and the mirror and fork facts from GitHub (`github_facts`). The
    thresholds, the activity, the sampling frame's mirror flag (`is_mirror`) and what the language
    check read off the README are recorded with the decision, so that the label can be repeated.
    `recorded` holds the results of READ_FIRST that a repeat takes from the decision that it
    repeats (`Classifier.read_first`). `second_model` is the record of the second model's label of
    a repository that the classifying model failed (`Run.second_model_record`), which the response
    keeps under `second_model` from the moment that it is made. `repository_key` names the
    repository in the classifier's record of failed attempts, its GitHub id in a run
    (`Classifier.label`).

    The classifier keeps the parsed response under the repository, the README's blob SHA and
    revision, the description and the activity until GitHub's facts are read. When GitHub fails
    to return them, the next attempt at the same input, after a pause for GitHub or in a later
    retry round, reads only GitHub's facts again, because a new request would pay the model again
    for the same input. `keep(key, response)` is called with a response that needs GitHub's facts
    before they are requested, so that the caller writes it to its partial file, from which a
    resumed run or comparison restores it (`kept_responses`). Kept in memory alone, the response
    would be paid for again after a stop or a kill."""
    repository_of = lazily(readmes.repository, client, readme.name)
    key = label_cache.digest([readme.name.lower(), readme.sha, readme.revision, description,
                              activity or {}])
    parsed = classifier.kept.get(key)
    if parsed is None:
        parsed = await classifier.label(readme.name, readme.revision, readme.path, readme.text,
                                        description, activity, recorded,
                                        repository_key=repository_key)
        # The record before the response is kept, so that a response restored from a partial
        # file still names the model that made it.
        if second_model is not None:
            parsed["second_model"] = second_model
        # The flag before the test, because a repository that GitHub marks as a mirror needs
        # GitHub's merges whatever the model's facts say (`criterion_facts`).
        parsed["is_mirror"] = is_mirror
        classifier.kept[key] = parsed
        if keep is not None and queries_github(parsed):
            keep(key, parsed)
    elif parsed.get("quote_check") != quote_check():
        # A response that an invocation under another purpose check kept, restored from a
        # partial file or from another run's kept responses. Its quotation is checked again, as
        # `Run.derive_again` checks a decision that it reuses.
        check_quote_again(parsed, description, readme.text)
    parsed["is_mirror"] = is_mirror
    await github_facts(parsed, client, readme.name, readme.revision, thresholds, repository_of)
    parsed["language"] = language
    derive(parsed)
    classifier.kept.pop(key, None)
    return parsed


def check_quote_again(decision: dict, description: str, text: str) -> None:
    """Check the quotation of the purpose in `decision` against `description`, the README's
    `text` and the purpose sentences of the pages that the decision read under the current
    purpose check, and record that check (`quote_check`)."""
    said = (decision.get("purpose_quote") or decision.get("purpose_quote_refused")
            or decision.get("purpose_quote_unlinked") or "")
    if said:
        decision.pop("purpose_quote_refused", None)
        decision.pop("purpose_quote_unlinked", None)
        decision.update(quotation_fields(said, description, text, decision.get("tools") or []))
    decision["quote_check"] = quote_check()


def exclusion(repository: dict, readme, reason: str, parameters_sha: str,
              language: dict | None = None) -> dict:
    """An exclusion decision. For a repository without a README it records the commit that was
    requested, because that commit never gains a README (`Run.decide`)."""
    revision = repository["revision"] if reason == readmes.NO_README_FOUND else readme.revision
    return {"id": repository["id"], "name": readme.name, "revision": revision,
            "readme_path": readme.path, "readme_sha": readme.sha, "readme_etag": readme.etag,
            "readme_wording": readmes.wording(readme.text) if readme.text else "",
            "outcome": EXCLUDED, "parameters": parameters_sha, "reason": reason,
            "language": language or {}, "language_check": readmes.language_check(),
            "decided_at": label_cache.iso(label_cache.utc_now())}


def same_text(one, other) -> bool:
    """Whether two descriptions are equal apart from whitespace, in which most of the measured
    differences lay (classify-repos-spec.md)."""
    return " ".join(str(one or "").split()) == " ".join(str(other or "").split())


class Gone(Exception):
    """The repository was gone when its README was read again."""


class StoredResponseError(Exception):
    """A stored response that the current rules cannot derive a label from: a defect of the code,
    which stops the run rather than counting as a failure of GitHub or the model."""


class RequestNeeded(Exception):
    """The check of a decision under the current rules needs a request to GitHub, for the
    README's text or for GitHub's facts (`Run.derives_unchanged`)."""


class Progress(logs.Progress):
    """The progress line, with the requests and tokens that the run has spent at its end, which
    `spent` returns as `spent` of the module counts them, the second model's included."""

    def __init__(self, total: int, already_done: int, spent):
        super().__init__(COMPONENT, total, already_done, PROGRESS_EVERY)
        self.spent = spent

    def details(self) -> str:
        spent = self.spent()
        return (f", {spent['github_requests']:,} GitHub and {spent['model_requests']:,} model "
                f"requests, {spent['input_tokens']:,} input and {spent['output_tokens']:,} output "
                f"tokens")


def fallback_settings(record: dict) -> dict:
    """The settings of a label of the higher temperature or of the second model, from its record
    in a decision, without why it was made (FALLBACK_OUTCOME) and without the provider of the
    second model, which a record written before 2026-11-04 names and which is no setting
    (`label_cache.PROVIDER`)."""
    return {key: value for key, value in record.items()
            if key not in FALLBACK_OUTCOME and key != label_cache.PROVIDER}


def run_fallbacks(classifier: Classifier, second: endpoints.Endpoint | None) -> dict:
    """The settings under which a run labels after a failed response of the classifying model,
    by the key of their record in a decision: the higher temperature of `classifier` and the
    second model `second` with its settings (`endpoints.settings_of`), each None when the run has
    none. `second` is the second model that the run is configured with, also in an invocation in
    which it cannot label. A decision with such a record applies to the run only when the run has
    the same settings (`Run.applies`)."""
    return {"higher_temperature": (classifier.higher.recorded()
                                   if classifier.higher is not None else None),
            "second_model": ({**second.parameters(),
                              **endpoints.settings_of(second).parameters()}
                             if second is not None else None)}


def applies(decision: dict, fallbacks: dict) -> bool:
    """Whether `decision` applies to a run with `fallbacks` (`run_fallbacks`): a decision without
    a label of the higher temperature or of the second model does, and a decision with one does
    only when the run labels under the settings that its record names."""
    return all(fallback_settings(decision[key]) == fallbacks.get(key)
               for key in fallbacks if decision.get(key))


class Run:
    """One run over the repositories: their READMEs, their decisions and the partial file.
    `second` is the classifier of the second model, which labels a repository that `classifier`
    leaves without a decision after the retry rounds when one of its attempts ended in a failed
    response (SECOND_MODEL_REASONS), or None when no second model labels in the invocation.
    `second_model` is the second model that the run is configured with (`second_endpoint`), the
    endpoint of `second` by default, and its labels and kept responses apply also in an
    invocation in which it cannot label, as when its endpoint is unserved (`run_fallbacks`)."""

    def __init__(self, rid: str, repositories: list, decisions, client, classifier, model: str,
                 parameters_sha: str, partial: Path, workers: int, thresholds: forks.Thresholds,
                 second: Classifier | None = None,
                 second_model: endpoints.Endpoint | None = None):
        self.rid, self.thresholds = rid, thresholds
        self.repositories, self.decisions, self.client = repositories, decisions, client
        self.classifier, self.model, self.second = classifier, model, second
        self.fallbacks = run_fallbacks(classifier, second_model or (second and second.endpoint))
        self.parameters_sha = parameters_sha
        self.partial, self.workers = partial, workers
        self.lines: dict[int, dict] = {}
        # The lines that an earlier invocation of the run wrote, by GitHub id, until `recheck`
        # checks them under the current rules.
        self.restored: dict[int, dict] = {}
        self.failed: dict[int, dict] = {}
        self.stopped = False
        # Set while the second model labels, after the retry rounds: a repository that fails
        # then is tried again by the next run, and its line names the second model.
        self.second_pass = False
        # Why the second model labels no further repository of the pass, as when its endpoint
        # refuses every request, or "".
        self.second_refused = ""
        self.progress: Progress | None = None
        self.spending = label_cache.Spending(lambda: self.spent())
        # One pause for the repositories decided at once, which share GitHub.
        self.github_pause = GitHubPause(lambda: client.responses, client.responds,
                                        classifier.sleep, classifier.clock)

    def spent(self) -> dict:
        """The requests and tokens that the run spent so far, the second model's included."""
        return spent(self.client, self.classifier, *([self.second] if self.second else []))

    def write(self, line: dict) -> None:
        """Append `line` to the partial file with what the run spent since the last line."""
        spent = self.spending.since()
        line = {**line, "spent": spent} if spent else line
        if line:
            with open(self.partial, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")

    def record_spent(self) -> None:
        """Record what the run spent since the last line, as the invocation ends."""
        self.write({})

    def keep(self, repository: dict, key: str, response: dict) -> None:
        """Write the model's response for `repository` to the partial file under the key of
        `Classifier.kept` while GitHub's facts are still to be read, so that a stop or a kill
        before the facts leaves the response for the next invocation (`kept_responses`)."""
        self.write({"id": repository["id"], "status": KEPT, "key": key, "response": response})

    def record(self, line: dict) -> None:
        self.write(line)
        if line["status"] == FAILED:
            self.failed[line["id"]] = line
        else:
            self.lines[line["id"]] = line
            self.failed.pop(line["id"], None)

    def fail(self, repository: dict, reason: str, error: Exception, retry: bool = True) -> None:
        """Record a repository that is left without a decision. A retry round decides it when
        `retry` is true, and otherwise the next run does, as for every failure while the second
        model labels, after the retry rounds. The line of a failure of the model records the
        kind of the failure, the counts and tokens of the failed attempts and each failed attempt
        (`ModelFailure`). A line written while the second model labels names the second model
        under `model`, the kind of the classifying model's failed response for which it labeled
        under `fallback_reason`, and the counts and tokens of the classifying model's failed
        attempts under `classifying_model_failures`."""
        retry = retry and not self.second_pass
        logs.warning(COMPONENT, f"Could not decide {repository['name']} ({error})"
                                f"{'. Retrying it later' if retry else ''}.")
        line = {"id": repository["id"], "status": FAILED, "name": repository["name"],
                "reason": reason, "retry": retry, "error": f"{type(error).__name__}: {error}",
                "at": label_cache.iso(label_cache.utc_now())}
        if isinstance(error, ModelFailure):
            line.update({key: value for key, value in (("model_failure", error.kind),
                                                       ("usage", error.usage),
                                                       ("attempts", error.attempts))
                         if value})
        if self.second_pass:
            history = self.classifier.history.get(repository["id"]) or new_history()
            line.update(model=self.second.model,
                        fallback_reason=(history["last"] or {}).get("reason", ""),
                        classifying_model_failures=dict(history["counts"]))
        self.record(line)

    def candidate(self, repository: dict, readme) -> dict | None:
        """The earlier decision that may apply to this README: the latest decision when GitHub
        returns 304, the exclusion of a repository without a README, the label or exclusion with
        the same blob SHA, or the label with the same wording (`readmes.wording`). A label of
        the higher temperature or of the second model under other settings than the run's
        applies to no README (`applies`), and the classifying model labels the repository
        again."""
        rid = repository["id"]
        if readme.status == readmes.UNCHANGED:
            latest = self.decisions.latest(rid, self.model, self.parameters_sha)
            return latest if latest is None or applies(latest, self.fallbacks) else None
        label = self.decisions.label(rid, self.model, self.parameters_sha)
        if label is not None and not applies(label, self.fallbacks):
            label = None
        excluded = self.decisions.exclusion(rid, self.parameters_sha)
        if readme.status == readmes.NO_README:
            return excluded if excluded and excluded.get("reason") == readmes.NO_README_FOUND \
                else None
        for decision in (label, excluded):
            if decision and decision.get("readme_sha") == readme.sha:
                return decision
        # An empty README has the wording of a README with only badges and images, which is not
        # a README that a label was made for.
        if (label and not readmes.unusable(readme)
                and label.get("readme_wording") == readmes.wording(readme.text)):
            return label
        return None

    async def derive_again(self, decision: dict, repository: dict, text, revision,
                           changed_readme: bool) -> None:
        """Derive the label of `decision` again under the current purpose check, the thresholds
        of the sampling frame and the rules. `text` returns the README's text, and for a README
        that changed, the quotation is checked against its new text. `revision` returns the
        revision at which GitHub returned the README, and GitHub's facts are requested at that
        revision. GitHub's mirror flag is that of the current sampling frame, which a later
        sampling frame can change."""
        decision["is_mirror"] = repository["is_mirror"]
        try:
            if changed_readme or decision.get("quote_check") != quote_check():
                said = (decision.get("purpose_quote") or decision.get("purpose_quote_refused")
                        or decision.get("purpose_quote_unlinked"))
                check_quote_again(decision, decision.get("description") or "",
                                  await text() if said else "")
            query_github = facts_missing(decision) or (
                (decision.get("fork") or decision.get("mirror_merges"))
                and decision.get("fork_thresholds") != self.thresholds.as_dict()) or (
                # A verdict of another version of the fork heuristic is decided again. Otherwise a
                # verdict that a later change of forks.py would decide differently would stay
                # while the thresholds are the same.
                bool(decision.get("fork"))
                and decision["fork"].get("heuristic") != forks.HEURISTIC) or (
                # Merges counted otherwise, such as with the pull requests of GitHub Apps.
                bool(decision.get("mirror_merges"))
                and decision["mirror_merges"].get("counted") != MERGES_COUNTED)
        except (KeyError, TypeError) as error:
            raise StoredResponseError(f"deriving the stored response for {repository['name']} "
                                      f"again raised {type(error).__name__} ({error})") from error
        if query_github:
            # GitHub's facts are requested at the revision at which GitHub returned the README
            # with the blob of this decision: the commit of the sampling frame, or the default
            # branch when GitHub no longer has that commit, as after a force push.
            # Requested at a commit that GitHub no longer has, they would fail with HTTP 404.
            decision["revision"] = await revision()
            await github_facts(decision, self.client, repository["name"], decision["revision"],
                               self.thresholds,
                               lazily(readmes.repository, self.client, repository["name"]))
        try:
            derive(decision)
        except (KeyError, TypeError) as error:
            raise StoredResponseError(f"deriving the stored response for {repository['name']} "
                                      f"again raised {type(error).__name__} ({error})") from error

    async def carry(self, candidate: dict, repository: dict, readme,
                    served: bool = True, offline: bool = False) -> tuple | None:
        """`candidate` checked against the current README, description and code, without the
        model. `served` states whether GitHub returned `readme` in this invocation. A README that
        GitHub did not return, such as a README that `recheck` builds from a line of the partial
        file, is requested before GitHub's facts, which are requested at the revision at which
        GitHub returns the README. With `offline`, the check raises `RequestNeeded` where it would
        request that README, which is the first request of the check.

        Returns the decision with REUSED when nothing changed, with CARRIED when the README changed
        only in what its wording leaves out or the current code derives a different result from
        the stored response, and with DECIDED when the language check now excludes a labeled README.
        Returns None when the model has to label the repository again, because the description
        changed or the language check now accepts a README that it excluded. Raises `Gone` when
        the repository is gone.
        """
        if candidate["outcome"] == EXCLUDED and candidate.get("reason") != readmes.NOT_ENGLISH:
            if (candidate.get("reason") == readmes.NO_README_FOUND and repository["revision"]
                    and candidate.get("revision") != repository["revision"]):
                # Recorded at this commit, so that the next run over the same sampling frame
                # sends GitHub no request (`decide`).
                moved = dict(candidate, revision=repository["revision"],
                             decided_at=label_cache.iso(label_cache.utc_now()))
                return moved, CARRIED
            return candidate, REUSED
        if (candidate["outcome"] == LABELED
                and not same_text(repository["description"], candidate["description"])):
            return None
        current = {"readme": readme, "served": served}

        async def text() -> str:
            """The README's text, fetched again after a 304, which has none."""
            if current["readme"].status != readmes.FOUND:
                if offline:
                    raise RequestNeeded(repository["name"])
                fetched = await asyncio.to_thread(readmes.fetch, self.client, repository["name"],
                                                  repository["id"], repository["revision"], "")
                if fetched.status == readmes.GONE:
                    raise Gone(repository["name"])
                current.update(readme=fetched, served=True)
            return current["readme"].text or ""

        async def revision() -> str:
            """The revision that GitHub returned the README at in this invocation: the commit
            of the sampling frame, or "" for the default branch when GitHub no longer has that
            commit (`readmes.fetch`)."""
            if not current["served"]:
                await text()
            return current["readme"].revision
        decision = copy.deepcopy(candidate)
        changed_readme = readme.status == readmes.FOUND and readme.sha != candidate.get("readme_sha")
        check = readmes.language_check()
        recorded = candidate.get("language_check") or {}
        if changed_readme or recorded.get("lingua") != check["lingua"]:
            decision["language"] = await asyncio.to_thread(readmes.language, await text())
        decision["language_check"] = check
        english = readmes.english(decision["language"] or {})
        if candidate["outcome"] == EXCLUDED:
            if english:
                return None
        elif not english:
            excluded = exclusion(repository, current["readme"] if current["readme"].status ==
                                 readmes.FOUND else readme, readmes.NOT_ENGLISH,
                                 self.parameters_sha, decision["language"])
            if not excluded["readme_sha"]:
                # After a 304 the README is the one that the label was made for.
                excluded.update({field: candidate.get(field, "") for field in
                                 ("revision", "readme_path", "readme_sha", "readme_etag",
                                  "readme_wording")})
            return excluded, DECIDED
        else:
            await self.derive_again(decision, repository, text, revision, changed_readme)
        if changed_readme:
            decision.update(revision=readme.revision, readme_path=readme.path,
                            readme_sha=readme.sha, readme_etag=readme.etag,
                            readme_wording=readmes.wording(readme.text))
        if decision == candidate:
            return candidate, REUSED
        # `earlier_decided_at` lists the decided_at of each decision that the candidate was itself
        # carried from, oldest first. The comparison attributes the change of a carried label
        # without the model when the previous run's decision is the candidate or one of these
        # decisions. A run that is not the previous run, such as a run that a kill ended, can
        # carry the label in between.
        earlier = candidate.get("carried_from") or {}
        decision["carried_from"] = {"run": self.decisions.origin(candidate),
                                    "decided_at": candidate.get("decided_at", ""),
                                    "earlier_decided_at": [
                                        *earlier.get("earlier_decided_at", []),
                                        *([earlier["decided_at"]] if earlier.get("decided_at")
                                          else [])],
                                    "readme_sha": candidate.get("readme_sha", ""),
                                    "label": candidate.get("label", "")}
        decision["decided_at"] = label_cache.iso(label_cache.utc_now())
        return decision, CARRIED

    def reuse(self, repository: dict, decision: dict) -> None:
        """Record an earlier decision that applies unchanged. This run writes a decision that
        only the partial file of a run that a kill ended contains, as carried, because the run's
        labels name the run that made each decision, and that run's decisions file has to contain
        the decision."""
        if self.decisions.written(decision):
            self.record({"id": repository["id"], "status": REUSED, "decision": decision,
                         "decided_in": self.decisions.origin(decision)})
            return
        self.decisions.add(decision, self.rid)
        self.record({"id": repository["id"], "status": CARRIED, "decision": decision})

    async def decide(self, repository: dict, second_model: dict | None = None) -> None:
        """Decide `repository`: reuse or carry over an earlier decision, exclude its README, or
        label it with the classifying model, or with the second model when `second_model` is its
        record (`second_model_record`)."""
        started = time.monotonic()
        classifier = self.classifier if second_model is None else self.second
        previous = self.decisions.latest(repository["id"], self.model, self.parameters_sha)
        if (previous and previous.get("reason") == readmes.NO_README_FOUND
                and repository["revision"] and previous.get("revision") == repository["revision"]):
            # A commit's tree does not change, so a repository without a README at this commit
            # still has none, and another request to GitHub would gain nothing.
            self.reuse(repository, previous)
            return
        etag = (previous or {}).get("readme_etag", "")
        readme = await asyncio.to_thread(readmes.fetch, self.client, repository["name"],
                                         repository["id"], repository["revision"], etag)
        if readme.status == readmes.GONE:
            self.record({"id": repository["id"], "status": GONE, "name": readme.name})
            return
        candidate = self.candidate(repository, readme)
        if candidate is not None:
            try:
                carried = await self.carry(candidate, repository, readme)
            except Gone:
                self.record({"id": repository["id"], "status": GONE, "name": readme.name})
                return
            if carried is not None:
                decision, status = carried
                if status == REUSED:
                    self.reuse(repository, decision)
                else:
                    self.decisions.add(decision, self.rid)
                    self.record({"id": repository["id"], "status": status, "decision": decision})
                return
        if readme.status == readmes.UNCHANGED:
            # The model labels the README again and needs its text, which a 304 does not contain.
            readme = await asyncio.to_thread(readmes.fetch, self.client, repository["name"],
                                             repository["id"], repository["revision"], "")
            if readme.status == readmes.GONE:
                self.record({"id": repository["id"], "status": GONE, "name": readme.name})
                return
        reason = readmes.unusable(readme)
        language = {}
        if not reason:
            language = await asyncio.to_thread(readmes.language, readme.text)
            if not readmes.english(language):
                reason = readmes.NOT_ENGLISH
        if reason:
            decision = exclusion(repository, readme, reason, self.parameters_sha, language)
        else:
            parsed = await assess(classifier, self.client, readme, self.thresholds,
                                  repository.get("activity") or {}, repository["description"],
                                  language,
                                  keep=lambda key, response: self.keep(repository, key, response),
                                  is_mirror=repository["is_mirror"], second_model=second_model,
                                  repository_key=repository["id"])
            parsed["usage"]["seconds"] = round(time.monotonic() - started, 1)
            decision = {"id": repository["id"], "name": readme.name, "revision": readme.revision,
                        "readme_path": readme.path, "readme_sha": readme.sha,
                        "readme_etag": readme.etag, "readme_wording": readmes.wording(readme.text),
                        "outcome": LABELED, "model": self.model,
                        "parameters": self.parameters_sha, **parsed,
                        "language_check": readmes.language_check(),
                        "decided_at": label_cache.iso(label_cache.utc_now())}
        self.decisions.add(decision, self.rid)
        self.record({"id": repository["id"], "status": DECIDED, "decision": decision})

    def second_model_record(self, repository: dict) -> dict:
        """What a decision of the second model records under `second_model`: the second model
        with its weights, its quantization and the settings of its requests, the kind, the
        error and the start and the end of the text of the messages (`kept_message`) of the last
        failed response of the classifying model at `repository`, and the counts and tokens of
        all of its failed attempts at the repository in the run, those at the higher temperature
        among them (`Classifier.history`)."""
        history = self.classifier.history[repository["id"]]
        return {**self.fallbacks["second_model"], **history["last"],
                "classifying_model_failures": dict(history["counts"])}

    async def decide_with_second_model(self, repository: dict) -> None:
        """Decide `repository` with the second model after the classifying model left it
        without a decision (`decide`). A failure of the second model, or of GitHub, leaves the
        repository without a decision for the next run, as no retry round follows (`fail`).
        When the API refuses the second model or its service fails every request, the pass
        ends, and the repositories that it has not labeled stay without a decision, because the
        retry rounds of the run have finished, and a stop would make the next invocation pay
        the classifying model's attempts at them again."""
        if self.second_refused:
            self.fail(repository, MODEL_FAILED, ModelRefused(self.second_refused), retry=False)
            return
        try:
            await self.decide(repository, self.second_model_record(repository))
        except ModelFailure as error:
            self.fail(repository, MODEL_FAILED, error, retry=False)
        except (ModelRefused, ServiceDown) as error:
            if not self.second_refused:
                logs.warning(COMPONENT, f"The second model labels no further repository, "
                                        f"because {error}. The repositories that it has not "
                                        f"labeled stay without a decision for the next run.")
                self.second_refused = str(error)
                self.second.end_pauses(f"the second model labels no further repository, "
                                       f"because {error}")
            self.fail(repository, MODEL_FAILED, error, retry=False)

    def second_model_reason(self, repository: dict) -> str:
        """The kind of the classifying model's failed response for which the second model
        labels `repository`, one of SECOND_MODEL_REASONS, or "" when it does not. It labels a
        repository that the classifying model left without a decision after the retry rounds
        when any of the classifying model's attempts at it in the run ended in a failed
        response, and the reason is that of the last failed response (`Classifier.history`). It
        does not label a repository when no second model labels in the invocation, when the
        repository has a decision, when its last failure was GitHub's, an unexpected error or a
        refusal of its input, which fails every attempt the same way, or when the second model
        failed it already. The record is read by the GitHub id, which stays when the repository
        is renamed."""
        line = self.failed.get(repository["id"]) or {}
        history = self.classifier.history.get(repository["id"]) or new_history()
        if (self.second is None or line.get("reason") != MODEL_FAILED or not line.get("retry")
                or line.get("model") or history["last"] is None):
            return ""
        return history["last"]["reason"]

    async def label_with_second_model(self) -> None:
        """Label with the second model, in one pass after the retry rounds, every repository
        that the classifying model left without a decision after a failed response, with the
        same inputs and prompt (`second_model_reason`). The second model labels
        only what the classifying model failed through every attempt of every round, at its
        temperature and its higher temperature, so that the labels stay those of the
        classifying model wherever it gives one. The responses of the second model do not count
        toward FormatStreak, because the pass holds nothing but repositories that the
        classifying model failed, unless the classifying model's streak is still open. The pass
        then waits out the pause that holds and takes over the pauses of that streak, and the
        failed responses of the second model count toward its own streak, which pauses the
        workers of the pass and, after the last of FORMAT_PAUSES pauses, ends the pass
        (`FormatStreak.take_over`, `ServicePause.wait_for`). The same outage can fail both
        models, as when they share a provider, and the pauses then let it end before the second
        model uses up its attempts."""
        chosen = [r for r in self.repositories if self.second_model_reason(r)]
        if not chosen:
            return
        logs.info(COMPONENT, f"{self.second.endpoint.described()} labels the {len(chosen):,} "
                             f"{'repository' if len(chosen) == 1 else 'repositories'} that "
                             f"{self.classifier.endpoint.described()} left without a decision "
                             f"after the retry rounds and a failed response, the first of them "
                             f"{chosen[0]['name']}.")
        self.second.failed_earlier |= {repository["id"] for repository in chosen}
        if self.classifier.format_streak.open:
            self.second.format_streak.take_over(self.classifier.format_streak)
            self.second.pause.wait_for(self.classifier.pause)
        queue: asyncio.Queue = asyncio.Queue()
        for repository in chosen:
            queue.put_nowait(repository)
        self.progress = Progress(len(self.repositories), len(self.lines), self.spent)
        self.second_pass = True
        try:
            await asyncio.gather(*(self.work(queue, self.decide_with_second_model)
                                   for _ in range(self.workers)))
        finally:
            self.second_pass = False

    async def recheck(self, repository: dict) -> None:
        """The line that an earlier invocation of the run wrote for `repository`, checked under
        the current rules as `carry` checks an earlier run's decision, from the README that the
        decision names.

        A new image can change the rules, the language check, the purpose check or the fork
        heuristic between two invocations. A line kept as it was would then publish the label of
        the earlier rules beside the labels of the current rules. A request goes to GitHub only
        when the current rules need the README's text or GitHub's facts, and a resume under
        unchanged rules therefore sends none. A line that the current rules leave unchanged is
        kept as it is, and a repository that they send to the model again is decided with the
        other repositories. The line stays in `restored` until the worker leaves the repository,
        so that a check that GitHub failed reads it again after a pause for GitHub (`take`)."""
        line = self.restored[repository["id"]]
        if line["status"] == GONE:
            self.lines[repository["id"]] = line
            return
        if not applies(line["decision"], self.fallbacks):
            # A label of the higher temperature or of the second model under other settings,
            # such as an earlier invocation's label of the second model in an invocation with
            # --no-second-model: the classifying model labels the repository again.
            return
        written = readmes.Readme(readmes.UNCHANGED, repository["name"], repository["revision"],
                                 etag=line["decision"].get("readme_etag", ""))
        try:
            carried = await self.carry(line["decision"], repository, written, served=False)
        except Gone:
            self.record({"id": repository["id"], "status": GONE, "name": repository["name"]})
            return
        if carried is None:
            return
        decision, status = carried
        if status == REUSED:
            self.lines[repository["id"]] = line
            return
        self.decisions.add(decision, self.rid)
        self.record({"id": repository["id"], "status": status, "decision": decision})

    async def derives_unchanged(self, repository: dict, decision: dict) -> bool:
        """Whether the current rules derive `decision` for `repository` unchanged and without a
        request to GitHub, as `recheck` keeps the line of an earlier invocation that they leave
        unchanged. A stored response that they cannot derive a label from is no unchanged
        decision either, and neither is a label of the higher temperature or of the second
        model under other settings than the run's (`applies`)."""
        if not applies(decision, self.fallbacks):
            return False
        written = readmes.Readme(readmes.UNCHANGED, repository["name"], repository["revision"],
                                 etag=decision.get("readme_etag", ""))
        try:
            carried = await self.carry(decision, repository, written, served=False, offline=True)
        except (RequestNeeded, StoredResponseError):
            return False
        return carried is not None and carried[1] == REUSED

    async def work(self, queue: asyncio.Queue, step=None) -> None:
        """Take repositories through `step`, `decide` by default, until the queue is empty, or
        until the run stops (`take`)."""
        step = step or self.decide
        while not self.stopped:
            try:
                repository = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if await self.take(repository, step) and self.progress is not None:
                self.progress.note(repository["id"] in self.failed)

    async def take(self, repository: dict, step) -> bool:
        """Take `repository` through `step` after the pause for GitHub, and again after each
        pause that follows a failure of GitHub for the repository (`GitHubPause`). Return False
        when the run stopped.

        The run stops when no GitHub access token is left to read the remaining repositories,
        when GitHub fails through every pause, when the API refuses or its service fails every
        request, or on an exception that the classification does not expect. Such an exception
        is a defect, which would fail every later repository the same way and could spend the
        model's tokens on each. A repository that waited for a pause while another worker stopped
        the run does not go through its step again, because the step could pay the model for its
        README."""
        waited = False
        while True:
            sent_after, failure = 0, None
            try:
                sent_after = await self.github_pause.ready()
                if waited and self.stopped:
                    self.restored.pop(repository["id"], None)
                    return False
                await step(repository)
            except (NoTokens, ModelRefused, ServiceDown, GitHubDown, GitHubPauseEnded) as error:
                return self.stop(repository, error)
            except GITHUB_ERRORS as error:
                failure = error
            except ModelFailure as error:
                self.fail(repository, MODEL_FAILED, error, retry=not error.final)
            except Exception as error:  # noqa: BLE001 - logged with its traceback, then stops
                logs.error(COMPONENT, f"An unexpected error stopped the run at "
                                      f"{repository['name']}. The decisions so far are saved in "
                                      f"{self.partial.name}.\n"
                                      f"{traceback.format_exc().rstrip()}")
                self.record({"id": repository["id"], "status": FAILED,
                             "name": repository["name"], "reason": "error",
                             "error": f"{type(error).__name__}: {error}",
                             "at": label_cache.iso(label_cache.utc_now())})
                self.end_pauses(f"an unexpected error stopped the run at "
                                f"{repository['name']}")
                self.stopped = True
            if failure is not None:
                try:
                    if not self.stopped and await self.github_pause.failed(
                            repository["name"], sent_after, failure):
                        waited = True
                        continue
                except (NoTokens, GitHubDown) as error:
                    return self.stop(repository, error)
                self.fail(repository, GITHUB_FAILED, failure)
            self.restored.pop(repository["id"], None)
            return True

    def end_pauses(self, reason: str) -> None:
        """End the pauses for GitHub, for the API's service and for spent credit of a run that
        stopped for `reason`, so that no worker waits out a pause after the stop."""
        self.github_pause.end(reason)
        for classifier in (self.classifier, self.second):
            if classifier is not None:
                classifier.end_pauses(reason)

    def stop(self, repository: dict, error: Exception) -> bool:
        """Stop the run on `error`, after which no repository can be decided, and return False."""
        if not self.stopped:
            logs.error(COMPONENT, f"Stopped the run, because {error}. The decisions so far are "
                                  f"saved in {self.partial.name}.")
            self.end_pauses(f"the run stopped, because {error}")
        self.stopped = True
        self.restored.pop(repository["id"], None)
        return False

    async def run(self) -> None:
        """Check the lines of an earlier invocation under the current rules, decide every
        repository without a line, then try the repositories left without a decision again, up
        to RETRY_ROUNDS times, until none is left or the run stops. The second model then labels
        the repositories that the classifying model left without a decision after a failed
        response (`label_with_second_model`)."""
        restored = [r for r in self.repositories if r["id"] in self.restored]
        if restored:
            queue: asyncio.Queue = asyncio.Queue()
            for repository in restored:
                queue.put_nowait(repository)
            self.progress = None
            await asyncio.gather(*(self.work(queue, self.recheck) for _ in range(self.workers)))
            if self.stopped:
                # The next invocation checks the lines left unchecked. Until then they count as
                # decided, because their decisions are in the partial file.
                self.lines.update(self.restored)
                return
        pending = [r for r in self.repositories if r["id"] not in self.lines]
        for round_number in range(RETRY_ROUNDS + 1):
            if round_number:
                logs.info(COMPONENT, f"Retry round {round_number} of {RETRY_ROUNDS} starts in "
                                     f"{RETRY_ROUND_SECONDS} seconds for the {len(pending):,} "
                                     f"repositories without a decision.")
                await self.classifier.sleep(RETRY_ROUND_SECONDS)
            queue: asyncio.Queue = asyncio.Queue()
            for repository in pending:
                queue.put_nowait(repository)
            self.progress = Progress(len(self.repositories), len(self.lines), self.spent)
            await asyncio.gather(*(self.work(queue) for _ in range(self.workers)))
            pending = [r for r in self.repositories
                       if self.failed.get(r["id"], {}).get("retry")]
            if self.stopped:
                return
            if not pending:
                break
        await self.label_with_second_model()


def failure_histories(path: Path) -> dict[int, dict]:
    """The classifying model's failed attempts at each repository in the run of a partial file,
    by GitHub id (`new_history`): the counts and tokens summed over the failed lines of every
    round and every invocation of the run, and the first and the last failed response, in the
    order of the file, with the text of the messages that the entry of its attempt keeps
    (`ModelFailure.attempts`). A line of the second model's failure names that model and does not
    count."""
    histories: dict[int, dict] = {}
    for line in label_cache.partial_lines(path, (FAILED,)):
        if "id" not in line or line.get("reason") != MODEL_FAILED or line.get("model"):
            continue
        history = histories.setdefault(int(line["id"]), new_history())
        for key in FAILURE_COUNTS:
            history["counts"][key] += int((line.get("usage") or {}).get(key) or 0)
        for attempt in line.get("attempts") or []:
            if attempt.get("kind") in SECOND_MODEL_REASONS:
                failure = {"reason": attempt["kind"], "error": attempt.get("error", ""),
                           **({"message": attempt["message"]} if attempt.get("message") else {})}
                history["first"] = history["first"] or failure
                history["last"] = failure
    return histories


def read_partial(path: Path) -> dict[int, dict]:
    """The repositories that a stopped run finished, by GitHub id. A last line that a stop cut
    short is dropped, and that repository is decided again, as is a repository that the run
    failed. A line that keeps the model's response before GitHub's facts finishes no
    repository."""
    lines = {}
    for line in label_cache.partial_lines(path, (FAILED, *FINISHED)):
        if "id" not in line:
            continue
        if line["status"] == FAILED:
            lines.pop(int(line["id"]), None)
        else:
            lines[int(line["id"])] = line
    return lines


def kept_responses(path: Path, finished: tuple) -> dict[str, dict]:
    """The model's responses that the lines with status KEPT of a partial file keep, by the key
    of `Classifier.kept`, without the responses of a repository that a later line with a status
    of `finished` finished. A run or a comparison resumed after a stop or a kill between the model's
    response and GitHub's facts thereby reads only the facts (`assess`)."""
    by_repository: dict[int, dict] = {}
    for line in label_cache.partial_lines(path, (KEPT, *finished)):
        if "id" not in line:
            continue
        if line["status"] == KEPT:
            if "key" in line and "response" in line:
                by_repository.setdefault(int(line["id"]), {})[line["key"]] = line["response"]
        else:
            by_repository.pop(int(line["id"]), None)
    return {key: response for kept in by_repository.values() for key, response in kept.items()}


def partial_decisions(path: Path) -> list[dict]:
    """The decisions that the run of a partial file made or carried over, the latest of each
    repository, in the order of the file. A decision is kept when a later line of the
    repository failed, as when GitHub failed while a resumed run checked the decision under the
    current rules (`Run.recheck`), because a dropped decision would make the run pay for the
    model's label again."""
    decisions = {}
    for line in label_cache.partial_lines(path, (DECIDED, CARRIED)):
        if "decision" in line:
            decisions[int(line["id"])] = line["decision"]
    return list(decisions.values())


def github_failures(path: Path) -> set[str]:
    """The repositories that GitHub failed in the run of a partial file, by name, which fail
    again on their own in the rest of the run (`GitHubPause`)."""
    return {line["name"] for line in label_cache.partial_lines(path, (FAILED,))
            if line.get("reason") == GITHUB_FAILED and line.get("name")}


def model_failures(path: Path) -> set[int]:
    """The repositories that the model left without a decision in the run of a partial file, by
    GitHub id, as `Classifier.failed_earlier` names them in a run."""
    return {int(line["id"]) for line in label_cache.partial_lines(path, (FAILED,))
            if line.get("reason") == MODEL_FAILED and "id" in line}


def spent(client, classifier, *others) -> dict:
    """The GitHub and model requests and the tokens that `client` and `classifier` spent,
    together with the classifiers in `others`, such as the classifier of the second model."""
    totals = {"github_requests": client.requests, "model_requests": classifier.requests,
              **classifier.usage}
    for other in others:
        totals["model_requests"] += other.requests
        for key, value in other.usage.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def summary(lines) -> dict:
    counts = {"labeled": 0, "excluded": 0, "gone": 0, "reused": 0}
    for line in lines:
        if line["status"] == GONE:
            counts["gone"] += 1
            continue
        counts["reused"] += line["status"] in (REUSED, CARRIED)
        counts["labeled" if line["decision"]["outcome"] == LABELED else "excluded"] += 1
    return counts


def write_csv(path: Path, columns: list, rows: list) -> None:
    """Write `rows` under `columns` in one step (`caches.replace_file`). On a power loss, a
    rename that reached the disk before the data would leave an empty file, and an empty
    `repos_annotated.csv` stops the engineered project selection."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    caches.replace_file(Path(path), buffer.getvalue())


def first_of_copies(repositories: list, lines: dict, undecided: dict | None = None) -> dict:
    """{head commit: the GitHub id of the repository that the sample keeps} for the head commits
    that several repositories labeled as engineered projects share.

    Repositories at one head commit have one history, as a mirror and the repository that it
    copies have, and in the sample that history would count once for each of them. Of those that
    the model labeled `true`, the sample keeps, of the repositories that neither GitHub's mark nor
    the model's response describes as a mirror, the one that GitHub created first, by
    `createdAt` in the sampling frame, then the one with the lowest id. A repository without a
    creation time comes last. A copy described as a mirror is labeled `true` when pull requests
    that people opened were merged in it since the start of the window, and the copy that nothing
    describes as a mirror is then still the one in which the project is developed. A copy labeled
    `false` leaves the choice to the others."""
    labeled = {}
    for repository in repositories:
        line = lines.get(repository["id"])
        if (repository["id"] in (undecided or {}) or not repository["revision"] or line is None
                or line["status"] == GONE or line["decision"]["outcome"] != LABELED
                or line["decision"]["label"] != TRUE):
            continue
        described = criterion_facts(line["decision"], "mirror")["described_as_mirror"] != FALSE
        labeled.setdefault(repository["revision"], []).append((described, repository))
    return {head: min(copies, key=lambda copy: (copy[0], not copy[1].get("created_at"),
                                                copy[1].get("created_at") or "",
                                                copy[1]["id"]))[1]["id"]
            for head, copies in labeled.items() if len(copies) > 1}


# The reason that the run's labels give a repository that the second model left without a
# decision too (`labels`), from which the outputs of a finished run read the second model and the
# kind of the classifying model's failed response (`second_model_failure`).
SECOND_MODEL_FAILED = re.compile(r"^the second model (?P<model>\S+), after the classifying "
                                 r"model's (?P<reason>\w+), ")


def second_model_failure(entry: dict) -> dict | None:
    """The second model and the kind of the classifying model's failed response of a repository
    that the second model left without a decision too, from its failed line (`Run.fail`) or its
    row of the run's labels (`labels`), or None for another repository without a decision."""
    if entry.get("model"):
        return {"model": entry["model"], "reason": entry.get("fallback_reason") or ""}
    found = SECOND_MODEL_FAILED.match(str(entry.get("reason") or ""))
    return found.groupdict() if found else None


def backup_served(decision: dict) -> dict | None:
    """The backup providers that served a label, from the provider of each response under
    `served_by` and of each summary of a page (`Classifier.label_readme`), and whether they
    served it wholly, every response and every summary, or None when the pinned providers served
    it alone. A decision made before the backup providers records no provider, and its pinned
    providers served it."""
    served = [*(decision.get("served_by") or []),
              *({"provider": call["summary_provider"],
                 **({"backup": True} if call.get("summary_backup") else {})}
                for call in decision.get("tools") or [] if call.get("summary_provider"))]
    backups = sorted({entry["provider"] for entry in served if entry.get("backup")})
    if not backups:
        return None
    return {"providers": backups, "wholly": all(entry.get("backup") for entry in served)}


def write_outputs(output_dir: Path, repositories: list, lines: dict,
                  record: dict, undecided: dict | None = None,
                  classifying_model: str = "") -> tuple[int, int]:
    """Write repos_annotated.csv and repos_excluded.csv in the order of the sampling frame and
    under its names, which the rest of the pipeline joins on, and `record`, the inputs of the
    labels, as repos_annotated.json beside them. Return the number of rows of each CSV. A
    repository in `undecided`, which the run left without a decision, is excluded under the
    reason UNDECIDED, and the record counts these repositories under the same key. A repository
    labeled `true` at the head commit of another one that the sample keeps is excluded under the
    reason COPY (`first_of_copies`). The record names the second model among the models when it
    labeled a repository of repos_annotated.csv. It lists each repository of
    repos_annotated.csv that the classifying model labeled at its higher temperature under
    HIGHER_TEMPERATURE_LABELS and each one that the second model labeled under
    SECOND_MODEL_LABELS, with the model, the classifying model and the kind of the classifying
    model's failed response. A repository that the second model left without a decision too is
    among the repositories without a decision, and the record lists it under
    SECOND_MODEL_FAILURES with the second model, `classifying_model` and that kind
    (`second_model_failure`). It lists each repository of repos_annotated.csv with a label that
    a backup provider served wholly or in part under BACKUP_PROVIDER_LABELS (`backup_served`)."""
    annotated, excluded = [], []
    kept_copies = first_of_copies(repositories, lines, undecided)
    models, response_models = set(), set()
    fallbacks = {HIGHER_TEMPERATURE_LABELS: [], SECOND_MODEL_LABELS: [],
                 SECOND_MODEL_FAILURES: [], BACKUP_PROVIDER_LABELS: []}
    without_decision = 0
    for repository in repositories:
        if repository["id"] in (undecided or {}):
            without_decision += 1
            failed = second_model_failure(undecided[repository["id"]])
            if failed is not None:
                fallbacks[SECOND_MODEL_FAILURES].append({
                    "repo_name": repository["name"], "github_id": repository["id"],
                    "classifying_model": classifying_model, **failed})
            excluded.append({"repo_name": repository["name"],
                             "repo_url": f"https://github.com/{repository['name']}",
                             "reason": UNDECIDED, "detected_language": ""})
            continue
        line = lines[repository["id"]]
        if line["status"] == GONE:
            excluded.append({"repo_name": repository["name"],
                             "repo_url": f"https://github.com/{repository['name']}",
                             "reason": readmes.NOT_FOUND_OR_NO_ACCESS,
                             "detected_language": ""})
            continue
        decision = line["decision"]
        kept = kept_copies.get(repository["revision"])
        if decision["outcome"] == LABELED and decision["label"] == TRUE and kept not in (
                None, repository["id"]):
            excluded.append({"repo_name": repository["name"],
                             "repo_url": f"https://github.com/{repository['name']}",
                             "reason": COPY, "detected_language": ""})
            continue
        if decision["outcome"] == LABELED:
            second = decision.get("second_model") or {}
            higher = decision.get("higher_temperature") or {}
            models.add(second.get("model") or decision["model"])
            entry = {"repo_name": repository["name"], "github_id": repository["id"],
                     "classifying_model": decision["model"]}
            if second:
                fallbacks[SECOND_MODEL_LABELS].append({**entry, "model": second["model"],
                                                       "reason": second["reason"]})
            elif higher:
                fallbacks[HIGHER_TEMPERATURE_LABELS].append({
                    **entry, "model": decision["model"], "temperature": higher["temperature"],
                    "reason": higher["reason"]})
            backup = backup_served(decision)
            if backup is not None:
                fallbacks[BACKUP_PROVIDER_LABELS].append({
                    **entry, "model": second.get("model") or decision["model"], **backup})
            if decision.get("response_model"):
                response_models.add(decision["response_model"])
            annotated.append({"repo_name": repository["name"],
                              "github_id": repository["id"],
                              "default_branch": repository["default_branch"],
                              "engineered_project": decision["label"],
                              "justification": published_justification(decision)})
        else:
            excluded.append({"repo_name": repository["name"],
                             "repo_url": f"https://github.com/{repository['name']}",
                             "reason": decision["reason"],
                             "detected_language": (decision.get("language") or {})
                             .get("detected") or ""})
    write_csv(Path(output_dir) / "repos_annotated.csv", ANNOTATED_COLUMNS, annotated)
    write_csv(Path(output_dir) / "repos_excluded.csv", EXCLUDED_COLUMNS, excluded)
    # The release names the model that labeled the sample. The models are read from the labels,
    # not from the run's `--model`, because a label reused from an earlier run records the response
    # model, the dated snapshot that responded in that run. The release also reads how many
    # repositories the sample lacks because the run left them without a decision, which
    # repositories the classifying model labeled at its higher temperature or the second model
    # labeled, and which ones the second model left without a decision too.
    caches.replace_file(Path(output_dir) / ANNOTATION_RECORD,
                        json.dumps({**record, "models": sorted(models),
                                    "response_models": sorted(response_models),
                                    UNDECIDED: without_decision, **fallbacks},
                                   indent=2) + "\n")
    return len(annotated), len(excluded)


def labels(rid: str, repositories: list, lines: dict,
           undecided: dict | None = None) -> list[dict]:
    """The outcome of every repository of run `rid`, in the order of the sampling frame, as the
    cache keeps it for the comparison with the next run. A repository in `undecided`, which holds
    the failed lines of the repositories left without a decision, has the outcome UNDECIDED, with
    what failed and the error as its reason, and for a failure of the second model the second
    model and the kind of the classifying model's failed response for which it labeled
    (SECOND_MODEL_FAILED)."""
    rows = []
    for repository in repositories:
        row = {"id": repository["id"], "repo_name": repository["name"]}
        failed = (undecided or {}).get(repository["id"])
        if failed is not None:
            reason = f"{failed['reason']}: {failed['error']}"
            if failed.get("model"):
                reason = (f"the second model {failed['model']}, after the classifying model's "
                          f"{failed.get('fallback_reason') or 'failure'}, {reason}")
            rows.append({**row, "outcome": UNDECIDED, "reason": reason})
            continue
        line = lines[repository["id"]]
        if line["status"] == GONE:
            rows.append({**row, "outcome": label_cache.GONE})
            continue
        decision = line["decision"]
        rows.append({**row, "outcome": decision["outcome"], "label": decision.get("label", ""),
                     "reason": decision.get("reason", ""),
                     "decided_in": (rid if line["status"] in (DECIDED, CARRIED)
                                    else line["decided_in"])})
    return rows


def scope_of(args) -> str:
    """What a run covered: the whole sampling frame, a list of repositories or the first N."""
    if args.repository_list:
        return "repository_list"
    if args.limit is not None:
        return "limit"
    return label_cache.SAMPLING_FRAME_SCOPE


def repositories_of(path: Path) -> str:
    """The SHA-256 of the GitHub ids of the repositories that the run of a partial file
    covers, from the file's first line, or "" for a file without it."""
    try:
        with open(path, encoding="utf-8") as f:
            first = json.loads(f.readline())
    except (OSError, ValueError):
        return ""
    return str(first.get("repositories") or "") if isinstance(first, dict) else ""


def start_partial(path: Path, repositories: str) -> None:
    """A new run's partial file, its first line naming the repositories that it covers."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"repositories": repositories}) + "\n", encoding="utf-8")


def same_run_key(cache_dir: Path, row: dict, values: dict) -> bool:
    """Whether the index row `row` has the model, the retrieval, the scope and the classification
    parameters of `values`, the parameters by their keys (`label_cache.ParameterKeys`). A run
    under parameters recorded before 2026-11-04 that differ from those of `values` only in the
    provider of a model is a run under the same parameters."""
    keys = label_cache.ParameterKeys(cache_dir)
    return all(keys(row[k]) == keys(str(v)) if k == "parameters" else row[k] == str(v)
               for k, v in values.items())


def resumable_run(cache_dir: Path, values: dict, repositories: str) -> str | None:
    """The id of a stopped run with the same model, classification parameters, retrieval and
    scope (`same_run_key`), with a partial file that is still there and names the same
    repositories. `repositories` is the SHA-256 of their GitHub ids. Two lists of repositories
    share one scope, and without the SHA-256 a run over the second list would take up a stopped
    run over the first list. The run over the first list would then never decide the
    repositories that it lacked."""
    for row in reversed(label_cache.read_index(cache_dir)):
        partial = label_cache.partial_path(cache_dir, row["run_id"])
        if (row["outcome"] in label_cache.RESUMABLE and same_run_key(cache_dir, row, values)
                and partial.is_file() and repositories_of(partial) == repositories):
            return row["run_id"]
    return None


def written_by(output_dir: Path, rid: str) -> bool:
    """Whether the outputs in `output_dir` are those of run `rid`: `write_outputs` writes
    ANNOTATION_RECORD, which names the run, after both CSVs."""
    try:
        record = json.loads((Path(output_dir) / ANNOTATION_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("run_id") == rid


def finished_run_to_complete(cache_dir: Path, values: dict, repositories: str,
                             output_dir: Path | None,
                             new_run: bool = False) -> tuple[dict, list[dict]] | None:
    """The index row and the labels of the latest finished run of the scope in `values`, if the
    run has the model, classification parameters and retrieval of `values` and covers the
    repositories that `repositories` names by the SHA-256 of their GitHub ids. Otherwise None.
    A finished run over part of the sampling frame is returned only while it lacks its outputs,
    and with `new_run`, no finished run over the sampling frame is returned.

    The index records a run as finished before the run writes its outputs and before it is
    compared with the previous run (`compare_with_previous_runs`). When the comparison exits 1,
    or a stop ends the invocation while it compares or writes the outputs, the step has not
    finished. A new run would then request every README again and add a run to the cache of
    labels before the comparison of the earlier run is finished.

    A finished run over the sampling frame with its outputs and its comparison is returned too.
    The runner runs the labeling again after a change to the code of the script or of one of its
    modules, even a change that decides no label, such as one to the cost record. A new run
    would then request every README again, record each repository that GitHub no longer serves
    as `not_found_or_no_access` and change the sample that the collection clones, and the
    cloning step would begin a new collection. The run is completed instead when the current
    rules derive each of its decisions unchanged (`complete_finished_run`).

    The outputs are complete when `output_dir` is None or when ANNOTATION_RECORD there names the
    run (`written_by`). The comparison is complete when the index row names the run that the run
    was compared with (`compared_with`), when no previous run precedes the run, and for a run
    over part of the sampling frame, which is never compared. The classification parameters are
    compared by their keys (`same_run_key`)."""
    row = latest_finished_run(cache_dir, values)
    if row is None:
        return None
    if values["scope"] == label_cache.SAMPLING_FRAME_SCOPE:
        if new_run:
            return None
    elif output_dir is None or written_by(output_dir, row["run_id"]):
        return None
    labels = label_cache.read_labels(cache_dir, row["run_id"], row["labels_sha256"])
    if label_cache.digest(sorted(int(label["id"]) for label in labels)) != repositories:
        return None
    return row, labels


def latest_finished_run(cache_dir: Path, values: dict) -> dict | None:
    """The index row of the latest finished run over the scope in `values` that has its labels,
    if the run has the model, the classification parameters and the retrieval of `values`
    (`same_run_key`). Otherwise None."""
    rows = [row for row in label_cache.read_index(cache_dir)
            if row["outcome"] == "finished" and row["scope"] == values["scope"]
            and row["labels_sha256"]]
    if not rows or not same_run_key(cache_dir, rows[-1], values):
        return None
    return rows[-1]


def failed_in_finished_run(cache_dir: Path, values: dict) -> tuple[set[int], set[str]]:
    """The repositories that the latest finished run under the model, the classification
    parameters, the retrieval and the scope of `values` left without a decision
    (`latest_finished_run`), by what failed them, as the run's labels give it in the reason of
    each (`labels`). The first set holds the GitHub ids of the repositories that the model failed,
    the classifying model or the second model after it, as `Classifier.failed_earlier` names them
    in a run. The second holds the names of the repositories that GitHub failed, as
    `GitHubPause.failed_before` names them. Both are empty without such a run."""
    row = latest_finished_run(cache_dir, values)
    model, github = set(), set()
    if row is None:
        return model, github
    for label in label_cache.read_labels(cache_dir, row["run_id"], row["labels_sha256"]):
        if label["outcome"] != UNDECIDED:
            continue
        reason = str(label.get("reason") or "")
        second = SECOND_MODEL_FAILED.match(reason)
        failure = reason[second.end():] if second else reason
        # The second model labels only repositories that the classifying model failed.
        if second or failure.startswith(f"{MODEL_FAILED}: "):
            model.add(int(label["id"]))
        if failure.startswith(f"{GITHUB_FAILED}: "):
            github.add(label["repo_name"])
    return model, github


def finished_lines(cache_dir: Path, rid: str, labels: list[dict]) -> tuple[dict, dict]:
    """The lines of finished run `rid` by GitHub id, with the decisions that its `labels` name,
    as `write_outputs` reads them, and the labels of the repositories that the run left without
    a decision."""
    wanted: dict = {}
    for label in labels:
        if label["outcome"] in (LABELED, EXCLUDED):
            wanted.setdefault(label["decided_in"], set()).add(int(label["id"]))
    decisions = label_cache.decisions_of(cache_dir, wanted)
    lines, undecided = {}, {}
    for label in labels:
        number = int(label["id"])
        if label["outcome"] == label_cache.GONE:
            lines[number] = {"id": number, "status": GONE, "name": label["repo_name"]}
        elif label["outcome"] == UNDECIDED:
            undecided[number] = label
        else:
            lines[number] = {"id": number,
                             "status": DECIDED if label["decided_in"] == rid else REUSED,
                             "decision": decisions[(label["decided_in"], number)],
                             "decided_in": label["decided_in"]}
    return lines, undecided


# The directory of the collection stage, where the cloning step keeps the list of finished
# repositories and the records of its collections (`agentcontext.resume`), which tell whether a
# collection over the sample has begun (`collection_over`).
COLLECTION_DIR = STAGE_DIR
# The time at which a retrieval of GHS or a collection began, in UTC, at the start of its name.
STARTED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z")


def collection_over(retrieval_id: str, collection_dir: Path | None = None) -> str | None:
    """The name of a collection that began after the retrieval `retrieval_id` of the
    sampling frame, and which therefore collects a sample of that sampling frame, or None when
    none began after it. The collection is the collection in progress, the last collection that
    ended, or a collection that wrote a line of the list of finished repositories in
    `collection_dir` (COLLECTION_DIR by default). The names of both begin with the time at which
    each began (STARTED_AT), and a retrieval id of another form counts every collection.

    The list is read beside the records, because a collection that the runner of an earlier image ended
    left no record. A collection of an earlier sampling frame began before the retrieval of this
    sampling frame, which the monthly run takes after the run before it has completed. A monthly
    run whose GHS download fell back to the previous retrieval reuses its retrieval id, and the
    collection of the previous month began after it. That run's `--from repository_cloning` has
    marked that collection as replaced (`resume.replaced_collection`), and neither it nor a
    collection that began before it counts."""
    from agentcontext import resume
    finished = (COLLECTION_DIR if collection_dir is None else collection_dir) / resume.FINISHED_NAME
    in_progress = resume.read_collection(resume.collection_path(finished))
    ended = resume.read_collection(resume.ended_path(finished))
    names = [record["collection"] for record in (ended,) if record is not None]
    names += sorted({line[10] for line in resume.read_finished(finished).values()
                     if isinstance(line[10], str)}, reverse=True)
    replaced = resume.replaced_collection(finished)
    if replaced is not None:
        names = [name for name in names if name != replaced
                 and began_after(name, STARTED_AT.match(replaced))]
    if in_progress is not None:
        names.insert(0, in_progress["collection"])
    began = STARTED_AT.match(retrieval_id or "")
    for name in names:
        if began is None or began_after(name, began):
            return name
    return None


def began_after(name: str, began: re.Match | None) -> bool:
    """Whether the collection `name` began at or after the time `began` (STARTED_AT), and False
    where either time is missing."""
    started = STARTED_AT.match(name)
    return began is not None and started is not None and started.group() >= began.group()


def compared_and_written(cache_dir: Path, row: dict, output_dir: Path | None) -> bool:
    """Whether the finished run of the index row `row` has been compared with the previous run
    and has written its outputs to `output_dir`, as `finished_run_to_complete` decides both."""
    compared = (row["scope"] != label_cache.SAMPLING_FRAME_SCOPE or row["compared_with"]
                or label_cache.previous_run(cache_dir, row["run_id"]) is None)
    return bool(compared) and (output_dir is None or written_by(output_dir, row["run_id"]))


def earlier_comparisons(cache_dir: Path, rid: str) -> list[str]:
    """The runs with a comparison that label_changes.csv lists before the comparison of run
    `rid`, in the order of the runs. They are found from `rid` backward: the run that `rid` was
    or will be compared with (`label_cache.previous_run`), then the run that this run was
    compared with, and so on, while the run is over the retrieval and the scope of `rid` and the
    cache of labels has its comparison (`changes_sha256`). A run that left repositories without
    a decision and the new run that tries them again are over the same retrieval (`main`). The
    changes of the first therefore stay in the file when a later invocation compares the new run
    after a kill ended it, or writes the comparison of the new run again from the cache of
    labels."""
    rows = {row["run_id"]: row for row in label_cache.read_index(cache_dir)}
    run = rows[rid]
    previous = run["compared_with"]
    if not previous:
        found = label_cache.previous_run(cache_dir, rid)
        previous = found["run_id"] if found else ""
    earlier = []
    while (previous in rows and rows[previous]["retrieval_id"] == run["retrieval_id"]
           and rows[previous]["scope"] == run["scope"] and rows[previous]["changes_sha256"]):
        earlier.insert(0, previous)
        previous = rows[previous]["compared_with"]
    return earlier


async def complete_finished_run(run: Run, cache_dir: Path, row: dict, labels: list[dict],
                                output_dir: Path | None, retrieval_id: str,
                                unserved: bool = False) -> tuple[bool, int]:
    """Write the outputs of `run`, the finished run that `finished_run_to_complete` returned
    with its index row `row` and its `labels`, and return True, so that the comparison follows
    without a new run, with the number of repositories that the run left without a decision for
    a new run after the comparison to try again (`main`), or 0 where there are none. The
    outputs are written again with the same bytes where the run already wrote them, because the
    runner records the step only when the step rewrote each output that it declares, and stops
    the Start otherwise. For the same reason, label_changes.csv is written again from the cache
    of labels when the run was compared, because the comparison that follows leaves a compared
    run alone. The file lists the comparisons of the earlier runs over the same retrieval that
    lead to the run before its own comparison (`earlier_comparisons`). Return False and 0 when
    the current rules derive one of the run's decisions differently or need GitHub to check it
    (`Run.derives_unchanged`). A new image can change the rules after the run finished, and the
    run's outputs would then publish the labels of the earlier rules. A new run labels the
    repositories instead. A repository that GitHub stopped serving after the run keeps the run's
    decision.

    Missing labels are tried again by default. A run over the sampling frame that left
    repositories without a decision is therefore not completed while no collection over its
    sample has begun (`collection_over`): the function returns False and 0, and the new run
    that follows requests their READMEs and labels them, reusing every other decision. Once a
    collection has begun, a new run would request every README again, and a repository that it
    labels `true` or finds gone would change the sample of the collection. The function then
    completes the run without them and logs a WARNING with their count and `--new-run`, which
    starts that new run. A run with outputs or a comparison still to complete is completed
    first, and the invocation then starts the new run, after the comparison (`main`). The
    runner records the labeling once the invocation exits 0, and the selection and the
    collection follow without another invocation of the labeling, which would leave the
    repositories without a decision once the collection had begun.

    `unserved` is true while OpenRouter's lists show that OpenRouter serves no request of the
    classifying model or the summary model (`args.unserved`). A new run would then exit 1
    before its first request, at this invocation and at every later one until the lists change,
    and the pipeline would stop at the labeling, before the collection. The function therefore
    completes the run without a new run, as it does once a collection has begun, and returns the
    number of these repositories, and `main` skips the new run with a WARNING. A new run that
    tries the repositories again stops only where any run stops, as when the API's service or
    GitHub fails through every pause, or when the credit stays spent through the pause for
    credit."""
    lines, undecided = finished_lines(cache_dir, run.rid, labels)
    begun = None
    if undecided and row["scope"] == label_cache.SAMPLING_FRAME_SCOPE:
        first = next(iter(undecided.values()))["repo_name"]
        begun = collection_over(retrieval_id)
        if begun is None and not unserved and compared_and_written(cache_dir, row, output_dir):
            logs.info(COMPONENT, f"Run {run.rid} left {len(undecided):,} repositories without a "
                                 f"decision, such as {first}, and no collection over its sample "
                                 f"has begun. Starting a new run, which tries them again.")
            return False, 0
    for repository in run.repositories:
        line = lines.get(repository["id"])
        if (line is not None and line["status"] != GONE
                and not await run.derives_unchanged(repository, line["decision"])):
            logs.info(COMPONENT, f"Run {run.rid} finished over the same repositories, model, "
                                 f"classification parameters and retrieval. Starting a new run, "
                                 f"because the current rules derive the decision for "
                                 f"{repository['name']} differently or need GitHub to check it.")
            return False, 0
    logs.info(COMPONENT, f"Run {run.rid} finished over the same repositories, model, "
                         f"classification parameters and retrieval, and the current rules derive "
                         f"each of its decisions unchanged. Writing its outputs from its labels "
                         f"without a new run, which requests no README.")
    try_again = len(undecided) if row["scope"] == label_cache.SAMPLING_FRAME_SCOPE else 0
    if begun is not None:
        try_again = 0
        logs.warning(COMPONENT, f"Run {run.rid} left {len(undecided):,} repositories without a "
                                f"decision, such as {first}, and the collection {begun} over "
                                f"its sample has begun. Completing the run without them, because "
                                f"a new run would request every README again and could change "
                                f"the sample that the collection collects. To try them again, "
                                f"label in a new run with --new-run.")
    elif try_again and not unserved:
        logs.info(COMPONENT, f"Run {run.rid} left {len(undecided):,} repositories without a "
                             f"decision, such as {first}, and no collection over its sample has "
                             f"begun. Once the outputs and the comparison of the run are "
                             f"complete, this invocation starts a new run, which tries them "
                             f"again.")
    if output_dir is not None:
        write_run_outputs(output_dir, run.rid, run.repositories, lines, undecided,
                          run.parameters_sha, retrieval_id, run.model)
        if row["compared_with"]:
            import changed_labels
            changed_labels.write_changes_csv(
                cache_dir, output_dir, [*earlier_comparisons(cache_dir, run.rid), run.rid])
    run.partial.unlink(missing_ok=True)
    return True, try_again


def write_run_outputs(output_dir: Path, rid: str, repositories: list, lines: dict,
                      undecided: dict, parameters_sha: str, retrieval_id: str,
                      model: str) -> None:
    """The outputs of run `rid` of the classifying model `model` in `output_dir`, with the
    record that names the run."""
    annotated, excluded = write_outputs(
        output_dir, repositories, lines,
        {"description": f"run {rid} of the classification stage", "run_id": rid,
         "parameters": parameters_sha, "retrieval_id": retrieval_id}, undecided, model)
    logs.info(COMPONENT, f"Wrote {annotated:,} labeled and {excluded:,} excluded repositories "
                         f"to {output_dir}.")


def summary_endpoint(args) -> endpoints.Endpoint:
    """The endpoint of the summary model that `args` name, or ValueError
    (`endpoints.endpoint`)."""
    return endpoints.endpoint(args.summary_model, getattr(args, "summary_provider", ""),
                              getattr(args, "summary_quantization", ""),
                              getattr(args, "summary_hugging_face_id", ""))


def classifying_endpoint(args) -> endpoints.Endpoint:
    """The endpoint of the classifying model that `args` name, or ValueError."""
    return endpoints.endpoint(args.model, getattr(args, "provider", ""),
                              getattr(args, "quantization", ""),
                              getattr(args, "hugging_face_id", ""))


def run_settings(args) -> endpoints.Settings:
    """The settings of the requests of the classifying model that `args` name: those of the
    model (`endpoints.settings_of`), with the reasoning effort, the temperature, the top_p and
    the seed that the command line names. With --endpoint-default-temperature, the requests send
    neither a temperature, a top_p nor a seed, and the endpoint applies its own defaults, also in
    the attempts after a failed response. ValueError for an OpenAI model
    given a temperature, a top_p, a seed or --endpoint-default-temperature, because OpenAI's
    reasoning models reject a temperature."""
    found = classifying_endpoint(args)
    named = {setting: getattr(args, setting, None) for setting in ("temperature", "top_p", "seed")}
    endpoint_default = getattr(args, "endpoint_default_temperature", False)
    if endpoint_default and any(value is not None for value in named.values()):
        raise ValueError("--endpoint-default-temperature sends no temperature, top_p or seed, "
                         "and it takes neither --temperature, --top-p nor --seed")
    if not found.openrouter and (endpoint_default
                                 or any(value is not None for value in named.values())):
        raise ValueError(f"{found.model} is an OpenAI model, whose requests take no "
                         f"temperature, top_p or seed")
    settings = endpoints.settings_of(found)
    if getattr(args, "reasoning_effort", None):
        settings = dataclasses.replace(settings, reasoning_effort=args.reasoning_effort)
    if endpoint_default:
        return dataclasses.replace(settings, temperature=None, top_p=None, seed=None,
                                   higher_temperature=None)
    return dataclasses.replace(settings, **{setting: value for setting, value in named.items()
                                            if value is not None})


def described(settings: endpoints.Settings, found: endpoints.Endpoint) -> str:
    """The settings of the requests of the model at `found` as a log line names them."""
    parts = [f"reasoning effort {settings.reasoning_effort}"]
    if found.openrouter:
        parts += [f"{name} {value}" for name, value in settings.sampled().items()]
        if settings.max_output_tokens is not None:
            parts.append(f"an output limit of {settings.max_output_tokens:,} tokens")
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


# The roles of the models of a run, as the log lines and the check of the backup providers name
# them (`backup_check`, `backup_for`).
CLASSIFYING_ROLE, SUMMARY_ROLE, SECOND_ROLE = "classifying model", "summary model", "second model"


def backup_for(args, found: endpoints.Endpoint, role: str) -> endpoints.Endpoint | None:
    """The backup provider of the model at `found` in `role` for a run with `args`
    (`endpoints.backup_of`), or None: with --no-backup-provider, and when the check before the
    first request found that the backup provider serves no request of the model in that role
    (`backup_check`, `args.unserved_backups`)."""
    if (getattr(args, "no_backup_provider", False)
            or role in (getattr(args, "unserved_backups", None) or {})):
        return None
    return endpoints.backup_of(found)


def start_on_backup(args, classifier: "Classifier", role: str) -> None:
    """Start the requests of the model of `classifier` in `role` on its backup provider when the
    check before the first request found its pinned endpoint missing from OpenRouter's endpoint
    list (`endpoint_check`, `args.started_on_backups`). One request every
    `endpoints.PINNED_CHECK_SECONDS` tests the pinned provider, as after a switch. The summary
    model, in the server of the tools, moves to its backup provider on its first failed
    summary."""
    reason = (getattr(args, "started_on_backups", None) or {}).get(role)
    if reason:
        classifier.providers.switch(f"is missing from OpenRouter's endpoint list of the model at "
                                    f"the start of the invocation ({reason})")


def backup_check(classifying: endpoints.Endpoint, settings: endpoints.Settings,
                 summary: endpoints.Endpoint, second: endpoints.Endpoint | None = None,
                 get=None) -> dict[str, str]:
    """Why the backup provider of a model of the run serves no request of its role, by role,
    from OpenRouter's endpoint list before the first request (`endpoints.unserved`): the
    classifying model with `settings`, the summary model and the second model `second` with its
    own settings, each with the parameters and the output limit of its requests. Each backup
    provider that serves none gets a WARNING, and the model labels or summarizes without it, as
    without a backup provider. A list that cannot be read leaves the test to the first request
    that the backup provider gets, with a WARNING."""
    checked = [(CLASSIFYING_ROLE, classifying, endpoints.request_parameters(settings),
                settings.largest_output_tokens()),
               (SUMMARY_ROLE, summary, endpoints.SUMMARY_PARAMETERS, None)]
    if second is not None:
        own = endpoints.settings_of(second)
        checked.append((SECOND_ROLE, second, endpoints.request_parameters(own),
                        own.max_output_tokens))
    unserved = {}
    for role, found, parameters, limit in checked:
        backup = endpoints.backup_of(found)
        if backup is None:
            continue
        try:
            reason = endpoints.unserved(backup, parameters, get, limit)
        except (OSError, ValueError) as error:
            logs.warning(COMPONENT, f"Could not read OpenRouter's endpoint list of {found.model} "
                                    f"({error}). The first request that the backup provider "
                                    f"{backup.provider} of the {role} gets tests it instead.")
            continue
        if reason:
            unserved[role] = reason
            logs.warning(COMPONENT, f"The backup provider {backup.provider} of the {role}, "
                                    f"{found.model}, serves no request of the {role} in this "
                                    f"invocation. {reason}. While the pinned provider "
                                    f"{found.provider} fails, the requests of the {role} pause "
                                    f"and use attempts as without a backup provider.")
    return unserved


def second_endpoint(args, classifying: endpoints.Endpoint) -> endpoints.Endpoint | None:
    """The second model of a run of the classifying model at `classifying`,
    endpoints.SECOND_MODEL on its pinned endpoint with its own settings, or None: with
    --no-second-model, which a check of one model passes, and in a run of the second model
    itself, which would label again with the model that failed."""
    if (getattr(args, "no_second_model", False)
            or classifying.model == endpoints.SECOND_MODEL.model):
        return None
    return endpoints.SECOND_MODEL


def open_bridge(args):
    """The bridge to the MCP server of the tools, which the bridge starts with the summary model,
    its endpoint, its backup provider (`backup_for`) and the credentials file of the
    classification."""
    summary = summary_endpoint(args)
    arguments = ["--summary-model", summary.model, "--summary-effort", args.summary_effort]
    if summary.openrouter:
        arguments += ["--summary-provider", summary.provider,
                      "--summary-quantization", summary.quantization,
                      "--summary-hugging-face-id", summary.hugging_face_id]
        backup = backup_for(args, summary, SUMMARY_ROLE)
        if backup is not None:
            arguments += ["--summary-backup-provider", backup.provider]
    return mcp_bridge.Bridge(arguments=[*arguments, "--credentials", str(args.credentials)],
                             supplied=SUPPLIED, offered=OFFERED, own_tools=(CHECKLIST_TOOL_NAME,))


def refused_endpoint(found: endpoints.Endpoint, parameters: tuple, effort: str,
                     max_output_tokens: int | None, role: str, listed, get=None,
                     backup: endpoints.Endpoint | None = None,
                     delisted: dict | None = None) -> str | None:
    """Why the run sends no request of the model at `found` in `role`, from OpenRouter's lists
    before the first request, or None: its pinned endpoint serves no request with `parameters`
    or generates fewer tokens than `max_output_tokens` (`endpoints.unserved`), or the model
    does not take the reasoning effort `effort` (`endpoints.unsupported_effort`), which the
    classification parameters would then name. `listed()` returns OpenRouter's model list
    (`endpoints.model_list`). A list that cannot be read leaves its test to the first request,
    with a WARNING.

    A pinned endpoint that the endpoint list does not name (`endpoints.Delisted`), such as an
    endpoint that its provider took down for a while, refuses nothing when `backup`, the backup
    provider of the model, serves the same requests: `delisted` then gets the reason under
    `role`, and the model starts on its backup provider (`classify`)."""
    try:
        reason = endpoints.unserved(found, parameters, get, max_output_tokens)
    except (OSError, ValueError) as error:
        logs.warning(COMPONENT, f"Could not read OpenRouter's endpoint list of {found.model} "
                                f"({error}). The first request tests the endpoint "
                                f"{found.provider} instead.")
        reason = None
    if isinstance(reason, endpoints.Delisted) and backup is not None and delisted is not None:
        try:
            serves = not endpoints.unserved(backup, parameters, get, max_output_tokens)
        except (OSError, ValueError):
            serves = False
        if serves:
            delisted[role] = str(reason)
            reason = None
    if reason:
        return (f"{reason}. OpenRouter routes no request of the {role} to the endpoint "
                f"{found.provider} and responds to each with HTTP 404")
    try:
        models = listed()
    except (OSError, ValueError) as error:
        logs.warning(COMPONENT, f"Could not read OpenRouter's model list ({error}). The run "
                                f"sends the {role} the reasoning effort {effort} without "
                                f"checking that the model takes it.")
        return None
    return endpoints.unsupported_effort(found, effort, models)


def endpoint_check(classifying: endpoints.Endpoint, settings: endpoints.Settings,
                   summary: endpoints.Endpoint, summary_effort: str,
                   second: endpoints.Endpoint | None = None, get=None,
                   backups: bool = True) -> tuple:
    """What OpenRouter's endpoint list and model list show before the first request
    (`refused_endpoint`): why the run cannot start, because OpenRouter serves no request of the
    classifying model with `settings` or of the summary model with `summary_effort` as they
    are, or None, why the second model `second` cannot label, or None, and by role the reason
    of each model with a pinned endpoint that the endpoint list does not name and a backup
    provider that serves the same requests, which the model starts on (`start_on_backup`).
    `backups` is false with --no-backup-provider, and such a pinned endpoint then refuses the
    requests. A second model that cannot label leaves the repositories that it would have
    labeled without a decision, and the invocation starts without it, while its earlier labels
    still apply (`Run.applies`)."""
    cache: dict = {}
    delisted: dict = {}

    def listed() -> dict:
        """OpenRouter's model list, read once."""
        if "models" not in cache:
            cache["models"] = endpoints.model_list(get)
        return cache["models"]

    def backup(found: endpoints.Endpoint) -> endpoints.Endpoint | None:
        return endpoints.backup_of(found) if backups else None

    def started_on_backups() -> dict:
        """`delisted`, with a WARNING for the summary model, whose requests the server of the
        tools sends. The classifier of each other model logs its start on the backup provider
        (`start_on_backup`)."""
        if SUMMARY_ROLE in delisted:
            logs.warning(COMPONENT, f"{delisted[SUMMARY_ROLE]}. The first summary that the pinned "
                                    f"provider {summary.provider} of the summary model fails "
                                    f"goes to its backup provider {backup(summary).provider} in "
                                    f"{summary.quantization}, which serves the summaries with "
                                    f"the same settings until a request to the pinned provider "
                                    f"gets a response.")
        return delisted
    checked = [(classifying, endpoints.request_parameters(settings), settings.reasoning_effort,
                settings.largest_output_tokens(), CLASSIFYING_ROLE,
                "Pin an endpoint that serves them with --provider and --quantization, or name "
                "another effort with --reasoning-effort"),
               (summary, endpoints.SUMMARY_PARAMETERS, summary_effort, None, SUMMARY_ROLE,
                "Pin an endpoint that serves them with --summary-provider and "
                "--summary-quantization, or name another effort with --summary-effort")]
    for found, parameters, effort, limit, role, remedy in checked:
        if found.openrouter:
            reason = refused_endpoint(found, parameters, effort, limit, role, listed, get,
                                      backup(found), delisted)
            if reason:
                return f"{reason}. {remedy}", None, {}
    if second is None or not second.openrouter:
        return None, None, started_on_backups()
    own = endpoints.settings_of(second)
    reason = refused_endpoint(second, endpoints.request_parameters(own), own.reasoning_effort,
                              own.max_output_tokens, SECOND_ROLE, listed, get, backup(second),
                              delisted)
    return None, reason, started_on_backups()


def requests_unserved(args) -> bool:
    """Whether the invocation with `args` sends no request, because OpenRouter's lists showed
    before the first request that it serves no request of the classifying model or of the
    summary model as they are (`endpoint_check`, `args.unserved`), with an ERROR that names why.
    The labeling and the comparison test it before their first request, so that an invocation
    that completes a finished run from its labels and repeats no label is not refused."""
    unserved = getattr(args, "unserved", "")
    if unserved:
        logs.error(COMPONENT, f"{unserved}.")
    return bool(unserved)


def credit_check(key_credit):
    """The test of `CreditPause` for a run with `key_credit`, the credit of the OpenRouter API key
    (`endpoints.KeyCredit`): OpenRouter's credit counts as spent while the key shows none left,
    and a key that limits nothing, a key that cannot be read and OpenAI leave the test to one
    request, which costs nothing when it is refused."""
    async def check(service: str) -> bool:
        if service != endpoints.OPENROUTER or key_credit is None:
            return True
        try:
            remaining = await asyncio.to_thread(key_credit.remaining)
        except (OSError, ValueError) as error:
            logs.warning(COMPONENT, f"Could not read the credit of the OpenRouter API key "
                                    f"({error}). A request tests the credit instead.")
            return True
        return remaining is None or remaining > 0
    return check


async def log_credit(key_credit, cache_dir: Path, model: str, parameters_sha: str,
                     classifying: endpoints.Endpoint, summary: endpoints.Endpoint,
                     left: int, backups: tuple = (None, None)) -> None:
    """Before the first request of a run through OpenRouter, log the credit that the OpenRouter
    API key has left and the estimated cost of the `left` repositories (`costs.per_repository`),
    and a WARNING with the shortfall when the credit covers less. `backups` are the backup
    providers of the classifying model and the summary model, or None, and the line also states
    what the repositories cost if the backup providers serve every request, at their prices
    (`costs.per_repository_at`), with a WARNING when the credit covers the estimate at the pinned
    providers and not that one. The run starts anyway, because it waits for credit when
    OpenRouter refuses its requests (`CreditPause`)."""
    each, source = costs.per_repository(cache_dir, model, parameters_sha, classifying, summary)
    cost = each * left if each is not None else None
    costing = (f"The {left:,} repositories left cost about ${cost:,.2f} at ${each:,.4f} per "
               f"repository, {source}" if each is not None
               else f"The cost of the {left:,} repositories left has no estimate, because "
                    f"{source}")
    served = (backups[0] or classifying, backups[1] or summary)
    backup_cost = None
    if each is not None and served != (classifying, summary):
        backup_each = costs.per_repository_at(cache_dir, model, parameters_sha, *served)
        if backup_each is not None:
            backup_cost = backup_each * left
            providers = sorted({found.provider for found in backups if found is not None})
            named = (f"provider {providers[0]} serves" if len(providers) == 1
                     else f"providers {' and '.join(providers)} serve")
            costing += (f", and about ${backup_cost:,.2f} at ${backup_each:,.4f} per "
                        f"repository if the backup {named} every request")
    try:
        remaining = await asyncio.to_thread(key_credit.remaining)
    except (OSError, ValueError) as error:
        logs.warning(COMPONENT, f"Could not read the credit of the OpenRouter API key "
                                f"({error}). {costing}.")
        return
    if remaining is not None:
        remaining = costs.Decimal(str(remaining))
    if remaining is None:
        logs.info(COMPONENT, f"The OpenRouter API key has no credit limit and no budget, and the "
                             f"balance of the account takes a management key to read. {costing}.")
        return
    logs.info(COMPONENT, f"The OpenRouter API key has ${remaining:,.2f} of credit left. "
                         f"{costing}.")
    if cost is not None and remaining < cost:
        logs.warning(COMPONENT, f"The credit of the OpenRouter API key covers ${remaining:,.2f} "
                                f"of the estimated ${cost:,.2f}, ${cost - remaining:,.2f} short. "
                                f"The run starts, and it waits for credit when OpenRouter refuses "
                                f"its requests. To continue then, "
                                f"{endpoints.TOP_UP[endpoints.OPENROUTER]}.")
    elif backup_cost is not None and remaining < backup_cost:
        logs.warning(COMPONENT, f"The credit of the OpenRouter API key covers the estimated "
                                f"${cost:,.2f} at the pinned providers, but only ${remaining:,.2f} "
                                f"of the estimated ${backup_cost:,.2f} if the backup providers "
                                f"serve every request. The run starts, and it waits for credit "
                                f"when OpenRouter refuses its requests. To continue then, "
                                f"{endpoints.TOP_UP[endpoints.OPENROUTER]}.")


async def classify(args, repositories: list, retrieval_id: str, thresholds: forks.Thresholds,
                   github, openai_client, bridge_for=open_bridge, sleep=None,
                   clock=None, key_credit=None, second_client=None) -> int:
    """The run over `repositories` in the sampling frame of `retrieval_id`, which decides a fork's
    independence under `thresholds`, and the run's exit code. `openai_client` is the SDK's client
    of the classifying model's service, OpenAI's or OpenRouter's, `second_client` the client of
    the second model's service, `openai_client` by default, and `key_credit` the credit of the
    OpenRouter API key (`endpoints.KeyCredit`) when a model of the run is OpenRouter's. `sleep`
    and `clock` are the waits and the clock of the run's pauses and retry rounds,
    `asyncio.sleep` and `time.monotonic` by default."""
    cache_dir = Path(args.cache_dir)
    prompt = PROMPT.read_text(encoding="utf-8")
    classifying, summarizing = classifying_endpoint(args), summary_endpoint(args)
    settings = run_settings(args)
    # The second model that the run is configured with, whose labels and kept responses apply
    # also in an invocation in which it cannot label, such as while its endpoint is unserved
    # (`second_model_unavailable`, `Run.applies`). Its classifier labels only when it can.
    second = second_endpoint(args, classifying)
    labels_with_second = second is not None and not getattr(args, "second_model_unavailable",
                                                            "")
    async with bridge_for(args) as bridge:
        parameters_sha = label_cache.record(
            "parameters", run_parameters(args, prompt, bridge.function_tools,
                                         bridge.output_schemas), cache_dir)
        key = {"model": args.model, "parameters": parameters_sha, "retrieval_id": retrieval_id,
               "scope": scope_of(args)}
        covered = label_cache.digest(sorted(r["id"] for r in repositories))
        # OpenRouter has no service tier of OpenAI's, and the classifier names none in the
        # requests of an OpenRouter model. The provider that serves a request, the pinned provider
        # or its backup provider, sets its price. The cost record prices the tokens of such a model
        # that only the index counts at the provider that `costs.beyond_tier` gives, and reads no
        # tier from here.
        service_tier = (classifying.provider if classifying.openrouter
                        else getattr(args, "service_tier", None))
        classifier = Classifier(openai_client, bridge, args.model, prompt, settings,
                                parameters_sha, sleep=sleep, service_tier=service_tier,
                                clock=clock, summary_model=args.summary_model,
                                endpoint=classifying, summary=summarizing,
                                credit_check=credit_check(key_credit), higher=settings.higher(),
                                backup=backup_for(args, classifying, CLASSIFYING_ROLE),
                                role=f"the {CLASSIFYING_ROLE}")
        start_on_backup(args, classifier, CLASSIFYING_ROLE)
        second_classifier = None
        if labels_with_second:
            second_classifier = Classifier(second_client or openai_client, bridge, second.model,
                                           prompt, endpoints.settings_of(second), parameters_sha,
                                           sleep=sleep, clock=clock,
                                           summary_model=args.summary_model, endpoint=second,
                                           summary=summarizing,
                                           credit_check=credit_check(key_credit),
                                           backup=backup_for(args, second, SECOND_ROLE),
                                           role=f"the {SECOND_ROLE}")
            start_on_backup(args, second_classifier, SECOND_ROLE)
            # The responses that wait for GitHub's facts, one dict for both models: a response
            # of the second model names that model (`assess`), whichever classifier finds it.
            second_classifier.kept = classifier.kept
        fallbacks = run_fallbacks(classifier, second)
        output_dir = args.output_dir or (CLASSIFICATION_DIR if key["scope"] ==
                                         label_cache.SAMPLING_FRAME_SCOPE else None)
        rid = resumable_run(cache_dir, key, covered)
        if rid is None:
            found = finished_run_to_complete(cache_dir, key, covered, output_dir,
                                             new_run=getattr(args, "new_run", False))
            if found is not None:
                row, finished_labels = found
                # The finished run's decisions are checked under the current rules as `recheck`
                # checks the lines of a resumed run, without a request and without a line written.
                checked = Run(row["run_id"], repositories, label_cache.Decisions(), github,
                              classifier, args.model, parameters_sha,
                              label_cache.partial_path(cache_dir, row["run_id"]), args.workers,
                              thresholds, second=second_classifier, second_model=second)
                completed, args.try_undecided_again = await complete_finished_run(
                    checked, cache_dir, row, finished_labels, output_dir, retrieval_id,
                    unserved=bool(getattr(args, "unserved", "")))
                if completed:
                    if getattr(args, "unserved", ""):
                        logs.warning(COMPONENT, f"{args.unserved}. The invocation completes run "
                                                f"{row['run_id']} from its labels without a "
                                                f"request, and an invocation that labels exits 1 "
                                                f"before its first request while OpenRouter's "
                                                f"lists show this.")
                    return 0
        if requests_unserved(args):
            return 1
        resumed = rid is not None
        if rid is None:
            rid = label_cache.add_run({"outcome": "running",
                                       "started_at": label_cache.iso(label_cache.utc_now()),
                                       **key}, cache_dir)
            start_partial(label_cache.partial_path(cache_dir, rid), covered)
        # The decisions of a run that a kill ended are only in its partial file, which a run
        # over another sampling frame or scope does not resume. Without them, the run would pay
        # the model again for each of those labels.
        decisions = label_cache.Decisions.load(cache_dir, model=args.model,
                                               parameters=parameters_sha,
                                               partial=partial_decisions, skip=rid)
        partial = label_cache.partial_path(cache_dir, rid)
        label_cache.end_partial(partial)
        for decision in partial_decisions(partial):
            decisions.add(decision, rid)
        # A response of the higher temperature or of the second model under other settings than
        # the run's is not restored, and the classifying model labels its repository again, as
        # for a decision that it reuses (`applies`).
        restorable = {**label_cache.Decisions.kept(
                          cache_dir, args.model, parameters_sha,
                          partial=lambda path: kept_responses(path, FINISHED), skip=rid),
                      **kept_responses(partial, FINISHED)}
        classifier.kept.update({key: response for key, response in restorable.items()
                                if applies(response, fallbacks)})
        run = Run(rid, repositories, decisions, github, classifier, args.model, parameters_sha,
                  partial, args.workers, thresholds, second=second_classifier,
                  second_model=second)
        covered_ids = {m["id"] for m in repositories}
        run.restored = {k: v for k, v in read_partial(partial).items() if k in covered_ids}
        # The repositories that GitHub failed in the earlier invocations of the run can be all
        # that a resumed run has left. If their failures counted toward the pause while GitHub
        # responds to other requests, they would pause every worker and stop the run at every
        # resume.
        run.github_pause.failed_before |= github_failures(partial)
        # As for the pause for GitHub, the repositories that the model failed before are all
        # that a resumed run can have left, and their responses count toward the pauses and the
        # stop for an endpoint that ignores the response format only while a streak of this
        # invocation is open (`FormatStreak`).
        classifier.failed_earlier |= model_failures(partial)
        # A run that follows a finished run under the same model, classification parameters,
        # retrieval and scope reuses each decision of that run that applies, as the run that
        # tries again the repositories that the finished run left without a decision does
        # (`complete_finished_run`). The model's requests in its main round can then go to
        # nothing but these repositories, and their failures count as failures in an earlier
        # round of the run. The run requests every README from GitHub in that round, and the
        # repositories that GitHub failed in the finished run count as failed earlier in the
        # run, so that they fail on their own while GitHub serves the README that it served last
        # (`GitHubPause`).
        model_failed, github_failed = failed_in_finished_run(cache_dir, key)
        classifier.failed_earlier |= model_failed
        run.github_pause.failed_before |= github_failed
        # The failed attempts of the earlier invocations, by GitHub id, from which a repository
        # continues at the higher temperature and the second model takes its record.
        classifier.history.update(failure_histories(partial))
        # The pause for GitHub tests whether GitHub responds by requesting the README that GitHub
        # served last (`readmes.Client.responds`). Until this invocation has such a README, the
        # README of an earlier decision stands in. An outage of the README requests that is under
        # way when the invocation starts then begins the pause too, instead of `/rate_limit`
        # deciding, because GitHub can respond to `/rate_limit` during such an outage. The README
        # of a repository that GitHub failed earlier in the run does not stand in, because GitHub
        # can fail that repository on its own.
        for repository in repositories:
            if repository["name"] in run.github_pause.failed_before:
                continue
            earlier = decisions.latest(repository["id"], args.model, parameters_sha)
            if earlier and github.served_earlier(repository["name"], earlier):
                break
        pending = len(repositories) - len(run.restored)
        logs.info(COMPONENT, f"Run {rid} {'resumed' if resumed else 'started'} over "
                             f"{len(repositories):,} repositories in retrieval {retrieval_id}, "
                             f"{len(run.restored):,} of them already decided and {pending:,} "
                             f"pending.")
        recorded = next(row["parameters"] for row in label_cache.read_index(cache_dir)
                        if row["run_id"] == rid)
        if recorded != parameters_sha:
            logs.info(COMPONENT, f"Run {rid} started under the classification parameters "
                                 f"{recorded[:12]}, which differ from the current parameters "
                                 f"{parameters_sha[:12]} only in the provider of a model. The "
                                 f"provider is no classification parameter, and the decisions "
                                 f"of the run apply.")
        tier = (f" on the service tier {service_tier}"
                if service_tier and not classifying.openrouter else "")
        summary_backup = backup_for(args, summarizing, SUMMARY_ROLE)
        logs.info(COMPONENT, f"The model {classifying.described_with(classifier.providers.backup)} "
                             f"labels at {described(settings, classifying)}{tier} with "
                             f"{args.workers:,} workers under the classification parameters "
                             f"{parameters_sha[:12]}, and "
                             f"{summarizing.described_with(summary_backup)} summarizes the "
                             f"linked pages.")
        if classifier.higher is not None:
            limit = classifier.higher.max_output_tokens
            logs.info(COMPONENT, f"After a failed response at a repository, the model labels the "
                                 f"repository at temperature {classifier.higher.temperature}"
                                 + (f" with an output limit of {limit:,} tokens"
                                    if limit is not None and classifying.openrouter else "")
                                 + ".")
        if second_classifier is not None:
            logs.info(COMPONENT, f"The second model "
                                 f"{second.described_with(second_classifier.providers.backup)} "
                                 f"labels at {described(second_classifier.settings, second)} the "
                                 f"repositories that the model leaves without a decision after "
                                 f"the retry rounds and a failed response.")
        if any(backup is not None for backup in (classifier.providers.backup, summary_backup,
                                                 second_classifier and
                                                 second_classifier.providers.backup)):
            logs.info(COMPONENT, f"While the pinned provider of a model fails, its backup "
                                 f"provider serves the same requests, and every "
                                 f"{logs.hms(endpoints.PINNED_CHECK_SECONDS)} one request tests "
                                 f"whether the pinned provider serves again.")
        logs.info(COMPONENT, f"Appending each decision to {partial}.")
        if key_credit is not None and pending:
            await log_credit(key_credit, cache_dir, args.model, parameters_sha, classifying,
                             summarizing, pending,
                             (classifier.providers.available, summary_backup))
        try:
            await run.run()
        finally:
            run.record_spent()
            # The tokens of this invocation by the tier that OpenAI billed or the provider that
            # served an OpenRouter model, which the cost record prices, the second model's under
            # that model. The partial file records them only without their tier and their model.
            label_cache.add_tokens(cache_dir, rid, classifier.take_billed())
            if second_classifier is not None:
                label_cache.add_tokens(cache_dir, rid, second_classifier.take_billed())
    counts = summary(run.lines.values())
    totals = run.spent()
    report(run, counts, totals)
    # Every invocation of the run, including a killed one, recorded what it spent in the partial
    # file.
    run_totals = label_cache.spent_in(partial, totals)
    made = partial_decisions(partial)
    # A run that did not stop finishes without the repositories left without a decision after the
    # retry rounds, up to its ceiling.
    ceiling = undecided_ceiling(len(repositories))
    if (run.stopped or len(run.failed) > ceiling
            or len(run.lines) + len(run.failed) < len(repositories)):
        # The decisions so far are written as well, so that a later run over another
        # sampling frame or scope can reuse them.
        written, checksums = write_run_decisions(cache_dir, rid, made)
        label_cache.put_index_row({"run_id": rid, "outcome": "incomplete", **counts,
                                   "repositories": len(repositories), **run_totals,
                                   "decisions": written, "decisions_sha256": checksums},
                                  cache_dir)
        costs.record_or_warn(cache_dir, rid, service_tier)
        over = "" if run.stopped else (f", more than the ceiling of {ceiling:,} for "
                                       f"{len(repositories):,} repositories")
        logs.error(COMPONENT, f"{len(repositories) - len(run.lines):,} of "
                              f"{len(repositories):,} repositories have no decision{over}. Saved "
                              f"the {len(run.lines):,} decisions in {partial.name}. The next run "
                              f"decides the rest.")
        return 1
    written, checksums = write_run_decisions(cache_dir, rid, made)
    labels_sha = label_cache.write_labels(cache_dir, rid,
                                          labels(rid, repositories, run.lines, run.failed))
    # The kept responses are written before the run is recorded as finished. After that, its
    # partial file is removed and no run reads its kept lines. The next run then reads only
    # GitHub's facts for a repository that GitHub failed through every retry round, instead of
    # paying the model again.
    label_cache.write_kept(cache_dir, rid, kept_responses(partial, FINISHED))
    label_cache.put_index_row({"run_id": rid, "outcome": "finished",
                               "finished_at": label_cache.iso(label_cache.utc_now()),
                               "repositories": len(repositories), **counts, **run_totals,
                               "decisions": written, "decisions_sha256": checksums,
                               "labels_sha256": labels_sha}, cache_dir)
    costs.record_or_warn(cache_dir, rid, service_tier)
    if output_dir is not None:
        write_run_outputs(output_dir, rid, repositories, run.lines, run.failed, parameters_sha,
                          retrieval_id, args.model)
    if run.failed:
        logs.warning(COMPONENT, f"{len(run.failed):,} of {len(repositories):,} repositories have "
                                f"no decision after the retry rounds, within the ceiling of "
                                f"{ceiling:,} for {len(repositories):,} repositories. Listed them "
                                f"with the outcome {UNDECIDED} and their errors in the run's "
                                f"labels, for the next run to try again.")
    partial.unlink(missing_ok=True)
    logs.info(COMPONENT, f"Run {rid} finished and wrote {written:,} new decisions to "
                         f"{len(checksums.split()):,} file(s), the first of them "
                         f"{label_cache.decisions_path(cache_dir, rid)}.")
    return 0


def write_run_decisions(cache_dir: Path, rid: str, decisions: list) -> tuple[int, str]:
    """Write the decisions of run `rid` and return what the index records of them. First the
    index stops naming the run's decisions, because a resumed run rewrites the files that an
    incomplete run wrote. A stop between the files and the index row would otherwise leave
    checksums that no file matches, and every later run would refuse to read them. Until the
    index row is written, the run's partial file contains the decisions."""
    label_cache.put_index_row({"run_id": rid, "decisions": "", "decisions_sha256": ""}, cache_dir)
    return label_cache.write_decisions(cache_dir, rid, decisions)


def report(run: Run, counts: dict, totals: dict) -> None:
    """The run's summary in the log: what it decided, what it spent, which repositories reached
    a limit and which it left without a decision. The failures of GitHub for a GraphQL response
    with errors in place of a field are counted separately."""
    new = [line["decision"] for line in run.lines.values() if line["status"] == DECIDED]
    carried = sum(line["status"] == CARRIED for line in run.lines.values())
    logs.info(COMPONENT, f"Labeled {counts['labeled']:,} repositories, excluded "
                         f"{counts['excluded']:,} and found {counts['gone']:,} gone. Reused "
                         f"{counts['reused']:,} decisions, {carried:,} of them carried over, and "
                         f"made {len(new):,} new ones.")
    logs.info(COMPONENT, f"Spent {totals['github_requests']:,} GitHub requests and "
                         f"{totals['model_requests']:,} model requests with "
                         f"{totals['input_tokens']:,} input tokens, {totals['cached_tokens']:,} "
                         f"of them cached, and {totals['output_tokens']:,} output tokens. The "
                         f"page summaries took {totals['summary_input_tokens']:,} input and "
                         f"{totals['summary_output_tokens']:,} output tokens.")
    for limit, what in (("linked_pages", f"the limit of {LINKED_PAGES} linked pages"),
                        ("tool_rounds", f"the limit of {MAX_TOOL_ROUNDS} rounds of tool calls")):
        reached = [d["name"] for d in new if limit in d.get("limits_reached", ())]
        if reached:
            logs.info(COMPONENT, f"{len(reached):,} repositories reached {what}, the first of "
                                 f"them {reached[0]}. Their decisions list the limit under "
                                 f"limits_reached.")
    for key, who in (("higher_temperature", "The classifying model at its higher temperature"),
                     ("second_model", "The second model")):
        labeled = [d["name"] for d in new if d.get(key)]
        if labeled:
            noun = "repository" if len(labeled) == 1 else "repositories"
            logs.info(COMPONENT, f"{who} labeled {len(labeled):,} {noun} after a failed response "
                                 f"of the classifying model, the first of them {labeled[0]}. "
                                 f"Their decisions record the settings, the reason and the failed "
                                 f"attempts of the classifying model under {key}.")
    served = [d["name"] for d in new if backup_served(d) is not None]
    if served:
        noun = "repository" if len(served) == 1 else "repositories"
        logs.info(COMPONENT, f"A backup provider served the requests of {len(served):,} {noun} "
                             f"wholly or in part, the first of them {served[0]}. Their decisions "
                             f"name the provider of each response under served_by and of each "
                             f"summary under summary_provider.")
    if run.failed:
        reasons = [line["reason"] for line in run.failed.values()]
        first = next(iter(run.failed.values()))
        unexpected = len(reasons) - reasons.count(GITHUB_FAILED) - reasons.count(MODEL_FAILED)
        # GraphQL's errors in place of a field are counted separately, so that a recurring cause,
        # such as a field that GitHub fails to resolve for many repositories, is visible.
        fields = sum(line["reason"] == GITHUB_FAILED
                     and tools.field_errors(str(line.get("error") or ""))
                     for line in run.failed.values())
        among = (f" ({fields:,} of them a GraphQL response with errors in place of a field)"
                 if reasons.count(GITHUB_FAILED) else "")
        logs.info(COMPONENT, f"{len(run.failed):,} repositories have no decision, "
                             f"{reasons.count(GITHUB_FAILED):,} for GitHub's response{among}, "
                             f"{reasons.count(MODEL_FAILED):,} for the model's response and "
                             f"{unexpected:,} for an unexpected error. The first is "
                             f"{first['name']}. The next invocation tries them again in a new "
                             f"run while no collection over the sample has begun.")


def build_parser() -> argparse.ArgumentParser:
    """The command line, with the defaults of a run of the pipeline."""
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(description="Label the repositories in the sampling frame.",
                                     allow_abbrev=False)
    parser.add_argument("--sampling-frame", type=Path, default=SAMPLING_FRAME,
                        help="The sampling frame (default: %(default)s)")
    parser.add_argument("--cache-dir", type=Path, default=label_cache.CACHE_DIR,
                        help="The cache of labels (default: %(default)s)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="The model that labels the repositories: an OpenAI model, or an "
                             "OpenRouter model, whose id names its author, such as "
                             "deepseek/deepseek-v4.1-flash (default: %(default)s, on the "
                             f"endpoint {endpoints.PRODUCTION.provider} unless --provider, "
                             "--quantization and --hugging-face-id name another)")
    parser.add_argument("--provider", default="",
                        help="The provider slug of the endpoint of an OpenRouter model, as "
                             "OpenRouter's endpoint list gives it in tag, such as morph/fp8")
    parser.add_argument("--quantization", default="",
                        help="The quantization of that endpoint, such as fp8")
    parser.add_argument("--hugging-face-id", default="",
                        help="The Hugging Face id of the weights of an OpenRouter model, such as "
                             "deepseek-ai/DeepSeek-V4.1-Flash")
    parser.add_argument("--reasoning-effort", default=None,
                        help=f"The model's reasoning effort (default: that of the model's "
                             f"settings, {endpoints.REASONING_EFFORT} for "
                             f"{endpoints.PRODUCTION.model} and {endpoints.SECOND_MODEL.model}, "
                             f"{endpoints.OPENAI_REASONING_EFFORT} for an OpenAI model)")
    parser.add_argument("--temperature", type=float, default=None,
                        help=f"The temperature of an OpenRouter model's requests (default: that "
                             f"of the model's settings, {endpoints.TEMPERATURE} for "
                             f"{endpoints.PRODUCTION.model}, none for a model without settings "
                             f"of its own). An OpenAI model takes none")
    parser.add_argument("--top-p", type=float, default=None,
                        help=f"The top_p of an OpenRouter model's requests (default: that of the "
                             f"model's settings, {endpoints.TOP_P} for "
                             f"{endpoints.PRODUCTION.model}, none for a model without settings "
                             f"of its own). An OpenAI model takes none")
    parser.add_argument("--seed", type=int, default=None,
                        help=f"The seed of an OpenRouter model's requests (default: that of the "
                             f"model's settings, {endpoints.SEED}). An OpenAI model takes none")
    parser.add_argument("--endpoint-default-temperature", action="store_true",
                        help="Send an OpenRouter model's requests neither a temperature, a top_p "
                             "nor a seed, so that its endpoint applies its own defaults, also "
                             "after a failed response. It takes neither --temperature, --top-p "
                             "nor --seed")
    parser.add_argument("--no-second-model", action="store_true",
                        help="Leave a repository without a decision that the model leaves "
                             "without one after the retry rounds and a failed response, instead "
                             f"of labeling it with the second model, "
                             f"{endpoints.SECOND_MODEL.model}, as a check of one model needs. A "
                             "run of the second model never labels with it again")
    parser.add_argument("--no-backup-provider", action="store_true",
                        help="Send every request of each model to its pinned provider, also "
                             "while the pinned provider fails, instead of sending the requests "
                             "that would use an attempt to the backup provider of the model in "
                             "endpoints.py, such as "
                             f"{endpoints.BACKUPS[endpoints.PRODUCTION.model].provider} for "
                             f"{endpoints.PRODUCTION.model}")
    parser.add_argument("--new-run", action="store_true",
                        help="Label the sampling frame in a new run, which requests every README "
                             "again, even when the latest finished run over it has the same "
                             "model, classification parameters and retrieval and the current "
                             "rules derive each of its decisions unchanged")
    parser.add_argument("--repository-list", type=Path, default=None,
                        help="A CSV with an id column that names the repositories to label")
    parser.add_argument("--limit", type=int, default=None,
                        help="Label only the first N repositories in the sampling frame")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help="Repositories decided at once (default: %(default)s)")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="The directory of repos_annotated.csv and repos_excluded.csv "
                             "(default: the stage's data directory for a run over the whole "
                             "sampling frame, none for a run over part of it)")
    parser.add_argument("--summary-model", default=tools.SUMMARY_MODEL,
                        help="The model that summarizes linked pages (default: %(default)s, on "
                             f"the endpoint {endpoints.PRODUCTION.provider} unless "
                             "--summary-provider, --summary-quantization and "
                             "--summary-hugging-face-id name another)")
    parser.add_argument("--summary-effort", default=tools.SUMMARY_EFFORT,
                        help="The reasoning effort of the summary model (default: %(default)s)")
    parser.add_argument("--summary-provider", default="",
                        help="The provider slug of the endpoint of an OpenRouter summary model")
    parser.add_argument("--summary-quantization", default="",
                        help="The quantization of that endpoint")
    parser.add_argument("--summary-hugging-face-id", default="",
                        help="The Hugging Face id of the weights of an OpenRouter summary model")
    parser.add_argument("--openai-service-tier", dest="service_tier", choices=SERVICE_TIERS,
                        default="flex",
                        help="The OpenAI service tier of the labels and of their repeats in the "
                             "comparison with the previous run: flex for half the standard price "
                             "and slower responses, default for the standard tier, auto for the "
                             "project's setting. The option does not apply to an OpenRouter model "
                             "(default: %(default)s)")
    parser.add_argument("--credentials", type=Path, default=credentials.default_path(),
                        help="The credentials file, read after the environment (default: "
                             "%(default)s)")
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The OpenAI client and the MCP client log through `logging`. Their warnings go into the
    # step log in its format, and their requests at INFO do not.
    logs.capture_libraries()
    repositories, retrieval_id, thresholds = read_sampling_frame(args.sampling_frame)
    if args.repository_list:
        wanted = set(read_repository_ids(args.repository_list))
        repositories = [m for m in repositories if m["id"] in wanted]
        missing = len(wanted) - len(repositories)
        if missing:
            logs.warning(COMPONENT, f"{missing:,} of the ids in {args.repository_list} are not "
                                    f"repositories in the sampling frame.")
    if args.limit is not None:
        repositories = repositories[:args.limit]
    configured = credentials.github_tokens(args.credentials, os.environ)
    if not configured:
        logs.error(COMPONENT, f"GITHUB_TOKENS, GITHUB_TOKEN and {args.credentials} hold no "
                              f"GitHub access token.")
        return 1
    try:
        classifying, summary = classifying_endpoint(args), summary_endpoint(args)
        settings = run_settings(args)
        second = second_endpoint(args, classifying)
        client = endpoints.client(classifying, args.credentials,
                                  timeout=FLEX_TIMEOUT if args.service_tier == "flex"
                                  and not classifying.openrouter else REQUEST_TIMEOUT,
                                  max_retries=SDK_RETRIES)
        # The summaries go through the MCP server, which reads the key itself. A missing key of
        # the summary model's service stops the run here rather than at the first linked page.
        endpoints.api_key(summary.service, args.credentials)
    except (ValueError, endpoints.NoKey) as error:
        logs.error(COMPONENT, f"{error}.")
        return 1
    unserved, unlabeled, args.started_on_backups = endpoint_check(
        classifying, settings, summary, args.summary_effort, second,
        backups=not args.no_backup_provider)
    # The refusal stops the invocation before its first request rather than here, because an
    # invocation that completes a finished run from its labels sends none (`requests_unserved`).
    # Stopped here, such an invocation would stop the pipeline before the collection while a
    # provider is missing from OpenRouter's lists.
    args.unserved = unserved or ""
    second_client = client
    if second is not None and unlabeled is None and second.service != classifying.service:
        # The second model of an OpenAI model's run is OpenRouter's, with a client of its own.
        try:
            second_client = endpoints.client(second, args.credentials, timeout=REQUEST_TIMEOUT,
                                             max_retries=SDK_RETRIES)
        except endpoints.NoKey as error:
            unlabeled = str(error)
    if second is not None and unlabeled is not None:
        # The second model is no classification parameter, and the invocation labels under the
        # same parameters without it: the repositories that it would label stay without a
        # decision. Its earlier labels and kept responses still apply, because the run is
        # configured with it, unlike a run with --no-second-model (`classify`).
        logs.warning(COMPONENT, f"The second model {second.described()} labels nothing in this "
                                f"invocation. {unlabeled}. The run reuses its earlier labels, and "
                                f"the repositories that the model leaves without a decision after "
                                f"the retry rounds and a failed response stay without one, for "
                                f"the next run. The comparison with the previous run repeats "
                                f"none of the labels of the second model.")
        args.second_model_unavailable, second = unlabeled, None
    if not args.no_backup_provider:
        args.unserved_backups = backup_check(classifying, settings, summary, second)
    services = {classifying.service, summary.service, *([second.service] if second else [])}
    try:
        key_credit = (endpoints.KeyCredit(endpoints.api_key(endpoints.OPENROUTER,
                                                            args.credentials))
                      if endpoints.OPENROUTER in services else None)
    except endpoints.NoKey as error:
        logs.error(COMPONENT, f"{error}.")
        return 1
    # GitHub can reject valid GitHub access tokens for a while. When it rejects every one at the
    # start, the run begins with an empty pool, and its first request checks them again after
    # each wait, as a request does when GitHub rejects every one later in the run
    # (`readmes.Client`).
    tokens = readmes.accepted_tokens(configured)
    pool = TokenPool(tokens)
    if tokens:
        logs.info(COMPONENT, f"Labeling with {len(tokens):,} GitHub access token(s) of "
                             f"{pool.accounts():,} account(s).")
    else:
        logs.warning(COMPONENT, "GitHub rejects every configured GitHub access token (HTTP 401). "
                                "The first request to GitHub checks them again after each wait.")
    github = readmes.Client(pool, configured=configured)

    def label() -> int:
        """The exit code of the labeling of the sampling frame (`classify`)."""
        try:
            return asyncio.run(classify(args, repositories, retrieval_id, thresholds, github,
                                        client, key_credit=key_credit,
                                        second_client=second_client))
        except CacheError as error:
            logs.error(COMPONENT, f"The cache of labels cannot be used, because {error}.")
            return 1

    def compare() -> int:
        """The exit code of the comparison with the previous runs (`compare_with_previous_runs`)."""
        return compare_with_previous_runs(
            Path(args.cache_dir), Path(args.output_dir or CLASSIFICATION_DIR), args.workers,
            args.credentials, service_tier=args.service_tier,
            second_model_unavailable=getattr(args, "second_model_unavailable", ""),
            no_backup_provider=args.no_backup_provider,
            unserved_backups=getattr(args, "unserved_backups", {}), unserved=args.unserved)
    args.try_undecided_again = 0
    code = label()
    if code or scope_of(args) != label_cache.SAMPLING_FRAME_SCOPE:
        return code
    code = compare()
    if code or not args.try_undecided_again:
        return code
    # The finished run that the labeling completed left repositories without a decision, and no
    # collection over its sample has begun (`complete_finished_run`). The runner records the
    # labeling once the invocation exits 0, and the selection and the collection follow without
    # another invocation of the labeling. The new run that tries them again therefore follows
    # here, once the comparison of the completed run is complete.
    if args.unserved:
        # The new run would exit 1 before its first request, here and at every later Start
        # until OpenRouter's lists change, and the collection would not follow. The invocation
        # therefore skips it. A labeling that has repositories to label for the first time still
        # exits 1 (`requests_unserved`).
        count = args.try_undecided_again
        logs.warning(COMPONENT, f"{args.unserved}. The invocation does not try again the "
                                f"{count:,} {'repository' if count == 1 else 'repositories'} "
                                f"that the completed run left without a decision, so that the "
                                f"selection and the collection follow. The next new run of the "
                                f"labeling, such as the run over the next monthly retrieval, "
                                f"tries {'it' if count == 1 else 'them'} again.")
        return 0
    args.try_undecided_again = 0
    return label() or compare()


def compare_with_previous_runs(cache_dir: Path, output_dir: Path, workers: int,
                               credentials_path: Path, compare=None,
                               service_tier: str | None = None,
                               second_model_unavailable: str = "",
                               no_backup_provider: bool = False,
                               unserved_backups: dict | None = None,
                               unserved: str = "") -> int:
    """The labels that changed since the previous run over a sampling frame, and why
    (changed_labels.py), for the latest finished run over a sampling frame and for every earlier
    run with a comparison that an earlier invocation started and left unfinished, oldest first.
    A run that was never compared is left alone, because the comparison labels again with the
    classifier of the current code, which differs from an older run's classifier once the code
    has changed. The invocation writes its comparisons to label_changes.csv in `output_dir`,
    oldest first, with each row naming its previous run. The changes of an earlier run with a
    comparison that this invocation finished are then shown beside the changes of the latest run.
    Each comparison follows the comparisons of the earlier runs over the same retrieval that lead
    to its run (`earlier_comparisons`), such as the comparison of a run that left repositories
    without a decision before that of the run that tries them again. The file is empty when no
    earlier run precedes the latest run. The repeated labels go to the run's `service_tier`, so
    that they cost what the labels cost. `second_model_unavailable` is why the second model
    labels nothing in the invocation, or "", and the comparison then repeats none of its labels
    (`changed_labels.compare`). The repeats go to the backup provider of a model as the labels
    do, unless `no_backup_provider` is set or `unserved_backups` names the role of the model
    (`backup_for`). `unserved` is why OpenRouter serves no request of the run's models as they
    are (`endpoint_check`), or "", and a comparison then exits 1 before its first repeat
    (`requests_unserved`)."""
    import changed_labels
    compare = compare or changed_labels.compare
    runs = [row for row in label_cache.read_index(cache_dir)
            if row["outcome"] == "finished" and row["scope"] == label_cache.SAMPLING_FRAME_SCOPE
            and row["labels_sha256"]]
    made = []
    for row in runs:
        last = row is runs[-1]
        started = label_cache.changes_partial_path(cache_dir, row["run_id"]).is_file()
        if row["compared_with"] or not (last or (started and label_cache.previous_run(
                cache_dir, row["run_id"]))):
            continue
        # Run ids are UTC times of a fixed format, so that their order is the order of the runs.
        earlier = sorted({*made, *earlier_comparisons(cache_dir, row["run_id"])})
        code = compare(SimpleNamespace(run=row["run_id"], previous=None, cache_dir=cache_dir,
                                       output_dir=output_dir, workers=workers,
                                       credentials=credentials_path, earlier_runs=earlier,
                                       service_tier=service_tier,
                                       second_model_unavailable=second_model_unavailable,
                                       no_backup_provider=no_backup_provider,
                                       unserved_backups=dict(unserved_backups or {}),
                                       unserved=unserved))
        if code:
            return code
        if label_cache.previous_run(cache_dir, row["run_id"]):
            made.append(row["run_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
