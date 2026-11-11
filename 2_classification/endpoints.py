#!/usr/bin/env python3
"""How the classification calls a model: through OpenAI's Responses API, or through OpenRouter's
OpenAI-compatible Responses API at one pinned endpoint, and how it reads the refusals and the
credit of either service.

A model with an id that names its author, as OpenRouter's ids do (`deepseek/deepseek-v4.1-flash`),
is an OpenRouter model, and any other model, such as `gpt-6-luna`, is an OpenAI model. OpenAI's
ids name no author. An OpenRouter model is called on one endpoint, which the provider slug of the
endpoint and its quantization name, as OpenRouter's endpoint list gives them in `tag` and
`quantization`, such as `morph/fp8` and `fp8`. The request routes to that endpoint alone, without
fallbacks, and only when the endpoint supports every parameter of the request
(`Endpoint.routing`). The Hugging Face id names the weights that the model id stands for. The
model id, the quantization and the Hugging Face id are classification parameters
(`Endpoint.parameters`), so that a run of other weights or in another quantization labels again.
The provider is none: The maintainer decided on 2026-11-04 that "the model, quantization,
configuration should be part of, but not the [...] provider", and the endpoint on which a model
is called (`with_provider`) changes no label that a run reuses.

Each pinned model has a backup provider for the same weights in the same quantization
(`BACKUPS`, `backup_of`), which serves its requests with the same settings while its pinned
provider fails. `Providers` decides which of the two serves the next request of a model: the
pinned provider while it serves, else the backup provider, with one request every
PINNED_CHECK_SECONDS that tests whether the pinned provider serves again.

Both services refuse a request for spent credit (`credit_refusal`). OpenRouter refuses it with
HTTP 402, or with HTTP 403 for a key or a budget at its limit, and OpenAI with
`insufficient_quota` or `billing_hard_limit_reached`. A refused request costs nothing. A top-up
ends such a refusal, and the classification waits for it. A rejected key (HTTP 401) or a key
without access (HTTP 403) stays refused, and `classification_tools.refusal` stops the run on it.
`KeyCredit` reads the credit that an OpenRouter API key has left, `unserved` reads from
OpenRouter's endpoint list whether the pinned endpoint serves the requests of a model, and
`unsupported_effort` reads from OpenRouter's model list whether the model takes the reasoning
effort of its requests. None of them spends anything.

The two pinned models, the classifying model and the second model, carry the settings of their
requests (`Settings`): the reasoning effort, the output limit, the temperature, the top_p and the
seed, and for the classifying model the higher temperature of its attempts at a repository after
a failed response. The settings of a pinned model apply to its weights on any endpoint, and any
other model has settings that name no other model's temperature or top_p (`settings_of`).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentcontext import credentials, logs  # noqa: E402

# The component of the log lines of the classification, which `Providers` writes for the
# classification and for the server of its tools.
COMPONENT = "repository_labeling"
OPENAI, OPENROUTER = credentials.OPENAI, credentials.OPENROUTER
SERVICE_NAMES = {OPENAI: "OpenAI", OPENROUTER: "OpenRouter"}
# The base URL of OpenRouter's API: "https://openrouter.ai/api/v1/responses"
# (openrouter.ai/docs/api/reference/responses/overview), GET /api/v1/key for the credit of a key
# (openrouter.ai/docs/api-reference/limits), and GET /api/v1/models/{author}/{slug}/endpoints
# for the endpoints of a model (openrouter.ai/docs/api/api-reference/endpoints/list-endpoints).
OPENROUTER_API = "https://openrouter.ai/api/v1"
# The quantizations that OpenRouter's provider routing filters by: "int4, int8, fp4, mxfp4,
# nvfp4, fp6, fp8, mxfp8, fp16, bf16, fp32, and unknown" (openrouter.ai/docs/features/
# provider-routing). `unknown` is the quantization of an endpoint that states none, which its
# provider slug still pins.
QUANTIZATIONS = ("int4", "int8", "fp4", "mxfp4", "nvfp4", "fp6", "fp8", "mxfp8", "fp16", "bf16",
                 "fp32", "unknown")
# What a person does about spent credit, by service, for the log line of the pause. OpenRouter's
# errors page: "Your account or API key has insufficient credits. Add more credits and retry the
# request" (openrouter.ai/docs/api-reference/errors), and for `openrouter_key_limit` "raise the
# key's credit limit or wait for it to reset" (openrouter.ai/docs/api-reference/limits).
TOP_UP = {OPENROUTER: "add credits at https://openrouter.ai/settings/credits, or raise the "
                      "credit limit of the OpenRouter API key",
          OPENAI: "raise the quota or the billing limit of the OpenAI project"}
# The codes with which OpenAI refuses every request of a project that has spent its quota or
# reached its billing limit, with HTTP 429 or 400.
QUOTA_CODES = ("insufficient_quota", "billing_hard_limit_reached")
# OpenRouter's `limit_source` of a 402 that a wait ends without a top-up: "wait for the
# `Retry-After` header and retry" (openrouter.ai/docs/api-reference/limits). The requests in
# flight have filled the budget for requests in flight, not the balance.
IN_FLIGHT = "openrouter_in_flight_budget"
# OpenRouter's `limit_source` of a 403 for a budget or a key limit that is spent: "A spent
# guardrail budget, workspace budget, or key limit produces a 403 before it reaches a provider"
# (openrouter.ai/docs/api-reference/errors).
BUDGET_SOURCES = ("openrouter_guardrail_budget", "openrouter_workspace_budget",
                  "openrouter_key_limit")
# The names that the endpoint list gives in `supported_parameters` to the parameters of the
# requests: every request of a model that labels sends `reasoning` and `tools`, among them the
# tool of the checklist, and those of its settings that are set (`request_parameters`), and the
# summary model's requests send `reasoning`. With `require_parameters`, OpenRouter routes a
# request only to an endpoint that supports each of its parameters (`Endpoint.routing`).
CLASSIFYING_PARAMETERS = ("reasoning", "tools")
SUMMARY_PARAMETERS = ("reasoning",)
# The names in `supported_parameters` of the settings that a request sends, by the field of
# `Settings`. The endpoint list names the output limit `max_tokens`, which a request of the
# Responses API sends as `max_output_tokens`: "On Chat Completions (`/api/v1/chat/completions`),
# the same budget is `max_tokens`" (openrouter.ai/docs/guides/best-practices/reasoning-tokens).
SETTING_PARAMETERS = {"temperature": "temperature", "top_p": "top_p", "seed": "seed",
                      "max_output_tokens": "max_tokens"}
# The seconds that a GET of the credit of a key or of an endpoint list waits for OpenRouter.
GET_TIMEOUT = 30
# The seconds after which a model whose requests the backup provider serves sends one request to
# the pinned provider again, which tests whether the pinned provider serves again (`Providers`).
# GROUNDING: a policy. The pinned provider is the endpoint whose responses the pilots checked and
# whose prices the cost estimate assumes, and the backup provider serves only while the pinned
# provider fails. A test costs at most one request that the pinned provider fails, because it goes
# without the SDK's retries (TEST_RETRIES), after which the same request goes to the backup
# provider, and ten minutes bound the time that the backup serves after the pinned provider has
# recovered.
PINNED_CHECK_SECONDS = 600
# The retries of the SDK for a request that tests the pinned provider: none, so that a test that
# fails sends one request to the pinned provider and holds its worker for that request alone.
TEST_RETRIES = 0


class NoKey(Exception):
    """No API key of the service that a model needs."""


@dataclass(frozen=True)
class Settings:
    """The settings of the requests of a model that labels repositories: its reasoning effort,
    and for an OpenRouter model the output limit, the temperature, the top_p and the seed of its
    requests. A setting that is None is not sent, and the endpoint applies its own default:
    "When a sampling parameter is absent from your request, OpenRouter omits it upstream rather
    than substituting a hardcoded value, so the provider applies its own default"
    (openrouter.ai/docs/api-reference/parameters). `higher_temperature` is the temperature of the
    model's attempts at a repository after a failed response there
    (`1_classify_repos.Classifier.label`), or None for a model without such attempts, and
    `higher_max_output_tokens` the output limit of those attempts, or None for the limit of the
    other requests."""

    reasoning_effort: str
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    higher_temperature: float | None = None
    higher_max_output_tokens: int | None = None

    def sampled(self) -> dict:
        """The temperature, the top_p and the seed that the requests send, those that are
        set."""
        return {name: value for name, value in (("temperature", self.temperature),
                                                ("top_p", self.top_p), ("seed", self.seed))
                if value is not None}

    def recorded(self) -> dict:
        """The temperature, the top_p, the seed and the output limit that the requests send,
        those that are set, as a label of the higher temperature records them."""
        return {**self.sampled(), **({"max_output_tokens": self.max_output_tokens}
                                     if self.max_output_tokens is not None else {})}

    def largest_output_tokens(self) -> int | None:
        """The larger of the output limit and the output limit of the attempts after a failed
        response, those that are set, which the endpoint has to generate."""
        limits = [limit for limit in (self.max_output_tokens, self.higher_max_output_tokens)
                  if limit is not None]
        return max(limits) if limits else None

    def parameters(self) -> dict:
        """What the classification parameters record of the settings: the reasoning effort,
        the temperature, the top_p, the seed and the output limit, those that are set. The
        higher temperature decides only the labels of the attempts after a failed response, and
        each such label records it (`1_classify_repos.Classifier.label`)."""
        return {"reasoning_effort": self.reasoning_effort, **self.sampled(),
                **({"max_output_tokens": self.max_output_tokens}
                   if self.max_output_tokens is not None else {})}

    def higher(self) -> Settings | None:
        """The settings of the attempts at a repository after a failed response: these settings
        at the higher temperature and with the output limit of those attempts, or None when the
        model has no higher temperature or its temperature is not below it."""
        if (self.higher_temperature is None or self.temperature is None
                or self.temperature >= self.higher_temperature):
            return None
        limit = (self.max_output_tokens if self.higher_max_output_tokens is None
                 else self.higher_max_output_tokens)
        return replace(self, temperature=self.higher_temperature, max_output_tokens=limit,
                       higher_temperature=None, higher_max_output_tokens=None)

    @classmethod
    def of(cls, recorded: dict) -> Settings:
        """The settings that a block of recorded classification parameters names
        (`parameters`), without a higher temperature."""
        return cls(str(recorded.get("reasoning_effort") or ""),
                   recorded.get("max_output_tokens"), recorded.get("temperature"),
                   recorded.get("top_p"), recorded.get("seed"))


def request_parameters(settings: Settings) -> tuple:
    """The names in `supported_parameters` of the parameters of a request of a model that
    labels with `settings`: CLASSIFYING_PARAMETERS and the settings that are set
    (SETTING_PARAMETERS)."""
    return (*CLASSIFYING_PARAMETERS,
            *(name for setting, name in SETTING_PARAMETERS.items()
              if getattr(settings, setting) is not None))


@dataclass(frozen=True)
class Endpoint:
    """A model and where the classification calls it. An OpenAI model has no provider,
    quantization or Hugging Face id. `settings` are the settings of the requests of a pinned
    model (PINNED), which `settings_of` gives every endpoint of the same weights. Two endpoints
    are equal when they name the same model at the same place, whatever their settings."""

    model: str
    provider: str = ""
    quantization: str = ""
    hugging_face_id: str = ""
    settings: Settings | None = field(default=None, compare=False)

    @property
    def service(self) -> str:
        """OPENROUTER for a model id that names its author, OPENAI otherwise."""
        return OPENROUTER if "/" in self.model else OPENAI

    @property
    def openrouter(self) -> bool:
        return self.service == OPENROUTER

    def parameters(self) -> dict:
        """What the classification parameters record of the endpoint: the model, and for an
        OpenRouter model its Hugging Face id and its quantization. The provider serves the
        weights and is no classification parameter (the maintainer's decision of 2026-11-04)."""
        if not self.openrouter:
            return {"model": self.model}
        return {"model": self.model, "hugging_face_id": self.hugging_face_id,
                "quantization": self.quantization}

    @classmethod
    def of(cls, recorded: dict) -> Endpoint:
        """The endpoint that a block of recorded classification parameters names, with the
        provider of a block recorded before 2026-11-04 and without one for a later block
        (`with_provider`)."""
        return cls(str(recorded.get("model") or ""), str(recorded.get("provider") or ""),
                   str(recorded.get("quantization") or ""),
                   str(recorded.get("hugging_face_id") or ""))

    def routing(self) -> dict:
        """The body that a request to the endpoint adds, OpenRouter's provider routing: "List of
        provider slugs to try in order", "Whether to allow backup providers when the primary is
        unavailable", "Only use providers that support all parameters in your request" and
        "List of quantization levels to filter by" (openrouter.ai/docs/features/
        provider-routing). An OpenAI model adds nothing."""
        if not self.openrouter:
            return {}
        return {"provider": {"order": [self.provider], "allow_fallbacks": False,
                             "quantizations": [self.quantization],
                             "require_parameters": True}}

    def described(self) -> str:
        """The endpoint as a log line names it."""
        if not self.openrouter:
            return f"{self.model} through OpenAI"
        return (f"{self.model} ({self.hugging_face_id}) on the endpoint {self.provider} in "
                f"{self.quantization} through OpenRouter")

    def described_with(self, backup: Endpoint | None) -> str:
        """The endpoint as the log line at the start of a run names it, with its backup
        provider when it has one (`backup_of`)."""
        if backup is None:
            return self.described()
        return (f"{self.described()}, with the backup provider {backup.provider} in "
                f"{backup.quantization}")


# The settings of the requests of the two pinned models, which the maintainer set on 2026-11-02.
#
# The reasoning effort of both pinned models, which the maintainer chose. OpenRouter's model list
# gives DeepSeek V4.1 Flash the efforts "max", "high", "low" and "none" with the default "high",
# and GLM-5.3 "max", "high" and "low" with the default "max" (openrouter.ai/api/v1/models, read
# 2026-11-02). Both model cards report their evaluations at the effort max. Both models list high,
# so the classification parameters name an effort that each model takes.
REASONING_EFFORT = "high"
# The output limits of the requests, reasoning included: "For reasoning models, this limit covers
# reasoning tokens and visible output together on most providers"
# (openrouter.ai/docs/api-reference/parameters). A request without a limit gets the endpoint's
# own, which no page of OpenRouter or of the provider states.
#
# MAX_OUTPUT_TOKENS is the limit of the requests of the classifying model at TEMPERATURE. The
# maintainer chose it on 2026-11-02 so that a response that reasons until its limit ends sooner
# and its repository gets the attempts at the higher temperature. It lies above the output of
# every decision without a failed response in the checks of 2026-11-02.
#
# HIGHER_MAX_OUTPUT_TOKENS is the limit of the attempts at HIGHER_TEMPERATURE, of the second
# model and of a model without settings of its own. It is the least value that DeepSeek's model
# card recommends, "| `max_tokens` | ≥ 256K |" (huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash,
# section "Minimal Inference", read 2026-11-02), with K counted as 1,024, as DeepSeek's API
# reference counts it: "The value must be between 1 and 384K (393216)"
# (api-docs.deepseek.com/api/create-chat-completion, read 2026-11-02). It covers the "maximum
# generation length of `163,840` tokens" of the evaluations of GLM-5.3
# (huggingface.co/zai-org/GLM-5.3, read 2026-11-02).
MAX_OUTPUT_TOKENS = 65_536
HIGHER_MAX_OUTPUT_TOKENS = 262_144
# The seed of the requests of both pinned models, which OpenRouter passes to the endpoint.
SEED = 7
# The temperature, the higher temperature and the top_p of DeepSeek V4.1 Flash. Its model card
# recommends "| `temperature` | 1.0 |" and "| `top_p` | 0.95 or 1.0 |", and the example of its
# inference README runs with "--temperature 0.6"
# (huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/inference/README.md). The model card
# of DeepSeek-R1 says "Set the temperature within the range of 0.5-0.7 (0.6 is recommended) to
# prevent endless repetitions or incoherent outputs" (huggingface.co/deepseek-ai/DeepSeek-R1).
# The maintainer chose TEMPERATURE for the requests, and HIGHER_TEMPERATURE, the temperature of
# the model card, for the attempts at a repository after a failed response, before the second
# model labels the repository. Both send TOP_P, one of the two values of the model card.
TEMPERATURE = 0.6
HIGHER_TEMPERATURE = 1.0
TOP_P = 0.95
# The temperature and the top_p of GLM-5.3: Z.ai's generation_config.json sets "temperature":
# 1.0 and "top_p": 0.95 (huggingface.co/zai-org/GLM-5.3/blob/main/generation_config.json), and
# OpenRouter's model list gives z-ai/glm-5.3 the default_parameters {"temperature": 1,
# "top_p": 0.95} (openrouter.ai/api/v1/models, read 2026-11-02).
SECOND_MODEL_TEMPERATURE = 1.0
SECOND_MODEL_TOP_P = 0.95
# The reasoning effort of an OpenAI model, such as a reference model of the pilots of the
# classification. On the reviewed disagreements with a definite label, gpt-6-luna at medium had
# agreed with the reviewer more often than at low (the check of 2026-10-09).
OPENAI_REASONING_EFFORT = "medium"
# The settings of an OpenRouter model without settings of its own, such as a candidate model of
# a check: the reasoning effort, the output limit of the second model and the seed of the pinned
# models, and neither
# a temperature nor a top_p, so that its endpoint applies the defaults of its provider rather
# than the values chosen for another model.
OTHER_MODEL = Settings(REASONING_EFFORT, HIGHER_MAX_OUTPUT_TOKENS, seed=SEED)
# The model with which the classification labels repositories and summarizes linked pages by
# default, on its pinned endpoint. The maintainer chose it on 2026-10-31 from the pilots of the
# classification.
PRODUCTION = Endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8",
                      "deepseek-ai/DeepSeek-V4.1-Flash",
                      Settings(REASONING_EFFORT, MAX_OUTPUT_TOKENS, TEMPERATURE, TOP_P, SEED,
                               HIGHER_TEMPERATURE, HIGHER_MAX_OUTPUT_TOKENS))
# The second model, on its pinned endpoint. It labels the evaluation sample of a label
# evaluation, and in a run of another model it labels a repository that the classifying model
# left without a decision after a failed response (`1_classify_repos.SECOND_MODEL_REASONS`).
# The maintainer chose it on 2026-10-31 from the candidate models that labeled the repositories
# of evaluation 2026-10-31T094615Z.
SECOND_MODEL = Endpoint("z-ai/glm-5.3", "morph/fp8", "fp8", "zai-org/GLM-5.3",
                        Settings(REASONING_EFFORT, HIGHER_MAX_OUTPUT_TOKENS,
                                 SECOND_MODEL_TEMPERATURE, SECOND_MODEL_TOP_P, SEED))
PINNED = {found.model: found for found in (PRODUCTION, SECOND_MODEL)}
# The backup provider of each pinned model: the endpoint of the same weights in the same
# quantization that serves the model's requests, with the same settings, tools and response
# format, while its pinned provider fails (`Providers`). The maintainer decided on 2026-11-04 on a
# backup provider for the same model rather than a key of the pinned provider at OpenRouter:
# "Only the backup endpoint". OpenRouter's endpoint lists of 2026-11-04 give deepinfra/fp8 for
# DeepSeek V4.1 Flash, at "prompt": "0.00000014" and "completion": "0.00000042", with every
# parameter of the requests among its supported_parameters and a max_completion_tokens of 393,216
# (openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints), and gmicloud/fp8 for
# GLM-5.3, at "prompt": "0.0000007" and "completion": "0.0000022", with every parameter of the
# requests among its supported_parameters and a max_completion_tokens of 943,718
# (openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints). GROUNDING: the check of the backup
# providers of 2026-11-04, in which both labeled a sample of reviewed repositories with the
# settings of their model and followed the response format in every response. A backup that the
# endpoint list shows unable to serve the requests serves none of them in a run
# (`1_classify_repos.backup_check`).
BACKUPS = {PRODUCTION.model: replace(PRODUCTION, provider="deepinfra/fp8"),
           SECOND_MODEL.model: replace(SECOND_MODEL, provider="gmicloud/fp8")}


def settings_of(found: Endpoint) -> Settings:
    """The settings of the requests of the model at `found`: those of its record, those of the
    pinned record of the same model id and Hugging Face id on any endpoint,
    OPENAI_REASONING_EFFORT alone for an OpenAI model, and OTHER_MODEL for any other OpenRouter
    model. The settings belong to the weights, because another endpoint of the same weights,
    such as the one that a run pins after the pinned endpoint is retired, serves the model that
    they were chosen for."""
    if found.settings is not None:
        return found.settings
    pinned = PINNED.get(found.model)
    if pinned is not None and pinned.hugging_face_id == found.hugging_face_id:
        return pinned.settings
    return OTHER_MODEL if found.openrouter else Settings(OPENAI_REASONING_EFFORT)


def endpoint(model: str, provider: str = "", quantization: str = "",
             hugging_face_id: str = "") -> Endpoint:
    """The endpoint of `model`, or ValueError when an OpenRouter model lacks its provider, its
    quantization or its Hugging Face id, or an OpenAI model names any of them. The model of
    PRODUCTION or SECOND_MODEL without an endpoint, or with the endpoint of its record, is that
    record with its settings (PINNED)."""
    if model in PINNED and (not (provider or quantization or hugging_face_id)
                            or PINNED[model] == Endpoint(model, provider, quantization,
                                                         hugging_face_id)):
        return PINNED[model]
    found = Endpoint(model, provider or "", quantization or "", hugging_face_id or "")
    named = [name for name, value in (("provider", found.provider),
                                      ("quantization", found.quantization),
                                      ("Hugging Face id", found.hugging_face_id)) if value]
    if not found.openrouter:
        if named:
            raise ValueError(f"{model} is an OpenAI model, which has no {' or '.join(named)}. "
                             f"An OpenRouter model id names its author, such as "
                             f"deepseek/deepseek-v4.1-flash")
        return found
    missing = [name for name in ("provider", "quantization", "Hugging Face id")
               if name not in named]
    if missing:
        raise ValueError(f"{model} is an OpenRouter model, which is called on one endpoint and "
                         f"needs its {', '.join(missing)}")
    if found.quantization not in QUANTIZATIONS:
        raise ValueError(f"{found.quantization} is not a quantization of OpenRouter's provider "
                         f"routing ({', '.join(QUANTIZATIONS)})")
    return found


def backup_of(found: Endpoint) -> Endpoint | None:
    """The backup provider of the model at `found` (BACKUPS), with the settings of `found`, or
    None: for an OpenAI model, for a model without a backup, for other weights or another
    quantization than those of the backup, and when the backup is the provider of `found`."""
    backup = BACKUPS.get(found.model)
    if (not found.openrouter or backup is None or backup.hugging_face_id != found.hugging_face_id
            or backup.quantization != found.quantization or backup.provider == found.provider):
        return None
    return replace(backup, settings=found.settings)


def with_provider(found: Endpoint) -> Endpoint:
    """`found` with the provider on which the classification calls it: the provider that it
    names, as a block of classification parameters recorded before 2026-11-04 names one, and for
    an OpenRouter model without one the provider of the pinned record of the same model id,
    Hugging Face id and quantization, with its settings (PINNED). An OpenAI model has no
    provider. ValueError for an OpenRouter model without a provider that no pinned record
    matches."""
    if found.provider or not found.openrouter:
        return found
    pinned = PINNED.get(found.model)
    if (pinned is None or pinned.hugging_face_id != found.hugging_face_id
            or pinned.quantization != found.quantization):
        raise ValueError(f"{found.model} ({found.hugging_face_id}) in {found.quantization} has "
                         f"no pinned provider in endpoints.py, and its record names none")
    return pinned


class Providers:
    """The pinned provider and the backup provider of one model, and which of them serves the
    model's next request (`next`).

    The pinned provider serves every request while it serves. `failed` records the repositories
    whose requests the pinned provider failed since its last response, and `switch` moves every
    request of the model to the backup provider (`serving`), as the classification does once the
    pinned provider has failed the requests of two repositories and a failure would use an
    attempt. From then on, one request every PINNED_CHECK_SECONDS goes to the pinned provider as
    a test, and a response of the pinned provider ends the switch (`responded`), so that the
    pinned provider serves again once it responds. A test that fails sends the same request to
    the backup provider. A model without a backup provider never switches, and neither does a
    model whose backup provider was retired (`retire`). `role` names the model in the log lines,
    such as "the classifying model"."""

    def __init__(self, pinned: Endpoint, backup: Endpoint | None, role: str,
                 clock=time.monotonic):
        self.pinned, self.backup, self.role, self.clock = pinned, backup, role, clock
        self.switched = False
        # Why the backup provider takes no request for the rest of the invocation, or "".
        self.retired = ""
        # The clock when the switch began and when the next request tests the pinned provider.
        self.since, self.next_test = 0.0, 0.0
        # The repositories whose requests the pinned provider failed since its last response.
        self.failing: set = set()

    def failed(self, repository) -> None:
        """Record that the pinned provider failed a request of `repository`."""
        self.failing.add(repository)

    @property
    def available(self) -> Endpoint | None:
        """The backup provider while it takes requests, or None for a model without one and
        after `retire`."""
        return None if self.retired else self.backup

    @property
    def serving(self) -> Endpoint:
        """The provider that serves the requests: the backup provider during a switch, else the
        pinned provider."""
        return self.backup if self.switched and self.backup is not None else self.pinned

    def next(self) -> tuple[Endpoint, bool]:
        """The endpoint of the next request, and whether the request tests the pinned provider
        during a switch. A test is due PINNED_CHECK_SECONDS after the switch began or after the
        last test began, also while that test is in flight, so that a test that never reports
        holds back no further test."""
        if self.switched and self.clock() >= self.next_test:
            self.next_test = self.clock() + PINNED_CHECK_SECONDS
            return self.pinned, True
        return self.serving, False

    def switch(self, reason: str) -> None:
        """Move every request of the model to the backup provider, because the pinned provider
        failed for `reason`, with a WARNING. Nothing changes during a switch or for a model
        without a backup provider."""
        if self.switched or self.available is None:
            return
        self.switched, self.since = True, self.clock()
        self.next_test = self.since + PINNED_CHECK_SECONDS
        logs.warning(COMPONENT, f"The pinned provider {self.pinned.provider} of {self.role}, "
                                f"{self.pinned.model}, {reason}. The backup provider "
                                f"{self.backup.provider} in {self.backup.quantization} serves the "
                                f"requests of the model with the same settings, and every "
                                f"{logs.hms(PINNED_CHECK_SECONDS)} one request tests whether the "
                                f"pinned provider serves again.")

    def responded(self, endpoint: Endpoint) -> None:
        """Record a response of `endpoint`. A response of the pinned provider clears its failures
        and ends a switch, and the pinned provider serves every request again, with an INFO
        line."""
        if endpoint != self.pinned:
            return
        self.failing.clear()
        if not self.switched:
            return
        self.switched = False
        logs.info(COMPONENT, f"The pinned provider {self.pinned.provider} of {self.role}, "
                             f"{self.pinned.model}, responds again after "
                             f"{logs.hms(self.clock() - self.since)} on the backup provider "
                             f"{self.backup.provider}. It serves the requests of the model "
                             f"again.")

    def retire(self, reason: str) -> None:
        """End a switch and send no further request of the model to the backup provider in this
        invocation, because the backup provider failed for `reason`, with a WARNING. The pinned
        provider serves every request again, and while it fails, its failures pause and use
        attempts as for a model without a backup provider. Nothing changes for a model without a
        backup provider or after an earlier retirement."""
        if self.available is None:
            return
        self.retired, self.switched = reason, False
        logs.warning(COMPONENT, f"The backup provider {self.backup.provider} of {self.role}, "
                                f"{self.backup.model}, {reason}. The pinned provider "
                                f"{self.pinned.provider} serves the requests of the model again, "
                                f"and the backup provider serves none of them for the rest of "
                                f"this invocation.")


def api_key(service: str, credentials_path: Path | None, environ=None) -> str:
    """The first API key of `service`, or NoKey."""
    environ = os.environ if environ is None else environ
    keys = (credentials.openrouter_keys if service == OPENROUTER
            else credentials.openai_keys)(credentials_path, environ)
    if not keys:
        variable = credentials.ENVIRONMENT[service][0]
        raise NoKey(f"{variable} is unset, and {credentials_path} holds no "
                    f"{credentials.NAMES[service].split(' ', 1)[1]}")
    return keys[0].value


def client(found: Endpoint, credentials_path: Path | None, timeout: float,
           max_retries: int, environ=None):
    """The SDK's client for the service of `found`, with the first API key of the service."""
    import openai
    key = api_key(found.service, credentials_path, environ)
    if found.openrouter:
        return openai.AsyncOpenAI(api_key=key, base_url=OPENROUTER_API, timeout=timeout,
                                  max_retries=max_retries)
    return openai.AsyncOpenAI(api_key=key, timeout=timeout, max_retries=max_retries)


def _status(error) -> int | None:
    return getattr(error, "status_code", None)


def _metadata(error) -> dict:
    """The `metadata` of an error of OpenRouter, which the SDK keeps in `body`."""
    body = getattr(error, "body", None)
    metadata = body.get("metadata") if isinstance(body, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def blocked(error) -> str:
    """Why OpenRouter blocked the content of one request, or "" for any other error: HTTP 403 for
    an input that moderation flagged, "If your input was flagged, the error.metadata will contain
    information about the issue" with `reasons` and `flagged_input`, or for a guardrail that
    found prompt injection patterns, "Request blocked: prompt injection patterns detected" with
    `patterns` (openrouter.ai/docs/api-reference/errors). OpenRouter blocks the same content
    again, and a request with other content, such as another repository's, is not blocked."""
    if _status(error) != 403 or not from_openrouter(error):
        return ""
    metadata = _metadata(error)
    if "reasons" in metadata or "flagged_input" in metadata:
        reasons = ", ".join(str(reason) for reason in metadata.get("reasons") or [])
        return f"OpenRouter's moderation flagged the input ({reasons or 'no reason given'})"
    if "patterns" in metadata:
        patterns = ", ".join(repr(str(pattern)) for pattern in metadata.get("patterns") or [])
        return f"a guardrail of OpenRouter blocked the request for the patterns {patterns}"
    return ""


def from_openrouter(error) -> bool:
    """Whether an error of the SDK came from OpenRouter, whose error body has the HTTP status as
    an integer `code`, where OpenAI's has a string or none."""
    body = getattr(error, "body", None)
    return isinstance(body, dict) and isinstance(body.get("code"), int)


def in_flight(error) -> bool:
    """Whether OpenRouter refused the request because the requests in flight fill the budget
    for requests in flight (IN_FLIGHT), which a wait ends."""
    return _status(error) == 402 and _metadata(error).get("limit_source") == IN_FLIGHT


@dataclass(frozen=True)
class CreditRefusal:
    """A service that refuses requests for spent credit, why, and what a person does about it."""

    service: str
    reason: str

    @property
    def top_up(self) -> str:
        return TOP_UP[self.service]


def credit_refusal(error) -> CreditRefusal | None:
    """The refusal for spent credit that an error of the SDK states, or None for any other
    error: OpenRouter's HTTP 402 other than IN_FLIGHT, OpenRouter's HTTP 403 for a spent budget
    or key limit (BUDGET_SOURCES), or OpenAI's QUOTA_CODES."""
    status, metadata = _status(error), _metadata(error)
    source = str(metadata.get("limit_source") or "")
    if status == 402 and source != IN_FLIGHT:
        return CreditRefusal(OPENROUTER, f"OpenRouter refuses the requests for insufficient "
                                         f"credit (HTTP 402"
                                         f"{f', {source}' if source else ''})")
    if status == 403 and source in BUDGET_SOURCES:
        return CreditRefusal(OPENROUTER, f"OpenRouter refuses the requests, because a budget or "
                                         f"the limit of the key is spent (HTTP 403, {source})")
    code, kind = getattr(error, "code", None), getattr(error, "type", None)
    if code in QUOTA_CODES or kind in QUOTA_CODES:
        return CreditRefusal(OPENAI, f"OpenAI refuses every request of the project until its "
                                     f"quota or billing limit is raised, with the code "
                                     f"{code if code in QUOTA_CODES else kind}")
    return None


class KeyCredit:
    """The credit that an OpenRouter API key has left, from GET /api/v1/key, which spends
    nothing. `get(url, headers)` returns the body of a response and is urllib's by default."""

    def __init__(self, key: str, get=None):
        self.key, self.get = key, get or _get

    def remaining(self) -> float | None:
        """The least of the key's `limit_remaining`, "Remaining credits for the key, or null if
        unlimited", and the `remaining_usd` of its `effective_budget`, "The budget that blocks
        requests first, or null" (openrouter.ai/docs/api-reference/limits), or None when neither
        limits the key. The balance of the account takes a management key to read
        (`GET /api/v1/credits`), which the classification does not have. Raises OSError or
        ValueError when the key's data cannot be read."""
        body = json.loads(self.get(f"{OPENROUTER_API}/key",
                                   {"Authorization": f"Bearer {self.key}"}))
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise ValueError("the response of GET /api/v1/key has no object at data")
        budget = data.get("effective_budget")
        limits = [data.get("limit_remaining"),
                  budget.get("remaining_usd") if isinstance(budget, dict) else None]
        found = [float(value) for value in limits
                 if isinstance(value, (int, float)) and not isinstance(value, bool)]
        return min(found) if found else None


class Delisted(str):
    """The reason of `unserved` for an endpoint that OpenRouter's endpoint list of the model does
    not name, such as an endpoint that its provider took down, as distinct from an endpoint that
    the list names without a parameter or the output limit of the requests."""


def unserved(found: Endpoint, parameters, get=None,
             max_output_tokens: int | None = None) -> str | None:
    """Why the endpoint of `found` serves no request with `parameters`, as OpenRouter's endpoint
    list of the model shows it, or None when it serves them: the list has no such model, no
    endpoint with the provider slug of `found`, the endpoint in another quantization, or the
    endpoint without one of `parameters` among its `supported_parameters`. OpenRouter responds
    to each request to such an endpoint with HTTP 404 "No endpoints found". The reason names the
    endpoints in the quantization of `found` that serve `parameters`. With `max_output_tokens`,
    the output limit of the requests, an endpoint with a lower `max_completion_tokens` serves
    none either, because the endpoint would end a response before the limit that the request
    sets. The list spends nothing, and it responds without a key. `get(url, headers)` returns the
    body of a response and is urllib's by default. Raises OSError or ValueError when the list
    cannot be read. The reason for an endpoint that the list does not name is `Delisted`."""
    url = f"{OPENROUTER_API}/models/{found.model}/endpoints"
    try:
        body = json.loads((get or _get)(url, {}))
    except HTTPStatusError as error:
        if error.status == 404:
            return f"OpenRouter lists no model {found.model}"
        raise
    data = body.get("data") if isinstance(body, dict) else None
    listed = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(listed, list):
        raise ValueError(f"the response of GET {url} has no list at data.endpoints")
    entries = {str(entry.get("tag")): entry for entry in listed if isinstance(entry, dict)}

    def lacking(entry: dict) -> list[str]:
        supported = entry.get("supported_parameters") or []
        return [name for name in parameters if name not in supported]
    serving = sorted(tag for tag, entry in entries.items()
                     if entry.get("quantization") == found.quantization and not lacking(entry))
    wanted = ", ".join(parameters)
    others = (f". The endpoints in {found.quantization} with {wanted} are {', '.join(serving)}"
              if serving else f". No endpoint in {found.quantization} has {wanted}")
    entry = entries.get(found.provider)
    if entry is None:
        return Delisted(f"OpenRouter lists no endpoint {found.provider} of {found.model}{others}")
    if entry.get("quantization") != found.quantization:
        return (f"The endpoint {found.provider} of {found.model} is in "
                f"{entry.get('quantization')}, not in {found.quantization}{others}")
    missing = lacking(entry)
    if missing:
        return (f"The endpoint {found.provider} of {found.model} lacks {', '.join(missing)} "
                f"among its supported parameters{others}")
    most = entry.get("max_completion_tokens")
    if (max_output_tokens is not None and isinstance(most, int) and not isinstance(most, bool)
            and most < max_output_tokens):
        return (f"The endpoint {found.provider} of {found.model} generates at most {most:,} "
                f"tokens (max_completion_tokens), fewer than the output limit of "
                f"{max_output_tokens:,} tokens of the requests")
    return None


def unsupported_effort(found: Endpoint, effort: str, listed: dict) -> str | None:
    """Why OpenRouter does not take the reasoning effort `effort` for the model of `found` as it
    is, from the model's entry of OpenRouter's model list, `listed` by model id (`model_list`), or
    None when it takes it: the list has no such model, or the model's `reasoning` lists
    `supported_efforts` without `effort` or exposes no effort. OpenRouter's reasoning page:
    "supported_efforts: Filter effort selectors to these values ... When null, all gateway
    effort values are accepted. When omitted, the model does not expose effort selection"
    (openrouter.ai/docs/guides/best-practices/reasoning-tokens). Under its heading "Google
    Gemini 3 Models with Thinking Levels", the same page says "If a model doesn't support a
    specific effort level ..., OpenRouter will map your requested effort to the nearest
    supported level", and no page says which level another model gets."""
    entry = listed.get(found.model)
    if not isinstance(entry, dict):
        return f"OpenRouter's model list has no model {found.model}"
    reasoning = entry.get("reasoning")
    if not isinstance(reasoning, dict) or "supported_efforts" not in reasoning:
        return (f"OpenRouter's model list gives {found.model} no reasoning effort to select, and "
                f"its requests send the effort {effort}")
    efforts = reasoning["supported_efforts"]
    if efforts is None or effort in efforts:
        return None
    return (f"OpenRouter's model list gives {found.model} the reasoning efforts "
            f"{', '.join(str(level) for level in efforts)}, not {effort}, and the "
            f"classification parameters would name an effort that the model does not take")


def model_list(get=None) -> dict:
    """The entries of OpenRouter's model list, `GET https://openrouter.ai/api/v1/models`, by
    model id: "Each model in GET /api/v1/models may include a reasoning object describing which
    effort levels it accepts" (openrouter.ai/docs/guides/best-practices/reasoning-tokens). The
    list spends nothing, and it responds without a key. Raises OSError or ValueError when the
    list cannot be read."""
    url = f"{OPENROUTER_API}/models"
    body = json.loads((get or _get)(url, {}))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise ValueError(f"the response of GET {url} has no list at data")
    return {str(entry.get("id")): entry for entry in data if isinstance(entry, dict)}


class HTTPStatusError(OSError):
    """A response with an HTTP status other than 200."""

    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status


def _get(url: str, headers: dict) -> str:
    """The body of a GET request, or OSError, which is HTTPStatusError for a response with an
    error status."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=GET_TIMEOUT) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise HTTPStatusError(error.code, url) from None
