# The cost record of a run

## Context

`costs.py` writes the cost record of a run of the classification: what OpenAI or OpenRouter billed for the requests of the run and of the comparison of its labels with the previous run, in USD. Without the record, the cost of a run can only be estimated from the tokens of the run's row in `../data/classification/labels/index.csv` and from list prices of other sites. The index sums the tokens of a run without the service tier that served each request, and it counts the input tokens that the API wrote to its prompt cache among the uncached input tokens. OpenAI bills both at prices of their own. The cost record applies the prices from OpenAI's pricing page and OpenRouter's model endpoint lists to the reported tokens. Its notes state each assumption made where the cache of labels lacks a fact.

## Key decisions

- **Language:** Python 3.14, in the single module `costs.py`, with the standard library, `label_cache.py` and the package's `logs` and `caches`. Prices are `Decimal`, so that a cost in the record is the exact product of its tokens and its price
- The prices are in `config/model_prices.csv`, one row per model and service tier, with the price in USD per million tokens of uncached input, cached input, cache writes and output, the page that lists the prices in `doc_url`, the date on which it was read in `doc_verified`, and in `notes` the quoted prices and the quoted rules for each column. The page of an OpenAI model is OpenAI's own, `https://developers.openai.com/api/docs/pricing`. An OpenRouter model has one row per pinned endpoint, with the provider slug of the endpoint in `service_tier`, because the endpoints of one model have prices of their own, and its page is OpenRouter's list of the model's endpoints, `https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints`, which gives the price of each endpoint in USD per token. An endpoint without an `input_cache_write` price bills no cache write of its own, and its row prices a cache write at its input price. No price comes from another site. OpenAI's other pricing page, `https://openai.com/api/pricing/`, refuses the documentation watch with HTTP 403, and the watch does not get around that refusal. The weekly documentation watch reads the price file as it reads the heuristics files (`.github/workflows/doc-watch.yml`), so that a changed pricing page opens an issue
- A request is billed at the service tier that served it, which its response reports in `service_tier` and which can differ from the tier that the request named (openai 3.13.0, `types/responses/response.py`). The flex tier is priced at the rates of the Batch API, which the pricing page lists in the tab "Flex/Batch". A request that names no tier, such as a page summary, or names `auto` gets the tier of the OpenAI project, which is the standard tier, `default`, unless the project is configured otherwise. A request to an OpenRouter model routes to one provider without fallbacks, the pinned provider or its backup provider, and the classification records the provider slug of the provider that served it as the tier that the request named and that served it. Reasoning tokens "are considered output tokens and charged accordingly" ([reasoning tokens](https://openrouter.ai/docs/use-cases/reasoning-tokens))
- Every input token of a request is billed at one rate: "Cache-write pricing is not an additive fee: input tokens use the uncached-input, cached-input, or cache-write rate" ([prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)). The uncached input tokens are the input tokens less the cached tokens and the tokens written to the cache, which a response reports as `cached_tokens` and `cache_write_tokens`. The classification sets no lifetime of the cache, and a cache write is billed at the price of the default lifetime of 30 minutes, the column "30m write" of the pricing page. Reasoning tokens "are billed as output tokens" ([reasoning](https://developers.openai.com/api/docs/guides/reasoning)), and the output tokens of a response include them
- The tokens come from the tokens file of the run, `<id>.tokens.csv` in the cache of labels (`label-cache-spec.md`), which counts every response by the role of its model, the model that the request named, the tier that the request named and the tier that the response reported. The record names the model that the request named, because the price file lists the models by those names. The requests of the second model, which labels a repository that the classifying model left without a decision after the retry rounds and a failed response, count under the role of the classifying model with the second model's name and the provider that served them, and the record prices them at the second model's prices. A response that the backup provider of a model served while its pinned provider failed counts under the backup provider, and the record prices it at the backup provider's prices, which the price file lists for `deepinfra/fp8` of DeepSeek V4.1 Flash and `gmicloud/fp8` of GLM-5.3 (`endpoints.BACKUPS`, `classify-repos-spec.md`). Each invocation of a run and of its comparison adds its tokens to the file once, as it ends. An invocation that a kill ended adds nothing, while the index counts its tokens from the lines of the partial file. A run from before the tokens file has no tokens file at all
- The record prices what the index counts beyond the tokens file at an assumed tier and states the assumption in the `note` of each such row. The index counts the tokens of the second model with those of the classifying model, and such tokens of the second model are priced at the classifying model's prices. For the classifying model, the tier is the tier of `--openai-service-tier` when the run or the command names one, else the tier that the requests of the tokens file named, else the default of `--openai-service-tier` of `1_classify_repos.py`. For the page summaries, it is the standard tier. The index separates no cache writes, and the summaries have no cached tokens in the index, so such tokens are priced as uncached input. The note of such a row also gives what the row would cost if the API had written all of its tokens to the cache, the most that they can have cost. The model of the page summaries is the one that the run's record of its classification parameters names, or the default of `--summary-model` when the cache has no such record
- Where the tokens file counts more than the index, for example when a comparison that stopped has not yet added its tokens to the index, the record keeps what the responses reported and adds no row for the difference
- A row with a model or a tier that the price file does not list keeps its tokens, leaves its price and its cost empty, and says so in its note. The total leaves it out, and the log line names the number of such tokens
- The record is written whole through `caches.replace_file`, with LF line endings. A run that sent no request to a model writes a record with no rows. `record_or_warn`, which a run and a comparison call as they end, logs a WARNING for a record that it cannot write and leaves the outcome of the run as it is, because no step reads the record and the run has already written its labels. The price file and the record are no classification parameters, and a change of either one labels no repository again (`classify-repos-spec.md`)

## Module: `costs.py`

### CLI interface

```
python3 costs.py --run RUN [--cache-dir DIR] [--prices FILE] [--output FILE] [--openai-service-tier TIER]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--run` | required | The run, as `index.csv` names it |
| `--cache-dir` | `../data/classification/labels/` | The cache of labels |
| `--prices` | `config/model_prices.csv` | The prices of the models |
| `--output` | `<run>.cost.csv` in the cache of labels | The file of the cost record |
| `--openai-service-tier` | the tier that the requests of the tokens file named, else the default of `--openai-service-tier` of `1_classify_repos.py` | The service tier that prices the tokens of the classifying model where no tokens file records their tier |

### Output

`<run>.cost.csv` has one row per role of the model, model, service tier and rate at which OpenAI or OpenRouter bills the tokens, with the columns `role` (`classifying_model` or `summary_model`), `model`, `service_tier` (the tier that billed the tokens, the endpoint that served an OpenRouter model, its pinned provider or its backup provider, or the assumed tier), `billed_as` (`uncached_input`, `cached_input`, `cache_write` or `output`), `tokens`, `usd_per_million`, `usd`, `price_url` and `price_read` (the `doc_url` and the `doc_verified` of the price), and `note`, which is empty for tokens that the responses reported with their tier. The log line gives the total in USD, the date of the prices and the file, and the part of the total for which the notes state an assumption.

### Function structure

**`read_prices(path)`**, **`PriceError`**: the prices by model and service tier, and the refusal of a file with other columns, a second price for a model and tier, a price that is no number or below 0, or a price without its page or date

**`token_rows(index_row, recorded, run_tier, tier_source, summary, model_source, classifying)`**, **`tier_of(requested)`**, **`run_tier_of(recorded, service_tier)`**, **`endpoints_of(index_row, cache_dir)`**: the tokens by role, model, tier and rate, from the tokens file and from what the index counts beyond it, and the tier and the endpoints at which the tokens that only the index counts are priced, each with its source for the notes. The tokens of an OpenRouter model beyond the tokens file are priced at the provider that a record of the run's classification parameters from before 2026-11-04 names, or at the default endpoint of the default model (`beyond_tier`). A later record names no provider, and the tokens are priced at the provider that served most of the model's input tokens in the tokens file (`most_tokens`), the pinned provider or its backup provider, because the provider pinned today need not be the one that served the run. Without such tokens, they are priced at the pinned provider of the same weights and quantization in `endpoints.py` (`pinned`, `endpoints.with_provider`), and a model without one has no price. **`per_repository(...)`** matches the classification parameters of a run by their keys (`label_cache.ParameterKeys`)

**`priced(rows, prices, prices_path)`**, **`plain(value)`**, **`total(records)`**: the rows with their prices and costs, a number of USD as the record writes it, and the cost of the rows with a price

**`cost_record(cache_dir, rid, service_tier, prices_path)`**, **`write_record(records, path)`**, **`log_record(...)`**, **`record_cost(...)`**, **`record_or_warn(cache_dir, rid, service_tier)`**, **`main(argv)`**: the record of a run, written and logged

**`per_repository(cache_dir, model, parameters, classifying, summary, prices_path)`**: the estimated cost of one repository that a run labels, which a run through OpenRouter compares with the credit of its key before its first request (`classify-repos-spec.md`). The estimate is the cost per repository of the latest finished run of the model under the parameters, or else the tokens per repository of the latest finished run of any model at the prices of the two endpoints. A repository counts when the run neither reused it nor found it gone. Without an estimate, it gives the reason instead: no finished run in the cache of labels, no price of the classifying model's endpoint, or a cache or a price file that cannot be read. **`per_repository_at(...)`** gives the tokens per repository of the latest finished run of the model under the parameters, or else of any model, at the prices of other endpoints, such as the backup providers of the models, which the same comparison states beside the estimate (`at_prices`)

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| The index has no row for the run | `ERROR repository_labeling: Could not write the cost record of run ... (the index of ... has no run ...)`, exit 1. As a run ends, a `WARNING` with the same text |
| The price file has other columns, a second price for a model and tier, a price that is no number, or no page or date for a price | The same `ERROR` or `WARNING`, with the line of the price file |
| A response reported no service tier | Priced at the tier that its request named, the standard tier for a request that named none or `auto`, with a note |
| The index counts tokens that the tokens file lacks | Priced at the assumed tier as uncached input, cached input and output, with a note that gives the assumption and the cost if every uncached input token was a cache write |
| A model or a tier without a price | The row keeps its tokens with an empty price and cost, the note says so, and the total and the log line leave the tokens out |

## Files

| File | Action |
|------|--------|
| `costs.py` | The module, called by `1_classify_repos.py` and `changed_labels.py` |
| `config/model_prices.csv` | Read |
| `../data/classification/labels/index.csv`, `<id>.tokens.csv`, `parameters/<sha256>.json` | Read |
| `../data/classification/labels/<id>.cost.csv` | Written |
| `test_costs.py` | Tests |

## Tests

See `test_costs.py`, which uses the standard library's `unittest`. Each test case writes an index row and a tokens file into a temporary cache of labels, and a price file of round prices, and compares the record with expected costs that were computed by hand from those prices.

- **`TestPrices`** (5): the default models of a run have a price on each tier that a run uses, and each price names its page, OpenAI's pricing page or OpenRouter's list of the endpoints of its model, and its date; each backup provider has its price; the backup providers have the prices of the endpoint lists of 2026-11-04; each candidate of the comparison has the price of its pinned endpoint; a price file with a second price, a price that is no number, a price without its date or other columns is refused
- **`TestRecord`** (11): recorded tokens are priced at the tier that served them, with the cache writes apart from the uncached input; the tokens of an OpenRouter model beyond the tokens file are priced at its endpoint; with classification parameters that name no provider, they are priced at the provider that served most of the model's input tokens in the tokens file, the backup provider among them, and not at the provider pinned today, the tokens of a model that the tokens file names no provider of at the pinned provider of its weights, and those of a model without one have no price; a response without a tier is priced at the tier that its request named, and a request without a tier at the standard tier; tokens that only the index counts are priced at the tier that the requests of the tokens file named, with a note that gives the cost if they were all cache writes; a run without a tokens file is priced from the index at the default of `--openai-service-tier`, or at the tier that `--openai-service-tier` names; the summary model of a run without the record of its parameters is the default; a model without a price is left out of the total; the command writes the record of a run and logs one line with its total, and refuses a run that the index lacks; a run that sent no request has an empty record; a record that cannot be written logs a warning and raises no error

`test_classify_repos.py` and `test_changed_labels.py` test that a run that ends and a comparison that ends write the record.

Run with:

```shell
python3 -m unittest test_costs -v
```

## Known limitations

- The prices are those of the pricing page on the date of `doc_verified`. A run before a change of the prices costs what the prices of its day were, and the record of a run written later prices it at the prices of the file
- For an OpenAI model, the price file lists the prices of the short context length, and a request over the boundary of the long context costs more. Rule 1 of `0_README.md` keeps a request of the classifying model under it by an estimate of OpenAI's tokens, and a page summary crosses it only for a page with text that tokenizes at fewer characters per token than rule 1 assumes. The prices that the file quotes for the pinned endpoints of OpenRouter models have no long context rate. DeepSeek V4.1 Flash counted <!--s:decisions_2026_11_02.largest_single_request_input_tokens-->334,012<!--/s--> input tokens for one request in the check of 2026-11-02, more than the boundary, and the price of its pinned endpoint has one rate for every input token
- The tier of the OpenAI project is taken as the standard tier, as OpenAI documents its default. OpenAI bills the page summaries of a project configured otherwise at another tier
