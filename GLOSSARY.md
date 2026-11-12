# Glossary

This glossary defines the terms that this repository uses in its prose, its code and its commit
messages, and in the dataset that it builds, *AgentTorrent: A Dataset of Context and Harness
Engineering Artifacts on GitHub*. AgentTorrent succeeds AgentConfigs, the dataset that the
AIware 2026 dataset paper *A Dataset of Agentic AI Coding Tool Configurations* describes, and
each of the two datasets has versions of its own. Before its first release, AgentTorrent was
called *AgentContext: A Dataset of Agentic AI Coding Tool Configurations* from 2026-10-19 to
2026-11-04, *AgentContext: A Dataset of Version-Controlled Coding Agent Context* until 2026-11-06
and *AgentContext: A Dataset of Context and Harness Engineering Artifacts on GitHub* until
2026-11-07. Write the name in camel case. Until the release after AgentTorrent 1.0.0, the names
of the package, its command line, the container image, the NAS bundle, the handovers and the
volumes keep `agentcontext`, the lowercase form of the name AgentContext.

## Populations

From the sampling frame to the configured repositories, each population in the table below is
drawn from the population above it, and the subsample is drawn from the sample. A repository is
*in* a population, such as in the sampling frame or in the sample.

| Term | What it is |
|---|---|
| Sampling frame | The repositories that the filter of the sampling stage keeps from a retrieval of SEART GitHub Search (GHS). They are actively maintained repositories in <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages:words-->ten<!--/s--> languages, each with an OSI-approved license, at least <!--s:parameters.1_sampling.config.ghs_download.query.contributorsMin:words-->two<!--/s--> contributors, at least <!--s:parameters.1_sampling.config.ghs_download.query.pullsMin:words-->one<!--/s--> pull request, and at least the median number of commits and of watchers. Forks and archived, disabled, locked and educational repositories are left out. A repository that GitHub marks as a mirror stays in the sampling frame, and the classification leaves it out of the sample unless it invites pull requests or has merged pull requests since the sampling window began. Each repository was created at least <!--s:parameters.1_sampling.config.ghs_download.window.created_before_months-->18<!--/s--> months before the end date of the sampling window and has a commit in the <!--s:parameters.1_sampling.config.ghs_download.window.committed_within_months-->6<!--/s--> months before that date. The tracked sampling frame, which the development dataset uses, comes from retrieval `2026-11-02T223052Z` and has the end date 2026-10-22. Its repositories were created by 2025-04-22 and have a commit since 2026-04-22 |
| Sample | The studied population: the repositories in the sampling frame that the classification labeled as engineered software projects, with one repository for each head commit that several of them share, namely, of the repositories that neither GitHub's mark nor the model's response describes as a mirror, the one that GitHub created first. For the tracked sampling frame, DeepSeek V4.1 Flash gave the labels in run `2026-11-06T170312Z`. Each analysis states its adoption denominator, and percentages over `output/repos.csv` use configured repositories |
| Captured | The sample without its clone failures |
| Configured repositories | The captured repositories that have at least one configuration artifact after the collection has validated and cleaned the files that it copied. `output/repos.csv` has one row for each |
| Subsample | The repositories that development runs use, chosen so that they cover the heuristics, mechanisms, tool flags and collection outcomes found in the sample, at or below the packed size that `subsample`'s `--max-size-mib` sets. `subsample` selects them, logs each `coverage` entry that no eligible repository covers, and writes `dev_subsample.csv`. The subsample run clones the repositories in its run list, the subsample and the repositories added to it, from local Git mirrors |

**Time bound and size bound.** The limits on cloning a repository and on fetching its history:
`--clone-timeout` seconds, and `--max-clone-mb` MiB for the directory that the git command fills.
The cloning step stops a clone over the size bound and records it as `size_bound_exceeded`, and it
records a clone that exceeds the time bound twice as `time_bound_exceeded`.

**Clone failure.** A repository in the sample that git could not clone because it had left GitHub,
or because the clone failed or exceeded its time or size bound, or for which the cloning step failed
after the clone (`collector_error`). `data/collection/data/collected/clone_failed.csv` records one
row for each, with the reason. The next run retries a `clone_failed` or `collector_error` row. A
repository that has left GitHub stays recorded under the reason that GitHub reported:
`not_found_or_no_access`, `dmca_takedown` or `disabled_by_github`. The cloning step retries a
repository over the size bound (`size_bound_exceeded`) only under a larger `--max-clone-mb`. It
retries a repository past the time bound (`time_bound_exceeded`) within the collection that
recorded it only under a larger `--clone-timeout`, and again in the next collection. When a retry
fails or exceeds the time bound, the cloning step keeps the earlier collection of the repository if
that collection is complete on disk and was collected under the same path heuristics, and if its
commit history does not have to be read again.

**GHS search result.** The JSON file that GHS returns for the query of a retrieval from its
download endpoint, `/api/r/download/json`: the repositories that match the query under `items`,
and the query as GHS applied it under `parameters`. The download streams the file
gzip-compressed and writes its rows into the cache of retrievals. A response from GHS's search
endpoint, `/api/r/search`, provides counts and crawl dates for the download but is not a GHS
search result. Write "the search result" only in a sentence that names GHS, and do not call the
file *the export*.

**GHS retrieval.** The GHS search result for one query, taken at one time, together with the data
that GitHub returned for each of its repositories. `data/sampling/ghs_retrievals/`
keeps every retrieval, named by the UTC time at which it started, such as
`2026-03-16T180701Z`. That directory is **the cache of retrievals**. Its `index.csv` has one row
per retrieval with its outcome, sampling window and counts, and
`data/sampling/ghs_retrieval.json` names the retrieval that the steps after the download read.

**Sampling window.** The two date bounds of a retrieval's query, counted back from the window's
**end date**: A repository was created at least <!--s:parameters.1_sampling.config.ghs_download.window.created_before_months-->18<!--/s--> months before the end date and has a last
commit in the <!--s:parameters.1_sampling.config.ghs_download.window.committed_within_months-->6<!--/s--> months before it. The end date is a fixed date or, by default, the oldest crawl
date among GHS's languages. With the default, the window moves with every collection
([refresh strategy](docs/refresh.md)).

**Crawl date.** For one language, the latest `lastCommit` date among the repositories that GHS has
in that language. GHS crawls one language at a time and updates a repository only when it crawls a
new push to it. GHS has therefore recorded every push before the oldest crawl date of its
languages.

**Local Git mirror.** A bare local copy of a repository's refs and commit history, which
collection tests use as a fixed input. The pipeline clones from the mirror instead of from
GitHub. Results over local mirrors state the source, the retrieval date and the revisions of each
mirror, and they do not replace a collection of the full sample from GitHub.

**Subsample run.** `python -m agentcontext mirror-run`: the collection and the publication over the
subsample, cloned from local Git mirrors. The run compares its outputs with the outputs of a
**reference run**, an earlier subsample run, and lists every difference ([the tool](docs/tools/mirror_run.md)).

**`coverage`.** The semicolon-separated column of `dev_subsample.csv` that records why `subsample`
chose each repository. The prefixes `heuristic:`, `mechanism:` and `tool:` name a matched path
pattern, a configuration mechanism and a tool flag. The prefixes `commit_heuristic:`,
`ai_commit:`, `deleted:`, `stage_failure:`, `marker_at_dir:`, `marker_inside_dir:`, `symlink:`,
`history:`, `agent_workflow:`, `agentic_workflow:` and `workflow_command:` name a commit match, a
file that the collected file cleanup deleted, or a behavior of the collection or of a workflow.
[The subsample guide](docs/tools/subsample.md) defines their formats and how `subsample` chooses
the repositories that cover the most entries.

## The classification

**Label.** The classification's result for one repository in the sampling frame, `true`,
`false` or `unclear`, with the model's justification. The classification derives the label from
the model's response. The label is `false` when an exclusion criterion applies. It is `unclear`
when no criterion applies and one of them is unclear, either because the model set it to
`unclear` or because no attempt succeeded and the model's purpose quote is not in the
description or the README, nor in the purpose sentence of a page that either of them or the
homepage links to. Otherwise, the label is the model's judgment of the evidence of
software engineering practices, `true` when that evidence is clear and `unclear` when it is not.
The **purpose sentence** of a page is the sentence in which the page says what the project is or
does. The repositories labeled `true` are the sample, except each one at the head commit of
another one labeled `true` that the sample keeps instead. The labels of the batch
classification of March 2026 write `unclear` as `unsure`.

**Decision.** What the classification recorded for one repository: a label, which it derived from
the model's response for the README, or an exclusion. The classification keeps every decision in
the cache of labels with the inputs that it was made under, and it reuses the decision while these
inputs are unchanged. For a label, the inputs are the README's wording, the description, the model
and the classification parameters. For an exclusion, they are the README and the classification
parameters. The classification derives a reused label again from the stored response. It applies
the current rules of the criteria, the language check, the fork heuristic, the check whether a pull
request was merged in a mirror since the sampling window began, and the check that the model's
purpose quote is in the description or the README, or in the purpose sentence of a page that either
of them or the homepage links to. A reused label can therefore change or become an exclusion, and
the classification sends an exclusion that the language check now accepts to the model again.

**Exclusion criterion.** One of the reasons for which the classification labels a repository
`false`. The model sets the fields of each criterion of the prompt, such as `inactive` or
`website`, to `true`, `false` or `unclear` and gives its evidence. The classification adds
`not_english` for a README that the model finds not mainly in English, `no_purpose` for a
repository that states no purpose, and `fork` for a fork that is not independent. A decision
records the criteria that applied as `applied_criteria`.

**Description.** The one-line summary that GitHub shows at the top of a repository's page. The
classification sends it to the model before the README, or a line that states that the
repository has none.

**Fork.** In the classification, a repository that its description, its README or a linked page
calls a fork or a downstream of another project, or describes as started from another project.
That project is its **upstream**, which the model names as `fork_of`. GitHub's own forks are not
in the sampling frame, because the filter of the sampling stage drops every repository that
GitHub marks as a fork.

**Fork heuristic.** The heuristic of the classification in `2_classification/forks.py` that
tests with GitHub's data whether a fork is independent of its upstream. A fork is independent
when it does not track its upstream and is developed on its own: It has a commit in the sampling
window, and the number of its own authors, bots left out, reaches the contributor minimum of the
GHS query. Its own authors are the authors of its commits since GitHub created it, or of all of
its commits when it contains none of its upstream's history. A fork tracks its upstream when it
contains the upstream's newest commit from before the window while the upstream still has commits
in the window, and that commit is newer than GitHub's creation of the fork. The fork heuristic is
not one of the detection heuristics under `heuristics/`.

**Exclusion.** The decision that the classification records without a model when it excludes a
repository in the sampling frame before any model reads its README. The repository has no README,
the README is empty, or the language check finds that the README is not in English. Such a
repository gets no label, unlike a repository that an exclusion criterion applies to, which gets
the label `false`. The classification sends a README in another language that passes the
language check to the model. The model counts the README's words by language, and when English
has no more than half of them, the criterion `not_english` gives the label `false`. A repository that a run leaves without a decision after its
retry rounds is no exclusion: The outputs list it among the excluded repositories as `undecided`,
and the next run decides it.

**Language check.** Lingua's language detection over a README's prose, with the thresholds by
which the classification determines from its result whether the README is in English. Each
decision records the thresholds and Lingua's version, which are not classification parameters.

**Classification parameters.** Everything that decides a label besides the repository's description,
activity and README: the classifying model with the Hugging Face id of its weights, its quantization and the settings of its requests, its reasoning effort, temperature, top_p, seed and output limit, the prompt,
the descriptions of the functions that the model can call (`classification_tools.py`), the response
format, the format of the input with the results of the tools that the classification reads for it,
the messages that replace the result of a tool call, the limits on what the model reads, and the
prompt and the model with its weights and its quantization that summarize a linked page.
`submit_classification`, the tool with which the model submits its checklist, takes the response
format as its parameters. The **endpoint** of a model that OpenRouter serves is the combination on
which the classification calls it without fallbacks: its model id, the Hugging Face id of its
weights, the provider slug and the quantization. The provider is no classification parameter, by
the maintainer's decision of 2026-11-04, and each decision names the provider of each of its
responses instead. Parameters recorded before that day name the provider, and the cache of labels
matches them by their key, their digest without the provider. An OpenAI model has no endpoint. A
label is reused while the README's wording, the description, the model and the classification
parameters are unchanged. The labels of the two models of a label evaluation are made under
classification parameters that differ only in the classifying model. Do not call the
classification parameters *the configuration*, which in this repository is what a coding agent
reads.

**Pinned provider.** The provider of the endpoint on which the classification calls a model that
OpenRouter serves while that provider serves, such as `morph/fp8` for DeepSeek V4.1 Flash
(`endpoints.PRODUCTION`). **Backup provider.** The provider of the same weights in the same
quantization that serves the same requests of a model while its pinned provider fails, where a
failure would otherwise use an attempt at a repository's label (`endpoints.BACKUPS`). A request
every <!--s:parameters.2_classification.endpoints.PINNED_CHECK_SECONDS.minutes:words-->ten<!--/s--> minutes tests whether the pinned provider serves again. A label that the backup provider
served is a label under the same classification parameters, and its decision names the backup
provider under `served_by`. Do not call the backup provider *the fallback*, which in this
repository is the higher temperature or the second model after a failed response.

**The cache of labels.** `data/classification/labels/`: every label and exclusion that the
classification made, in files named by the run that made them.

**Cost record.** `<id>.cost.csv` in the cache of labels: the cost in USD of the requests of a run
of the classification and of the comparison of its labels, by the role and the model, the service
tier that served the requests or, for an OpenRouter model, the provider that served them, the
pinned provider or its backup provider, and the rate at which the tokens are billed, that is,
uncached input, cached input, cache writes or output. The prices come from
`2_classification/config/model_prices.csv`, which cites OpenAI's pricing page and OpenRouter's
lists of the endpoints of each model, and the tokens from the run's tokens file, `<id>.tokens.csv`.
A row that rests on an assumption, such as the tier of the tokens that only `index.csv` of the
cache of labels counts, states it in its note.

**Data root.** `data/`, or the directory that `AGENTCONTEXT_DATA_ROOT` names. It contains everything
that a run reads and writes, in a directory per stage: `sampling/`, `classification/`,
`collection/`, which the publication shares, and `analysis/`. It also contains the pipeline
execution state of a run of the whole pipeline. The code is outside it, in `1_sampling/`,
`2_classification/`, `agentcontext/` and `4_data-analysis/`. Do not call the data root *the stage
directory*, which is the collection stage's directory under it, `data/collection/`. The entrypoint
of the container records the revision of the stored formats of the data root in
`run_logs/data_root.json`. Before any step, it converts the stored files of a data root of an
earlier revision for which it has a conversion, and it runs no step over a data root of any other
revision ([deploy](docs/deploy.md)).

**Previous run.** The last run of the classification over a whole sampling frame that finished
before a given run. A run over a list of repositories, such as the second model's run over an
evaluation sample, or a trial over the first N repositories is never the previous run.

**Label change.** A repository with a label or exclusion that differs between a run and the previous
run, with its cause:

- *model variation*: one classifier gave one input two labels.
- *input change*: each input kept its label, and the README, the revision or the description
  differs.
- *classifier change*: the model, the classification parameters, the language check or the rules
  that derive a label changed and explain the new label, such as a new model that gives the
  previous input the new label.
- *undetermined*: the repeated requests cannot determine the cause, such as when a README can no
  longer be read, a new classifier gives a third label, a repeated request fails in every retry
  round, the run's classification parameters can no longer be sent, the API no longer offers the
  run's model, or the second model made the label and labels nothing in the invocation.

`changed_labels.py` requests the labels again where a repeated request can determine the cause.

**Evaluation sample.** Repositories drawn at random from the repositories that a model labeled.
A second model labels them, so that people review only the repositories that the two models label
differently. Do not shorten the term to *the sample*, which is the studied population.

**Label evaluation.** `2_classification/2_evaluate_labels.py` over `data/classification/evaluation/<id>/`.
A second model labels an evaluation sample, and a person reviews the repositories that the two
models label differently. `draw-check` adds a seeded sample of the repositories that the models
agree on, and `check` compares each model's labels with the reviewed and agreed labels of that
check sample. The label evaluation is distinct from the *detection accuracy evaluation*, which
measures the collection stage.

**Second model.** GLM-5.3 on its pinned endpoint (`endpoints.SECOND_MODEL`), with the settings
of its own record. It labels the evaluation sample of a label evaluation. In a run of another
model, it also labels a repository that the classifying model left without a decision after its
retry rounds, at its temperature and its higher temperature, when one of the classifying model's
attempts there ended in a *failed response*. The decision then names the classifying model as its
model and records the second model, the reason and the classifying model's failed attempts under
`second_model`. The label evaluation leaves such a repository out of its evaluation sample. The
second model is no classification parameter.

**Failed response.** A response that stopped at the output limit before the model called
`submit_classification` or in the arguments of the call, a final response that did not call
`submit_classification` with its schema, or a response to a request that offered only
`submit_classification` in which the model called another tool (`SECOND_MODEL_REASONS` of
`1_classify_repos.py`). A decision counts its failed responses under `failed_responses`, and so
does the record of a check. A failure of GitHub, of a tool or of the API's service is none,
and so is a response that the service ended, with an error or with the status `failed`
(`ServiceEnded`).

**Higher temperature.** The temperature of the classifying model's attempts at a repository after
a *failed response* there (`endpoints.HIGHER_TEMPERATURE` for DeepSeek V4.1 Flash). A decision
made at it records it under `higher_temperature`. It is no classification parameter.

**Settings of the requests.** The reasoning effort, the output limit, the temperature, the top_p
and the seed that a model's requests send, from the record of its pinned model, which applies to
the model's weights on any endpoint (`endpoints.Settings`, `endpoints.settings_of`).

## The heuristics

**Heuristics.** Criteria for deciding whether a file is configuration and whether a commit is a
tool's contribution. `heuristics/` contains path and commit patterns, patterns for tools outside
the six, the patterns of the planning documents, and negative examples in
`not_configuration.csv`, `not_ai_commits.csv` and `not_planning_documents.csv`. One row is **a
heuristic**.

**Path pattern.** One heuristic of `config_path_patterns.csv`. It names a place where a tool reads
configuration, such as `.claude/skills` or `<claude-plugin>/skills`, with the tool, the mechanism
and the match mode, which defines how the path is compared.

**Match mode.** The `match_mode` of a path pattern. It defines how a collected path is compared with
the pattern's `path`, such as `file` for an exact path, `basename` for a filename at any depth or
`dir_ext:.md` for the Markdown files directly in a directory. For some modes, it also defines what
the file has to contain. The module docstring of `agentcontext/heuristics.py` lists every mode.

**Content test.** What a file has to contain, beyond its path, for the match mode of its path
pattern to count it. The **front matter tests** require a non-empty `description`, a `trigger`
that Antigravity accepts, or a non-empty `name` and `description`. The other content tests
require a JSON or TOML file with the keys that the tool reads, such as a top-level `hooks` key
(`json_hooks`), an `mcpServers` key (`json_mcp_servers`) or Antigravity's named hooks
(`json_named_hooks`). The workflow modes test what an agent workflow runs.

**Commit pattern.** One heuristic of `ai_commit_patterns.csv`, or of
`other_ai_commit_patterns.csv` for the tools outside the six: a regular expression over the
author or committer, the trailers or the whole message of a commit, as its `scope` specifies
(`author`, `trailer` or `full_body`). `matched_heuristics` in the commit CSV names the
heuristics that matched a commit, by their `id`.

**Attribution.** An author, a committer or a line of a commit message that credits somebody with
writing the commit, a tool or a person, such as a `Co-authored-by` trailer. The `attribution` of
the heuristic that matches an attribution states how the attribution credits the tool, with one
of these values:

- `account`: the commit was authored or committed by the tool's own account, by the identity that
  the tool's code or environment writes, name and address together, or by an agent account or a
  GitHub App that its own page or the configuration of its project identifies as running the
  tool, such as a workflow that runs the tool's action with the app's token or an organization's
  AGENTS.md that names the app as the tool's identity. An address that GitHub links to a vendor's
  account is `account` only when the tool's code or environment writes it.
- `reviewer`: the co-author trailer that GitHub writes when a person commits a review app's
  suggestion.
- `standard`: the line that the tool writes itself, with its name and its own address or
  account, or a marker that the tool writes into a commit or a pull request.
- `convention`: any other line that names the tool, which a person writes, or which an agent
  writes because a repository's instructions require it, and any other name or address that
  names the tool and that users chose for an author or a committer.

`attributions` in the commit CSV lists every `Tool:attribution` pair that credits the commit,
joined by `;`, such as `Claude:standard` or `Copilot:account;Copilot:standard`. The value is
`standard` rather than `default` because VS Code writes Copilot's line only when a setting that is
off by default is turned on.

**`source`.** Where a heuristic came from. The column is on every row of the heuristics files
other than `config_path_patterns.csv` and `planning_document_patterns.csv`, in which every row
cites a page. A `doc` row cites the vendor page that states it and the date on which the page was
read. A `forum` row of `import_patterns.csv` or `not_imports.csv` cites posts of the vendor's
staff in its forum. A `sample` row was derived from the collected data, and its `evidence` links
the commits or files that support it. A `dropped` row of
`not_configuration.csv` was derived from repositories that the collected file cleanup dropped.

**Planning document pattern.** One heuristic of `planning_document_patterns.csv`: a regular
expression over a file's path relative to the bases that its match mode gives, with the
framework or the tool that writes the file, the source's name for the document and an optional
anchor. The module docstring of `agentcontext/planning.py` lists the match modes and anchors.

**Anchor.** What a planning document pattern requires beside a file's path, because the folder
alone has a common name: a marker such as `.specify/`, a file beside the document such as
cc-sdd's `spec.json`, a path elsewhere in the repository, or the opening of the file or of a
document of its folder. An anchor that is a file or a folder of the repository is no planning
document, and the cloning step copies it with the documents that it anchors.

**Path glob.** A path with its first <!--s:parameters.agentcontext.checks.unmatched.glob_for.segments:words-->two<!--/s--> segments kept and the rest replaced by `**`, such as
`.claude/skills/**`. `unmatched paths` groups the paths that it reads by path glob, and
`not_configuration.csv` records paths in this form. A path of at most <!--s:parameters.agentcontext.checks.unmatched.glob_for.segments:words-->two<!--/s--> segments stays as it
is, such as `.cursor/BUGBOT.md`. A file in a spec directory below the root has the path glob of
its path below that directory, so that `apps/web/.claude/memory/notes.md` has `.claude/memory/**`,
and an ignore file has its file name.

## Tools, mechanisms, configuration artifacts and commits

**Coding agent.** A program that reads the agent context of a repository and changes the
repository on a task that a person or a workflow gives it, such as Claude Code, Codex CLI or
Cursor's agent. Write "coding agent" for the program and "tool" for a vendor's coding agent under
all its names, the unit by which the dataset names what reads a configuration artifact and what a
commit credits.

**Agent context.** The files that a repository keeps under version control for its coding agents:
its configuration artifacts, the files that they import and its planning documents. The dataset is
named for it. A session log and the local state of a coding agent are not part of it, and neither
is the context that an agent assembles in a session, into which a tool loads an imported file.

**Planning document.** A file that a spec-driven development framework or a planning feature
of one of the six tools writes into the repository, at a location that the framework or the tool
documents. Agents read it as a work product through a command or a reference rather than as
configuration. Examples are the `spec.md` of a Spec Kit feature and a plan that Cursor saves to
the workspace. Planning documents are not configuration artifacts: They set no tool flag, do not
make a repository configured, appear in no mechanism CSV and change no count of the configuration
artifacts. `output/planning-documents.csv` contains the planning documents of the configured
repositories. A file named `spec.md` or `plan.md` outside a documented layout is no planning
document.

**Session log.** The record of one session of a coding agent: its prompts, responses and tool calls
and what the session read, such as a transcript of Claude Code or a rollout of Codex. It is neither
a configuration artifact nor a planning document, and the dataset does not collect it
([Session logs](docs/heuristics.md#session-logs)).

**Local state.** The files that a coding agent writes for its own use on a machine: session logs,
prompt histories, caches, locks, logins and the checkouts of its worktrees. A tool keeps them in
its home directory, and in a few places of a project's tool directory, such as Claude Code's
`.claude/worktrees/`. `heuristics/local_state_paths.csv` lists their places inside the tool
directories that the collector copies whole, and the collection leaves them out
([The local state of the tools](docs/heuristics.md#the-local-state-of-the-tools)).

**Configuration mechanism.** A means of tailoring tool or agent behavior to a project or
workflow. The dataset distinguishes ten mechanisms, each with its own CSV. `MECHANISMS` lists them
in the analysis scripts, and the table below names them. They belong to what the loop engineering
study calls the *outer harness*: what developers configure on top of what a tool ships with.

| Key | Mechanism | What a repository puts in it |
|---|---|---|
| `context_file` | Context files | Project knowledge that the tool reads in every agent loop, such as `CLAUDE.md` or `AGENTS.md` |
| `skills` | Skills | A `SKILL.md` with its scripts and assets, a Claude Code command (`.claude/commands/*.md`), a Claude Code dynamic workflow (`.claude/workflows/*.js`) or a Markdown file of OpenCode 2 with a description (`.opencode/skills/*.md`) |
| `subagents` | Subagents | An agent definition to which the tool can delegate part of a task |
| `commands` | Commands | A saved prompt that Cursor, Gemini or Copilot invokes by name |
| `rules` | Rules | Conventions that a tool applies while it edits |
| `settings` | Settings | The tool's own configuration file, or an agent workflow |
| `hooks` | Hooks | A command that the tool runs at a fixed point of a session, such as before a tool call |
| `mcp` | MCP | An MCP server that the tool can call |
| `plugins` | Plugins | A bundle of the above, with its manifest or marketplace catalog |
| `lsp` | LSP | A language server that the tool starts for a language's diagnostics and code navigation |

**Tool.** A vendor's coding agent under all its names, with the vendor's code review product where
it has one. The dataset covers six tools, *the six*: Claude Code, GitHub Copilot, OpenAI Codex,
Cursor, Gemini and OpenCode. The sixth was chosen by counting the tools in the file lists of a
collection ([the tools](docs/heuristics.md#the-tools)), and a tool that the dataset does not cover
is a *tool outside the six*. Gemini is Google's coding agent under each of its names: Gemini CLI,
Gemini Code Assist and Antigravity, which Google names the successor of Gemini CLI. Antigravity's
configuration therefore counts under `gemini` in the mechanism CSVs, and the commits attributed to
it under `Gemini` in the `ai_tool` column of the commit CSVs. A commit that names Gemini but none of
the three products is attributed to `Gemini (unspecified)`, outside the six. `SPECIFIC_TOOLS` in the
analysis scripts lists the six. The `tool` column of every mechanism CSV row names the tools that
the row's configuration artifact is attributed to, joined by `|` in the order claude, gemini, codex,
copilot, cursor and opencode, such as `codex|copilot`, or `any`. A repository's tool flags are the
union of these tools over its rows of the ten mechanisms. A row of `other.csv` names the tool of the
directory that contains the file and sets no flag.

**`any`.** The `tool` of a heuristic, and of a mechanism CSV row, for a configuration artifact
that the tools share, such as `AGENTS.md`, `.agents/skills` or a root `.mcp.json`. The value
`any` sets no tool flag.

**`no_specific_tool`.** The flag of a repository with a detected mechanism and none of the six
tool flags. Every configuration artifact of such a repository is attributed to `any`. `TOOLS` in
the analysis scripts includes the flag, third in the order of the figures, because the figures
show it as a seventh group. It is not a tool.

**Tool flag.** One of the six columns `claude`, `copilot`, `codex`, `cursor`, `gemini` and
`opencode` of `output/repos.csv`. It is TRUE when one of the repository's configuration artifacts
of the ten mechanisms is attributed to that tool.

**File flag.** One of the five columns `claude.md`, `gemini.md`, `agents.md`,
`copilot-instructions` and `cursorrules` of `output/repos.csv`. It is TRUE when the repository has a
context file of that column: `CLAUDE.md`, `GEMINI.md`, `AGENTS.md` or `AGENTS.override.md`,
`.github/copilot-instructions.md` or a file under `.github/instructions/`, at the root or in a
directory below it, or `.cursorrules`.

**Context file category.** The series of the analysis figures that a context file belongs to by its
path pattern, such as `CLAUDE.md`, `REVIEW.md` or `Copilot Instructions`, or `Named in
Configuration` for a file that a configuration names. `CATEGORY_OF_HEURISTIC_PATH` in
`4_data-analysis/_shared.py` lists them.

**Configuration artifact.** One instance of a mechanism: a single file such as
`CLAUDE.md`, or a directory with files that jointly define a skill, plugin or other
configuration. A skill includes its `SKILL.md`, scripts, references and assets.
Each row of a mechanism CSV other than `other.csv` describes one configuration artifact.

**File.** One file that collection copied from a repository. A skill directory can contain several
files and still be one configuration artifact. The archive of collected files,
`release/repos_data.7z`, also includes unclassified files. File counts and configuration artifact
counts therefore measure different units. `docs/dataset.md` and the generated release README
state the current counts.

**Agent workflow.** A GitHub Actions workflow under `.github/workflows` that runs one of the six
tools and counts under `settings` for that tool. Detection recognizes it by one of the file names
that the vendors give, or by what it runs: an action in a `uses:` line (match mode
`workflow_uses:`), an invocation that a tool documents for running without a person, such as
`claude -p` (`workflow_command:`), or the engine of a GitHub Agentic Workflow (`workflow_engine:`).
A **GitHub Agentic Workflow** is the kind of agent workflow that GitHub's gh-aw compiles: a
Markdown file under `.github/workflows` with an `on:` key in its front matter, compiled into a
`.lock.yml`. Its **engine** is the tool that runs it. `engine:` names the engine in the
workflow's front matter or in a file that the workflow imports. When neither names an engine, the
engine is Copilot. A Markdown file that the workflow's `imports:` lists is an imported file of the
workflow (see **Import**). The cloning step copies it with the workflow into the collected tree,
and detection counts it as part of the workflow. The file that an agent action of a workflow
reads as its prompt, such as the file that the `prompt-file` input of the Codex action names, is
a part of the workflow too, and the cloning step copies it with the workflow. The workflow counts
for no tool when its engine sets `command:` to a program with a name that does not begin with the
engine's id ([the heuristics](docs/heuristics.md)). Write the vendor's name in full, because
*agentic workflow* alone is easily read as *agent workflow*, the wider term.

**Import.** A reference in a file that makes a tool load another file into context with it
automatically, such as `@docs/style.md` in a CLAUDE.md, or a Markdown file that the `imports:` of
a GitHub Agentic Workflow lists. The file that contains it is the **importing file**, and the file
that it names is its **target**, or the **imported file** once the tool reads it. An imported file
can import further files, and the imports that start at one importing file form a **chain**, read
up to the depth that the tool reads. `heuristics/import_patterns.csv` names, for each tool, the
configuration artifacts that are importing files, the syntax and how the tool resolves a path.
`heuristics/not_imports.csv` names what a tool does not read as an import ([the
heuristics](docs/heuristics.md#imports)). The imports of a workflow determine its engine and are
part of the configuration artifact. Every other import is an extension of the dataset beside the
configuration artifacts: An imported file sets no tool flag and is in no mechanism CSV.
`output/imports.csv` has one row per tool and import.

**AI commit.** A commit that a commit pattern attributes to one of the six tools. AI commits are in
`output/all_ai_commits.csv`, **the commit CSV**. A commit attributed to a tool outside the six is
in `output/other_ai_commits.csv`, and the two files are **the commit CSVs**. The detection step
restricts both to the configured repositories. The attribution criterion of
[the heuristics](docs/heuristics.md#ai-contribution-detection) determines which commits count,
including a commit that records no change. AI commits record what a tool contributed, and
configuration artifacts record what a repository configured. Separate heuristics therefore detect
the two.

## The collection and its outputs

Paths of the collection stage, such as `output/` and `data/collected/`, are relative to
`data/collection/`.

**The output CSVs.** `output/*.csv`, the CSVs that the collection writes. Eleven of them are **the
mechanism CSVs** (`MECHANISM_CSVS`): one per mechanism, with one row per configuration artifact,
and `other.csv`, with one row per unclassified file. The dataset paper calls them the
per-mechanism CSVs.

**The published dataset.** What `release` builds into `release/` for Zenodo. The dataset that
the sampling stage reuses is **the GHS dataset**. Do not call either of them *the data* or *the output*.

**The unclassified files.** The collected files that no mechanism covers, one row each in
`other.csv`. The release withholds `other.csv` but keeps the files in the archive of collected
files, except the files that the archive step prunes (`prune.is_prunable`), so that a later
heuristic can classify them.

**The unmatched attributions.** `output/all_ai_commits-unmatched.csv`. It has one row per trailer
that credits somebody with writing a commit, such as `Co-authored-by`, and per author or committer
identity that looks like a bot, in the commits that no commit pattern matched. Each row gives the
commits and repositories in which the trailer or identity appeared. A row can credit a tool that
no heuristic covers, an automation or a person. `output/all_ai_commits-mentions.csv` groups the
lines of such commits that name one of the six tools by their phrasing, with the commit and
repository counts of each phrasing. These lines include credits written in prose, which the
unmatched attributions do not contain. `unmatched commits` ranks the rows by repository count. The
attributions that appear in the most repositories, which most need a commit pattern, therefore
come first. The repository tracks the lines of the current collection that name a bot or a tool
and no person as `output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z`, with the <!--s:parameters.agentcontext.checks.unmatched.UNMATCHED_COLUMNS_READ:words-->four<!--/s-->
columns that the check reads. The full list stays in the handover of each run.

**The imported tree.** `data/imported/<owner§repo>/<path>`: the files that an import of a copied
configuration artifact reaches. The cloning step copies them out of the clone into this tree
beside the collected tree, even where the collected tree contains the file as well. The
collected file cleanup does not touch the imported tree, and the import detection step reads it.
The release packs the imported files that `repos_data.7z` does not contain into
`imported_files.7z`. A **stub** stands in the imported tree for a target that the tool does not
read, with the target's extension and its size up to the tool's limit. `<owner§repo>.stubs.json`
beside the directory of the repository lists the stubs, so that no step reads a stub as the file.

**List of finished repositories.** `finished_repositories.jsonl`, an append-only list with a line
for each repository that the cloning step has finished, failed clones included. A later line for a
repository replaces its earlier lines. Each line contains the repository's rows, so
that a resumed run can take the repository over without cloning it again. Do not call it *the
collected repositories*, because a repository with a failed clone is in it too.

**Collected tree.** `data/collection/data/collected/`, a directory `<owner§repo>/` for each
repository with the files that the cloning step copied from it. No later step changes the
collected tree. Its list, `data/collected/repos.csv`, the **collected list**, has one row per
repository in the sample, in input order, with its scan time, branch, head commit and head commit
date. The last three are blank for a clone failure.

**Validated tree.** `data/collection/data/validated/`, which the collected file cleanup derives
from the collected tree on every run, without the files that are not configuration. The later
steps of the collection read the validated tree. Its list, `data/validated/repos.csv`, the
**validated list**, has the rows of the repositories that still have a detected mechanism.

**Deletion log.** `deletion.csv`: one row for every file, empty directory and repository that the
collected file cleanup removed, with the reason.

**Clone log.** `clone_logs/<owner§repo>.log`: git's output for each clone attempt of one
repository, and the errors that occurred after the clone, such as file names that are not valid
UTF-8 or a file list that the cloning step could not write.

**File list.** `data/file_lists/<owner§repo>.ls-tree.xz`: every path of a repository at its
collected commit. The cloning step writes it so that a new path pattern can be counted without
cloning again.

**Git columns and AI columns.** The columns of a mechanism CSV that come from the commit history of
a configuration artifact's file: `created_at`, `#commits`, `first_commit_sha` and
`last_commit_sha`, which `file_history.csv` caches, and for a context file
`first_commit_ai_created`, `first_commit_ai_tool`, `#ai_commits` and `ai_commit_tools`, which
`ai_authorship.csv` caches.

**Taken over.** A repository that a collection keeps from the earlier collection without cloning it
again. The cloning step takes over a repository when its default branch is still at the collected
commit, its git version and the records that it keeps are unchanged, and no change to the
collector's code, the code of the imports or the heuristics since the collection reaches the
repository's files.

**Versions of the collector's code.** `code_versions.COLLECTOR` in `agentcontext/code_versions.py`:
every fingerprint of the collector's code that a collection was made under, in order, and for each
version after the first the **reach** of the change from the version before it, that is, no
repository, every repository, a predicate over a repository's file list, or a predicate over its
per-repository commit record.
`code_versions.IMPORTS` records the versions of the code of the imports in the same way. A change
of either code adds a version
([repository cloning](docs/steps/repository_cloning.md#recording-a-change-of-the-collector)).

**Connectivity probe.** A `git ls-remote` of `--probe-url` that the cloning step runs after
`--max-consecutive-failures` clones failed in a row, to distinguish a network failure from
failures of the repositories.

**Scratch directory.** `_scratch/` in the collection stage's directory, or the directory that
`--scratch-dir` names. Repository cloning and the file history step put their clones there, and
the pre-checks measure the free disk space there.

**Reading the commit history.** What the cloning step does after it fetches a repository's whole
history. `git log` over every ref lists each commit, and the commit patterns classify each commit.
`output/ai_commits-repos.csv` records the outcome: `history_read`, `history_read_failed` when
reading the history failed, `history_unavailable` when fetching did not retrieve the history,
`no_files` for a repository from which the cloning step copied nothing, a clone failure's reason,
such as `not_found_or_no_access`, or `shard_unreadable` for a per-repository commit record that
could not be parsed. Only `history_read`, `history_read_failed` and `history_unavailable` have a
readable per-repository commit record, which records the outcome too. `commits_read` counts the
commits that the cloning step read. When the commit history of a repository was not read, the
reclassification records `history_not_read`.

**Per-repository commit record.** `collect_commits/<owner§repo>.json`, what the cloning step
recorded about one repository as it read the repository's commit history: its outcome and counts,
the commits of each copied path (`path_commits`), the unmatched attributions and the mentions,
the AI authorship of the copied files, the evidence that its commit log is complete
(`commit_evidence`), and `classified_by`. `classified_by` is a digest of the columns of the two
commit pattern files that the classification reads (`commits.CLASSIFICATION_COLUMNS`) and of the
code of `agentcontext/commits.py` and `agentcontext/heuristics.py`. A change to these columns or
to that code, except a comment, a docstring, a log message or the layout of the code, makes the
record due for reclassification. Beside the record are the repository's **commit
log**, `<owner§repo>.log.xz`, with the hash, identities, date and message of every commit that the
cloning step read, and its **shards**: `<owner§repo>.csv` with the commits that the commit patterns
matched and `other/<owner§repo>.csv` with the commits of the tools outside the six. The cloning
step, and a reclassification after it, join the shards into the two commit CSVs.

**Reclassification.** The `commit-reclassification` step. It applies the current commit heuristics
to the commit log that the cloning step kept for a repository, without cloning the repository
again. After it validates the evidence, it rewrites the repository's shards, the commit CSVs, the
AI rows of `ai_authorship.csv` and the `#commits` of `file_history.csv`. From these files, the
detection step then writes the AI columns and the Git columns.
`output/commit-reclassification.json` records each repository's outcome (`repositories`) and
whether every repository with a required commit history was reclassified from validated evidence
(`complete`). It also records whether, in addition, no repository in the sample is a clone failure
(`population_complete`).

**Commit attribution validation.** The `commit-attribution-validation` check, which matches the
commit patterns with the scope `trailer` or `full_body` against the commit CSVs again:
`ai_commit_patterns.csv` against `all_ai_commits.csv`, and `other_ai_commit_patterns.csv` against
`other_ai_commits.csv`, which the step passes as `--other-input`. It shows whether each row's
`matched_heuristics` and `attributions` are what those patterns give for the stored message now.
The check does not read the author and committer, which the CSV does not store, and it therefore
does not replace a reclassification.

## Runs, runners and steps

**Run.** The work of a runner, or of one step's program such as `1_classify_repos.py`, from its
start until it finishes. One execution is an **invocation**. A runner starts the pending steps in
order and records each step that finishes in the pipeline execution state, so that the next
invocation over the same data resumes a stopped run and skips the recorded steps. At a Start, the
entrypoint runs the runner, which begins or resumes a run of the five stages, and `mirror-run`
begins a subsample run. In this repository a run is always a run of the pipeline or of a part of
it, never an execution of a coding agent, which the loop engineering study calls an *agent loop*.

**Handover.** The archive that the entrypoint of the container packs into `handover/` when the
runner ends, `agentcontext-handover-<time>-<outcome>.7z`. A `-complete` handover contains the
whole data root and is the development input ([dataset](docs/dataset.md)). After any other
outcome, the handover contains the pipeline execution state and logs, the data of the sampling,
classification and analysis stages, and the collection's lists, caches and output CSVs. A second
archive, `agentcontext-handover-<time>-<outcome>-trees.7z`, then contains the collected and
validated trees, unless an earlier such archive in `handover/` already contains the same trees.

**Working copy.** A handover extracted with `7zz x -snld` into an empty directory, on macOS inside a
Linux Docker volume. The steps after repository cloning run in the working copy, and the
downloaded handover stays unchanged ([development](docs/development.md#handover-working-copy)).

**Fetched file.** A file, or the marker of a link, that was copied into the collected tree of a
working copy after the collection, at the repository's collected commit, because a later change
of the collector or the path heuristics copies it. `data/collection/fetched_files.csv` records
each with the collected commit, the day of the fetch and the commit of the change that required
it ([dataset](docs/dataset.md)).

**Pipeline execution state.** `run_logs/state.json` under the data directory of a runner, which
is the data root for the whole pipeline and a stage's directory for one stage. The file contains the
step completion fingerprints that resume the pipeline, and the latest invocation's command, source
revision, timing, outcome, step results and runtime information. `write_execution_state` writes
it. The execution logs are the separate `run_logs/<step>.log` files, and
`finished_repositories.jsonl` stores the progress of the collection.

**Run lock.** `run_logs/run.lock` under a runner's data directory, an `flock` that the runner holds
while it runs, so that one run at a time uses that directory.

**Modified input.** An output of a step that a later step changed in place. For example, the file
history step removes from the validated tree the directories of repositories that git reports as
gone. The runner's step list declares such a change with `mutates`. Before a run, the **check for
modified inputs** finds each pending step that would read such an input. The runner then produces
the input again first, by running the step that wrote it and every step after. The runner stops
before the first step only when that step is recorded by its output and cannot run again, and its
log message then names the `--from` that produces the input.

**Regeneration.** A run of the steps after repository cloning over an earlier collection that is on
disk. It applies the current heuristics and computes the outputs again without repository
cloning. The file history step still clones a repository with rows that its caches lack.

**Irreproducible step.** A step that takes a snapshot of an outside source: Running it again would
take another snapshot rather than redo its work (`irreproducible` in its `Step`). The GHS download
is the only such step. When its output is on disk, the runner records the step as complete
without running it, unless `--from` names the step or its last run did not finish. Repository
cloning also takes a snapshot, but you record a collection restored from a handover yourself with
`--mark-complete`.

**Regression manifest.** `output/baseline.json`: the SHA-256, row count, columns and count of TRUE
in every boolean column of the mechanism CSVs, `repos.csv`, `excluded.csv` and
`consistency-report.csv` (`TRACKED_FILES`), one entry per file. The commit CSVs are not in it.
`regressions check` compares `output/` with it.

**Log message.** A line that a program of the pipeline writes to stderr in the format
`LEVEL component: message`, through `agentcontext/logs.py` or through the container's entrypoint.
The entrypoint repeats the format because it has to log when the package cannot be imported. A log
message states what the program does, what it found, its counts and tables, or what went wrong. A
program prints nothing besides its log messages and the help of its command, and `docs/logs.md`
gives the reason.

**GitHub access token.** A GitHub personal access token with read access to public
repositories, held in `credentials.csv`, `GITHUB_TOKENS` and `GITHUB_TOKEN`
(`agentcontext/credentials.py`). Repository cloning and the file history step clone with the
first GitHub access token that the pre-checks validate. The GitHub query of the
sampling stage, the classification, `snapshot validation` and `measure sizes` rotate through all
of them. The other credential in `credentials.csv` is an OpenRouter API key. An OpenAI API key is
needed only for an OpenAI model that a command names.

**Pre-checks.** The checks that a runner makes before its first pending step starts: the free disk
space, the programs and Python packages that the pending steps need, their credentials, and the
inputs that they read and that no pending step writes. When a check fails, no step starts. The
pre-checks are not a step.

**Stage.** One of the five parts of the pipeline, in order: sampling, classification,
collection, publication and analysis. Each is a sequence of steps, and each step names its
stage in the `stage` field of its `Step` object. `--list` shows the steps under their
stage, and `--from` takes a stage to select all of its steps. A stage is not a step.

**Step.** One program that the runner starts. The runner records a step when the step finishes,
so that the next invocation can skip it. `Step` in `agentcontext/runner.py` is the class that
declares a step. A step with a module under `agentcontext/steps/` has one name, a noun phrase:
Repository cloning in progress messages, `repository_cloning` as its completion key and module,
and `repository-cloning` as its command. A step that runs a check runs the check's command, such
as `snapshot write` for `collection_summary`, and a step of another stage runs that stage's
script. The filter of the sampling stage applies filter steps of its own, which are not steps of
the runner.

**Runner.** A script that runs a list of steps in order and keeps one pipeline execution state.
`run_pipeline.py` runs the five stages, and four runners run its parts:
`1_sampling/run_sampling.py`, `2_classification/run_classification.py`, `agentcontext/runner.py` for
the collection and the publication, which starts with the classification's engineered project
selection, and `4_data-analysis/run_analysis.py`. `agentcontext/runner.py` also defines what the
other runners import: `Step`, the lock, the pre-checks, the resume rules and the stop
handling.

## Deployment

**Image.** `agentcontext-pipeline`, the Linux x86-64 container image that the `container image`
workflow builds from the commit that it is started on or from a release tag, and from `main` for
a deployment. It contains the code and the heuristics and no data.

**NAS bundle.** `agentcontext-nas-bundle.zip`, which the same workflow builds: `compose.yaml`,
`credentials.csv.example`, an example of the `credentials.csv` that the operator writes, the
runbook `deploy.md` and the empty folders `data/` and `handover/`.

**Project folder.** The folder on the NAS with the unpacked NAS bundle and `credentials.csv`.
Container Manager runs the container from it and mounts its `data/` and `handover/`.

**Credentials file.** `credentials.csv` at the repository root, or the file that
`AGENTCONTEXT_CREDENTIALS` names, which the container sets to the file in its project folder. The
file has one GitHub access token or API key per row under the header
`service,account,credential`.

**Start.** Starting the container on the NAS: In Container Manager, open Container in the left
menu, select `agentcontext-agentcontext-1`, then choose Action, Start. That runs
`docker start agentcontext-agentcontext-1`, the same command that the scheduled tasks of the
runbook run. Container Manager, Project, the project, Action, Build is different: Synology
documents it as `docker-compose up --build --no-start`, which creates the container again from
`compose.yaml` when the file or the image changed and does not start it. At each Start, the
entrypoint runs the runner over the five stages and packs the handover when the runner ends
([deploy](docs/deploy.md#import-create-start)).

**Argument for one Start.** `--from`, `--mark-complete`, or `--fresh`, `--accept-smaller` or
`--end-date` in a `--step-arg`, in the `command:` of `compose.yaml`. The entrypoint passes it to
the runner until a run uses it. At later Starts, the entrypoint leaves it out and logs a warning,
until you remove it from `command:`. Every other argument is a setting that the entrypoint passes at every Start
([deploy](docs/deploy.md)).

**Monthly run.** The run that the entrypoint begins at a Start when the current calendar month in
the time zone of the NAS is later than the month in which the GHS download of the last run started,
and the last run completed or failed in a step of the analysis stage after its release step
completed. After a download that fell back to the previous retrieval, the month of the retrieval
that the download attempted counts. The entrypoint tests these conditions again after each run of
a Start, monthly runs included, and it begins another run only when the run before it took a new
GHS download. For a monthly run, the entrypoint adds `--from ghs_download --from repository_cloning`
to the command. Before that, it packs the data root of the last run when no recorded handover
contains it.

## Documents and checks

**Stated statistic.** A number in a maintained document between `<!--s:KEY-->` and `<!--/s-->`.
`python -m agentcontext statistics` writes it from the entry `KEY` of `docs/statistics.json`, and
with `--check` compares the two. Every entry has a source that computes its value from data
([stated statistics](docs/tools/statistics.md)).

**Computation.** A function of a module of `agentcontext/statistics/` that a `function` or a
`collection` source of `docs/statistics.json` names as `MODULE.FUNCTION`, with its arguments. It
reads the files of the checkout, of an earlier commit, or of the data root of a working copy, and
returns the value of a stated statistic.

**Collection statistics file.** `data/collection/collection_statistics.json`: the values of the
stated statistics with a `collection` source, which only the data root of the working copy of a
handover holds the data for. During the import of a handover, `python -m agentcontext statistics
--collection --archive HANDOVER` computes them over the working copy and writes them with the
SHA-256 of the snapshot of the collection, of the code of each computation and of the handover.
The code of a computation is its module and every module of the package that the module imports,
directly or through another module. The checkout states such a value only while the file records
the SHA-256 of the tracked `output/snapshot.json` and of the current code.

**Documentation watch.** `python -m agentcontext docs`. It fetches every vendor page that a
heuristic or a stated statistic with a `vendor` source cites, hashes the page's visible text, and compares the hash with the page's **recorded
hash** in `heuristics/doc_hashes.json`. The weekly `doc-watch.yml` workflow runs it over every
heuristics file that cites pages and opens an issue when a page changed or could not be checked.
With `--quotes`, it also checks every quotation of the notes of the planning heuristics against
the pages that the quotation's row cites. With `--update`, it records the quotations that it found,
by a digest, in `heuristics/doc_quotes.json`.

**Detection accuracy evaluation.** `python -m agentcontext evaluation` over
`data/collection/evaluation/<day>/`, one directory per evaluated collection, named by the day on
which the collection started. It draws samples of a collection's configuration artifacts,
imports, planning documents, commits and mentions. One pass labels every item, and a second
pass, which does not see the first pass's labels, labels a seeded fifth of each sample. A person
resolves the uncertain labels and the disagreements, and `report` estimates the precision and
recall of the heuristics from the labels
([the tool](docs/tools/evaluation.md)). The detection accuracy evaluation is distinct from the
*label evaluation*, which measures the classification.

**Verification suite.** The repository's complete set of automated tests and consistency checks,
which `python -m agentcontext verify` runs. **Pre-push verification** is the full verification
that a committed state passes before it is pushed. The **pre-deployment checks** validate the
collection and the build before a Start. The **reproduction check**,
`4_data-analysis/check_reproduction.py`, compares regenerated analysis outputs.

**Dependency lock.** One of the files under `locks/`, which `dev/lock.sh` writes with uv from the
requirements files: every Python distribution that an environment installs, with its exact
version and the SHA-256 of each of its files. `locks/development.txt`, **the development lock**,
locks every requirements file and is what `.venv`, CI and the development image install.
`locks/pipeline.txt`, which the production image installs, and `locks/build.txt`, the setuptools
that every environment installs first, are subsets of it ([locks/0_README.md](locks/0_README.md)).
Write "dependency lock" in full, because the run lock is a different lock. The **virtual
environment of the checkout** is `.venv`, which mise creates from the release of Python that
`mise.toml` pins and into which `dev/setup.sh` installs `locks/development.txt`.

## Retired vocabulary

Do not reintroduce the words below. `tests/test_glossary.py` checks every entry except "report",
"rule", "token", "working copy", "working tree", "engine", "evaluation", "theme" and "agent run".
For these words, a pattern cannot distinguish the retired sense from ordinary use, and the writer
checks the sense. A pattern can also match more than the retired sense, and the test then fails
on an ordinary use that a row keeps. Rephrase such a use. `tests/test_logs.py` checks "token"
in the log messages and help texts of `agentcontext/`, `deploy/entrypoint.py` and
`run_pipeline.py`.

`tests/test_glossary.py` reads every tracked prose file, the ignore files included: a Python file's
comments, docstrings, help texts, log messages and exception texts but not its identifiers, a page's
visible text but not its markup, and no link. A link names another project's page or repository in that project's words.
The check removes code spans before it matches. Quote a name or a string that the code writes in a
code span. The test also fails on a semicolon in a log message, a help text or an exception text:
Write separate sentences, and put context in parentheses. It also fails on such a text when the
program narrates its own reasoning in it, as docs/logs.md describes.

Internal names follow the glossary too, such as `heuristic_rows` for detection criteria,
`MECHANISM_CSVS` for the mechanism CSVs and `minimum_free_bytes` for the storage minimum.
Keep published column names and vendor configuration keys unchanged. `tests/test_glossary.py`
reads Python code through its AST and fails on each name of `RETIRED_IDENTIFIERS` wherever the
code uses it as a name, an attribute, an argument, a definition or an import. It checks no other
identifier.

| Retired | Write instead | Why |
|---|---|---|
| "registry", "ruleset", in any sense | "the heuristics" for the files under `heuristics/`, and another project's own name in a code span or link | Each row of those files is a heuristic, as defined above, and a second name suggests a second kind of criterion. The glossary check fails on every use of the words. |
| "shape", in any sense | "its columns", "its format" or "its layout", whichever it is | Shape leaves the reader to guess whether the columns, the format or the layout is meant. The glossary check fails on every use of the word, and `.shape` remains the pandas attribute used in `4_data-analysis/rq3/cooccurrence_analysis.py`. `planning_document_patterns.csv` keeps the file name `shape.md` of Agent OS and its "shaping notes". |
| "collapsed path" for what `unmatched paths` groups by | "path glob" | Path glob says how the path was shortened, and collapsed does not. A path pattern is a different thing, a heuristic. |
| "config type", "config option", "kind of configuration" | "mechanism" | The ten mechanisms defined above are the categories, and the CSVs and the analyses are named by them. |
| bare "artifact" | "configuration artifact" | The qualifier distinguishes configuration artifacts from GitHub workflow artifacts. Code identifiers, including the `artifact` column in `file_history.csv`, keep their spelling. |
| "configuration file" where the unit is meant, as in "one configuration file per row" | "configuration artifact" | A configuration artifact may contain several files. Use file only when counting or describing individual files, and write "one file" or "per file" then, because the glossary check fails on "one configuration file" and "per configuration file" in either sense. |
| "raw file" | "file" | Use a qualifier only when it distinguishes a processing state. |
| "frame member", "member of the sampling frame" | "repository in the sampling frame" | The sampling frame is a population of repositories, and a repository is what its rows name. |
| "repository of the sampling frame", "repository of the sample" | "repository in the sampling frame", "repository in the sample" | A sampling frame is a list that the sample is drawn from, and both populations take one preposition. |
| "corpus" | "sample" | Sample names the studied population. |
| "the subset", "dev_subset" for the mirrored repositories | "the subsample" | The mirrored repositories form a subsample of the sample. Ordinary uses such as "a subset of the rows" remain valid. |
| "feature set", "feature matrix", "a case", "the features a repository covers" | "`coverage`", "what a repository covers", "an entry of `coverage`" | The entries of `coverage` are heuristics, mechanisms, tool flags and collection behaviors, not features of a tool. Letter case, test cases and tool features keep their ordinary meanings. |
| "the catch-all" | "the unclassified files" | The files belong to no mechanism, and catch-all suggests a class of their own. |
| "AI-authored commit", "AI-co-authored commit" | "AI commit", or "a commit that credits a tool" | A commit pattern finds a credit, and a credit does not show who wrote the change. The glossary check fails on "AI-authored", "AI-co-authored" and "co-authored by AI". |
| "census", in any sense | for `output/identity-labels.csv`, name the file, or say what it contains: one label per commit that only an author or committer identity attributes to a tool | A census counts a whole population, and the file labels only those commits. The glossary check fails on every use of the word. |
| "residue" | "the unmatched attributions" | Unmatched attributions and unclassified files are different outputs. The per-repository commit record keeps them under the key `unmatched`. |
| "diagnostic" for a line a program writes about itself | "log message" | A diagnostic script is one of the scripts under `2_classification/diagnostics/`, which analyze data. Log messages describe what a program does. |
| "report" for what a command prints | name it: the counts, the ranked list, the summary | Report does not say whether the counts, a ranked list or a summary was printed. A saved report may include counts, findings and command results. |
| "the tables" for `output/*.csv` | "the output CSVs", or "the mechanism CSVs" | The papers have tables, and the output CSVs are files. A Markdown, LaTeX or TOML table keeps the word table, but the glossary check fails on "the tables" in every sense. Name such tables by their subject. |
| "the finished list" | "the list of finished repositories", or `finished_repositories.jsonl` | The file also lists repositories with failed clones, and the list of finished repositories is the name that the code and the help texts use. |
| "rule" for a heuristic | "heuristic" | Heuristics detect configuration. Rules are also a configuration mechanism with a `rules` column and `rules.csv`. |
| "floor", in any sense | "minimum", or "threshold" | Use minimum or threshold for a bound. The glossary check fails on every use of the word in prose, and `math.floor` keeps its mathematical meaning. |
| bare "token" for the credential that the pipeline sends to GitHub | "GitHub access token" | Other kinds of tokens may enter the log. An attribution in the unmatched attributions and a word of a `--step-arg` value keep the word, and `GITHUB_TOKEN` keeps its name. |
| "run record" | "pipeline execution state", or "pipeline execution state and logs" when both are meant | A record of a run could be the state file or a log file. The pipeline execution state is the file that resumes the pipeline. |
| "git history", "revision history" | "commit history" | Commit history distinguishes repository changes from run logs. |
| "sweep", in any sense | `unmatched paths`, `unmatched commits` for the checks over what nothing matched | The two checks have names, and Sweep is the name of a tool outside the six. The glossary check fails on every use of the word outside `heuristics/other_ai_commit_patterns.csv`, which names that tool, except the tool's name, capitalized. |
| "kind of signal", "signal kind" for how a commit credits a tool | "the `attribution` value" | The column is `attribution` in the heuristics files and `attributions` in the commit CSVs, and its values are `account`, `reviewer`, `standard` and `convention`. |
| "unmeasured", in any sense | "no history row", "blank git columns", or "the history was not read", whichever is meant | Unmeasured does not say what is missing: the history row, the Git columns or the reading of the history. The glossary check fails on every use of the word. |
| "replay" for applying the commit heuristics again | "reclassification" for the `commit-reclassification` step, "matched again" for the `commit-attribution-validation` check | The step is named for what it does, and the check validates the commit CSVs. For the replay of a capture, write "Wayback Machine replay", the only form that the glossary check accepts. |
| "stored log", "stored commit log" | "commit log" | The commit log is the file that the cloning step keeps, and "stored" adds nothing to the term. |
| "walk", "walks", "walked", "walking", "commit walk", "walk record" | "reading the commit history", "a repository with a commit history that was read", "the commits that the cloning step read", "the collection" for a run, or a plain verb such as "lists" for files and directories | One word named the reading of a history, the repository, its commits, the run and a pass over files alike. Python's `os.walk` and `ast.walk` keep their names. |
| "sidecar" | "per-repository commit record" | Sidecar says where the file sits, not what it holds: what the cloning step recorded about one repository. |
| "handover archive", "NAS archive" | "handover" | The handover is the archive, and the trees of a run that did not complete are in the second archive that the handover entry names. |
| "writable copy", "writable handover" | "working copy" | An extracted handover has one name, and writable describes every copy on disk. |
| "working copy", "working tree" for `data/validated/` | "the validated tree" | The working copy is an extracted handover, and git's working tree keeps its meaning. |
| "engine" for `agentcontext/runner.py` | "`agentcontext/runner.py`", or "the runner" where the sentence names one | The engine of a GitHub Agentic Workflow is the tool that runs it. |
| "content check" | "content test" | One name for what a file has to contain for its match mode. |
| "regression baseline" | "regression manifest" | One name for `output/baseline.json`, which `regressions check` compares `output/` with. |
| "stated figure", "figures of the documentation", "figures of the documents", the command `figures` | "stated statistic", the command `statistics` | A figure is a plot that an analysis script writes, and a number that a document states is a statistic. |
| bare "evaluation" where a document could mean either evaluation | "detection accuracy evaluation", "label evaluation" | The two measure different stages. |
| "gate", "gated", in any sense | "verification suite", "pre-push verification", "pre-deployment checks", "reproduction check", or the condition itself | Gate is a metaphor that hides which check or condition is meant, and the glossary check fails on every use of the word. The match mode `copy_dir_gated` keeps its name, and `not_configuration.csv` quotes the "gate manifest" of Agent Fleet, and `planning_document_patterns.csv` keeps BMAD's folder `docs/qa/gates/` and its name "QA gate". |
| "crawl position" | "crawl date" | The term names a date, the latest `lastCommit` date of a language in GHS. |
| "anchor", "anchored" for the sampling window | "end date" | The window is counted back from a date, and the term names that date. An HTML anchor keeps the word. |
| "ask", "asks", "asked", "asking", in any sense | "query", "request", "check", "test whether" | A step queries GitHub and a check tests a condition. The glossary check fails on every use of the word. Write "request" for a person too. The license name "BSD - ask to endorse" in `1_sampling/config/spdx_licenses.json` is SPDX's and keeps the word, and so does `ask-bonk`, the name of Cloudflare's GitHub app. `heuristics/ai_commit_patterns.csv` and `heuristics/other_ai_commit_patterns.csv` keep it too, because their rows read the word in the lines that commits write, such as `I asked Claude to write` and `asked GPT-5.5 to`. |
| "answer", "answered", "unanswered", in any sense | "response", "returns", "responds", "returned no response", "the model's response" | GitHub, GHS, git and a model return a response. The glossary check fails on every use of the word. Write "reply" or "response" for a person too. |
| "frame", in any sense outside "sampling frame" | "sampling frame", or "DataFrame" for a pandas table | One term for one concept: the sampling frame is always named in full. The glossary check fails on every other use of the word, except the framework Frame and its folder `.frame/` in `planning_document_patterns.csv`. |
| "agent run", "run" for one execution of a coding agent | "agent loop" | The loop engineering study defines the agent loop, and in this repository a run is the work of a runner or of a step's program. A pattern cannot tell the noun from "the agent runs", and the writer therefore checks it. |
| "theme" for a change | the module, defect or behavior that changed | A theme groups changes without naming any of them. A tool's color theme keeps the word. |
| "coding tool", "AI coding tool", "agentic coding tool", "agentic AI coding tool", "AI tool" | "coding agent" for the program, "tool" for a vendor's coding agent under all its names, "the six tools", "a tool outside the six" | One concept had five names. A coding agent is what reads the context and makes the change, and a tool is the unit by which the dataset names it. The glossary check fails on every lowercase use of the words, so that the titles of the AIware 2026 papers keep theirs, and `planning_document_patterns.csv` keeps the vendors' quoted words. |
| "preflight" | "pre-checks", and the component `pre_checks` in the log | Preflight borrows a pilot's checklist for the checks before the first pending step starts, which "pre-checks" names literally. |

Source quotations and design records under `docs/history/` keep the vocabulary that their
evidence needs. The glossary check covers hyphenated compounds as well as phrases with
spaces. It also checks the latest commit message after joining wrapped lines and removing
attribution trailers and code spans. Earlier commit messages are historical records and are not
rewritten.

## Adding a term

Define the term in its section, then add the word that it replaces to the table above and to
`tests/test_glossary.py`. A word with a legitimate second sense needs a pattern that matches only
the retired sense, as the `diagnostic` and `frame member` entries in that file do.

State a count in a definition only when the distinction requires it. `docs/dataset.md` and
`output/snapshot.json` contain the current dataset counts, and `tests/test_stated_facts.py` checks
them against the current inputs. Where `jaws26-loop-engineering/GLOSSARY.md` defines a term that
describes this dataset, such as the sampling frame, the sample or the commit history, use the
same definition, because that study draws its data from this dataset. Its tool set, its loop
mechanisms, its heuristics and rules, and its populations after the sample belong to its own
pipeline, and its retired words apply to that repository.
