"""
Tests for costs.py

Strategy:
  - Each case writes an index row and a tokens file into a temporary cache of labels, and a price
    file of round prices in USD per million tokens. The expected costs are worked out by hand
    from those prices.
  - Two cases read the real price file: one for the default models of a run of the
    classification at their pinned endpoints, and one for the endpoints on which the comparison
    of the models and its pilot call the candidates.

Requirements: Python 3.14, the packages of requirements.txt for the defaults of
              1_classify_repos.py
Run with:  python3 -m unittest test_costs -v
"""

import contextlib
import csv
import importlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import costs  # noqa: E402
import endpoints  # noqa: E402
import label_cache  # noqa: E402
from costs import PriceError  # noqa: E402
from label_cache import CacheError  # noqa: E402

RID = "2026-10-23T093036Z"
PRICES = ("model,service_tier,uncached_input_usd_per_million,cached_input_usd_per_million,"
          "cache_write_usd_per_million,output_usd_per_million,doc_url,doc_verified,notes\n"
          "gpt-6-luna,flex,1000,100,2000,10000,https://prices.example/,2026-10-28,\n"
          "gpt-6-luna,default,4000,400,5000,20000,https://prices.example/,2026-10-28,\n"
          "deepseek/deepseek-v4.1-flash,morph/fp8,31,4,31,270,https://prices.example/,2026-10-30,"
          "\n"
          "deepseek/deepseek-v4.1-flash,wafer/us,45,6,45,500,https://prices.example/,2026-10-30,"
          "\n")
FLEX = ("classifying_model", "gpt-6-luna", "flex", "flex")
FLEX_SERVED_AS_DEFAULT = ("classifying_model", "gpt-6-luna", "flex", "default")
SUMMARY = ("summary_model", "gpt-6-luna", "auto", "default")


def _counts(requests, given, cached, written, output):
    return {"requests": requests, "input_tokens": given, "cached_tokens": cached,
            "cache_write_tokens": written, "output_tokens": output, "reasoning_tokens": 0}


class _CostCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "labels"
        self.prices = self.tmp / "model_prices.csv"
        self.prices.write_text(PRICES, encoding="utf-8")

    def index(self, given=0, cached=0, output=0, summary_input=0, summary_output=0,
              summary_model=None, model="gpt-6-luna", recorded=None):
        """The index row of RID with these tokens and the classifying model `model`. With
        `summary_model`, the row names a record of its classification parameters, which are
        `summary_model` as the summary model and the entries of `recorded`. Without
        `summary_model`, the row names no record."""
        parameters = (label_cache.record("parameters", {"summary": {"model": summary_model},
                                                        **(recorded or {})}, self.cache)
                      if summary_model else "0" * 64)
        label_cache.put_index_row({"run_id": RID, "outcome": "finished", "model": model,
                                   "parameters": parameters, "input_tokens": given,
                                   "cached_tokens": cached, "output_tokens": output,
                                   "summary_input_tokens": summary_input,
                                   "summary_output_tokens": summary_output}, self.cache)

    def record(self, service_tier=None):
        return costs.cost_record(self.cache, RID, service_tier, self.prices)

    @staticmethod
    def costs_of(records):
        return [(r["role"], r["service_tier"], r["billed_as"], r["tokens"], r["usd_per_million"],
                 r["usd"]) for r in records]


class TestPrices(unittest.TestCase):

    def test_the_default_models_of_a_run_have_a_price_on_each_tier_that_a_run_uses(self):
        """The default models of a run are OpenRouter models, which are billed at their pinned
        endpoint. A run with an OpenAI model labels on flex by default or on the standard tier
        with `--openai-service-tier default`, and the requests of its page summaries name no
        tier, which gives them the standard tier. Every price names the page and the date on
        which it was read."""
        defaults = importlib.import_module("1_classify_repos").build_parser()
        prices = costs.read_prices()
        for model in (defaults.get_default("model"), defaults.get_default("summary_model")):
            # An OpenRouter model is billed at its pinned endpoint, which names no OpenAI tier.
            found = endpoints.endpoint(model)
            for tier in ((found.provider,) if found.openrouter else ("flex", "default")):
                with self.subTest(model=model, tier=tier):
                    self.assertIn((model, tier), prices)
        for (model, tier), price in prices.items():
            with self.subTest(price=(model, tier)):
                # An OpenRouter model is priced at its pinned endpoint, from OpenRouter's list of
                # the model's endpoints, and an OpenAI model from OpenAI's pricing page.
                page = (f"https://openrouter.ai/api/v1/models/{model}/endpoints" if "/" in model
                        else "https://developers.openai.com/api/docs/pricing")
                self.assertEqual(price["doc_url"], page)
                self.assertRegex(price["doc_verified"], r"^\d{4}-\d{2}-\d{2}$")

    def test_each_backup_provider_has_its_price(self):
        """A backup provider serves the requests of a model while its pinned provider fails,
        and the cost record prices its tokens at its own prices (`endpoints.BACKUPS`)."""
        prices = costs.read_prices()
        for model, backup in endpoints.BACKUPS.items():
            with self.subTest(model=model):
                self.assertIn((model, backup.provider), prices)

    def test_the_backup_providers_have_the_prices_of_the_endpoint_lists(self):
        """OpenRouter's endpoint lists of 2026-11-04 give deepinfra/fp8 of DeepSeek V4.1 Flash
        "prompt": "0.00000014", "input_cache_read": "0.0000000042" and "completion":
        "0.00000042", and gmicloud/fp8 of GLM-5.3 "prompt": "0.0000007", "input_cache_read":
        "0.00000013" and "completion": "0.0000022", in USD per token. A cache write costs the
        input price."""
        prices = costs.read_prices()
        for model, provider, listed in (
                ("deepseek/deepseek-v4.1-flash", "deepinfra/fp8", ("0.14", "0.0042", "0.42")),
                ("z-ai/glm-5.3", "gmicloud/fp8", ("0.70", "0.13", "2.20"))):
            with self.subTest(provider=provider):
                self.assertEqual(endpoints.BACKUPS[model].provider, provider)
                uncached, cached, output = (costs.Decimal(price) for price in listed)
                self.assertEqual({rate: prices[(model, provider)][rate]
                                  for rate in costs.RATES},
                                 {"uncached_input": uncached, "cached_input": cached,
                                  "cache_write": uncached, "output": output})

    def test_each_endpoint_that_the_pilot_called_has_its_price(self):
        """The price file has a price for each endpoint on which the pilot of 2026-10-30 called a
        candidate model. These endpoints are in fp8 or in a 4-bit quantization,
        except `together` of Qwen3.8 2.4T-A95B, which states no quantization."""
        prices = costs.read_prices()
        for model, endpoint in (("deepseek/deepseek-v4.1-flash", "morph/fp8"),
                                ("z-ai/glm-5.3-flash", "morph/fp8"),
                                ("qwen/qwen3.8-2.4t-a95b", "siliconflow/fp8"),
                                ("qwen/qwen3.8-2.4t-a95b", "together"),
                                ("moonshotai/kimi-k3", "morph/fp8"),
                                ("deepseek/deepseek-v4.1-flash", "wafer/us"),
                                ("z-ai/glm-5.3-flash", "wafer/nvfp4"),
                                ("moonshotai/kimi-k3", "wafer/us")):
            with self.subTest(model=model):
                self.assertIn((model, endpoint), prices)

    def test_a_price_file_with_a_second_price_an_invalid_value_or_other_columns_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        header, flex = PRICES.splitlines()[:2]
        for name, text in (("second", f"{header}\n{flex}\n{flex}\n"),
                           ("number", f"{header}\n{flex.replace('1000', 'ten', 1)}\n"),
                           ("date", f"{header}\n{flex.replace(',2026-10-28,', ',,')}\n"),
                           ("columns", "model,service_tier,price\ngpt-6-luna,flex,1\n")):
            with self.subTest(name=name):
                path = tmp / f"{name}.csv"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(PriceError):
                    costs.read_prices(path)


class TestRecord(_CostCase):

    def test_recorded_tokens_are_priced_at_the_tier_that_served_them(self):
        """The classifying model's requests named flex, and one was served on the standard
        tier. Uncached input is the input less the cached tokens and the cache writes. With the
        index counting the same tokens, no row needs a note."""
        label_cache.add_tokens(self.cache, RID, {
            FLEX: _counts(3, 1000, 200, 300, 100),
            FLEX_SERVED_AS_DEFAULT: _counts(1, 400, 0, 100, 50),
            SUMMARY: _counts(2, 600, 100, 200, 30)})
        self.index(given=1400, cached=200, output=150, summary_input=600, summary_output=30,
                   summary_model="gpt-6-luna")
        records = self.record()
        self.assertEqual(self.costs_of(records), [
            ("classifying_model", "default", "uncached_input", "300", "4000", "1.2"),
            ("classifying_model", "default", "cache_write", "100", "5000", "0.5"),
            ("classifying_model", "default", "output", "50", "20000", "1"),
            ("classifying_model", "flex", "uncached_input", "500", "1000", "0.5"),
            ("classifying_model", "flex", "cached_input", "200", "100", "0.02"),
            ("classifying_model", "flex", "cache_write", "300", "2000", "0.6"),
            ("classifying_model", "flex", "output", "100", "10000", "1"),
            ("summary_model", "default", "uncached_input", "300", "4000", "1.2"),
            ("summary_model", "default", "cached_input", "100", "400", "0.04"),
            ("summary_model", "default", "cache_write", "200", "5000", "1"),
            ("summary_model", "default", "output", "30", "20000", "0.6")])
        self.assertEqual({(r["price_url"], r["price_read"], r["note"]) for r in records},
                         {("https://prices.example/", "2026-10-28", "")})
        self.assertEqual(str(costs.total(records)), "7.66")

    def test_a_response_without_a_tier_is_priced_at_the_tier_that_its_request_named(self):
        """A request that names no tier, or names auto, gets the project's tier, which is the
        standard tier unless the project is configured otherwise."""
        label_cache.add_tokens(self.cache, RID, {
            ("classifying_model", "gpt-6-luna", "flex", ""): _counts(1, 100, 0, 0, 10),
            ("summary_model", "gpt-6-luna", "auto", ""): _counts(1, 50, 0, 0, 5)})
        self.index(given=100, output=10, summary_input=50, summary_output=5,
                   summary_model="gpt-6-luna")
        records = self.record()
        self.assertEqual(self.costs_of(records), [
            ("classifying_model", "flex", "uncached_input", "100", "1000", "0.1"),
            ("classifying_model", "flex", "output", "10", "10000", "0.1"),
            ("summary_model", "default", "uncached_input", "50", "4000", "0.2"),
            ("summary_model", "default", "output", "5", "20000", "0.1")])
        self.assertIn("The responses reported no service tier. The tokens are priced at flex, "
                      "the tier that their requests named.", records[0]["note"])
        self.assertIn("priced at default, the tier that their requests named, which leaves the "
                      "tier to the OpenAI project", records[2]["note"])

    def test_tokens_that_only_the_index_counts_are_priced_at_the_tier_of_the_requests(self):
        """An invocation that a kill ended added nothing to the tokens file, and the index
        counts its tokens from the partial file. Those tokens are priced at the tier that the
        requests of the tokens file named. Because `index.csv` does not separate the cache
        writes among those tokens from the other uncached input, the note of their uncached
        input row states that its 160 tokens would cost $0.32 if the API wrote all of them to
        the cache."""
        label_cache.add_tokens(self.cache, RID, {FLEX: _counts(1, 100, 10, 50, 20)})
        self.index(given=300, cached=50, output=60, summary_model="gpt-6-luna")
        records = self.record()
        self.assertEqual(self.costs_of(records), [
            ("classifying_model", "flex", "uncached_input", "40", "1000", "0.04"),
            ("classifying_model", "flex", "cached_input", "10", "100", "0.001"),
            ("classifying_model", "flex", "cache_write", "50", "2000", "0.1"),
            ("classifying_model", "flex", "output", "20", "10000", "0.2"),
            ("classifying_model", "flex", "uncached_input", "160", "1000", "0.16"),
            ("classifying_model", "flex", "cached_input", "40", "100", "0.004"),
            ("classifying_model", "flex", "output", "40", "10000", "0.4")])
        self.assertEqual([r["note"] for r in records[:4]], [""] * 4)
        self.assertTrue(all(r["note"].startswith(
            "The tokens file of the run does not contain these tokens of index.csv, such as the "
            "tokens of an invocation that a kill ended. They are priced at flex, the tier that "
            "the requests of the tokens file named.") for r in records[4:]))
        self.assertIn("and the tokens of this row cost $0.32 if the API wrote all of them to the "
                      "cache.", records[4]["note"])

    def test_a_run_without_a_tokens_file_is_priced_from_the_index(self):
        """In a run without a tokens file, the classifying model's tokens are priced at the
        default of --openai-service-tier, flex, or at the tier that `service_tier` names, and
        the tokens of the page summaries at the standard tier. The index counts no cached tokens
        of the page summaries."""
        self.index(given=1000, cached=400, output=100, summary_input=500, summary_output=50,
                   summary_model="gpt-6-luna")
        records = self.record()
        self.assertEqual(self.costs_of(records), [
            ("classifying_model", "flex", "uncached_input", "600", "1000", "0.6"),
            ("classifying_model", "flex", "cached_input", "400", "100", "0.04"),
            ("classifying_model", "flex", "output", "100", "10000", "1"),
            ("summary_model", "default", "uncached_input", "500", "4000", "2"),
            ("summary_model", "default", "output", "50", "20000", "1")])
        self.assertIn("The run has no tokens file, and index.csv records these tokens without "
                      "their service tier. They are priced at flex, the default of "
                      "--openai-service-tier of 1_classify_repos.py.", records[0]["note"])
        self.assertIn("the tokens of this row cost $1.2 if the API wrote all of them",
                      records[0]["note"])
        self.assertIn("index.csv records no cached tokens of the page summaries",
                      records[3]["note"])
        self.assertEqual([r[5] for r in self.costs_of(self.record("default"))[:3]],
                         ["2.4", "0.16", "2"])

    def test_the_summary_model_of_a_run_without_its_parameters_is_the_default(self):
        """The record of the classification parameters names the summary model. Without it, the
        model is the default of --summary-model, and the note says so."""
        self.index(summary_input=500, summary_output=50, summary_model="gpt-6.1-sol")
        self.assertEqual({r["model"] for r in self.record()}, {"gpt-6.1-sol"})
        self.index(summary_input=500, summary_output=50)
        records = self.record()
        self.assertEqual({(r["model"], r["service_tier"]) for r in records},
                         {("deepseek/deepseek-v4.1-flash", "morph/fp8")})
        self.assertTrue(all(r["usd"] for r in records))
        self.assertIn("The cache has no record of the classification parameters of the run, and "
                      "the model is deepseek/deepseek-v4.1-flash, the default of --summary-model",
                      records[0]["note"])

    def test_tokens_that_only_the_index_counts_are_priced_at_the_endpoint_of_an_openrouter_model(
            self):
        """OpenRouter bills a model at its pinned endpoint, which the tokens file records as the
        tier. The tokens that only the index counts, such as those of an invocation that a kill
        ended, are priced there too, rather than at an OpenAI tier, which has no price for an
        OpenRouter model."""
        self.index(given=1000, output=100, summary_input=500, summary_output=50,
                   model="deepseek/deepseek-v4.1-flash",
                   summary_model="deepseek/deepseek-v4.1-flash",
                   recorded={"summary": {"model": "deepseek/deepseek-v4.1-flash",
                                         "provider": "morph/fp8", "quantization": "fp8",
                                         "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash"},
                             "classifying_model": {
                                 "model": "deepseek/deepseek-v4.1-flash",
                                 "provider": "wafer/us", "quantization": "fp8",
                                 "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash"}})
        records = self.record()
        self.assertEqual({(r["role"], r["service_tier"]) for r in records},
                         {("classifying_model", "wafer/us"), ("summary_model", "morph/fp8")})
        self.assertTrue(all(r["usd"] for r in records))

    def test_the_parameters_without_a_provider_price_the_tokens_beyond_the_file_as_served(self):
        """Since 2026-11-04, the classification parameters name no provider. The tokens that
        only the index counts, such as those of an invocation that a kill ended, are priced at
        the provider that served most of the model's input tokens in the tokens file, here the
        backup provider, and not at the provider that endpoints.py pins today. A model of which
        the tokens file names no provider, such as the summary model here, is priced at the
        pinned provider of its weights, and a model without one has no price."""
        label_cache.add_tokens(self.cache, RID, {
            ("classifying_model", "deepseek/deepseek-v4.1-flash", "deepinfra/fp8",
             "deepinfra/fp8"): _counts(1, 300, 0, 0, 30),
            ("classifying_model", "deepseek/deepseek-v4.1-flash", "morph/fp8", "morph/fp8"):
                _counts(1, 100, 0, 0, 10),
            ("classifying_model", "z-ai/glm-5.3", "morph/fp8", "morph/fp8"):
                _counts(1, 900, 0, 0, 90)})
        weights = {"model": "deepseek/deepseek-v4.1-flash", "quantization": "fp8",
                   "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash"}
        self.index(given=2000, output=200, summary_input=500, summary_output=50,
                   model="deepseek/deepseek-v4.1-flash",
                   summary_model="deepseek/deepseek-v4.1-flash",
                   recorded={"summary": weights, "classifying_model": weights})
        records = self.record()
        self.assertEqual({(r["role"], r["model"], r["service_tier"], r["tokens"])
                          for r in records},
                         {("classifying_model", "deepseek/deepseek-v4.1-flash", "deepinfra/fp8",
                           "300"),
                          ("classifying_model", "deepseek/deepseek-v4.1-flash", "deepinfra/fp8",
                           "30"),
                          ("classifying_model", "deepseek/deepseek-v4.1-flash", "morph/fp8",
                           "100"),
                          ("classifying_model", "deepseek/deepseek-v4.1-flash", "morph/fp8",
                           "10"),
                          ("classifying_model", "z-ai/glm-5.3", "morph/fp8", "900"),
                          ("classifying_model", "z-ai/glm-5.3", "morph/fp8", "90"),
                          ("classifying_model", "deepseek/deepseek-v4.1-flash", "deepinfra/fp8",
                           "700"),
                          ("classifying_model", "deepseek/deepseek-v4.1-flash", "deepinfra/fp8",
                           "70"),
                          ("summary_model", "deepseek/deepseek-v4.1-flash", "morph/fp8", "500"),
                          ("summary_model", "deepseek/deepseek-v4.1-flash", "morph/fp8", "50")})
        [beyond] = [r for r in records if r["tokens"] == "700"]
        self.assertIn("They are priced at deepinfra/fp8, the provider that served most of the "
                      "input tokens of the model in the tokens file", beyond["note"])
        [summary] = [r for r in records if r["tokens"] == "500"]
        self.assertIn("They are priced at morph/fp8, the pinned provider of the weights of the "
                      "summary model, because the tokens file names no provider of it",
                      summary["note"])
        unpinned = {"model": "deepseek/deepseek-v4.1-flash", "quantization": "mxfp4",
                    "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash"}
        self.index(given=1000, output=100, summary_input=500, summary_output=50,
                   model="deepseek/deepseek-v4.1-flash",
                   summary_model="deepseek/deepseek-v4.1-flash",
                   recorded={"summary": unpinned, "classifying_model": unpinned})
        [summary] = [r for r in self.record() if r["tokens"] == "500"]
        self.assertEqual((summary["service_tier"], summary["usd"]), ("", ""))
        self.assertIn("They have no price, because neither the classification parameters nor "
                      "the tokens file name a provider of the summary model", summary["note"])

    def test_a_model_without_a_price_is_left_out_of_the_total(self):
        label_cache.add_tokens(self.cache, RID, {
            ("classifying_model", "gpt-7", "flex", "flex"): _counts(1, 100, 0, 0, 10),
            FLEX: _counts(1, 100, 0, 0, 10)})
        self.index(given=200, output=20, summary_model="gpt-6-luna")
        records = self.record()
        unpriced = [r for r in records if r["model"] == "gpt-7"]
        self.assertEqual([(r["tokens"], r["usd_per_million"], r["usd"]) for r in unpriced],
                         [("100", "", ""), ("10", "", "")])
        self.assertIn("model_prices.csv has no price for gpt-7 on flex, and the total leaves the "
                      "tokens out.", unpriced[0]["note"])
        self.assertEqual(str(costs.total(records)), "0.2")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            costs.record_cost(self.cache, RID, prices_path=self.prices)
        self.assertIn("110 tokens have no price in model_prices.csv, and the total leaves them out",
                      err.getvalue())

    def test_the_command_writes_the_record_of_a_run_and_logs_one_line_with_its_total(self):
        label_cache.add_tokens(self.cache, RID, {FLEX: _counts(2, 2000, 500, 500, 100)})
        self.index(given=2000, cached=500, output=100, summary_model="gpt-6-luna")
        output = self.tmp / "record.csv"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = costs.main(["--run", RID, "--cache-dir", str(self.cache),
                               "--prices", str(self.prices), "--output", str(output)])
        self.assertEqual(code, 0, err.getvalue())
        data = output.read_bytes()
        self.assertNotIn(b"\r\n", data)
        with open(output, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(reader.fieldnames, costs.COST_COLUMNS)
        self.assertEqual([(r["billed_as"], r["usd"]) for r in rows],
                         [("uncached_input", "1"), ("cached_input", "0.05"),
                          ("cache_write", "1"), ("output", "1")])
        self.assertEqual(err.getvalue(),
                         f"INFO repository_labeling: Run {RID} cost $3.05 at the prices of "
                         f"model_prices.csv, read on 2026-10-28. Wrote "
                         f"the cost by model, service tier and rate to {output}.\n")
        self.assertFalse(label_cache.cost_path(self.cache, RID).exists())
        with contextlib.redirect_stderr(io.StringIO()) as refused:
            code = costs.main(["--run", "2026-11-01T000000Z", "--cache-dir", str(self.cache),
                               "--prices", str(self.prices)])
        self.assertEqual(code, 1)
        self.assertRegex(refused.getvalue(), re.compile(
            r"^ERROR repository_labeling: Could not write the cost record of run "
            r"2026-11-01T000000Z \(the index of .* has no run 2026-11-01T000000Z\)\.\n$"))

    def test_a_run_that_sent_no_request_has_an_empty_record(self):
        self.index(summary_model="gpt-6-luna")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            records = costs.record_cost(self.cache, RID, prices_path=self.prices)
        self.assertEqual(records, [])
        self.assertEqual(label_cache.cost_path(self.cache, RID).read_text(encoding="utf-8"),
                         ",".join(costs.COST_COLUMNS) + "\n")
        self.assertIn(f"Run {RID} sent no request to a model and cost nothing", err.getvalue())

    def test_a_record_that_cannot_be_written_logs_a_warning_and_raises_no_error(self):
        """No step reads the record, and the run has written its labels when it writes the
        record."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            costs.record_or_warn(self.cache, RID)
        self.assertRegex(err.getvalue(), r"^WARNING repository_labeling: Could not write the cost "
                                         r"record of run " + RID)
        with self.assertRaises(CacheError):
            costs.cost_record(self.cache, RID, prices_path=self.prices)


if __name__ == "__main__":
    unittest.main()
