# Changelog

This file documents every notable change to this pipeline and to the two datasets that it has
built. AgentTorrent, *AgentTorrent: A Dataset of Context and Harness Engineering Artifacts on
GitHub*, succeeds AgentConfigs, *A Dataset of Agentic AI Coding Tool Configurations*, which an
earlier version of the pipeline built and which the AIware 2026 dataset paper describes.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Versioning policy

The version number describes the dataset as well as the code, because a downstream user cannot tell
the two apart: The same script over a different snapshot and a different script over the same
snapshot both produce different CSVs.

| Bump | When |
|-------|------|
| **Major** | The meaning or membership of a published column or row changes. A new collection snapshot is always major, and so is a detection heuristic that reclassifies existing configuration artifacts. |
| **Minor** | Something is added without changing what is already there: a new column, a new mechanism that was previously unclassified, a new script. |
| **Patch** | Fixes with no effect on any published value: documentation, tests, refactoring, tooling. |

Each dataset has versions of its own, and a version heading names its dataset, because both
datasets have a version 1.0.0. Every release of AgentTorrent is git-tagged
`agenttorrent_<version>`, such as `agenttorrent_1.0.0`, and recorded in the `dataset_version`
column of `repos.csv`, of every mechanism CSV, of `planning-documents.csv` and of `imports.csv`.
The commit CSVs have no such column, and the release's `provenance.json` records the version.
AgentConfigs 1.0.0 is tagged `agentconfigs_1.0.0` in the private repository
`se-uhd/agenttorrent-pipeline-internal`, and its CSVs have no `dataset_version` column.

## [Unreleased]

## [AgentTorrent 1.0.0] - 2026-11-12

The first release of AgentTorrent, published on Zenodo with its pipeline. The entries of this
section list the changes since AgentConfigs 1.0.0. The detection heuristics of this release
reclassify the configuration artifacts that AgentConfigs 1.0.0 published: Claude Code command
files are skills, files that were `other` are rules, hooks or plugins, and a repository is
configured when a mechanism is detected in it. The generated tables and figures describe the new
collection under the heuristics recorded with it.

Before its publication, this release was tagged `v2.0.0`, and until it was renumbered, the
website offered its files with the `dataset_version` 2.0.0.

### Added

- Plugins as a ninth mechanism: `plugins.csv` and a `plugins` flag in `repos.csv` record the plugin
  manifests and marketplace catalogs of Claude Code, Cursor, Codex, Copilot CLI, Gemini CLI and
  Agent Plugins, with the parts of a plugin under their own mechanisms.
- LSP servers as a tenth mechanism: `lsp.csv` and an `lsp` flag in `repos.csv` record the language
  servers that Copilot CLI, Claude Code and OpenCode start for a repository.
- OpenCode as a sixth tool, with an `opencode` flag in `repos.csv`, its configuration in the
  mechanism CSVs, its plans among the planning documents and its commits in `commits.csv`.
- Imports, which set no tool flag: `imports.csv` lists each import, such as `@docs/style.md` in a
  `CLAUDE.md`, that Claude Code, Claude Code Review, Copilot CLI, Gemini CLI, Antigravity or Bugbot
  reads, and `imported_files.7z` contains the imported files that `repos_data.7z` lacks.
- Planning documents, which set no tool flag: `planning-documents.csv` lists the specs, plans and
  tasks that spec-driven development frameworks, such as Spec Kit and OpenSpec, and the planning
  features of the tools write at a documented location, and it tells frameworks that write the
  same paths apart by the opening of their documents, such as Google's Conductor and the conductor
  plugin of claude-code-workflows.
- `other_commits.csv` with the commits that credit one of
  <!--s:heuristics.other_ai_commit_patterns.tools-->154<!--/s--> tools outside the six, such as
  Devin, Jules, CodeRabbit and aider, and under `tool not named` the commits that credit only a
  model, its vendor or AI. An `Impl-Model:` trailer, which names the model that implemented the
  change, credits `tool not named` for a model of Anthropic, OpenAI, xAI, Moonshot AI or Z.ai, for
  Cursor's Composer, for a list of such models and for `auto`, and `Gemini (unspecified)` for a
  Gemini model. `Helix-Harness: pi` and `Harness: pi` credit pi.
- Columns:
  - `head_sha`, `scanned_at` and `dataset_version` in every mechanism CSV and in `repos.csv`, which
    has <!--s:parameters.agentcontext.steps.release.REPOS_COLUMN_ORDER.length-->54<!--/s--> columns
  - `tool` in every mechanism CSV, with the tools that read the configuration artifact joined with
    `|`, or `any` for a file that several tools read and none owns, such as `AGENTS.md`, and the
    tool flags of a repository are the tools of its rows
  - `matched_heuristics` in `commits.csv`, with the `id` of each commit heuristic that matched
  - `attributions` in `commits.csv`, which states for each tool whether the commit credits it as
    `account`, `reviewer`, `standard` or `convention`
  - the <!--s:parameters.agentcontext.heuristics.AI_COLUMNS:words-->four<!--/s--> AI columns of
    `context_files.csv`, which were a separate file
- Configuration that the tools introduced after AgentConfigs 1.0.0:
  - Context files: `CLAUDE.local.md`, `.cursor/BUGBOT.md`, `.gemini/styleguide.md` and a root
    `REVIEW.md`, which names no tool.
  - Settings: `.claude/launch.json`, `.cursor/environment.json`, Bugbot's configuration,
    `.codex/environments/*.toml`, `gemini-extension.json`, `.github/copilot/settings.json` and
    `.github/allowed_models.txt`.
  - Skills: `.agents/skills`, a `SKILL.md` at any depth, `.claude/workflows/*.js`, and the skills
    that an Antigravity plugin's manifest lists.
  - Subagents: `.gemini/agents`, `.codex/agents`, `.github/chatmodes`, the role file that
    `config_file` of `.codex/config.toml` names, and the memory directories of Claude Code's
    subagents.
  - Commands: Copilot prompt files, `.github/prompts/*.prompt.md`.
  - Hooks: `.claude/settings.local.json`, `.codex/hooks.json`, a `[hooks]` table in
    `.codex/config.toml`, `.cursor/hooks.json`, the hooks of Copilot CLI's repository settings and
    of Claude Code mods, and the rule files of Anthropic's hookify plugin.
  - Rules: `.claude/rules` and `.claude/output-styles`.
  - Plugins: Copilot CLI's marketplaces, the directories that the marketplaces of Cursor, Codex
    and Copilot CLI name, as plugins of the marketplace's tool, the portable `skills/` and
    `mcp.json` as parts of Codex or Copilot CLI where its manifest or marketplace makes the
    plugin its own, and Antigravity's plugin manifests with their `agents.json` and
    `skills.json`.
  - MCP: `.github/mcp.json`, `.vscode/mcp.json`, a `gemini-extension.json` that declares
    `mcpServers`, and the Agent Plugins `mcp.json`.
  - Unclassified files under `.agents/`, `.github/` and the plugin manifest directories, and the
    root ignore files of Cursor, Gemini CLI and Antigravity.
- Workflows that run an agent count as `settings` of that tool, such as a workflow that runs a
  tool's action or `claude -p` and a GitHub Agentic Workflow, and the collection keeps the prompt
  file that a workflow gives the Codex action or the Claude Code Base Action.
- Context files that a configuration names, such as Gemini CLI's `context.fileName` and Codex's
  `model_instructions_file`, in the category `Named in Configuration` of the analysis figures.
- Context files named `AGENTS.md`, `CLAUDE.md` or `GEMINI.md` in another letter case at the
  repository root, such as `claude.md` or `Agents.md`.
- `.claude/AGENTS.md` sets the Claude flag where Claude Code reads it.
- Mechanism directories below the repository root, as in a monorepo that configures its packages
  one by one.
- A root `plugin.json` in the form of a legacy Copilot manifest counts as a Copilot CLI plugin.
- In the release: the heuristics and their negatives, the record of the documentation watch,
  `provenance.json`, `snapshot.json`, `sampling.json`, `checksums.sha256`, `CITATION.cff` and the
  records of the collection, such as `deletions.csv` and `commit-reclassification.json`.
- `unmatched-attributions.csv` in the release, with the attributions of unmatched commits that name
  a bot or a tool and no person.
- The code of the pipeline is under the MIT license and the data under CC BY 4.0, and the collected
  files and commit messages keep the licenses of their repositories.
- `github_id` in `repos_annotated.csv`, GitHub's id of the repository, which appears once in the
  sample.
- Of the engineered projects at one head commit, such as a mirror and its source, the sample keeps
  the first that GitHub created of those that nothing describes as a mirror, and
  `repos_excluded.csv` lists the others as `copy`.
- The cache of retrievals, `data/sampling/ghs_retrievals/`, with each GHS search result and
  GitHub's response for its repositories, and `data/sampling/ghs_changes.json` with the
  repositories that entered, left or changed between two retrievals.
- `sampling.json` in the release records the GHS retrieval with its window and query, when GitHub
  was queried, what each filter step removed and the decision on each license that the license
  step read.
- `python -m agentcontext evaluation`, which estimates from labeled samples the precision and
  recall of the detection and the precision of the imports and the planning documents.
- The collection and the publication as one package, `agentcontext`, with one command line,
  `python -m agentcontext <command>`.
- `run_pipeline.py`, which runs the five stages with one pipeline execution state.
- A container for an unattended run of every stage on a NAS, with the runbook `docs/deploy.md`, and
  an x86-64 development image.
- A collection that clones each repository once, within
  <!--s:parameters.agentcontext.git.bounds.DEFAULT_MAX_BYTES-->32<!--/s--> GiB and
  <!--s:parameters.agentcontext.git.history.GIT_TIMEOUT_SECONDS.hours:words-->one<!--/s--> hour,
  resumes after a stop and takes over each unchanged repository from an earlier collection.
- The collection keeps the file list and the commit log of each clone, and
  `commit-reclassification`, also as `run_pipeline.py --with-commit-reclassification`, applies new
  commit heuristics to the commit logs without a clone.
- The heuristics as data under `heuristics/`, each row with the vendor page that defines it and the
  date of reading or with the commits or files that it was read off, and the negatives
  `not_configuration.csv` and `not_ai_commits.csv`.
- `python -m agentcontext docs`, which detects a changed vendor page that a heuristic cites.
- Records and checks of the dataset: `output/snapshot.json`, `regressions check`,
  `snapshot validation`, `deleted-files-report`, `python -m agentcontext verify` and
  `4_data-analysis/check_reproduction.py`.
- Two analysis tables of the planning documents and of the imports.
- Each classification run records the tokens of its requests and their cost in USD in
  `<id>.cost.csv` in the cache of labels.

### Changed

- AgentTorrent 1.0.0 contains a new collection of a run of all five stages over its own GHS
  retrieval:
  <!--s:collection.configured_repositories-->10,944<!--/s--> configured repositories of
  <!--s:collection.sample_repositories-->40,901<!--/s--> in the sample,
  <!--s:collection.commits.tools-->1,281,628<!--/s--> commits that credit the six tools and
  <!--s:collection.commits.other_tools-->72,527<!--/s--> that credit others.
- The dataset is named *AgentTorrent: A Dataset of Context and Harness Engineering Artifacts on
  GitHub*, succeeds the AgentConfigs dataset and numbers its versions from 1.0.0. It was called
  AgentContext before its release, and published schemas keep their names.
- The website moved to https://se-uhd.de/agenttorrent/, which the old addresses forward to, and has
  tabs for plugins, LSP servers, planning documents and imports.
- The website's timeline of the earliest configuration artifacts and the analysis figure
  `cumulative_repo_adoption.pdf` show the kinds of the timeline of AgentConfigs 1.0.0 and the
  planning documents, and leave out the files created before 2023.
- The website's commit browser lists the commits of other tools, with an Other tools option in
  its tool filter.
- The website's usage example studies a research question with each CSV that it reads, including
  `imports.csv`, `planning-documents.csv` and the `attributions` of `commits.csv`.
- Every CSV of the dataset ends its lines with LF instead of CRLF.
- `python -m agentcontext release` builds the release into `release/`, and `_dataset/` is gone.
- The release README computes every count from the files beside it, defines every column and every
  criterion that a justification names, and names the GHS retrieval, the scan dates, the model
  that labeled the sample and each evidence limit, such as clone failures. Its disclaimer covers
  the planning documents and the imported files, and it groups the repositories that the
  classifying model labeled at its higher temperature by the reason.
- The release stops before it writes anything when its inputs disagree.
- The sampling window moves with every collection, ends on the oldest crawl date of GHS's
  languages and takes repositories created at least
  <!--s:parameters.1_sampling.config.ghs_download.window.created_before_months-->18<!--/s--> months
  before that date with a commit in the
  <!--s:parameters.1_sampling.config.ghs_download.window.committed_within_months-->6<!--/s--> months
  before it.
- The filter reads GitHub's values, removes repositories that GitHub no longer has or that are
  newer than the sampling window allows, treats rows that GitHub resolves to one repository as
  duplicates, and matches whole topics and name segments in its educational step.
- The classification labels each repository in real time, not in a batch, from its README, its
  description and its activity.
- The classification gives the model an outline of the files, GitHub's metadata, the activity of
  the issues and pull requests, the profile description of the owner and the linked pages that
  could change a field, but neither the stars nor the forks.
- By default, DeepSeek V4.1 Flash labels the repositories and summarizes the linked pages through
  OpenRouter on Morph's fp8 endpoint at reasoning effort high, temperature
  <!--s:parameters.2_classification.endpoints.TEMPERATURE-->0.6<!--/s-->, top_p
  <!--s:parameters.2_classification.endpoints.TOP_P-->0.95<!--/s-->, seed
  <!--s:parameters.2_classification.endpoints.SEED-->7<!--/s--> and an output limit of
  <!--s:parameters.2_classification.endpoints.MAX_OUTPUT_TOKENS-->65,536<!--/s--> tokens.
- After a failed response, DeepSeek V4.1 Flash labels the repository at temperature
  <!--s:parameters.2_classification.endpoints.HIGHER_TEMPERATURE-->1.0<!--/s--> with an output
  limit of <!--s:parameters.2_classification.endpoints.HIGHER_MAX_OUTPUT_TOKENS-->262,144<!--/s-->
  tokens, and GLM-5.3, at temperature
  <!--s:parameters.2_classification.endpoints.SECOND_MODEL_TEMPERATURE-->1.0<!--/s--> and top_p
  <!--s:parameters.2_classification.endpoints.SECOND_MODEL_TOP_P-->0.95<!--/s-->, labels a
  repository that DeepSeek V4.1 Flash still leaves without a decision.
- While the pinned provider of a model fails, a backup provider of the same weights in the same
  quantization serves its requests, and `repos_annotated.json` lists the repositories labeled
  through a backup provider, at the higher temperature or by GLM-5.3.
- The model sets each exclusion criterion to `true`, `false` or `unclear`, a repository to which a
  criterion applies or that states no purpose is labeled `false`, and the label `unclear` replaces
  `unsure`.
- New exclusion criteria cover, among others, a website as the primary purpose, inactivity,
  datasets and work in progress, and they exclude code for a paper or an experiment, software for
  its author's own use and single scripts unless others maintain and use them.
- A mirror is left out unless its own texts invite pull requests or it merged pull requests other
  than those of GitHub Apps in the sampling window, and a repository with a README that says that
  the project moved is left out in any case.
- A fork is kept when it does not track its upstream and has at least
  <!--s:parameters.1_sampling.config.ghs_download.query.contributorsMin:words-->two<!--/s--> authors
  of its own and a commit in the sampling window.
- A README that is not mainly English by the model's count of its words of prose is labeled
  `false`.
- The classification prompt defines in plain language the terms of its criteria, such as a purpose,
  an engineering practice and an inactive project.
- A purpose counts only from the description, the README or a page that one of them or the
  homepage links to.
- A label is reused while the wording of the README, the description, the model and the
  classification parameters are unchanged, and `label_changes.csv` lists each changed label with
  its cause.
- The justification in `repos_annotated.csv` ends with the verdict of the classification and the
  criteria that decided it.
- The label evaluation compares the labels with those of GLM-5.3 on a sample that leaves out the
  earlier samples and the repositories that the classification prompt names.
- A classification run stops when the endpoint keeps failing the response format, its tool server
  has ended or the API refuses the key or the model.
- Commit detection reads the <!--s:heuristics.ai_commit_patterns.rows-->431<!--/s--> rows of
  `heuristics/ai_commit_patterns.csv`,
  <!--s:heuristics.ai_commit_patterns.doc_rows:words-->eleven<!--/s--> of them documented by a
  vendor.
- A person named Claude no longer counts as Claude Code, and a credit to Gemini alone is in
  `other_commits.csv` as `Gemini (unspecified)`.
- An identity credits a tool as `account` only when it is the tool's account, an identity that the
  tool writes, or an agent account or app that its page or its project's configuration identifies
  as running the tool, and as `convention` when users named it after the tool, such as
  `claude@anthropic.com` as an author.
- A co-author line is `standard` only when it names the tool at its own address or names an
  identity that credits the tool as `account`, or when a layer that runs the tool writes it, such as
  `Co-authored-by: OmX <omx@oh-my-codex.dev>` for Codex.
- A review suggestion of the `claude` app, MelvinBot, CodeRabbit, Devin, Macroscope or Roomote
  that a person committed with GitHub's subject for it credits the app or the account as
  `reviewer`.
- `commits.csv` credits the six tools in more forms, such as `Assisted-by:`, `Made-with:` and
  `Helix-Harness:` lines, the AI disclosures of projects and pull request templates, a statement
  that a tool helped make a part of the change, Copilot Autofix's footer and the links of Codex
  cloud tasks.
- `commits.csv` credits a tool of the six in `Agent:`, `Tool:`, `Client:`, `Harness:`,
  `Coding-Harness:`, `X-AI-Tool:` and `AI:` lines that name it as their whole value, in list
  items such as `- Harness: Codex desktop`, in the session footer of a shipping skill, such as
  `Agent session: codex resume <session>`, in the provenance footer of elizaOS/eliza, in a
  `Generated-by` line with a space before the colon and in `Note: AI (Codex 5.6) was used to
  generate this set of PRs`.
- `commits.csv` credits Cursor, Codex and Claude Code in the AI attribution statement of the AI
  Attribution Toolkit, such as `AI-attribution: AIA PAI Hin R Cursor 3.1.7 v1.0`, and
  `other_commits.csv` counts a statement that names only a model under `tool not named`.
- `commits.csv` credits Claude for `Co-authored-by: [CC] <noreply@anthropic.com>`, TA-Lib/ta-lib's
  `Co-authored-by: MF,CC`, Commonly's `Agent-Model: claude/<model>/<effort>` and the bold label
  `**Tool:** Claude Code`, Copilot for a line that names it after `and` and for
  `agent-tool: <copilot-chat>`, and Cursor for `Assisted-by: <Cursor>`.
- A trailer after the bullet `* ` at the start of a line credits its tool, as in the
  `* Co-authored-by: ...` items of GitHub's squash merges.
- A co-author line with an address at the end of a `* ` item credits its tool, as in the squash
  merges that list `* <subject> Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- `other_commits.csv` credits kres for `Assisted-by: kres (<model>)`, Jules for
  `Co-authored-by: Jules Agent`, Junie for the identity of its GitHub Action, Kiro for
  `Co-developed-by:`, `X-AI-Tool:` and `AI-Agent:` lines, Hermes Agent for `AI-Tool:` and
  `AI-Agent:` lines, oh-my-pi for `agent-tool: OMP`, Agent Kanban for its `Agent-Profile:` trailer
  and for the addresses of its agents at `mails.agent-kanban.dev` as the author or the committer,
  aider for the chat that it wrote into commits until 2024-06-11, pi for the footer of pi-config and
  for `AI-Assisted: yes [pi]`, ZCode for `AI-Assisted: yes (ZCode CLI, GLM model)`, and Grok Build
  and Hermes Agent for the client line of the provenance footer of elizaOS/eliza.
- `other_commits.csv` counts under `tool not named` the lines `AI-Assisted: yes` and
  `AI-Generated: true` without a tool, `AI-Model:` with an OpenAI model, `Agent: ChatGPT`,
  `agent-tool: unknown`, Moonshot AI's model Kimi K2.7 Code, a model before an address with the
  local part `ai`, such as `haiku <ai@salesforce.com>`, and the disclosures
  `AI-assisted: This change was developed with AI assistance.` and
  `This PR was prepared by an AI agent`, and under `Gemini (unspecified)` the lines
  `AI-Assisted: yes (Gemini 3.8 Flash)` and `*Note: Created by an AI agent (Gemini).*`.
- `commits.csv` credits Cursor for Composer at Cursor's address in `Co-authored-by:` and
  `Authored-by:` lines, such as `Co-authored-by: Composer <composer@cursor.com>`.
- `commits.csv` credits Gemini for Antigravity as the client of the provenance footer of
  elizaOS/eliza, such as `Client / agent tooling: Antigravity CLI`.
- `other_commits.csv` credits TRAE for `Co-authored-by: Trae AI <noreply@trae.ai>`.
- `other_commits.csv` counts `X-AI-Tool:` with only a Claude model, such as
  `X-AI-Tool: Claude Opus 5.5`, under `tool not named`.
- `commits.csv` credits a tool of the six in a co-author line without `-by`, such as
  `Co-Authored: Claude Code`, and under the keys `Co-author:`, `Co-authored-with:`, `AssistedBy:`
  and `Co-Generated:`, and `other_commits.csv` counts `AssistedBy: AI` under `tool not named`.
- `commits.csv` credits Claude for `Implementation-By: Claude Code`, `ClaudeCode:<model>` and the
  item `- Authoring: Claude Code` of the pull request template of UKGovernmentBEIS/inspect_ai,
  Codex for `Edited-by: Codex` and `Codex-Model:`, and Claude, Cursor and Copilot under
  `Generated-with:` and `Generated-with-ai:`, also Copilot, Codex, Cursor or OpenCode when such a
  line names it after `via` or in parentheses, as in `Generated-with-ai: claude-4-sonnet (Cursor)`.
- The `Agent-Signature:` trailer of beads credits the runtime that it names first: Claude Code,
  Codex, Amp or Kilo Code.
- `commits.csv` credits Gemini for Antigravity in rsyslog's AI footer, such as
  `With the help of AI-Agents: Antigravity`, and `other_commits.csv` counts the footer with only a
  model under `tool not named`, such as `With the help of AI-Agent: ChatGPT`,
  `With the help of AI-Agents: GPT-5.5` and `With the help of AI-Agents: Composer`.
- `commits.csv` credits Claude, Codex or Cursor after `Code Generated-by:`, `Code Assisted-by:`,
  `Implementation assisted-by:`, `Co-authored-by agent:` or `Assisted-by:i`, and Cursor misspelled
  as Cursour after `Generate-by:`, and `other_commits.csv` credits oh-my-pi and pi when the
  `Co-authored-by agent:` line of rizinorg/rizin names them as the agent.
- `other_commits.csv` counts under `tool not named` a model alone after `Assisted by:`, a mistyped
  co-author or assistant key or `AI Co-author:`, such as `Assisted by: Opus 4.8 LLM`,
  `Assited-by: ChatGPT (OpenAI)` and `AI Co-author: glm-5.1`, and
  `Agent-Authored-By: AI agent acting on <user>'s behalf`.
- `other_commits.csv` counts under `tool not named` a model alone as the value of
  `AI tools and models used:` in the disclosure of the pull request template of
  jupyterlab/jupyterlab, a labeled disclosure that credits a model or a vendor's assistant with a
  part of the change, such as `AI disclosure: Code partly written by GPT-6.1 Sol`, and the
  translations that ONLYOFFICE/DocSpace-client tags with a model, such as
  `Translate new keys with AI (Opus 5)`, and under `Gemini (unspecified)` a labeled disclosure that
  credits Gemini alone with a part of the change, such as `AI Usage: Change generated by Gemini.`
- `commits.csv` and `other_commits.csv` credit a tool in a disclosure after the label
  `Disclaimer:`, such as `Disclaimer: This PR has been co-authored by Copilot.`
- `commits.csv` credits Copilot for the prefix `Copilot agent :copilot: (on behalf of @<login>):`
  with which one developer's agent begins its pull requests, Codex for
  `Implementation and review: Codex`, `AI authorship: Codex` and
  `Contribution-attribution: <model> via Codex`, and Claude for `Initial prototype: Claude` and
  ONLYOFFICE/DocSpace-client's translation tag `AI translate <keys> with Claude Opus v4.8`, and
  `other_commits.csv` credits Devin for `Assisted-by: Devin: <model>`.
- `commits.csv` credits Claude for `Author:` with Claude Code or a Claude model, such as
  `**Author:** Claude Code (claude-opus-4-7)`, Codex for `Author: Codex`, Gemini for Antigravity
  after `Author:`, Cursor and Copilot for the header `Author:` with the address of Cursor's agent or
  of Copilot's apps in a list of `git merge --squash`, and Copilot, Codex, Cursor or OpenCode for a
  line under `AI-Tool:`, `X-AI-Tool:`, `AI-Agent:`, `AI-Assistant:`, `Coding agent:`, `Tools used:`
  or `Author:` that names Claude and then the tool after `via` or in parentheses, as in
  `X-AI-Tool: Claude Code via Cursor`.
- `other_commits.csv` credits Amp for the commits that `Amp <amp@ampcode.com>` authored, for its
  `Amp-Thread-ID:` trailer, also when GitHub wraps the URL of the thread onto the next line, and for
  a labeled disclosure that Amp made the change, such as
  `AI disclosure: this fix was generated by Amp.`
- Commits that credit Grok Bot, the agent app of SpaceXSI, take the label Grok Bot instead of
  `tool not named`, also in the `Client: Grok Bot.app` footer of elizaOS/eliza, in
  `written with an AI coding assistant (Grok Bot)` and for the agent Tali.
- The label Grok CLI is replaced by Grok Build, the product of xAI's `grok` CLI.
- `Grok Build 0.1` and `Kimi Code 2.7` at the addresses of their vendors count as models under
  `tool not named`, not as Grok Build and Kimi Code.
- `Client / agent tooling: Freebuff CLI` in the provenance footer of elizaOS/eliza credits Codebuff.
- `commits.csv` credits the apps and agent accounts under which projects run a tool, such as the
  GitHub apps of the Claude and Codex coding agents and Expensify's MelvinBot, and
  `other_commits.csv` the identities of Roo Code's cloud agent Roomote, with their co-author lines
  as `standard`.
- A model that another agent ran counts for that agent and not for the model's own tool, as in
  `Assisted-by: Claude Opus 4.8 (opencode)`.
- A credit that a commit only quotes, such as the release notes of a dependency update or the
  subject of a revert, credits no tool, and neither does a credit of a review, an analysis, a search
  for a bug, the navigation of the code, a run of a repro or an experiment, brainstorming, a
  consultation, comments on the pull request or its description alone, also when the credit
  follows a statement that the tool was used.
- A trailer after an escaped line break, an emoji, a lone carriage return or the color codes of a
  terminal credits its tool.
- A repository is configured when a mechanism is detected in it instead of when it has a tool
  directory.
- Claude Code command files, `.claude/commands/*.md`, are in `skills.csv`, and `commands.csv`
  contains the commands of Cursor, Gemini CLI, Copilot and OpenCode.
- `no_specific_tool` replaces `agents_md_only` in `repos.csv` and is TRUE for a repository with a
  detected mechanism and none of the six tool flags, also without an `AGENTS.md`.
- Matching follows the vendors' documentation and what each tool loads, such as only the `.mdc`
  files of `.cursor/rules`.
- The root `.mcp.json` sets no tool unless its servers are under `servers` alone, which sets
  Copilot, and a `.mcp.json` below the root with a server under `mcpServers` sets Claude Code beside
  Copilot. There and in a plugin, Claude Code counts only for a server that it loads, one with a
  `command`, or with a `url` and its `type`, and with the types that Claude Code declares for its
  other fields. A plugin manifest with servers inline counts for Claude Code only where Claude
  Code accepts every one of them.
- `.github/CLAUDE.md` is Claude Code's.
- Antigravity counts as Gemini, in its configuration as Antigravity loads it, such as a rule only
  with front matter that Antigravity decodes, and in its commits.
- `other.csv` no longer counts the plans in `.claude/` and `.cursor/`, which are planning
  documents, nor the prompt file that an OpenCode agent names with `{file:...}` and the file that
  a Codex agent role names in `model_instructions_file`, which are parts of their agents.
- The collection leaves out the session logs, caches, locks and worktrees that the tools keep in
  their directories, and `imports.csv` marks an import of such a file as `pruned`.
- A submodule's files belong to the repository that the submodule names.
- The `language` of a linked rule, agent, skill or command is the language of the file that the
  link names, or blank when the collection did not copy that file.
- `head_commit_date`, the committer date of `head_sha`, replaces `timestamp`.
- `#commits` counts a file's commits under every name that it had.
- A cell of the mechanism-by-tool heatmap counts the repositories with a configuration artifact of
  the mechanism attributed to the tool, and a column counts the repositories with no specific tool.
- The pipeline runs on Python 3.14 with git 2.55.0, rsync 3.5.1 and 7-Zip 25.01, as `mise.toml`
  pins them.
- Everything that a run reads and writes is under one data root, `data/`, with a directory per
  stage, or under the data root that `AGENTCONTEXT_DATA_ROOT` names, and neither the image nor the
  NAS bundle contains data.
- Unattended runs continue through failures:
  - The labeling lists the repositories that it cannot decide as `undecided` in
    `repos_excluded.csv` and tries them again in a later run before the collection begins.
  - A new image that changes the collector's code or the heuristics of what the collection copies
    continues an ended collection and collects again the repositories that the change reaches.
  - A retrieval with fewer than
    <!--s:parameters.1_sampling.1_download_ghs.MIN_SHARE_OF_PREVIOUS-->90%<!--/s--> of the previous
    retrieval's rows, or with fewer than
    <!--s:parameters.1_sampling.1_download_ghs.MIN_LANGUAGE_SHARE_OF_PREVIOUS-->75%<!--/s--> of the
    rows of a language that had at least
    <!--s:parameters.1_sampling.1_download_ghs.LANGUAGE_ROWS_MIN-->1,000<!--/s-->, counts as a
    failed download, and the run keeps the previous sampling frame.
  - The filter removes the rows that the GitHub query records as `unresolved` and keeps no language
    that the end date of the retrieval left out.
  - The collection retries what failed on the network, records a clone that exceeds
    `--clone-timeout` again on a retry as `time_bound_exceeded`, and continues when the commits
    of some repositories cannot be reclassified.
  - The entrypoint begins a monthly run in each new calendar month, runs no step with an outdated
    `compose.yaml`, a time zone other than UTC or a data root in a format that it does not convert,
    and resumes a run that a reboot killed.

### Removed

- From the pipeline's repository: the records and scripts of the batch classification of March
  2026, the shell collector, the historical paper audit, the comparisons with the previous
  publication, `count_tool_repos.py` and `4_data-analysis/rq1/`.
- `1_sampling/ghs_complete.json.7z`, the March GHS search result, which the cache of retrievals
  contains as `2026-03-16T180701Z`.
- The `is_empty` column of the mechanism CSVs.
- The `*-extended.csv` files and `context-files-ai.csv`, whose columns are now in the mechanism
  CSVs.

### Fixed

- Configuration artifacts that AgentConfigs 1.0.0 lost, such as `.claude/rules`,
  `AGENTS.override.md` and `CLAUDE.local.md` inside `.github`, and rows that it counted twice.
- `.github` subdirectories, the `copilot-instructions` flag and hook and MCP files are matched
  regardless of case and recorded as spelled on disk.
- Symbolic links:
  - A mechanism or tool directory that is a link is collected, and each tool counts for what it
    reads through the link.
  - The item behind a link or a chain of links is detected and kept, and a configuration artifact
    reached through a link is reported once at its real path.
  - A link inside a copied folder leaves its target unchanged, a link to a directory there is kept
    as its marker, and a link out of the repository is removed.
  - A link in another tool's directory to `.agents/skills` adds no reader of the shared skills,
    and a skill folder of `.cursor/skills` that links outside it adds Cursor only where Cursor
    reads the skill at its own path, such as in `.agents/skills`.
  - The collection of a repository fails when rsync copies the target of a link only in part.
- Skills:
  - Codex counts only for a `SKILL.md` that it loads and Cursor only for one with front matter
    that parses, and Cursor counts for one in `.codex/skills` that Codex skips.
  - Cursor and OpenCode get the skills in the category folders of `.claude/skills`.
  - A Gemini CLI skill is a `SKILL.md` in a folder directly in `.gemini/skills` or in an
    extension's `skills/`, and an Antigravity skill needs front matter.
  - A folder of `.claude/commands` with a `SKILL.md` is one skill, and the other files of a skill
    folder get no tool of the skills directory.
- Subagents:
  - A Gemini CLI agent needs front matter with a description and only tools that Gemini CLI
    accepts.
  - A Markdown file in `.cursor/agents`, `.codex/agents` or in `.claude/agents` that Claude Code
    does not load is a Cursor subagent only with front matter at its top.
  - OpenCode's `agent` and `agents` keys count unless they name only OpenCode's built-in agents,
    a Markdown agent of OpenCode named after a built-in agent does not count, and an Antigravity
    agent that is a folder counts with its `agent.md` in any letter case.
- A hooks file of Claude Code, Codex or Gemini CLI counts only with a handler in a matcher group of
  an event that its tool reads, and a Copilot hook file counts also in a subfolder of
  `.github/hooks`.
- An MCP file counts only when it configures a server in the form that its tool reads, the
  `.app.json` of a Codex plugin only when it names an app, and an Agent Plugins `mcp.json` outside
  a plugin only with the `$schema` and the `mcpServers` field that the standard requires.
- A plugin manifest that configures servers or hooks inline is a row of their mechanism: a Claude
  Code manifest for MCP servers, LSP servers and hooks, a Codex manifest for MCP servers and hooks,
  and a Cursor manifest or marketplace entry for MCP servers. A Codex marketplace counts as a
  plugin only with an entry from which Codex installs a plugin.
- A directory named `_agents` or `_agent` counts as an Antigravity customization root only where it
  contains a place that Antigravity reads.
- A workflow that a vendor names, such as `.github/workflows/claude.yml`, counts as `settings` only
  with a line that is not a comment, and `gemini-dispatch.yml` only where it calls a reusable
  workflow of the repository that runs Gemini.
- In `imports.csv`, a code fence of Copilot CLI closes as Copilot CLI 1.0.107 reads it. A chain of
  Claude Code or Gemini CLI from a root file in another case, such as `claude.md`, finds a file
  that an import names in another case, and Claude Code starts no chain at a root `agents.md`
  beside a root `claude.md`.
- In `imports.csv`, an `@` in the text of a Markdown link to a mail address, such as
  `[@octocat](mailto:octocat@example.com)`, and the `@` of a mail address in a `mailto:` URI or
  an autolink start no import of Claude Code, Copilot CLI or Gemini CLI.
- `repos_data.7z` keeps the files that a GitHub Agentic Workflow loads into its prompt with
  `{{#runtime-import}}` or `{{#import}}`.
- `referenced_file` keeps a leading `../` and the dot of a dot folder of an `@` reference, such as
  `@../AGENTS.md` and `@.github/copilot-instructions.md`.
- A configuration artifact that is no text has `language` `n/a` and a blank `line_count`, and one
  that cannot be read has blank `language`, `line_count`, `is_reference` and `referenced_file`.
- A context file that is a symbolic link is a reference of one line to its target in
  `context_files.csv` and in the content-quality table.
- `line_count`, `target_line_count` and the content-quality table end a line only at LF, CRLF or
  CR.
- `memory` in `subagents.csv` is TRUE when the memory directory of a Claude Code agent holds a
  file, and `n/a` outside Claude Code.
- Failed queries, renamed branches, merges and messages in another encoding no longer leave a row
  without git metadata.
- `created_at`, `first_commit_sha`, the `first_commit_ai_*` columns, `#ai_commits` and
  `ai_commit_tools` follow a file through its renames, and a file deleted and added again is dated
  by the commit that first added it.
- `commits.csv` stores each commit message verbatim, with its line breaks as newlines inside the
  quoted field, and only the commits of repositories in the sample.
- `Commit-Message-Assisted-by:` credits no tool, `AI-Assisted-By:` counts as `Assisted-by:`, the
  commit pattern `from copilot/` matches GitHub's merge commit, and `co-authored-by: gemini` no
  longer matches people.
- `repos.csv` publishes GitHub's repository id in `id` and the creation time in UTC in
  `createdAt`, instead of GHS's database id and GHS's 12-hour time.
- `github_link` percent-encodes the branch and the path, and a branch such as `19.0` is no longer
  published as `19`.
- `clone_failed.csv` records the reason that git's message gives, such as `dmca_takedown`, and the
  file history step removes the rows of a repository that GitHub closed or disabled.
- `artifact_mtime_window` in `snapshot.json` spans the times of the files that the collection
  scanned.
- The download of the sampling frame verifies the certificate of the GHS server.
- Every command exits 1 on a usage error instead of 2.
- The usage examples of the release README and of the website read `engineered_project` as text.
- The analysis figures and tables:
  - Repository age is measured at the day on which the snapshot was downloaded.
  - The monthly commit volume ends before the latest scan.
  - The language ranking covers the sample.
  - The co-occurrence table uses Fisher's exact test for small counts.
  - The LaTeX snippets escape names such as C#, and the x labels of the rq3 timeline no longer
    overlap.
  - The mechanism distribution figure labels its counts as configuration artifacts, and the
    tool-count figure shows the repositories with no specific tool separately.
  - The rq3 timeline and creation order figures date a category by the commit that created its
    earliest file.

## [AgentConfigs 1.0.0] - 2026-05-08

The release of AgentConfigs that the AIware 2026 dataset paper describes, tagged
`agentconfigs_1.0.0` and published to Zenodo. At this release, `_dataset/` held the archive contents and the record's own README.

### Added

- The four-stage pipeline: sampling from GitHub Search (`1_sampling/`), LLM-based classification of
  engineered projects (`2_classification/`), configuration artifact collection and detection
  (`3_data-collection/`), and the analyses (`4_data-analysis/`).
- `output/repos.csv`, 4,738 repositories with 24 columns: metadata, five tool flags (`claude`,
  `gemini`, `codex`, `copilot`, `cursor`), five context file flags, and ten mechanism flags.
- Nine detail CSVs, one per configuration mechanism, and their `*-extended.csv` counterparts with
  git metadata: `context-files` (9,470 rows), `skills` (2,430), `commands` (1,098), `rules` (997),
  `subagents` (884), `settings` (472), `mcp` (138), `hooks` (102), and the `other` CSV of
  unclassified files (1,969).
- Detection of AI commits across the sample, published as `all_ai_commits.csv`.
- `4_data-analysis/audit_paper_numbers.py`, which recomputes every numeric claim in the dataset
  paper from the pipeline outputs and fails if the paper and the data disagree.
- The published dataset (`_dataset/`) and the dataset explorer website (`_website/`).

Detection at this release covered context files (`CLAUDE.md`, `GEMINI.md`, `AGENTS.md`,
`AGENTS.override.md`, `.cursorrules`, `.github/copilot-instructions.md`, `.github/instructions/`);
settings (`.claude/settings.json`, `.claude/settings.local.json`, `.gemini/settings.json`,
`.gemini/config.yaml`, `.codex/config.toml`, `.cursor/cli.json`); skills (`.claude`, `.gemini`,
`.codex`, `.github`, `.cursor` `/skills`); subagents (`.claude/agents`, `.github/agents`,
`.cursor/agents`); commands (`.gemini/commands`, `.cursor/commands`, `.claude/commands`); hooks
(`.claude/settings.json`, `.gemini/settings.json`, `.github/hooks`); rules (`.codex/rules`,
`.cursor/rules`); and MCP (`.mcp.json`, `.cursor/mcp.json`, `.gemini/settings.json`,
`.codex/config.toml`).

[AgentTorrent 1.0.0]: https://doi.org/10.5281/zenodo.24058027
[AgentConfigs 1.0.0]: https://doi.org/10.5281/zenodo.20090356
