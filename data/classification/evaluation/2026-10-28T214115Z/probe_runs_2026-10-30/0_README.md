# The runs of 2026-10-30 behind the stricter rule of `read_linked_page`

A hash in this record names a commit of the private repository `se-uhd/agenttorrent-pipeline-internal`, which holds the history of this tree, and no check resolves it.

This directory records the runs of the classification that labeled repositories of the check
sample of this evaluation on 2026-10-30, after the pilot of that day (`prompt_check_2026-10-30.csv`
beside this directory). They show why DeepSeek V4.1 Flash and GLM-5.3 Flash read few linked
pages, and the table at the end names the finding that each run backs.

## The files of a run

Each directory holds one run. Its `cache/` is a cache of labels in the format of
`2_classification/label_cache.py` that contains this run alone: `index.csv` with the row of the
run, the labels (`<id>.labels.csv.xz`), the decisions (`<id>.decisions-001.jsonl.xz`), the tokens
file (`<id>.tokens.csv`), the cost record (`<id>.cost.csv`) and the classification parameters that
the labels name (`parameters/<sha256>.json`). A run is named by the UTC time at which it started.
`label_cache.read_labels` and `label_cache.all_decisions` read the files back and check them
against the SHA-256 that `index.csv` records. A decision records under `usage` the requests, the
rounds of tool calls, the linked pages and the tokens of its repository, and under `tools` each
tool call with its output. The cost record prices the tokens at `config/model_prices.csv` of the
classification as read on 2026-10-30, which had no price for DeepSeek V4.1 Flash 1017, so the cost
records of the `deepseek-1017-*` runs leave their tokens out.

The runs on which a statement about the responses of the model rests also have
`trace.jsonl.xz` beside `cache/`, with one line per response: the repository, the types of the
output items, the name and the first 300 characters of the arguments of each tool call, the first
3,000 characters of the reasoning text and the first 500 characters of a message. A wrapper around
`Classifier.create` of `1_classify_repos.py` wrote it, and no commit contains the wrapper.

## The repositories

The runs `deepseek-medium`, `deepseek-high`, `deepseek-1017-medium`, `deepseek-1017-high` and
`glm-flash-medium` labeled the twelve repositories of the check sample with the lowest ids among
those for which, in the runs of the pilot under the tool of the checklist, `gpt-6.1-sol` called
`read_linked_page` and DeepSeek V4.1 Flash on `morph/fp8` did not. The other runs labeled these
twelve and thirty more repositories of the check sample, the first thirty of the other repositories
of `check_sample.csv` after `random.Random(1030).shuffle`. The labels of each run list its
repositories, and `check_sample.csv` gives their expected labels. The caches of labels of the
pilot are not tracked, so this record cannot show for which repositories `gpt-6.1-sol` called
`read_linked_page`.

## The variants

The parameters of a run name its model, its endpoint and its reasoning effort under
`classifying_model`, and its summary model, the same model, under `summary`. Apart from these
blocks, the parameters of the runs of one variant are the same for every model. The variants
differ as follows:

- The rule of the pilot: the parameters of the runs of the pilot, which `cb6cc7f1` committed. The
  prompt says "Read a page when the input does not settle a field." `config/parameter_digests.csv`
  of the classification records the digest of `gpt-6-luna`, and the column `parameters` of
  `prompt_check_2026-10-30.csv` the digests of DeepSeek V4.1 Flash and GLM-5.3 Flash on
  `morph/fp8`.
- The stricter rule in the prompt: the prompt says instead "Before you submit, read the linked
  pages that could change a field", and the description of `read_linked_page` is that of the
  pilot. No commit contains these parameters.
- DeepSeek's own tags: the stricter rule in the prompt, which also tells the model to invoke
  `read_linked_page` and `submit_classification` in a `<｜DSML｜ calls>` block and says that the
  summary of a page comes back in a `<tool_result>` block. No commit contains these parameters.
- The stricter rule: the stricter rule in the prompt and in the description of
  `read_linked_page`, which `49e53cc0` committed. `config/parameter_digests.csv` records the digest
  of `gpt-6-luna`.
- The extra turn: the parameters of the stricter rule, and a turn that responds once to the first
  `submit_classification` of a repository without a round of tool calls with the request to read
  the linked pages that could change a field. The text of the turn is not among the classification
  parameters. Only the decisions, which count at least two requests for every repository, and the
  traces, in which a second response follows the first `submit_classification` of such a
  repository, show the turn.

The record does not contain the code of a variant that no commit contains, only the outputs and
the parameters of its runs. Runs of two other variants over the same repositories on 2026-10-30
back no statement of the paragraph, and the record leaves them out: one set a field to `unclear`
when the content points both ways, and the other added the two rules that `662f5c81` committed.

## The runs

The parameters column gives the first 12 characters of the digest, which names the file under
`cache/parameters/`. Every DeepSeek V4.1 Flash and GLM-5.3 Flash run went through OpenRouter to the
pinned endpoint.

| Directory | Run | Model | Endpoint | Effort | Parameters | Variant | Finding |
|---|---|---|---|---|---|---|---|
| `deepseek-medium` | `2026-10-30T181443Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `de0650128982` | The rule of the pilot | Their reasoning concludes that the first request settles every field, and the quote "No tools needed; input settles all" (`torakiki/pdfsam`) |
| `glm-flash-medium` | `2026-10-30T181443Z` | `z-ai/glm-5.3-flash` | `morph/fp8` | medium | `9904b09fec8a` | The rule of the pilot | Their reasoning concludes that the first request settles every field |
| `deepseek-high` | `2026-10-30T181909Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | high | `5d6ca36b5f00` | The rule of the pilot | No page at a high reasoning effort |
| `deepseek-1017-medium` | `2026-10-30T181909Z` | `deepseek/deepseek-v4.1-flash-1017` | `morph/fp8` | medium | `6fd3447a3e29` | The rule of the pilot | No page with the build DeepSeek V4.1 Flash 1017 |
| `deepseek-1017-high` | `2026-10-30T181909Z` | `deepseek/deepseek-v4.1-flash-1017` | `morph/fp8` | high | `50eb07716af2` | The rule of the pilot | No page with the build DeepSeek V4.1 Flash 1017 |
| `deepseek-pilot-rule` | `2026-10-30T182209Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `de0650128982` | The rule of the pilot | The rule of the pilot, which the runs under the stricter rule are compared with |
| `luna-pilot-rule` | `2026-10-30T182209Z` | `gpt-6-luna` | OpenAI, default tier | medium | `982d1fb9293e` | The rule of the pilot | The rule of the pilot, which the runs under the stricter rule are compared with |
| `deepseek-stricter-prompt-1` | `2026-10-30T182606Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `97f6e912f839` | The stricter rule in the prompt | Pages for more repositories, with more input tokens, with the same calls as under the rule of the pilot |
| `luna-stricter-prompt` | `2026-10-30T182606Z` | `gpt-6-luna` | OpenAI, default tier | medium | `21e0eba0a474` | The stricter rule in the prompt | Pages for more repositories, with more input tokens |
| `deepseek-stricter-prompt-2` | `2026-10-30T184629Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `97f6e912f839` | The stricter rule in the prompt | Pages for more repositories, with more input tokens, with the same calls as under the rule of the pilot, and the comparison at the same time for DeepSeek's own tags |
| `deepseek-own-tags-1` | `2026-10-30T184629Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `56add8b843a2` | DeepSeek's own tags | DeepSeek's own tags changed nothing measurable |
| `deepseek-own-tags-2` | `2026-10-30T184629Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `56add8b843a2` | DeepSeek's own tags | DeepSeek's own tags changed nothing measurable |
| `deepseek-stricter-1` | `2026-10-30T185327Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The stricter rule | Pages for more repositories, with more input tokens, and the comparison for the extra turn |
| `deepseek-stricter-2` | `2026-10-30T185327Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The stricter rule | Pages for more repositories, with more input tokens, and the comparison for the extra turn |
| `luna-stricter` | `2026-10-30T185327Z` | `gpt-6-luna` | OpenAI, default tier | medium | `d04a0efc30bf` | The stricter rule | Pages for more repositories, with more input tokens, and the comparison for the extra turn |
| `deepseek-extra-turn-1` | `2026-10-30T190557Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The extra turn | The turn added input tokens without better labels |
| `deepseek-extra-turn-2` | `2026-10-30T190557Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The extra turn | The turn added input tokens without better labels |
| `luna-extra-turn` | `2026-10-30T190557Z` | `gpt-6-luna` | OpenAI, default tier | medium | `d04a0efc30bf` | The extra turn | The turn added input tokens without better labels |
| `deepseek-stricter-3` | `2026-10-30T210349Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The stricter rule | Pages for more repositories, with more input tokens |
| `deepseek-stricter-4` | `2026-10-30T210349Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The stricter rule | Pages for more repositories, with more input tokens |
| `deepseek-stricter-5` | `2026-10-30T210349Z` | `deepseek/deepseek-v4.1-flash` | `morph/fp8` | medium | `6c0a184396f0` | The stricter rule | Pages for more repositories, with more input tokens |
