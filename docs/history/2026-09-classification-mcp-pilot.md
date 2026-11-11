# Classification stage - report


Date: 2026-09-02 (random-400 run added 2026-09-03)

## What the stage does

The classification stage decides for every repository from the sampling
stage whether it is an "engineered software project" or not. Only the
repositories with label `true` go into the data collection stage.

The existing pipeline produces the labels with eight scripts (steps 1-8). For every repository
it clones the repository, takes the README, runs GitHub Linguist, finds
the links in the README, downloads the link content and summarises it.
Then it builds one prompt per repository with all of this information and
sends it to the OpenAI Batch API.

The important point is that this context is computed for **every**
repository, before the model sees anything.

## What was done

I built an experiment (`2_classification/mcp_experiment/`) that does the
same classification, but fetches the extra context **only when the model
asks for it**.

It is an MCP server. The model first gets only the README. If that is not
enough to decide, the model calls a tool, and only then the Linguist data
or the link summaries are computed. The server is a normal MCP server
over stdio, so it can also be used from other MCP clients.

Files:

| File                           | What it does                                                  |
| ------------------------------ | ------------------------------------------------------------- |
| `classification_mcp_server.py` | the MCP server, computes context on demand                    |
| `mcp_client.py`                | connects the server to the OpenAI Responses API               |
| `classify_repos.py`            | the runner, replaces steps 1-8                                |
| `classification_core.py`       | shared parts: prompt loading, response format, usage counting |

The runner writes the same output files as the old pipeline
(`repos_annotated.csv`, `repos_excluded.csv`, `repos_annotated_included.csv`) plus `traces.jsonl` with
one line per repository, and `run_manifest.json` with the model, the
prompt hash, the versions and the counts. Re-running the same command
continues where it stopped. Each clone is deleted after the repository is
classified, because they are about 80 MB each.

## The experiments

I ran six experiments. The input CSVs contain a `baseline_label`
column - the label the old batch pipeline gave to the same repository -
so the results can be compared.

The main one is `run_400_random`: 400 repositories drawn at random from
the full list of 39,426. The other five came first, on small, hand-picked
sets of repositories, mainly to get the runner working and to check
whether the wording of the tool description changes how often the model
asks for more context. The rest of this report is based on
`run_400_random` unless a number is explicitly said to come from one of
the smaller runs.

| Run                 | Repos | Prompt variant       | Used the tools | Agreement with baseline |
| ------------------- | ----- | -------------------- | -------------- | ----------------------- |
| `run_400_random`    | 400   | only-if-insufficient | 7 (1.8 %)      | 394 / 400               |
| `run_30_demo`       | 30    | only-if-insufficient | 1 (3 %)        | -                       |
| `run_10_handpicked` | 10    | only-if-insufficient | 2 (20 %)       | 9 / 10                  |
| `run_25_handpicked` | 25    | only-if-insufficient | 4 (16 %)       | 21 / 25                 |
| `run_3_assertive`   | 3     | assertive            | 2 (67 %)       | 3 / 3                   |
| `run_3_cost_aware`  | 3     | cost-aware           | 1 (33 %)       | 3 / 3                   |

`run_400_random` is the only one of the six that is a random sample; the
other five used repositories picked by hand, so their percentages do not
estimate the whole list the way `run_400_random`'s do.
`pilot_sample_200.csv` (200 repositories with baseline labels) is
prepared for an even bigger comparison, but has not been run.

"Prompt variant" is the wording of the tool description. It changes how
often the model decides that it needs more information. With
`only-if-insufficient` the model asks rarely, with `assertive` it asks
often. `run_400_random` used `only-if-insufficient`, the wording meant
for normal use.

### What the numbers show

**Most repositories do not need the extra context.​** In `run_400_random`,
the model used a tool on 7 of 400 repositories, 1.8 %. Because this run
is a random sample, that number is a reasonable estimate for the whole
list of 39,426. The smaller runs before it used hand-picked repositories
and gave a higher percentage (11 % over `run_30_demo`,
`run_10_handpicked` and `run_25_handpicked` combined), but those
repositories were not representative, so 1.8 % is the number to use.

This means that in the old pipeline, the Linguist data and the link
summaries are computed for all 39,426 repositories, but they are only
needed for a small part of them. Steps 2 to 5 are the expensive steps of
the stage.

**Token use.​** In `run_400_random` the average input was 3,550 tokens per
repository, the median 2,501. The average is higher than the median
because the few repositories that used a tool add the Linguist output or
the link summaries to the prompt.

**The wording of the tool description matters.​** This was tested on the
smaller runs, not on `run_400_random`, since it needs different wordings
compared against each other rather than one wording applied at scale.
The same model with the same prompt asked for extra context 3 % of the
time (`run_30_demo`, `only-if-insufficient`) or 67 % of the time
(`run_3_assertive`, `assertive`), only depending on how the tool is
described. This has to be fixed and documented, otherwise results are
not comparable between runs. The manifest records the hash of the prompt
and of the tool schema for this reason. `run_400_random` used
`only-if-insufficient`, so its 1.8 % is not automatically what another
wording would give.

**Agreement with the old pipeline is good but not perfect.​** In
`run_400_random` the agreement was 394 of 400, 98.5 %. Six repositories
got a different label. One of them had moved to a new repository between
the old run and this one, so the old label was outdated, not wrong. The
other five repeat the same pattern: GitHub Linguist finds no programming
language in the repository, or the README describes a website or a
support repository rather than software. None of the six is a case the
model got obviously wrong. The smaller runs (21 of 25, 9 of 10) had shown
the same range beforehand, so this was not a surprise, but
`run_400_random` is the number to cite: it is sixteen times larger and
drawn at random. A run this size is still a small part of the 39,426
repositories, so this is evidence, not proof, that the approach works at
full scale.

### One limitation of this approach

The MCP runner works repository by repository and waits for the model.
`run_400_random` took about 10 minutes for 400 repositories with 6
parallel requests, which comes to roughly 1.5 seconds of wall-clock time
per repository on average. Scaled up to all 39,426 repositories at the
same rate, that is about 17 hours, not several days as an earlier,
smaller run suggested. This number depends on `run_400_random`'s low
rate of tool use (a repository that calls a tool takes much longer than
one decided from the README alone) and on a 10-minute run being long
enough to represent a 17-hour one, which it is not necessarily - things
like API rate limits only show up over a much longer run. The old batch
pipeline uses the OpenAI Batch API, which is made for this size and does
not have this uncertainty.

So this experiment should not be understood as a replacement of the batch
pipeline. It is a measurement: it shows how often the extra context is
really needed. The same idea can be used in the batch pipeline as two
passes - first pass with the README only, second pass with the full
context only for the repositories that were not decided.

## Files

The run directories and input CSVs of the experiments were not copied into
this repository. Only `pilot_sample_200.csv` is here, beside this report.

Note: This document was created with Opus 5 high level reasoning. The `run_400_random` results were added and the report reworked to be based on them with Sonnet 5.
