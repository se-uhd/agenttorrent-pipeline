# The probe of 2026-11-02 of the reasoning effort "medium"

A hash in this record names a commit of the private repository `se-uhd/agenttorrent-pipeline-internal`, which holds the history of this tree, and no check resolves it.

The classification sent the reasoning effort `medium` to DeepSeek V4.1 Flash and GLM-5.3 until
the maintainer's decisions of 2026-11-02.
OpenRouter's list of models, read on 2026-11-02 at https://openrouter.ai/api/v1/models, gives
DeepSeek V4.1 Flash the supported efforts `max`, `high`, `low` and `none` and GLM-5.3 `max`,
`high` and `low`, so neither model supports `medium`. Under its heading "Google Gemini 3 Models
with Thinking Levels", OpenRouter's page on reasoning tokens
(https://openrouter.ai/docs/guides/best-practices/reasoning-tokens, read on 2026-11-02) says "If a
model doesn't support a specific effort level (for example, if a model only supports `low` and
`high`), OpenRouter will map your requested effort to the nearest supported level". The page says
nothing of an effort that another model does not support. Z.ai's model card of GLM-5.3
(https://huggingface.co/zai-org/GLM-5.3, read on 2026-11-02) says that its reasoning effort
"accepts three levels: `low`, `high`, and `max`. It defaults to `max` if not passed (or if set to
any other value)", so a `medium` that reached GLM-5.3 unmapped would run at `max`.
This probe labeled the same repositories with each model at `low`, `medium` and `high`, and with
GLM-5.3 also at `max`, to show which level the runs that sent `medium` got.

Morph's documentation (https://docs.morphllm.com/llms.txt, read on 2026-11-03) says of DeepSeek
V4.1 Flash on its endpoint: "`reasoning_effort` accepts `low`, `high`, `xhigh`, `max`, an integer
from 1 to 100, or `none` to disable it. `medium` maps to `high`; `minimal` maps to `low`." Of GLM
models, it says only which efforts they take, "DeepSeek and GLM models take `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`" and "Unsupported values return 400", and not to which
level of GLM-5.3 it maps `medium`.

## The runs

Each directory holds one run of `2_classification/1_classify_repos.py` at commit `9b7886ee`, named
by its model and the effort that it sent: `deepseek-low`, `deepseek-medium`, `deepseek-high`,
`glm-low`, `glm-medium`, `glm-high` and `glm-max`. Each run labeled the repositories of
[repositories.csv](repositories.csv), the first rows of the check sample of this evaluation
(`check_sample.csv` beside this directory), on the pinned endpoint `morph/fp8` of its model, at
temperature 0.2 with seed 7, the values of `TEMPERATURE` and `SEED` at that commit, with the default
summary model. Its `cache/` is a cache of labels in the format of `2_classification/label_cache.py`
that contains this run alone: `index.csv`, the labels, the decisions, the tokens file, the cost
record and the classification parameters that the labels name. A decision records the reasoning
tokens of its repository under `usage`.

## What the probe shows

At `medium`, each model used about as many reasoning tokens as at `high`, and more than at `low`.
GLM-5.3 used many times as many reasoning tokens at `max` as at `medium`, so its `medium` did not
run at `max` and was mapped before it reached the model. The requests went through OpenRouter to
Morph's endpoint `morph/fp8`. For DeepSeek V4.1 Flash, the provider maps the effort: Morph's
documentation says that the endpoint maps `medium` to `high`, and the reasoning tokens of the
probe agree. For GLM-5.3, the probe does not show whether OpenRouter or Morph's endpoint mapped
it, and Morph's documentation does not say to which level. The earlier runs that sent `medium`, the pilots, the
six-model test and the checks of the temperature among them, therefore ran DeepSeek V4.1 Flash at
`high`, and the reasoning tokens suggest `high` for GLM-5.3 as well. The probe is one run per
level over a few repositories, and the reasoning tokens of one repository vary between runs at the
same settings, so for GLM-5.3 it is an indication and not a measurement of the effort of each
earlier run.
