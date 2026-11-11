#!/usr/bin/env python3
"""The cost record of a run of the classification: the tokens of its requests and of the
comparison of its labels with the previous run, at the prices of `config/model_prices.csv`. The
record counts the tokens by the role of the model, the model that the requests named, the service
tier that OpenAI billed or the provider that served an OpenRouter model, its pinned provider or
its backup provider, and the rate at which
OpenAI or OpenRouter bills them. The prices cite OpenAI's pricing page and OpenRouter's lists of
the endpoints of the models.

A run writes its cost record into the cache of labels as it ends, finished or stopped, and the
comparison with the previous run writes it again as the comparison ends, because the requests of
the comparison are the run's too. The command writes the cost record of any run in the index of a
cache of labels:

    python3 costs.py --run 2026-10-23T093036Z [--cache-dir DIR] [--output FILE]

The tokens come from the tokens file of the run (`label_cache.read_tokens`), which records what
each response reported. The index sums the same tokens without their service tier and without
telling apart the tokens that the API wrote to its prompt cache. The record prices what the index
counts beyond the tokens file, such as the tokens of an invocation that a kill ended or of a run
from before the tokens file, at an assumed tier, and its notes state each assumption.

Inputs:  index.csv, <id>.tokens.csv and parameters/<sha256>.json of the cache of labels, and
         config/model_prices.csv
Outputs: <id>.cost.csv in the cache of labels, or the file that --output names, and one log line
         with the total
"""

from __future__ import annotations

import argparse
import csv
import importlib
import io
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import endpoints  # noqa: E402
import label_cache  # noqa: E402
from label_cache import CLASSIFYING_MODEL, SUMMARY_MODEL, CacheError  # noqa: E402

from agentcontext import caches, logs  # noqa: E402

COMPONENT = "repository_labeling"
PRICES = HERE / "config" / "model_prices.csv"
PRICE_COLUMNS = ["model", "service_tier", "uncached_input_usd_per_million",
                 "cached_input_usd_per_million", "cache_write_usd_per_million",
                 "output_usd_per_million", "doc_url", "doc_verified", "notes"]
# The rates at which OpenAI bills the tokens of a request, each with its column of the prices.
# The input tokens of a request are billed at one of the first three: "Cache-write pricing is not
# an additive fee: input tokens use the uncached-input, cached-input, or cache-write rate"
# (developers.openai.com/api/docs/guides/prompt-caching). The output tokens include the
# reasoning tokens, which "are billed as output tokens"
# (developers.openai.com/api/docs/guides/reasoning).
RATES = {"uncached_input": "uncached_input_usd_per_million",
         "cached_input": "cached_input_usd_per_million",
         "cache_write": "cache_write_usd_per_million",
         "output": "output_usd_per_million"}
COST_COLUMNS = ["role", "model", "service_tier", "billed_as", "tokens", "usd_per_million", "usd",
                "price_url", "price_read", "note"]
# The tier of a request that names no tier or names auto, the setting of the OpenAI project:
# "Unless otherwise configured, the Project will use 'default'" (openai 3.13.0,
# `types/responses/response.py`).
PROJECT_TIER = "default"
UNNAMED_TIERS = ("", "auto")
# The columns of the index with the input, cached and output tokens of each role. The index
# records no cached tokens of the page summaries.
INDEX_TOKENS = {CLASSIFYING_MODEL: ("input_tokens", "cached_tokens", "output_tokens"),
                SUMMARY_MODEL: ("summary_input_tokens", "", "summary_output_tokens")}
MILLION = Decimal(1_000_000)


class PriceError(Exception):
    """The price file cannot be read, or it has other columns or an invalid row."""


def read_prices(path: Path = PRICES) -> dict:
    """The prices of `path` by (model, service tier): the rate of each of RATES in USD per
    million tokens, with the page and the date of the price."""
    path = Path(path)
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise PriceError(f"{path} cannot be read ({error})") from error
    if reader.fieldnames != PRICE_COLUMNS:
        raise PriceError(f"{path} has the columns {reader.fieldnames}, not {PRICE_COLUMNS}")
    prices = {}
    for number, row in enumerate(rows, 2):
        key = (row["model"], row["service_tier"])
        if key in prices:
            raise PriceError(f"line {number} of {path} gives {key[0]} on {key[1]} a second price")
        try:
            rates = {rate: Decimal(row[column]) for rate, column in RATES.items()}
        except InvalidOperation as error:
            raise PriceError(f"line {number} of {path} has a price that is not a number "
                             f"({error!r})") from error
        if any(not value.is_finite() or value < 0 for value in rates.values()):
            raise PriceError(f"line {number} of {path} has a price below 0 or not finite")
        if not row["doc_url"] or not row["doc_verified"]:
            raise PriceError(f"line {number} of {path} names no page or no date for its price")
        prices[key] = {**rates, "doc_url": row["doc_url"], "doc_verified": row["doc_verified"]}
    return prices


def _count(row: dict, column: str) -> int:
    return int(row.get(column) or 0) if column else 0


def tier_of(requested: str) -> tuple[str, str]:
    """The tier that prices a request that named `requested`, and what the note adds about it."""
    if requested in UNNAMED_TIERS:
        return PROJECT_TIER, (", which leaves the tier to the OpenAI project, and a project uses "
                              "the standard tier unless it is configured otherwise")
    return requested, ""


def most_tokens(recorded: list, role: str, model: str) -> str:
    """The tier that the requests of `model` in `role` in the rows of a tokens file named with
    the most input tokens, which for an OpenRouter model is the provider that served them, or
    ""."""
    named = sorted((row for row in recorded if row["role"] == role and row["model"] == model),
                   key=lambda row: row["input_tokens"], reverse=True)
    return named[0]["requested_tier"] if named else ""


def beyond_tier(found: endpoints.Endpoint, recorded: list, role: str, whose: str) -> tuple:
    """The provider at which the tokens of the OpenRouter model at `found` in `role` beyond the
    tokens file are priced, and the sentence of the note that says why: the provider that the
    recorded classification parameters name, as parameters recorded before 2026-11-04 do, else
    the provider that served most of the model's input tokens in the tokens file
    (`most_tokens`), the pinned provider or its backup provider, else the pinned provider of the
    weights today (`pinned`). The provider pinned today need not be the one that served the run.
    `whose` names the model in the note, such as "the summary model"."""
    if found.provider:
        return found.provider, (f" They are priced at {found.provider}, the pinned endpoint of "
                                f"{whose}.")
    served = most_tokens(recorded, role, found.model)
    if served:
        return served, (f" They are priced at {served}, the provider that served most of the "
                        f"input tokens of {whose} in the tokens file.")
    today = pinned(found).provider
    if not today:
        return "", (f" They have no price, because neither the classification parameters nor "
                    f"the tokens file name a provider of {whose} and endpoints.py pins none.")
    return today, (f" They are priced at {today}, the pinned provider of the weights of {whose}, "
                   f"because the tokens file names no provider of it.")


def token_rows(index_row: dict, recorded: list, run_tier: str, tier_source: str,
               summary: endpoints.Endpoint, model_source: str = "",
               classifying: endpoints.Endpoint | None = None) -> list[dict]:
    """The tokens of run `index_row` by role, model, service tier and rate, from `recorded`, the
    rows of its tokens file, and from what the index counts beyond them.

    The tokens beyond the tokens file of an OpenRouter model are priced at the provider that
    `beyond_tier` gives, the provider that served most of the model's tokens in the file where
    the recorded parameters name none. The provider that served each response is its tier in
    the tokens file, the pinned provider or its backup provider. For an OpenAI model, they are
    priced at `run_tier` for
    the classifying model, with `tier_source` saying where that tier comes from, and at
    PROJECT_TIER for the summaries, which name no tier. `summary` and `classifying` are the
    endpoints of the run's models, and `classifying` defaults to the endpoint of the index's model.
    `model_source` says where the summary model comes from when no record of the run's
    classification parameters names it. A row that only the index gives has `unsplit` set when its
    uncached input tokens may include cache writes."""
    classifying = classifying or endpoints.endpoint(index_row["model"])
    rows, counted = [], {role: [0, 0, 0] for role in INDEX_TOKENS}
    for row in recorded:
        tier, note = row["service_tier"], ""
        if not tier:
            tier, more = tier_of(row["requested_tier"])
            note = (f"The responses reported no service tier. The tokens are priced at {tier}, "
                    f"the tier that their requests named{more}.")
        cached, written = row["cached_tokens"], row["cache_write_tokens"]
        uncached = max(0, row["input_tokens"] - cached - written)
        for rate, tokens in (("uncached_input", uncached), ("cached_input", cached),
                             ("cache_write", written), ("output", row["output_tokens"])):
            if tokens:
                rows.append({"role": row["role"], "model": row["model"], "service_tier": tier,
                             "billed_as": rate, "tokens": tokens, "note": note})
        if row["role"] in counted:
            for position, name in enumerate(("input_tokens", "cached_tokens", "output_tokens")):
                counted[row["role"]][position] += row[name]
    for role, columns in INDEX_TOKENS.items():
        given, cached, output = (max(0, _count(index_row, column) - done)
                                 for column, done in zip(columns, counted[role]))
        if not (given or output):
            continue
        what = ("The tokens file of the run does not contain these tokens of index.csv, such as "
                "the tokens of an invocation that a kill ended." if recorded else
                "The run has no tokens file, and index.csv records these tokens without their "
                "service tier.")
        if role == CLASSIFYING_MODEL and classifying.openrouter:
            model = classifying.model
            tier, how = beyond_tier(classifying, recorded, role, "the model")
        elif role == CLASSIFYING_MODEL:
            model = index_row["model"]
            tier, more = tier_of(run_tier)
            how = f" They are priced at {tier}, {tier_source}{more}."
        else:
            model = summary.model
            if summary.openrouter:
                tier, how = beyond_tier(summary, recorded, role, "the summary model")
            else:
                tier = PROJECT_TIER
                how = (f" They are priced at {PROJECT_TIER}, the tier that a page summary gets, "
                       f"because its request names no tier and a project uses the standard tier "
                       f"unless it is configured otherwise.")
            if model_source:
                how += f" {model_source}"
        note = what + how
        uncached_note = note
        if role == SUMMARY_MODEL:
            uncached_note += (" index.csv records no cached tokens of the page summaries, and all "
                              "of their input tokens are priced as uncached input.")
        for rate, tokens, row_note in (("uncached_input", max(0, given - cached), uncached_note),
                                       ("cached_input", cached, note),
                                       ("output", output, note)):
            if tokens:
                rows.append({"role": role, "model": model, "service_tier": tier,
                             "billed_as": rate, "tokens": tokens, "note": row_note,
                             "unsplit": rate == "uncached_input"})
    return rows


def plain(value: Decimal) -> str:
    """A number of USD without trailing zeros, as the record writes it."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def priced(rows: list, prices: dict, prices_path: Path = PRICES) -> list[dict]:
    """The rows of `token_rows` in COST_COLUMNS, each with its price, its cost and the page and
    the date of its price. A row without a price keeps the tokens and leaves the price and the
    cost empty, and its note says so."""
    records = []
    for row in rows:
        price = prices.get((row["model"], row["service_tier"]))
        note = row["note"]
        record = {**{column: row.get(column, "") for column in COST_COLUMNS},
                  "tokens": str(row["tokens"]), "usd_per_million": "", "usd": "",
                  "price_url": "", "price_read": ""}
        if price is None:
            note = (f"{note} {prices_path.name} has no price for {row['model']} on "
                    f"{row['service_tier']}, and the total leaves the tokens out.").strip()
        else:
            rate = price[row["billed_as"]]
            record.update(usd_per_million=plain(rate),
                          usd=plain(Decimal(row["tokens"]) * rate / MILLION),
                          price_url=price["doc_url"], price_read=price["doc_verified"])
            if row.get("unsplit"):
                most = Decimal(row["tokens"]) * price["cache_write"] / MILLION
                note = (f"{note} index.csv does not tell apart the tokens that the API wrote to "
                        f"its prompt cache. Those cost ${plain(price['cache_write'])} per million "
                        f"tokens instead of ${plain(rate)}, and the tokens of this row cost "
                        f"${plain(most)} if the API wrote all of them to the cache.")
        record["note"] = note
        records.append(record)
    return records


def pinned(found: endpoints.Endpoint) -> endpoints.Endpoint:
    """`found` with its pinned provider (`endpoints.with_provider`): the provider that
    parameters recorded before 2026-11-04 name, else the pinned provider of the same weights and
    quantization in endpoints.py, else `found` without a provider."""
    try:
        return endpoints.with_provider(found)
    except ValueError:
        return found


def endpoints_of(index_row: dict, cache_dir: Path) -> tuple:
    """The endpoints of the summary model and the classifying model of run `index_row`, from the
    record of its classification parameters, each with the provider that the record names, as a
    record from before 2026-11-04 does (`beyond_tier`), and what a note adds about the summary
    model when the cache has no such record. Without the record,
    the summary model is the default of --summary-model on its default endpoint, and the
    classifying model is the index's model."""
    try:
        parameters = label_cache.recorded("parameters", index_row["parameters"], cache_dir)
        summary = endpoints.Endpoint.of(parameters["summary"])
    except (CacheError, KeyError, TypeError, AttributeError):
        default = _defaults().get_default("summary_model")
        return endpoints.endpoint(default), (
            f"The cache has no record of the classification parameters of the run, and the model "
            f"is {default}, the default of --summary-model of 1_classify_repos.py."), None
    recorded = parameters.get("classifying_model")
    classifying = endpoints.Endpoint.of(recorded) if isinstance(recorded, dict) and \
        recorded.get("model") else None
    return summary, "", classifying


def _defaults() -> argparse.ArgumentParser:
    """The argument parser of `1_classify_repos.py`, with the defaults that give the summary
    model and the service tier of a run when the cache of labels does not record them."""
    return importlib.import_module("1_classify_repos").build_parser()


def run_tier_of(recorded: list, service_tier: str | None) -> tuple[str, str]:
    """The tier at which the classifying model's tokens beyond the tokens file are priced, and
    where it comes from: `service_tier` when it is given, else the tier that the classifying
    model's requests in the tokens file named, else the default of --openai-service-tier."""
    if service_tier:
        return service_tier, "the tier of --openai-service-tier"
    named = sorted((row for row in recorded if row["role"] == CLASSIFYING_MODEL),
                   key=lambda row: row["input_tokens"], reverse=True)
    if named:
        return named[0]["requested_tier"], "the tier that the requests of the tokens file named"
    default = _defaults().get_default("service_tier")
    return default, "the default of --openai-service-tier of 1_classify_repos.py"


def cost_record(cache_dir: Path, rid: str, service_tier: str | None = None,
                prices_path: Path = PRICES) -> list[dict]:
    """The rows of the cost record of run `rid` in the cache of labels `cache_dir`."""
    cache_dir = Path(cache_dir)
    index_row = next((row for row in label_cache.read_index(cache_dir)
                      if row["run_id"] == label_cache.check_id(rid)), None)
    if index_row is None:
        raise CacheError(f"the index of {cache_dir} has no run {rid}")
    recorded = label_cache.read_tokens(cache_dir, rid)
    run_tier, tier_source = run_tier_of(recorded, service_tier)
    rows = token_rows(index_row, recorded, run_tier, tier_source,
                      *endpoints_of(index_row, cache_dir))
    return priced(rows, read_prices(prices_path), Path(prices_path))


def write_record(records: list, path: Path) -> None:
    """Write the rows of a cost record to `path` in one step (`caches.replace_file`)."""
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COST_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    caches.replace_file(Path(path), text.getvalue())


def total(records: list) -> Decimal:
    """The cost of the rows with a price, in USD."""
    return sum((Decimal(record["usd"]) for record in records if record["usd"]), Decimal(0))


def log_record(rid: str, records: list, path: Path, prices_path: Path = PRICES) -> None:
    """The log line of a cost record: the total, the date of the prices and the file, with the
    cost of the tokens priced at an assumed tier and the tokens without a price."""
    if not records:
        logs.info(COMPONENT, f"Run {rid} sent no request to a model and cost nothing. Wrote the "
                             f"empty cost record to {path}.")
        return
    dates = sorted({record["price_read"] for record in records if record["price_read"]})
    read = f", read on {' and '.join(dates)}" if dates else ""
    message = (f"Run {rid} cost ${total(records):,.2f} at the prices of {Path(prices_path).name}"
               f"{read}. Wrote the cost by model, service tier and rate to {path}")
    assumed = total([record for record in records if record["note"]])
    if assumed:
        message += (f". The notes of the record state the assumptions behind ${assumed:,.2f} of "
                    f"the total")
    unpriced = sum(int(record["tokens"]) for record in records if not record["usd"])
    if unpriced:
        message += (f". {unpriced:,} tokens have no price in {Path(prices_path).name}, and the "
                    f"total leaves them out")
    logs.info(COMPONENT, f"{message}.")


def record_cost(cache_dir: Path, rid: str, service_tier: str | None = None,
                output: Path | None = None, prices_path: Path | None = None) -> list[dict]:
    """Write the cost record of run `rid` to `output`, or to its place in the cache of labels,
    at the prices of `prices_path`, PRICES by default, log its line and return its rows."""
    prices_path = Path(prices_path or PRICES)
    records = cost_record(cache_dir, rid, service_tier, prices_path)
    path = Path(output) if output else label_cache.cost_path(Path(cache_dir), rid)
    write_record(records, path)
    log_record(rid, records, path, prices_path)
    return records


def record_or_warn(cache_dir: Path, rid: str, service_tier: str | None = None) -> None:
    """`record_cost` as a run or a comparison ends. When the cost record cannot be written, the
    function logs a WARNING and leaves the outcome of the run as it is, because no step reads the
    record, and a failure here would fail the run after its labels are written."""
    try:
        record_cost(cache_dir, rid, service_tier)
    except (CacheError, PriceError, OSError, ValueError) as error:
        logs.warning(COMPONENT, f"Could not write the cost record of run {rid} ({error}).")


def _sent(row: dict) -> int:
    """The number of repositories in index row `row` that the run neither reused nor found gone."""
    return (int(row.get("repositories") or 0) - int(row.get("reused") or 0)
            - int(row.get("gone") or 0))


def per_repository(cache_dir: Path, model: str, parameters: str, classifying, summary,
                   prices_path: Path | None = None) -> tuple[Decimal | None, str]:
    """The estimated cost of one repository that a run of `model` under `parameters` labels, with
    where the estimate comes from, or None with the reason that there is none: the cost per
    repository of the latest finished run of the model under the parameters, or else the tokens
    per repository of the latest finished run of any model at the prices of `classifying` and
    `summary`, the endpoints (`endpoints.Endpoint`) of the classifying model and the summary
    model. A repository counts when the run neither reused it nor found it gone."""
    prices_path = Path(prices_path or PRICES)
    try:
        rows = [row for row in label_cache.read_index(Path(cache_dir))
                if row["outcome"] == "finished" and _sent(row) > 0]
        prices = read_prices(prices_path)
        # The parameters of a run match by their keys, which leave out the provider.
        keys = label_cache.ParameterKeys(cache_dir)
        same = [row for row in rows
                if row["model"] == model and keys(row["parameters"]) == keys(parameters)]
        if same:
            row = same[-1]
            cost = total(cost_record(cache_dir, row["run_id"], prices_path=prices_path))
            return cost / _sent(row), f"the cost per repository of run {row['run_id']}"
    except (CacheError, PriceError, OSError, ValueError) as error:
        return None, f"the cache of labels or {prices_path.name} cannot be read ({error})"
    if not rows:
        return None, "no finished run in the cache of labels gives the tokens per repository"
    return at_prices(rows[-1], prices, classifying, summary, prices_path)


def at_prices(row: dict, prices: dict, classifying, summary,
              prices_path: Path) -> tuple[Decimal | None, str]:
    """The tokens per repository of the finished run of index row `row` at the prices of
    `classifying` and `summary`, with where the estimate comes from, or None with the reason
    that there is none."""
    def tier(endpoint) -> str:
        return endpoint.provider if endpoint.openrouter else PROJECT_TIER

    model_price = prices.get((classifying.model, tier(classifying)))
    summary_price = prices.get((summary.model, tier(summary)))
    if model_price is None:
        return None, (f"{prices_path.name} has no price of {classifying.model} at "
                      f"{tier(classifying)}")
    given, cached = _count(row, "input_tokens"), _count(row, "cached_tokens")
    cost = (Decimal(max(0, given - cached)) * model_price["uncached_input"]
            + Decimal(cached) * model_price["cached_input"]
            + Decimal(_count(row, "output_tokens")) * model_price["output"])
    if summary_price is not None:
        cost += (Decimal(_count(row, "summary_input_tokens")) * summary_price["uncached_input"]
                 + Decimal(_count(row, "summary_output_tokens")) * summary_price["output"])
    return (cost / MILLION / _sent(row),
            f"the tokens per repository of run {row['run_id']} of {row['model']} at the prices "
            f"of {prices_path.name}")


def per_repository_at(cache_dir: Path, model: str, parameters: str, classifying, summary,
                      prices_path: Path | None = None) -> Decimal | None:
    """The estimated cost of one repository at the prices of `classifying` and `summary`, such
    as the backup providers of the models (`endpoints.BACKUPS`), from the tokens per repository
    of the latest finished run of `model` under `parameters`, or else of any model, or None when
    no run or no price gives one."""
    prices_path = Path(prices_path or PRICES)
    try:
        rows = [row for row in label_cache.read_index(Path(cache_dir))
                if row["outcome"] == "finished" and _sent(row) > 0]
        prices = read_prices(prices_path)
        keys = label_cache.ParameterKeys(cache_dir)
        same = [row for row in rows
                if row["model"] == model and keys(row["parameters"]) == keys(parameters)]
    except (CacheError, PriceError, OSError, ValueError):
        return None
    if not rows:
        return None
    return at_prices((same or rows)[-1], prices, classifying, summary, prices_path)[0]


def main(argv: list | None = None) -> int:
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(description="Write the cost record of a run of the "
                                                 "classification.", allow_abbrev=False)
    parser.add_argument("--run", required=True, help="The run, as index.csv names it")
    parser.add_argument("--cache-dir", type=Path, default=label_cache.CACHE_DIR,
                        help="The cache of labels (default: %(default)s)")
    parser.add_argument("--prices", type=Path, default=PRICES,
                        help="The prices of the models (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=None,
                        help="The file of the cost record (default: <run>.cost.csv in the cache "
                             "of labels)")
    parser.add_argument("--openai-service-tier", dest="service_tier", default=None,
                        help="The service tier that prices the tokens of the classifying model "
                             "where no tokens file records their tier (default: the tier that "
                             "the requests of the tokens file named, else the default of "
                             "--openai-service-tier of 1_classify_repos.py)")
    args = parser.parse_args(argv)
    try:
        record_cost(args.cache_dir, args.run, args.service_tier, args.output, args.prices)
    except (CacheError, PriceError, OSError, ValueError) as error:
        logs.error(COMPONENT, f"Could not write the cost record of run {args.run} ({error}).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
