"""
Tests for endpoints.py

Strategy:
  - An error of the SDK is built as the SDK builds it from a response, with the body that
    OpenRouter's or OpenAI's error responses have. OpenRouter's body has the HTTP status as an
    integer `code` and a `metadata` object (openrouter.ai/docs/api-reference/errors).
  - The credit of an OpenRouter API key, the endpoints of a model and the model list come from a
    fake GET that returns the body of GET /api/v1/key, of GET
    /api/v1/models/{author}/{slug}/endpoints or of GET /api/v1/models, so that no test reaches
    the network.

Requirements: Python 3.14, openai (requirements.txt)
Run with:  python3 -m unittest test_endpoints -v
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classification_tools as tools  # noqa: E402
import endpoints  # noqa: E402

DEEPSEEK = endpoints.Endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8",
                              "deepseek-ai/DeepSeek-V4.1-Flash")


def _error(kind, status, body):
    """The SDK's error for a response with `status` and the JSON `body`. The SDK passes the
    object under `error` as the body (`openai._client._make_status_error`)."""
    response = SimpleNamespace(request=SimpleNamespace(), status_code=status, headers={})
    return kind("refused", response=response, body=body)


def _openrouter(kind, status, metadata=None):
    return _error(kind, status, {"code": status, "message": "refused", "metadata": metadata})


class TestTheEndpoint(unittest.TestCase):

    def test_a_model_id_with_its_author_is_an_openrouter_model(self):
        self.assertEqual(DEEPSEEK.service, endpoints.OPENROUTER)
        self.assertEqual(endpoints.Endpoint("gpt-6-luna").service, endpoints.OPENAI)

    def test_an_openrouter_model_needs_its_provider_quantization_and_weights(self):
        """Without them, OpenRouter would route the requests to any provider and quantization,
        and the classification parameters would not name the weights."""
        with self.assertRaisesRegex(ValueError, "needs its provider, quantization, Hugging Face id"):
            endpoints.endpoint("z-ai/glm-5.3-flash")
        with self.assertRaisesRegex(ValueError, "needs its Hugging Face id"):
            endpoints.endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8")
        with self.assertRaisesRegex(ValueError, "fp7 is not a quantization"):
            endpoints.endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp7", "d/D")
        self.assertEqual(endpoints.endpoint("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8",
                                            "deepseek-ai/DeepSeek-V4.1-Flash"), DEEPSEEK)

    def test_the_production_model_named_without_an_endpoint_is_called_on_its_pinned_endpoint(
            self):
        """The maintainer chose DeepSeek V4.1 Flash on Morph's fp8 endpoint on 2026-10-31. Named
        without an endpoint, as the default of --model names it, the model is called on that
        endpoint, and a caller can still name another endpoint of the model."""
        self.assertEqual(endpoints.endpoint("deepseek/deepseek-v4.1-flash"), DEEPSEEK)
        self.assertEqual(endpoints.endpoint("deepseek/deepseek-v4.1-flash", "wafer/us", "fp8",
                                            "deepseek-ai/DeepSeek-V4.1-Flash").provider,
                         "wafer/us")

    def test_the_second_model_named_without_an_endpoint_is_called_on_its_pinned_endpoint(self):
        """The maintainer chose GLM-5.3 on Morph's fp8 endpoint as the second model of the label
        evaluation on 2026-10-31. Named without an endpoint, the model is called on that
        endpoint, so that it labels as it did in the six-model test."""
        self.assertEqual(endpoints.endpoint("z-ai/glm-5.3"),
                         endpoints.Endpoint("z-ai/glm-5.3", "morph/fp8", "fp8",
                                            "zai-org/GLM-5.3"))

    def test_an_openai_model_has_no_endpoint_of_openrouter(self):
        with self.assertRaisesRegex(ValueError, "gpt-6-luna is an OpenAI model, which has no "
                                                "provider or quantization"):
            endpoints.endpoint("gpt-6-luna", "morph/fp8", "fp8")
        self.assertEqual(endpoints.endpoint("gpt-6-luna").parameters(), {"model": "gpt-6-luna"})

    def test_the_routing_pins_one_endpoint_without_fallbacks(self):
        self.assertEqual(DEEPSEEK.routing(),
                         {"provider": {"order": ["morph/fp8"], "allow_fallbacks": False,
                                       "quantizations": ["fp8"], "require_parameters": True}})
        self.assertEqual(endpoints.Endpoint("gpt-6-luna").routing(), {})

    def test_the_parameters_name_the_model_its_weights_and_its_quantization_not_the_provider(
            self):
        """The maintainer decided on 2026-11-04 that the provider that serves a model is no
        classification parameter. A block recorded since names the model, its weights and its
        quantization, and the classification calls it on the pinned provider of those weights. A
        block recorded before names the provider, which it is read with."""
        recorded = DEEPSEEK.parameters()
        self.assertEqual(recorded, {"model": "deepseek/deepseek-v4.1-flash",
                                    "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash",
                                    "quantization": "fp8"})
        read = endpoints.Endpoint.of({**recorded, "reasoning_effort": "medium"})
        self.assertEqual(read.provider, "")
        self.assertEqual(endpoints.with_provider(read), DEEPSEEK)
        earlier = endpoints.Endpoint.of({**recorded, "provider": "wafer/us",
                                         "reasoning_effort": "medium"})
        self.assertEqual(endpoints.with_provider(earlier).provider, "wafer/us")
        with self.assertRaisesRegex(ValueError, "has no pinned provider"):
            endpoints.with_provider(endpoints.Endpoint.of({**recorded, "quantization": "fp4"}))
        luna = endpoints.Endpoint("gpt-6-luna")
        self.assertEqual(endpoints.with_provider(luna), luna)

    def test_each_pinned_model_has_a_backup_provider_of_the_same_weights_and_quantization(self):
        """The maintainer decided on 2026-11-04 on a backup provider for the same model. The
        backup serves the same weights in the same quantization with the settings of the model,
        and no model has a backup that is its own provider or that serves other weights."""
        backup = endpoints.backup_of(DEEPSEEK)
        self.assertEqual(backup, endpoints.Endpoint("deepseek/deepseek-v4.1-flash",
                                                    "deepinfra/fp8", "fp8",
                                                    "deepseek-ai/DeepSeek-V4.1-Flash"))
        self.assertIs(endpoints.backup_of(endpoints.PRODUCTION).settings,
                      endpoints.PRODUCTION.settings)
        self.assertEqual(endpoints.backup_of(endpoints.SECOND_MODEL).provider, "gmicloud/fp8")
        self.assertEqual(endpoints.backup_of(endpoints.SECOND_MODEL).quantization, "fp8")
        for found in (endpoints.Endpoint("gpt-6-luna"),
                      endpoints.Endpoint("moonshotai/kimi-k3", "morph/fp8", "fp8",
                                         "moonshotai/Kimi-K3"),
                      endpoints.Endpoint("deepseek/deepseek-v4.1-flash", "wafer/us", "mxfp4",
                                         "deepseek-ai/DeepSeek-V4.1-Flash"),
                      endpoints.Endpoint("deepseek/deepseek-v4.1-flash", "deepinfra/fp8", "fp8",
                                         "deepseek-ai/DeepSeek-V4.1-Flash")):
            with self.subTest(found=found):
                self.assertIsNone(endpoints.backup_of(found))

    def test_the_client_of_an_openrouter_model_calls_openrouters_api_with_its_key(self):
        environ = {"OPENROUTER_API_KEY": "sk-or-v1-test", "OPENAI_API_KEY": "sk-proj-test"}
        router = endpoints.client(DEEPSEEK, None, timeout=1, max_retries=0, environ=environ)
        self.assertEqual((str(router.base_url), router.api_key),
                         ("https://openrouter.ai/api/v1/", "sk-or-v1-test"))
        luna = endpoints.client(endpoints.Endpoint("gpt-6-luna"), None, timeout=1,
                                max_retries=0, environ=environ)
        self.assertEqual(luna.api_key, "sk-proj-test")
        with self.assertRaisesRegex(endpoints.NoKey, "OPENROUTER_API_KEY is unset"):
            endpoints.client(DEEPSEEK, None, timeout=1, max_retries=0,
                             environ={"OPENAI_API_KEY": "sk-proj-test"})


class TestTheRefusals(unittest.TestCase):

    def test_spent_credit_is_a_refusal_for_credit_of_its_service(self):
        """OpenRouter: "Your account or API key has insufficient credits" (HTTP 402), and a
        spent budget or key limit with HTTP 403. OpenAI: insufficient_quota and
        billing_hard_limit_reached."""
        for error, service, said in (
                (_openrouter(openai.APIStatusError, 402), endpoints.OPENROUTER,
                 "insufficient credit (HTTP 402)"),
                (_openrouter(openai.APIStatusError, 402,
                             {"limit_source": "openrouter_key_limit"}), endpoints.OPENROUTER,
                 "(HTTP 402, openrouter_key_limit)"),
                (_openrouter(openai.PermissionDeniedError, 403,
                             {"limit_source": "openrouter_key_limit"}), endpoints.OPENROUTER,
                 "the limit of the key is spent (HTTP 403, openrouter_key_limit)"),
                (_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}),
                 endpoints.OPENAI, "with the code insufficient_quota"),
                (_error(openai.BadRequestError, 400, {"code": "billing_hard_limit_reached"}),
                 endpoints.OPENAI, "with the code billing_hard_limit_reached")):
            with self.subTest(said=said):
                spent = endpoints.credit_refusal(error)
                self.assertEqual(spent.service, service)
                self.assertIn(said, spent.reason)
                self.assertEqual(tools.refusal(error), "")
        self.assertIn("https://openrouter.ai/settings/credits",
                      endpoints.TOP_UP[endpoints.OPENROUTER])

    def test_a_full_budget_of_requests_in_flight_is_waited_out_not_topped_up(self):
        """OpenRouter's limits page says to "wait for the `Retry-After` header and retry"
        (openrouter.ai/docs/api-reference/limits). The requests in flight have filled the budget
        for requests in flight, not the balance, so a top-up does not end the refusal."""
        error = _openrouter(openai.APIStatusError, 402,
                            {"limit_source": "openrouter_in_flight_budget"})
        self.assertIsNone(endpoints.credit_refusal(error))
        self.assertTrue(endpoints.in_flight(error))

    def test_content_that_moderation_or_a_guardrail_blocks_concerns_that_request_alone(self):
        """OpenRouter's errors page: HTTP 403 for "guardrail block, or moderation flag". A
        flagged input has `reasons` and `flagged_input` in its metadata, and a guardrail's block
        "Request blocked: prompt injection patterns detected" has `patterns`."""
        moderated = _openrouter(openai.PermissionDeniedError, 403, {
            "reasons": ["violence"], "flagged_input": "ignore all...", "provider_name": "Morph",
            "model_slug": "deepseek/deepseek-v4.1-flash"})
        guarded = _openrouter(openai.PermissionDeniedError, 403,
                              {"patterns": ["ignore all previous instructions"]})
        self.assertEqual(endpoints.blocked(moderated),
                         "OpenRouter's moderation flagged the input (violence)")
        self.assertEqual(endpoints.blocked(guarded),
                         "a guardrail of OpenRouter blocked the request for the patterns "
                         "'ignore all previous instructions'")
        for error in (moderated, guarded):
            self.assertEqual(tools.refusal(error), "")
            self.assertIsNone(endpoints.credit_refusal(error))
        denied = _openrouter(openai.PermissionDeniedError, 403)
        self.assertEqual(endpoints.blocked(denied), "")
        self.assertEqual(tools.refusal(denied), "OpenRouter denies the OpenRouter API key access")

    def test_a_rejected_key_or_a_key_without_access_stays_refused(self):
        """A top-up does not end these refusals, and the run stops on them."""
        for error, said in (
                (_openrouter(openai.AuthenticationError, 401),
                 "OpenRouter rejects the OpenRouter API key"),
                (_openrouter(openai.PermissionDeniedError, 403),
                 "OpenRouter denies the OpenRouter API key access"),
                (_error(openai.AuthenticationError, 401, None), "the API rejects the OpenAI "
                                                                 "API key"),
                (_error(openai.PermissionDeniedError, 403, None), "the API denies the project "
                                                                  "access")):
            with self.subTest(said=said):
                self.assertIsNone(endpoints.credit_refusal(error))
                self.assertEqual(tools.refusal(error), said)

    def test_other_errors_are_neither_a_refusal_for_credit_nor_a_refusal_of_every_request(self):
        for error in (_openrouter(openai.RateLimitError, 429),
                      _openrouter(openai.InternalServerError, 503),
                      _error(openai.RateLimitError, 429, {"code": "rate_limit_exceeded"})):
            with self.subTest(error=error):
                self.assertIsNone(endpoints.credit_refusal(error))
                self.assertEqual(tools.refusal(error), "")


class TestTheCreditOfAKey(unittest.TestCase):

    def key(self, data):
        requested = []

        def get(url, headers):
            requested.append((url, headers))
            return json.dumps({"data": data})
        return endpoints.KeyCredit("sk-or-v1-test", get), requested

    def test_the_credit_left_is_the_least_of_the_key_limit_and_the_effective_budget(self):
        """The example of OpenRouter's API reference: "limit_remaining": 74.5 and an
        "effective_budget" with "remaining_usd": 17.5."""
        credit, requested = self.key({"limit": 100, "limit_remaining": 74.5,
                                      "effective_budget": {"limit_usd": 50,
                                                           "remaining_usd": 17.5}})
        self.assertEqual(credit.remaining(), 17.5)
        self.assertEqual(requested, [("https://openrouter.ai/api/v1/key",
                                      {"Authorization": "Bearer sk-or-v1-test"})])

    def test_a_key_without_a_limit_or_a_budget_is_unlimited_and_a_spent_limit_leaves_no_credit(
            self):
        credit, _ = self.key({"limit": None, "limit_remaining": None, "effective_budget": None})
        self.assertIsNone(credit.remaining())
        credit, _ = self.key({"limit_remaining": 0, "effective_budget": None})
        self.assertEqual(credit.remaining(), 0)

    def test_a_response_without_the_data_of_the_key_cannot_be_read(self):
        credit = endpoints.KeyCredit("sk-or-v1-test", lambda url, headers: json.dumps([]))
        with self.assertRaises(ValueError):
            credit.remaining()



def _entry(tag, quantization, *parameters):
    """An entry of OpenRouter's endpoint list with the fields that `unserved` reads."""
    return {"tag": tag, "quantization": quantization,
            "supported_parameters": ["max_tokens", "temperature", *parameters]}


GLM = endpoints.Endpoint("z-ai/glm-5.3-flash", "z-ai/fp8", "fp8", "zai-org/GLM-5.3-Flash")
EVERY = endpoints.request_parameters(endpoints.PRODUCTION.settings)


class TestTheEndpointList(unittest.TestCase):

    def unserved(self, found, parameters, *entries):
        requested = []

        def get(url, headers):
            requested.append((url, headers))
            return json.dumps({"data": {"id": found.model, "endpoints": list(entries)}})
        return endpoints.unserved(found, parameters, get), requested

    def test_an_endpoint_with_every_parameter_of_the_requests_serves_them(self):
        """OpenRouter returns the endpoint list without a key, and the request therefore sends
        none."""
        reason, requested = self.unserved(GLM, EVERY, _entry("z-ai/fp8", "fp8", *EVERY))
        self.assertIsNone(reason)
        self.assertEqual(requested, [("https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/"
                                      "endpoints", {})])

    def test_an_endpoint_without_a_parameter_serves_none_and_the_reason_names_those_that_do(self):
        """Chutes' mxfp4 endpoint of Kimi K3 supports `reasoning` but not `tools`, which the
        requests of the classifying model send. The summary model sends no tools, and the same
        endpoint serves its requests."""
        kimi = endpoints.Endpoint("moonshotai/kimi-k3", "chutes/mxfp4", "mxfp4",
                                  "moonshotai/Kimi-K3")
        entries = (_entry("chutes/mxfp4", "mxfp4", "reasoning", "response_format", "seed",
                          "structured_outputs", "top_p"),
                   _entry("wafer/us", "mxfp4", *EVERY), _entry("parasail/mxfp4", "mxfp4", *EVERY),
                   _entry("morph/fp8", "fp8", *EVERY))
        reason, _ = self.unserved(kimi, EVERY, *entries)
        self.assertEqual(reason, "The endpoint chutes/mxfp4 of moonshotai/kimi-k3 lacks tools "
                                 "among its supported parameters. The endpoints in mxfp4 with "
                                 "reasoning, tools, temperature, top_p, seed, max_tokens are "
                                 "parasail/mxfp4, wafer/us")
        self.assertIsNone(self.unserved(kimi, endpoints.SUMMARY_PARAMETERS, *entries)[0])

    def test_an_endpoint_without_a_temperature_a_seed_or_the_output_limit_serves_no_request(self):
        """The classifying model's requests send a temperature, a top_p, a seed and an output
        limit, which the endpoint list names `max_tokens`, and with `require_parameters`,
        OpenRouter routes them only to an endpoint that lists all four among its supported
        parameters. The summary model's requests send none of them, and such an endpoint serves
        them."""
        self.assertEqual(EVERY, ("reasoning", "tools", "temperature", "top_p", "seed",
                                 "max_tokens"))
        for lacking in ("temperature", "top_p", "seed", "max_tokens"):
            with self.subTest(lacking=lacking):
                entry = {"tag": "z-ai/fp8", "quantization": "fp8", "supported_parameters": [
                    name for name in ("reasoning", "tools", "temperature", "top_p", "seed",
                                      "max_tokens")
                    if name != lacking]}
                reason, _ = self.unserved(GLM, EVERY, entry)
                self.assertEqual(reason, f"The endpoint z-ai/fp8 of z-ai/glm-5.3-flash lacks "
                                         f"{lacking} among its supported parameters. No endpoint "
                                         f"in fp8 has reasoning, tools, temperature, top_p, "
                                         f"seed, max_tokens")
                self.assertIsNone(self.unserved(GLM, endpoints.SUMMARY_PARAMETERS, entry)[0])

    def test_an_endpoint_that_generates_fewer_tokens_than_the_output_limit_serves_none(self):
        """OpenRouter's endpoint list gives each endpoint its `max_completion_tokens`. An endpoint
        with fewer than the output limit of the requests would end a response before the limit
        that its request sets. A request without a limit, and an entry without the field, leave
        the endpoint to the other tests."""
        entry = {**_entry("z-ai/fp8", "fp8", *EVERY), "max_completion_tokens": 131_072}
        reason, _ = self.unserved(GLM, EVERY, entry)
        self.assertIsNone(reason)
        reason = endpoints.unserved(GLM, EVERY, lambda url, headers: json.dumps(
            {"data": {"endpoints": [entry]}}), max_output_tokens=262_144)
        self.assertEqual(reason, "The endpoint z-ai/fp8 of z-ai/glm-5.3-flash generates at most "
                                 "131,072 tokens (max_completion_tokens), fewer than the output "
                                 "limit of 262,144 tokens of the requests")
        for most in (262_144, 943_718, None):
            with self.subTest(max_completion_tokens=most):
                listed = {**entry, "max_completion_tokens": most}
                self.assertIsNone(endpoints.unserved(GLM, EVERY, lambda url, headers: json.dumps(
                    {"data": {"endpoints": [listed]}}), max_output_tokens=262_144))

    def test_a_missing_endpoint_or_one_in_another_quantization_serves_none(self):
        reason, _ = self.unserved(GLM, EVERY, _entry("z-ai", "unknown", *EVERY))
        self.assertEqual(reason, "OpenRouter lists no endpoint z-ai/fp8 of z-ai/glm-5.3-flash. "
                                 "No endpoint in fp8 has reasoning, tools, temperature, top_p, "
                                 "seed, max_tokens")
        reason, _ = self.unserved(endpoints.Endpoint(GLM.model, "wafer/nvfp4", "fp8", "w"),
                                  EVERY, _entry("wafer/nvfp4", "nvfp4", *EVERY))
        self.assertEqual(reason, "The endpoint wafer/nvfp4 of z-ai/glm-5.3-flash is in nvfp4, "
                                 "not in fp8. No endpoint in fp8 has reasoning, tools, "
                                 "temperature, top_p, seed, max_tokens")

    def test_a_model_that_openrouter_does_not_list_has_no_endpoint(self):
        """OpenRouter responds to an unknown model id with HTTP 404 and the body
        {"error":{"message":"Not Found","code":404}}."""
        def get(url, headers):
            raise endpoints.HTTPStatusError(404, url)
        self.assertEqual(endpoints.unserved(endpoints.Endpoint("z-ai/glm-9", "z-ai/fp8", "fp8",
                                                               "w"), EVERY, get),
                         "OpenRouter lists no model z-ai/glm-9")

    def test_a_list_that_cannot_be_read_raises(self):
        def get(url, headers):
            raise endpoints.HTTPStatusError(503, url)
        with self.assertRaises(OSError):
            endpoints.unserved(GLM, EVERY, get)
        with self.assertRaises(ValueError):
            endpoints.unserved(GLM, EVERY, lambda url, headers: json.dumps({"data": {}}))


# The entries of OpenRouter's model list of the two pinned models of 2026-11-02, with the fields
# that `unsupported_effort` reads (GET /api/v1/models).
MODEL_LIST = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash",
     "reasoning": {"mandatory": False, "default_enabled": True,
                   "supported_efforts": ["max", "high", "low", "none"], "default_effort": "high"}},
    {"id": "z-ai/glm-5.3",
     "reasoning": {"mandatory": True, "default_enabled": True,
                   "supported_efforts": ["max", "high", "low"], "default_effort": "max"}},
    {"id": "acme/any-effort", "reasoning": {"supported_efforts": None}},
    {"id": "acme/no-effort", "reasoning": {"mandatory": False}}]}


class TestTheModelList(unittest.TestCase):

    def listed(self) -> dict:
        requested = []

        def get(url, headers):
            requested.append((url, headers))
            return json.dumps(MODEL_LIST)
        models = endpoints.model_list(get)
        self.assertEqual(requested, [("https://openrouter.ai/api/v1/models", {})])
        return models

    def test_an_effort_that_the_model_list_does_not_give_the_model_is_refused(self):
        """Neither model lists "medium", and the classification parameters of a request with it
        would name an effort that the model does not take. Both list "high"."""
        models = self.listed()
        glm = endpoints.SECOND_MODEL
        self.assertEqual(endpoints.unsupported_effort(DEEPSEEK, "medium", models),
                         "OpenRouter's model list gives deepseek/deepseek-v4.1-flash the "
                         "reasoning efforts max, high, low, none, not medium, and the "
                         "classification parameters would name an effort that the model does "
                         "not take")
        self.assertIn("gives z-ai/glm-5.3 the reasoning efforts max, high, low, not medium",
                      endpoints.unsupported_effort(glm, "medium", models))
        self.assertIn("not none", endpoints.unsupported_effort(glm, "none", models))
        for found in (DEEPSEEK, glm):
            with self.subTest(model=found.model):
                self.assertIsNone(endpoints.unsupported_effort(found, "high", models))

    def test_an_effort_of_a_model_without_a_list_of_efforts_is_taken_or_refused(self):
        """"When null , all gateway effort values are accepted. When omitted, the model does not
        expose effort selection" (openrouter.ai/docs/guides/best-practices/reasoning-tokens). A
        model that the list lacks is refused too."""
        models = self.listed()
        self.assertIsNone(endpoints.unsupported_effort(
            endpoints.Endpoint("acme/any-effort", "acme/fp8", "fp8", "acme/A"), "medium", models))
        self.assertEqual(endpoints.unsupported_effort(
            endpoints.Endpoint("acme/no-effort", "acme/fp8", "fp8", "acme/N"), "high", models),
            "OpenRouter's model list gives acme/no-effort no reasoning effort to select, and its "
            "requests send the effort high")
        self.assertEqual(endpoints.unsupported_effort(
            endpoints.Endpoint("acme/gone", "acme/fp8", "fp8", "acme/G"), "high", models),
            "OpenRouter's model list has no model acme/gone")

    def test_a_model_list_that_cannot_be_read_raises(self):
        with self.assertRaises(ValueError):
            endpoints.model_list(lambda url, headers: json.dumps({"data": {}}))


class TestTheSettings(unittest.TestCase):
    """The settings of the requests of the pinned models, which the maintainer set on
    2026-11-02, and those of any other model."""

    def test_each_pinned_model_has_the_settings_that_the_maintainer_set(self):
        """DeepSeek V4.1 Flash labels at temperature 0.6, its higher temperature is the 1.0 of
        its model card, and GLM-5.3 labels at the temperature 1.0 and the top_p 0.95 of Z.ai's
        generation_config.json. Both label at effort high and seed 7. DeepSeek V4.1 Flash
        labels at 0.6 with an output limit of 65,536 tokens, which the maintainer chose so that
        a response that reasons without end ends sooner. Its attempts at the higher temperature
        and GLM-5.3 have an output limit of 262,144 tokens, the least that DeepSeek's model card
        recommends with K counted as 1,024."""
        self.assertEqual(endpoints.PRODUCTION.settings,
                         endpoints.Settings("high", 65_536, 0.6, 0.95, 7, 1.0, 262_144))
        self.assertEqual(endpoints.SECOND_MODEL.settings,
                         endpoints.Settings("high", 262_144, 1.0, 0.95, 7))
        self.assertEqual(endpoints.PRODUCTION.settings.higher(),
                         endpoints.Settings("high", 262_144, 1.0, 0.95, 7))
        self.assertIsNone(endpoints.SECOND_MODEL.settings.higher())
        self.assertEqual(endpoints.PRODUCTION.settings.parameters(),
                         {"reasoning_effort": "high", "temperature": 0.6, "top_p": 0.95,
                          "seed": 7, "max_output_tokens": 65_536})

    def test_the_attempts_after_a_failed_response_take_their_own_output_limit(self):
        """Settings with an output limit of their own for the attempts at the higher temperature
        send it there, settings without one keep their limit, and the endpoint has to generate
        the larger of the two."""
        own = endpoints.Settings("high", 100, 0.6, 0.95, 7, 1.0, 400)
        self.assertEqual(own.higher(), endpoints.Settings("high", 400, 1.0, 0.95, 7))
        self.assertEqual(own.largest_output_tokens(), 400)
        self.assertEqual(own.higher().recorded(),
                         {"temperature": 1.0, "top_p": 0.95, "seed": 7, "max_output_tokens": 400})
        kept = endpoints.Settings("high", 100, 0.6, 0.95, 7, 1.0)
        self.assertEqual(kept.higher(), endpoints.Settings("high", 100, 1.0, 0.95, 7))
        self.assertEqual(kept.largest_output_tokens(), 100)
        self.assertIsNone(endpoints.Settings("high").largest_output_tokens())

    def test_a_pinned_model_keeps_its_settings_on_any_endpoint_of_its_weights(self):
        """Named without an endpoint or with the endpoint of its record, a pinned model is its
        record. On another endpoint of the same weights, such as the endpoint that a run pins
        after the pinned one is retired, it keeps the settings of its record, the higher
        temperature among them. Other weights under the same model id, another OpenRouter model
        and an OpenAI model have settings that name no other model's temperature."""
        for found in (endpoints.endpoint("deepseek/deepseek-v4.1-flash"),
                      endpoints.endpoint(DEEPSEEK.model, DEEPSEEK.provider, DEEPSEEK.quantization,
                                         DEEPSEEK.hugging_face_id), DEEPSEEK,
                      endpoints.endpoint(DEEPSEEK.model, "wafer/us", "mxfp4",
                                         DEEPSEEK.hugging_face_id)):
            with self.subTest(found=found):
                self.assertEqual(endpoints.settings_of(found),
                                 endpoints.Settings("high", 65_536, 0.6, 0.95, 7, 1.0, 262_144))
        glm = endpoints.endpoint("z-ai/glm-5.3", "wafer/us", "mxfp4", "zai-org/GLM-5.3")
        self.assertEqual(endpoints.settings_of(glm),
                         endpoints.Settings("high", 262_144, 1.0, 0.95, 7))
        for other in (endpoints.endpoint(DEEPSEEK.model, "wafer/us", "fp8", "acme/Distilled"),
                      endpoints.endpoint("moonshotai/kimi-k3", "wafer/us", "mxfp4",
                                         "moonshotai/Kimi-K3")):
            with self.subTest(other=other):
                self.assertEqual(endpoints.settings_of(other),
                                 endpoints.Settings("high", 262_144, seed=7))
        self.assertEqual(endpoints.settings_of(endpoints.endpoint("gpt-6-luna")),
                         endpoints.Settings("medium"))
        self.assertEqual(endpoints.Settings("medium").parameters(),
                         {"reasoning_effort": "medium"})

    def test_a_higher_temperature_needs_a_lower_one_to_raise(self):
        """Without a temperature, or at a temperature at or above the higher one, the attempts
        after a failed response keep the model's settings."""
        for settings in (endpoints.Settings("high", temperature=None, higher_temperature=1.0),
                         endpoints.Settings("high", temperature=1.0, higher_temperature=1.0),
                         endpoints.Settings("high", temperature=0.6)):
            with self.subTest(settings=settings):
                self.assertIsNone(settings.higher())

    def test_the_settings_that_a_block_of_parameters_records_are_read_back(self):
        recorded = {**DEEPSEEK.parameters(), **endpoints.PRODUCTION.settings.parameters()}
        self.assertEqual(endpoints.Settings.of(recorded),
                         endpoints.Settings("high", 65_536, 0.6, 0.95, 7))
        self.assertEqual(endpoints.Settings.of({"model": "gpt-6-luna",
                                                "reasoning_effort": "medium"}),
                         endpoints.Settings("medium"))


class TestTheProviders(unittest.TestCase):
    """Which provider serves the next request of a model: the pinned provider while it serves,
    the backup provider after a switch, and the pinned provider again once a request that tests
    it gets a response."""

    def setUp(self):
        # The log lines of a switch go to a buffer, which `test_a_switch_and_its_end_are_logged`
        # reads.
        self.err = io.StringIO()
        redirect = contextlib.redirect_stderr(self.err)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.now = 0.0
        self.backup = endpoints.backup_of(DEEPSEEK)
        self.providers = endpoints.Providers(DEEPSEEK, self.backup, "the classifying model",
                                             clock=lambda: self.now)

    def test_the_pinned_provider_serves_until_a_switch(self):
        self.assertEqual(self.providers.next(), (DEEPSEEK, False))
        self.providers.responded(self.backup)
        self.assertEqual(self.providers.serving, DEEPSEEK)

    def test_after_a_switch_the_backup_serves_and_one_request_tests_the_pinned_provider(self):
        """During a switch, one request every PINNED_CHECK_SECONDS goes to the pinned provider,
        also while an earlier test is in flight, and the others go to the backup provider."""
        self.providers.switch("failed the requests of 2 repositories")
        self.assertEqual(self.providers.next(), (self.backup, False))
        self.now = endpoints.PINNED_CHECK_SECONDS
        self.assertEqual(self.providers.next(), (DEEPSEEK, True))
        self.assertEqual(self.providers.next(), (self.backup, False))
        self.now += endpoints.PINNED_CHECK_SECONDS - 1
        self.assertEqual(self.providers.next(), (self.backup, False))
        self.now += 1
        self.assertEqual(self.providers.next(), (DEEPSEEK, True))

    def test_a_response_of_the_pinned_provider_ends_the_switch(self):
        self.providers.switch("failed the requests of 2 repositories")
        self.providers.responded(self.backup)
        self.assertEqual(self.providers.serving, self.backup)
        self.now = 120
        self.providers.responded(DEEPSEEK)
        self.assertEqual(self.providers.next(), (DEEPSEEK, False))
        self.assertFalse(self.providers.switched)

    def test_a_retired_backup_ends_the_switch_and_takes_no_further_switch(self):
        """A backup provider that does not hold the model to the response format is retired:
        the switch ends, the pinned provider serves every request again, no test is due, and a
        later failure of the pinned provider switches to the backup provider no more."""
        self.providers.switch("failed the requests of 2 repositories")
        self.providers.retire("returned final responses that failed the response format")
        self.assertIsNone(self.providers.available)
        self.now = endpoints.PINNED_CHECK_SECONDS
        self.assertEqual(self.providers.next(), (DEEPSEEK, False))
        self.providers.switch("failed the requests of 2 repositories")
        self.assertEqual((self.providers.switched, self.providers.serving), (False, DEEPSEEK))
        self.assertIn("WARNING repository_labeling: The backup provider deepinfra/fp8 of the "
                      "classifying model, deepseek/deepseek-v4.1-flash, returned final responses "
                      "that failed the response format. The pinned provider morph/fp8 serves the "
                      "requests of the model again, and the backup provider serves none of them "
                      "for the rest of this invocation.", self.err.getvalue())

    def test_a_model_without_a_backup_never_switches(self):
        providers = endpoints.Providers(DEEPSEEK, None, "the classifying model")
        providers.switch("failed")
        self.assertEqual(providers.next(), (DEEPSEEK, False))

    def test_a_switch_and_its_end_are_logged(self):
        self.providers.switch("failed the requests of 2 repositories")
        self.now = 300
        self.providers.responded(DEEPSEEK)
        self.assertIn("WARNING repository_labeling: The pinned provider morph/fp8 of the "
                      "classifying model, deepseek/deepseek-v4.1-flash, failed the requests of 2 "
                      "repositories. The backup provider deepinfra/fp8 in fp8 serves the requests "
                      "of the model with the same settings, and every 0:10:00 one request tests "
                      "whether the pinned provider serves again.", self.err.getvalue())
        self.assertIn("INFO repository_labeling: The pinned provider morph/fp8 of the "
                      "classifying model, deepseek/deepseek-v4.1-flash, responds again after "
                      "0:05:00 on the backup provider deepinfra/fp8. It serves the requests of "
                      "the model again.", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
