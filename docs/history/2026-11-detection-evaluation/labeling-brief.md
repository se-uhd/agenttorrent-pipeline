# Brief for the labeling agents of the detection accuracy evaluation of 2026-11-04

You label the items of one shard of one sample of the detection accuracy evaluation of the
collection that started on 2026-11-04. The evaluation estimates how often the detection of the
AgentContext pipeline is wrong and how much it misses: for configuration artifacts (path
precision and path recall), for the commits that credit a coding agent (commit precision,
commit recall and the unmatched mentions), and for the rows of the imports and of the planning
documents (import precision and planning document precision). Your prompt names your shard, its
pass (1 or 2), the file that you write and the directory `WORK` of the evaluation.

## What you read and what you write

Read your shard, `WORK/shards/pass<N>/<sample>-<NN>.csv`, and for path recall also
`<sample>-<NN>-candidates.csv` beside it. Label every item of the shard exactly once.

You may read:

- the checkout at `<checkout>`, read-only:
  `heuristics/*.csv` (the path heuristics `config_path_patterns.csv` with `doc_url`,
  `doc_verified`, `evidence` and `notes`, the commit heuristics `ai_commit_patterns.csv` and
  `other_ai_commit_patterns.csv`, and the negatives `not_configuration.csv` and
  `not_ai_commits.csv`), `docs/heuristics.md` (sections "Configuration artifact detection", "AI
  contribution detection", "The parts of a plugin, wherever the repository puts it" and "The two
  negatives"), `docs/commits.md` (section "Detection scopes") and `docs/tools/evaluation.md`
- for import precision, the import heuristics `heuristics/import_patterns.csv` and their negatives
  `heuristics/not_imports.csv`, the section "Imports" of `docs/heuristics.md` and
  `docs/steps/import_detection.md`, and for planning document precision the heuristics
  `heuristics/planning_document_patterns.csv` and their negatives
  `heuristics/not_planning_documents.csv` and the section "Planning documents" of
  `docs/heuristics.md`, each heuristic with `doc_url`, `doc_verified`, `evidence` and `notes`
- the validated tree of the collection, `data/collection/data/validated/<owner§repo>/` in the
  checkout, which holds the configuration artifacts that the detection accepted and the planning
  documents. A file of one line that names a path is the marker of a symbolic link, and the line
  is its target.
- the imported tree of the collection, `data/collection/data/imported/<owner§repo>/` in the
  checkout, which holds every file that an import reaches
- `WORK/ai_commits-repos.csv`, which gives the collected commit of every repository in
  `head_sha`, and `WORK/submodules.csv`, the gitlinks of the collected repositories (`repo_name`,
  `path` and `submodule_repo`), copied from the working copy of the collection. The checkout's
  `data/collection/submodules.csv` describes an earlier collection.
- GitHub, read-only, at the collected commit: `gh api repos/OWNER/REPO/contents/PATH?ref=SHA`
  (the type `symlink` and its `target` show a link), `gh api "repos/OWNER/REPO/git/trees/SHA?recursive=1"`,
  `gh api repos/OWNER/REPO/commits/SHA` (the logins of the author and the committer) and
  `https://raw.githubusercontent.com/OWNER/REPO/SHA/PATH`. All agents share one GitHub access
  token, so read the validated tree and your shard first and request GitHub only for what they
  lack.
- the vendor pages that a heuristic cites in `doc_url`, through WebFetch, and where a page does
  not settle a question, the vendor's shipped code, archived pages or forum
- the labels and resolutions of the two earlier evaluations, in
  `data/collection/evaluation/2026-10-05/` and `2026-10-26/` of the checkout, as precedents

Write your labels to the file that your prompt names, `WORK/shards/pass<N>/out/<sample>-<NN>.labels.csv`:
UTF-8, LF line ends, a header and one row per item, with exactly these columns:

```
item,pass,label,tool,mechanism,attribution_correct,paths,uncertain,evidence,note
```

- `item`: the item of your shard, such as `pp-0042`
- `pass`: your pass, `1` or `2`
- `label`: one of the labels that the criteria of the sample below allow, spelled as given
- `tool`: in path precision with a label other than `correct`, the tools that do read the file,
  joined with `|`, such as `claude|cursor`. In commit recall with `credits`, the credited tools of
  the six, spelled `Claude`, `Codex`, `Copilot`, `Cursor`, `Gemini` or `OpenCode` and joined with
  `|`. In planning document precision with `wrong_framework`, the framework or the tool that
  writes the file, as `framework` of `planning_document_patterns.csv` spells it where a row names
  it, such as `spec-kit`. Empty otherwise.
- `mechanism`: in path precision with `wrong_mechanism`, the mechanism that the tool reads the
  file as, and empty otherwise
- `attribution_correct`: in commit precision, `yes` or `no`, and empty otherwise
- `paths`: in path recall, the configuration paths among the candidates, joined with `;`, each
  spelled as in the candidates file. In import precision with `wrong_target`, the
  repository-relative path of the one file that the tool loads through the import, or empty
  where it loads none. Empty otherwise.
- `uncertain`: `yes` when the evidence does not settle the label, and empty otherwise
- `evidence`: what decides the label, always given: the vendor page with the date on which you
  read it (`https://cursor.com/docs/skills, read 2026-11-08`), the heuristic (a path heuristic by
  its `path` and `match_mode`, a commit heuristic by its `id`), and a quote of the line, the file
  or the page
- `note`: anything else that a reviewer needs, such as what would settle an uncertain label

Do not edit the checkout, do not run a step or a check of the pipeline, do not write anywhere but
the file that your prompt names and a scratch directory of your own, and do not open issues or
pull requests.

### People

The labels become tracked files of a public dataset. In `evidence` and `note`, write `(a person)`
for the name, the address and the login of a person, also inside a quoted trailer, such as
`Co-authored-by: (a person)`. Quote the identity of a bot, an app or a tool in full.
`merge_labels.py`, which joins the labels, reads each identity alone: the value of a trailer, or
up to five words of a name before an address, or an address alone. It keeps the address of an
identity when the address is:

- the address of a GitHub App, with `[bot]`, such as
  `176961590+gemini-code-assist[bot]@users.noreply.github.com`
- an address with the mailbox `noreply` or `no-reply`, such as `noreply@anthropic.com`
- an address that a commit heuristic other than a `convention` row reads as the tool's own, such
  as `cursoragent@cursor.com` and `198982749+Copilot@users.noreply.github.com`
- the address of a bot that fits the bot's name, such as `bot@dependencies.io` for
  `Dependencies.io Bot`

It keeps the name before such an address when the name is a tool's or a bot's, such as
`Cursor Agent`, `Devin AI`, `OpenCode (gpt-5.4)`, `copilot-swe-agent[bot]` and `GitHub` before
`noreply@github.com`, or when such a heuristic reads the identity for its name, as in
`Claude Opus 4.7 (1M context) <noreply@anthropic.com>` and
`Co-authored-by: aider (gpt-4o) <aider@aider.chat>`. A name that joins two names with `&`, `+`,
`and`, `with`, `for` or `using` stays only when both are a tool's or a bot's. It also keeps a
name or a login without an address, such as `copilot-swe-agent[bot]`, and the words of your own
before a name, such as `author` in `author Claude <noreply@anthropic.com>`.

It replaces an identity with any other address, and the name before it, with `(a person)`. It
replaces any other name before a kept address with `(a person)` and keeps the address, as in
`Co-authored-by: (a person) <noreply@github.com>`, because people commit with their own name
under the addresses of GitHub's web flow and of GitHub Actions. That also replaces the name of a
model that no heuristic reads, as in `Grok 4.5 <noreply@x.ai>` and
`Space Bunny Free <noreply@opencode.ai>`, so give such a name without its address. The merge
stops on a label that contains the name of two or more words or the GitHub login of an author or
a committer of the sample of commit recall who is a person, except a login before a slash, the
owner in `owner/repo` and in the link of a commit, and a name with `[bot]` or the word `bot`,
such as `Release Bot`. It recognizes the name of a person without an address only among those
authors and committers, so write `(a person)` yourself, also for a name beside the identity of a
tool. Your shard of commit recall contains the full identities and messages, and the tracked
sample keeps only the identities of bots and the messages of credited commits.

### The second pass

A seeded fifth of every sample is labeled a second time, and the agreement of the passes is
reported with Cohen's kappa. If your pass is 2, you label without the first labels: do not open
`WORK/shards/pass1/`, `WORK/2026-11-04/labels/`, `review.csv` or `report.*`, and do not exchange
labels with other agents. Decide each item from the sources above alone. A second-pass shard of
commit recall has no screen, and you read every commit of it in full.

Both passes see which heuristics matched an item, and the people and agents that label also
write the heuristics, so the labels are not blind to the heuristics. Judge each item by the
vendor's documentation or code, not by whether a heuristic matched it.

### Resolutions

`evaluation report` lists in `WORK/2026-11-04/review.csv` every item with an uncertain pass or
with passes that disagree, and a person resolves each of them. A resolution is a row of
`WORK/2026-11-04/labels/resolutions.csv` with exactly these columns:

```
item,label,tool,attribution_correct,paths,note
```

`label`, `tool`, `attribution_correct` and `paths` have the values that a label of the item's
sample has, and `note` states what decides the item. A note that applies a decided case names its
item and evaluation, as in "as pp-0427 of the evaluation of 2026-10-05", and a decision of
principle gets its day, as in "(decided 2026-10-29)". A resolution replaces both passes of its
item in every estimate, and `evaluation report` exits 1 for a resolution of an item that no
sample has or with a label that its sample does not allow. Judge the `attribution_correct` of a
resolution under the meaning that the passes of its evaluation used. In this evaluation, that is
the Attributions table of `docs/heuristics.md`, as [commit_precision](#commit_precision) states
it. The evaluation of 2026-10-05 recorded `product` for what is `standard` now, and `product`
then covered any co-author line that named the tool, whatever its address, so read its
`attribution_correct` under that meaning when you cite it.

If your prompt asks for proposals of resolutions, read both passes of each item, their evidence
and the sources above, and write `WORK/resolution-proposals-<sample>.csv` with the columns
`item,label,tool,attribution_correct,paths,note,evidence`, with the source that decides each item
in `evidence`. The maintainer confirms or changes each proposal, and the lead writes the
confirmed rows without `evidence` into `resolutions.csv`.

## path_precision

Each item is a row of a mechanism CSV: a configuration artifact (`artifact`, a file or a
directory) in `repo_name`, with the `tool` and the `mechanism` that the detection gave it. Read
it at `local_path` under `data/collection/` of the checkout. `cell_rows` is the number of rows of
the pair of tool and mechanism in the outputs. The mechanisms are `context_file`, `skills`,
`subagents`, `commands`, `rules`, `settings`, `hooks`, `mcp`, `plugins`, `lsp` and `other`.
`.claude/commands/*.md` counts under `skills`, because Claude Code merged its commands into
skills.

- `correct`: a product of the named tool reads the configuration artifact as the named mechanism
  at its location, according to the vendor's documentation, which the matching row of
  `heuristics/config_path_patterns.csv` cites, or to the product's own code. For `any`, at least
  one of the six tools reads the shared configuration artifact. For several tools joined with
  `|`, each of them reads it. For `other`, the file is in a configuration directory of the named
  tool, which no named mechanism covers. A configuration artifact in a format that its vendor
  has since retired is correct when the tool read it in that format, such as Antigravity's
  workflows, which Antigravity stopped reading on 2026-10-19 (pp-0585 of 2026-10-26, decided
  2026-10-29).
- `wrong_tool`: configuration of another tool, or the named tools are not the tools that read it.
  Write the tools that read it in `tool`.
- `wrong_mechanism`: configuration of the tool, but as another mechanism. Write that mechanism in
  `mechanism`.
- `not_configuration`: no tool reads it as configuration, because of where it is or what it
  contains, such as a format that the tool refuses.

The earlier evaluations decided these cases (the item and the evaluation in parentheses):

- An MCP file counts only with a server that a tool loads, so `{"servers": {}}` is
  `not_configuration` (pp-0418 of 2026-10-26, decided 2026-10-29). This decision replaces the
  earlier label of an empty `mcpServers` object as correct (pp-0458 of 2026-10-05).
- A settings file that configures nothing is still the file that the tool reads, such as a
  `.codex/config.toml` of comments (pp-0308 and pp-0314 of 2026-10-05).
- VS Code reads the root `.mcp.json` and infers the type of a server with a `url` alone, so such
  a file configures a server (pp-0018 of 2026-10-26).
- A file in a tool's configuration directory that no mechanism covers is a correct `other`, such
  as a file in Copilot's agents directory or a prompt file below `.github/prompts` (pp-0384 and
  pp-0386 of 2026-10-05).
- `AGENTS.md` is a context file at any depth, with the tool `any` (pp-0162 of 2026-10-05).
- A configuration artifact counts by its location: A template is read where it is when a session
  starts in its directory (pp-0114 of 2026-10-05), and a `.cursorrules` outside `.cursor` in a
  folder that can be opened as the project is read "in the root of your project" (pp-0488 of
  2026-10-26).
- A directory is vendored by its name in `heuristics/vendored_dirs.txt`, and `deps/` is not
  (pp-0006 of 2026-10-05).
- A tool that loads its files through a link reads the target: Cursor's loader stats a linked
  command file and reads its target (pp-0427 of 2026-10-05, pp-0479 and pp-0481 of 2026-10-26).
- A tool that drops a file makes the row `not_configuration`, or `wrong_tool` where another named
  tool reads it. Antigravity discards a rule without a trigger (pp-0579 of 2026-10-05). Where
  Claude Code reads the rule through `.claude/rules`, a `claude|gemini` row of it is `wrong_tool`
  with the tool `claude` (pp-0220 of 2026-10-05). Antigravity requires front matter in a `SKILL.md`
  (pp-0658 of 2026-10-26) and in an agent, which Antigravity CLI 1.3.5 does not list without it
  (pp-0615 of 2026-10-05). Antigravity CLI 1.3.5 cannot decode Cursor's hooks format with
  `"version": 1` in `.agents/hooks.json` and drops the file whole (pp-0546 of 2026-10-05).
  Cursor's loader of subagents skips a Markdown file without YAML front matter (pp-0570 of
  2026-10-26). Gemini CLI refuses an agent with a tool name that `isValidToolName` rejects
  (pp-0667 of 2026-10-26).
- Antigravity's workflows page does not require front matter, so a workflow in
  `.agents/workflows/` without front matter is `correct` (pp-0525, pp-0528, pp-0533 and pp-0534
  of 2026-10-05). The rule for a `SKILL.md` and an agent does not apply to a workflow.
- A GitHub Agentic Workflow whose engine comes from an imported shared file that sets
  `engine.command` to the project's own launcher is `not_configuration`: gh-aw then skips
  installing the engine's CLI, so neither Copilot nor Claude Code runs (pp-0403 of 2026-10-05).
- Gemini CLI finds a lowercase `skill.md` on macOS and Windows, where its glob ignores case, so
  the row is `correct` (pp-0607 of 2026-10-05).
- A `.mdc` file in `.cursor/commands` is `correct` as `other`, because neither Cursor CLI nor, as
  far as shown, the IDE reads a `.mdc` file as a command (pp-0474 of 2026-10-05). The IDE reads
  `.cursor/commands` recursively, so a Markdown file in a subfolder is a command, and an `other`
  row of it was `wrong_mechanism` (pp-0469 of 2026-10-05).
- A tool that keeps a file with a defect makes the row correct: Cursor CLI keeps a skill without
  a description (pp-0508 of 2026-10-05), and Cursor's page requests a name that matches the
  folder but does not say that it drops a skill whose name differs (pp-0166 of 2026-10-26).
- Copilot CLI reads a legacy manifest at `plugin.json` wherever the plugin is installed from, and
  the default `rules/` of a legacy Copilot CLI plugin (pp-0436, pp-0440 and pp-0445 of
  2026-10-26).
- A row of a portable plugin part, beside the manifest of a vendor, names the vendor's tool, and
  the portable manifest (`plugin.json` with the `$schema` of Agent Plugins) names no tool, as
  `docs/heuristics.md` states (pp-0255, pp-0262 and pp-0278 of 2026-10-26). A reader
  that a tool directory or a link identifies counts: comet-ml/opik links `.cursor` to `.agents`,
  so Cursor reads `.agents/agents/architect.md`, and a row with `gemini` alone was `wrong_tool`
  (pp-0660 of 2026-10-26).

## path_recall

Each item is a repository: 200 drawn from the repositories without detected configuration
(`stratum` `unconfigured`) and 100 from those with it (`configured`). The candidates file lists
up to 200 candidate paths of the repository's file list, each with `detected`: `yes` when the
CSV of a mechanism lists the path or a directory that contains it, `other` when only `other.csv`
lists it, and empty when no CSV does. A detected candidate has its `local_path` in the validated
tree, and every candidate its `raw_url` at the collected commit.

List in `paths` every candidate path that a product of the six tools reads as configuration at
its location, as one of the ten mechanisms, whether the detection found it or not. A file that
the heuristics keep in `other.csv` by design belongs to no mechanism and is not listed, such as
an ignore file (`.cursorignore`, `.aiexclude`, `.worktreeinclude`), as pr-0234, pr-0257 and
pr-0284 of 2026-10-05 and pr-0282 of 2026-10-26 decided. The label is `missed` when a listed path
has a `detected` other than `yes`, and `none` otherwise, and `merge_labels.py` stops on a label
that disagrees with its paths. A path that only `other.csv` lists counts as not found, because the
detection gave it no mechanism. `heuristics/not_configuration.csv` lists paths that no tool reads
as configuration, and also files that a tool reads but that a maintainer decision leaves
uncounted, such as the Markdown files with a description in their front matter directly in
`.claude/skills/` and `.agents/skills/`, which OpenCode 2 reads as skills, under the decision of
2026-11-07 (section "Configuration artifact detection" of `docs/heuristics.md`). List such a file
in `paths`, because a product of the six tools reads it (pr-0255 of 2026-11-04). Do not list a
file there without a description, such as a `README.md`, as the rows of `.opencode/skills` count
no such file, because "skills without one are not advertised" (https://opencode.ai/docs/skills/).

- A tool that reads a path that the detection misses by a decided limit still makes the label
  `missed`, such as a nested context file in another letter case or a workflow that calls a
  reusable workflow of another repository that runs claude-code-action (pr-0203 and pr-0276 of
  2026-10-26).
- The files inside a detected skill folder belong to the skill (pr-0240 and pr-0253 of
  2026-10-05), and a linked skills directory is followed, so a linked skill folder is reported at
  its target (pr-0273 of 2026-10-05, pr-0243 of 2026-10-26).
- When `candidates_listed` is lower than `candidates_in_repo`, label the listed candidates. If
  the repository's tree on GitHub shows configuration among the others that the detection
  missed, say so in `note`, because `paths` takes only listed candidates.

## commit_precision

Each item is a commit of a commit CSV and one tool that it credits (`tool`), drawn per tool, per
heuristic that the per-tool items leave out (`stratum` `heuristic:<id>`), or among the tools
outside the six (`other_tools`). `matched_heuristics` gives the ids of the heuristics that
matched, and `attributions` the attribution of each tool, as `<tool>:<attribution>`. The CSV
keeps the message but no identity, so read the author and the committer with
`gh api repos/OWNER/REPO/commits/SHA` when an identity matters.

The attribution criterion: A commit counts as a contribution of a tool when a product of that
tool made the commit or produced the change that it records. The product wrote the change, made
the commit (an empty commit included, such as the "Initial plan" commit of Copilot's cloud
agent, cp-0143 of 2026-10-05), applied the change after a person confirmed it, or suggested the
change in a review that a person committed with one click. A change that a person copies by hand
from a review comment is the person's. When a reviewer delegates a fix to an agent, the commit is
the agent's.

- `correct`: the commit credits a product of the tool with making the commit or producing its
  change under that criterion.
- `wrong_tool`: the text credits another product or tool.
- `not_contribution`: the text that the heuristics matched credits no tool with the change, such
  as a person whose name resembles a tool's, a mention of the tool, or a branch named after it.

A credit that the message only quotes from another commit is `not_contribution`, and a credit
that a squash merge or a cherry-pick copies with the change is `correct`.

`attribution_correct` is `yes` when `attributions` gives the tool the attribution that its lines
have, and `no` otherwise, under the Attributions table of `docs/heuristics.md`:

- `account`: the tool's own account authored or committed the commit, or the identity that the
  tool's code or environment writes, name and address together, or an agent account or a GitHub
  App that its own page or its project's configuration grounds as running the tool
- `reviewer`: the co-author trailer that GitHub writes when a person commits a review app's
  suggestion, and the footer of a pull request that a person creates from a suggestion of Copilot
  Autofix
- `standard`: the line that the tool writes itself, with its name and its own address or account,
  or a marker that it writes into a commit or a pull request
- `convention`: any other line that names the tool, which a person writes or an agent writes
  because a repository's instructions require it, and any other name or address that names the
  tool and that users chose for an author or a committer

Of the heuristics that match one line, the line takes the attribution that comes first in the
order `reviewer`, `standard`, `convention`. A line that names the tool is the tool's own
(`standard`) only with the tool's own address. The decided cases:

- `noreply@openai.com` is the address of the account `codex`, so a line that names Codex there is
  `standard` (cp-0079 of 2026-10-26, decided 2026-10-29). Codex's git attribution writes that
  address (cp-0061, cp-0071, cp-0093 and cp-0099 of 2026-10-05).
- A commit that the identity of the Copilot CLI app made is `correct`, with the attribution
  `account` (cp-0167 of 2026-10-26).
- An identity at the address that `cursor-author-04` reads, which GitHub shows in repositories of
  unrelated owners and which Cursor's agent takes when no git identity is set, counts for Cursor
  (cp-0236 of 2026-10-05).
- An identity under which the same kind of change appears in many unrelated repositories is an
  automated agent: the co-author pi of 74 commits that fix typos, from July to September 2026, is
  the agent pi (cp-0352 of 2026-10-05).
- A sentence that names Copilot CLI and is no line that it writes is `convention` (cp-0140 of
  2026-10-26).
- OpenCode writes `noreply@opencode.ai` and no other `opencode.ai` address, so another address
  there is `convention` (cp-0326 of 2026-10-26).
- CodeRabbit made the change itself, so its line is `standard`, as for the other apps that review
  and make changes (cp-0468 of 2026-10-26). No page or code of TRAE states its line, so it stays
  `convention` (cp-0473 of 2026-10-26).
- The user account 200291788 of Gemini Code Assist writes Gemini's own line, which is `standard`
  (cp-0287 of 2026-10-05).
- GitHub's squash merge turns the git identity of an agent's commits into a co-author line, which
  counts for the agent (cp-0063 of 2026-10-05).

## commit_recall

Each item is a commit that the cloning step read, drawn with every commit equally likely. Your
shard has the full `author`, `committer` and `message`, and `detected` and `detected_other` name
the tools that the current heuristics credit.

The label is `credits` when the commit's identities or message credit one or more of the six
tools under the attribution criterion of commit precision, with the credited tools in `tool`, and
`none` otherwise. Write a credit to a tool outside the six into `note`, and the label stays
`none`. Commit recall covers the commits that the cloning step read, not a branch that the clone
did not fetch.

A first-pass shard has the column `screen_hits`: the words of the screen of `make_shards.py` that
occur, case-insensitively, in the author, the committer or the message. The screen lists the
names of the six tools and of their vendors, of other coding agents and models, words such as
`agent`, `assist`, `bot`, `generated`, `model`, `prompt` and `session`, and every trailer of the
form `<word>-by:`. Read every commit with a hit in full. A commit without a hit is `none`, with
the evidence `no word of the screen of make_shards.py occurs in the author, the committer or the
message`, once you have checked that its identities are people or GitHub's own identity. A
second-pass shard has no screen, and you read every commit.

- `Co-Authored-By: Claude <noreply@anthropic.com>`, the line that Claude Code adds, credits Claude,
  also where a squash merge copies it with the change.
- The co-author trailers of the apps credit their tools: 175728472 is
  `copilot-pull-request-reviewer`, 223556219 `github-copilot-cli`, 198982749 `copilot-swe-agent`,
  62310815 `github-advanced-security`, 223894421 `github-code-quality`, 176961590
  `gemini-code-assist`, 209825114 `claude` and 206951365 `cursor`.
- A hand-written disclosure such as `Assisted-by: Claude` credits the tool that it names.

## mentions

Each item is a phrasing, a line of a commit message that names a tool in commits that no
heuristic matched, drawn with a weight of its number of commits. `line` is the line as the
tracked sample writes it, with `(a person)` for people, `line_as_collected` the line as the
collection read it, and `pattern` the normalized phrasing that groups lines. `read_now` names the
tools that a current heuristic credits for the line read alone. `example_sha` is one commit with
the line. Where your shard has no `github_link`, and the line does not decide the label, find the
repository with `gh api -X GET search/commits -f q=hash:SHA`.

The label is `attribution` when the line credits the tool with producing the change of its
commit, as a trailer, an identity or a disclosure does, and `not_attribution` when the line
mentions the tool in another way, such as a link to its account, a dependency, a feature of the
project, a branch or pull request name, or a person's handle. In the line
`[codex] Add Weaver copilot UI (#900)`, Copilot names a UI of the project (mn-0067 of
2026-10-05). The decided cases:

- A repository's own agent that is named after a model or a tool is not the tool
  (mn-0122 of 2026-10-05, mn-0114, mn-0136 and mn-0166 of 2026-10-26), and neither is a workflow
  of the project that runs `cursor-agent` and names Cursor's model (mn-0003 of 2026-10-05).
- A line that credits another agent, which runs its own engine and names the tool only as the
  provider of its model, is `not_attribution`, such as OpenAgents' Coder in
  `Worked by Coder from an OpenAgents chat (codex gpt-6.1-sol)` (mn-0036 of 2026-10-05).
- Feedback from a tool that a person applied is `not_attribution` (mn-0037 of 2026-10-05,
  mn-0122 of 2026-10-26), and so is a review suggestion without a co-author trailer, which a
  person applied by hand (mn-0162 of 2026-10-05).
- A line that says what the tool found, not who wrote the change, is `not_attribution` (mn-0059
  of 2026-10-05), and so is a line that names a command of the tool that the commit adds (mn-0187
  of 2026-10-05) or a link definition beside the line with the credit (mn-0039 of 2026-10-05).
- A prefix that no vendor page names, such as `[codex]`, is `not_attribution` (mn-0190 of
  2026-10-26), unless the repository shows that it marks the commits that the tool made, as
  `(COPILOT)` of NextCapital/copy-service does (mn-0064 of 2026-10-26). The resolution of mn-0190
  cites mn-0067 of 2026-10-05, which decided only that Copilot names a UI of the project in its
  line, and nothing about the prefix.
- A message signed by the tool is `attribution` (mn-0116 of 2026-10-05).

## import_precision

Each item is a row of `output/imports.csv`: an import (`import`), written on `line` of the
importing file `importer`, that the tool of the import heuristic `heuristic` reads in a chain that
starts at the configuration artifact `root`, the path that the import resolves to (`target`) and
what happens to that target (`target_status`). `depth` is the hop of the import, 1 for an import
written in `root`, and `github_link` links its line at the collected branch. The stratum of an item
is the pair of its heuristic and its status, and `stratum_rows` the rows of the pair in
`imports.csv`. Where every row of a pair is in a repository that the `evidence` of a heuristic
links, the draw took the items of the pair from these repositories, and `from_evidence` is `yes`
for each of them (decided on 2026-11-08). Label such an item like any other, by the criteria below.
`from_evidence` is empty for every other item. Read the importing file at `importer_local_path`
under `data/collection/` of the checkout, in the validated tree where it is `root` and in the
imported tree where an import reached it, and a found target at `target_local_path`. Where a tree
lacks the file, read it at `importer_raw_url` or `target_raw_url`, at the collected commit
`head_sha`, and list the tree with `gh api "repos/OWNER/REPO/git/trees/SHA?recursive=1"` to see
whether a path has a file.

The row of `heuristic` in `heuristics/import_patterns.csv` gives the importing files (`path` and
`match_mode`), the syntax (`syntax`), what a relative path resolves against (`resolution`), the
deepest hop that the tool reads (`max_depth`), the limits of a chain (`max_files`, `max_bytes`
and `max_file_bytes`), the extensions that the tool reads and the conditions, with the page that
states them in `doc_url` and the quotes and code facts that decide them in `notes`. The section
"Imports" of `docs/heuristics.md` states for each tool what its page and its shipped code decide,
and `heuristics/not_imports.csv` names what a tool does not read as an import, each with its page:

- Claude Code (`claude_at`, https://code.claude.com/docs/en/memory) reads `@path` in every
  CLAUDE.md, `.claude/CLAUDE.md`, CLAUDE.local.md and rule of `.claude/rules`, and in an AGENTS.md
  only where neither the root nor its own directory has a CLAUDE file (`no_claude_md`). It reads
  four hops, resolves a relative path against the directory of the file that the importing
  file's links lead to (`real_dir`), and "Import parsing skips Markdown code spans and fenced
  code blocks". Inside a list item, it also reads the text of the item as written, code spans
  included, and it reads only the extensions that the row lists.
- Claude Code Review (`code_review_include`, https://code.claude.com/docs/en/code-review-customize)
  includes a file of the root where a line of the root REVIEW.md holds "only @ and the file
  name", in the case of the file's name, and a skill on a line of its own in the form
  `@.claude/skills/<folder>/SKILL.md`, one level, with the limits that the section states.
- Copilot CLI (`copilot_at`,
  https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions)
  reads `@path` in `.github/copilot-instructions.md`, AGENTS.md and CLAUDE.md, eight levels, a
  target of any extension with valid UTF-8 content, and no import in GEMINI.md, CLAUDE.local.md or
  the files of `.github/instructions`. Its fences follow the rules that the section states for
  Copilot CLI 1.0.106 and 1.0.107.
- Gemini CLI (`gemini_at`,
  https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/memport.md) reads `@file`
  in every GEMINI.md and in the context files that `context.fileName` names, five levels, relative
  to the importing file. Its code is a run of backticks closed by a run of the same length, and a
  tilde fence, indented code and an HTML comment are text.
- Antigravity (`antigravity_include`, https://antigravity.google/docs/rules) inlines
  `@[label](path)` into AGENTS.md, GEMINI.md and the rules with the trigger `always_on` or `glob`,
  one level, with the path as written and relative to the importing file, also in code. Its bare
  `@path` inlines nothing (`not_imports.csv`). The rows have the tool `gemini`.
- Bugbot (`markdown_link`, https://cursor.com/docs/bugbot) reads the `.md` and `.mdc` files that
  standard Markdown links of `.cursor/BUGBOT.md` name, by a relative path inside the repository,
  one level.

Label every item by what the tool does with the import as written, whatever its status, also
where the collection does not hold the target:

- `correct`: the tool reads `import` on `line` as an import, resolves it to `target`, and
  `target_status` states what happens to the target. A row of any status can be `correct`. A
  `missing` row is `correct` where the repository has no file at `target` at `head_sha`, as for
  the package name `@bpinternal/zui` where Claude Code reads it as a path, and an `outside` row
  where the path leaves the repository as written, as `@~/.claude/aio-libs/yarl/context.md`
  does. A `not_read` or `over_limit` row is `correct` where the tool skips the target for the
  reason that the status names.
- `not_loaded`: the tool reads no import in this text, whatever the status of the row. The text
  is in a code span or a fenced code block that the tool skips, it has a form that the tool does
  not read, or the tool reads no import of the importing file, such as an AGENTS.md beside a
  CLAUDE.md for Claude Code (`no_claude_md`).
- `wrong_target`: the tool reads the import, and the row misstates what it reaches: The tool
  resolves it to another path than `target`, or `target_status` is wrong, such as a `missing` row
  of a path with a regular file of the repository at `head_sha`. A `found` row is
  `wrong_target`, and not `not_loaded`, where the tool reads the import and loads no file through
  it, as for a target that resolves outside the repository or that the tool skips for its
  extension, its content or the limits of the chain. Write the file that the tool loads in
  `paths`, or leave `paths` empty where it loads none.

A `missing` row of a word that names no file, such as `@ts-expect-error`, is therefore `correct`
where the tool reads the word as an import, because the tool then looks the path up and finds
nothing, and `not_loaded` where the tool reads no import there, such as in a code span that it
skips.

The statuses describe the repository at the collected commit, as the comment above
`imports.STATUSES` in `agentcontext/imports.py` defines them:

- `found`: a regular file inside the repository, outside `.git` and every gitlink, that the tool
  reads
- `missing`: no such file at the path, which includes a directory, a link that leads out of the
  repository and a file in `.git` or a gitlink
- `outside`: a path that leaves the repository as written, such as an absolute path, a path in
  the home directory or a path above the root
- `not_read`: a file that the tool does not read because of its extension, its content or a link
  on its path
- `over_limit`: a file that the tool would read, beyond the files or the bytes that it reads for
  one importing file, beyond the imports of its kind that it reads, such as the skill lines of
  Claude Code Review, or larger than the largest file that it reads
- `pruned`: a path that the release leaves out, such as a session transcript
- `not_copied`: a file that the tool reads, of which the imported tree holds only a stub

`pruned` and `not_copied` state what the collection holds and not what the tool does, and a row
with one of them is `correct` where the tool reads the import and resolves it to `target`.

## planning_precision

Each item is a row of `output/planning-documents.csv`: a planning document (`planning_document`)
in `repo_name`, with the framework or the tool (`framework`) and the source's name for the
document (`document`) that the detection gave it. The stratum of an item is its framework, and
`stratum_rows` the rows of the framework in `planning-documents.csv`. Where every row of a
framework is in a repository that the `evidence` of a heuristic links, the draw took the items of
the framework from these repositories, and `from_evidence` is `yes` for each of them (decided on
2026-11-08). The heuristics of such a framework were read off these repositories, and the report
also gives the precision without these frameworks. Label such an item like any other, by the
criteria below. `from_evidence` is empty for every other item. Read the file at
`local_path` under `data/collection/` of the checkout, or at `raw_url` at the collected commit.

The first row of `heuristics/planning_document_patterns.csv` in file order that matches the path
gives the framework and the document. Among the rows of the item's `framework` and `document`,
`pattern` is the regular expression that the path matches, relative to the base that
`match_mode` gives, `anchor` what the row requires beside the path, `doc_url` the page that
states the location, and `notes` the quote of that page and of a sentence of the framework's
documentation that names one of the six tools. `heuristics/not_planning_documents.csv` names
the layouts that are left out, each with its reason.

- `correct`: a planning document that the framework or the tool of `framework` writes or reads at
  its location, as its documentation states. A planning document is a work product of a
  spec-driven development framework or of a planning feature of one of the six tools, such as a
  spec, a plan, a task or a design, and the project context that a framework generates for agents
  to read (section "Planning documents" of `docs/heuristics.md`).
- `wrong_framework`: a planning document of another framework or tool. Write that framework or
  tool in `tool`.
- `not_planning_document`: no planning document, such as a template, metadata, a script, an
  installation or the configuration of a framework, or a file that a project keeps at the
  location for a purpose of its own.

Write in `note` where `document` names another document of the framework than the file is. The
section "Planning documents" of `docs/heuristics.md` decides these cases:

- The metadata beside the documents, such as OpenSpec's `.openspec.yaml` and the `spec.json` of
  cc-sdd, the templates, such as Spec Kit's `.specify/templates/`, and an anchor are no planning
  documents.
- A file that `config_path_patterns.csv` classifies stays a configuration artifact, such as the
  `openspec/AGENTS.md` of earlier versions of OpenSpec or a skill that Spec Kit installs, and a
  row of it is `not_planning_document`.
- A planning folder inside a tool's directory, such as `.cursor/plans/`, `.claude/plans/` or the
  folder that `plansDirectory` names, holds planning documents.
- Spec-Flow's documents open with the headings of Spec Kit's templates, so a feature folder of
  Spec-Flow that a row of Spec Kit reads is `wrong_framework` with the tool `spec-flow`. cc-sdd
  writes the `spec.json` beside its documents into `.kiro/specs/`, which Kiro's own specs lack,
  so a spec of Kiro that a row of cc-sdd reads is `wrong_framework` with the tool `kiro`.
- A document at the location that an earlier version of the framework documents is `correct`,
  such as a document of HumanLayer's `thoughts/` folders, which the rows ground in the commands at
  the last commit that contains them.

## Decisions of 2026-11-07

These decisions settle the questions that the earlier labels left open. Label by them and do not
set `uncertain` for them.

- A link into a submodule (a gitlink that `WORK/submodules.csv` lists) is the configuration
  artifact at its location. Label a file link as the tool reads it in a checkout that has the
  submodule, by its location. A folder link into a submodule that the detection lists as `other`
  is `correct`, because a folder is a skill or a folder of instructions only by the files inside
  it, which the collection does not hold.
- A row names every tool that a file of the repository identifies as a reader: a vendor's
  manifest, a vendor's marketplace that lists the plugin folder, a tool directory or a link. A
  portable manifest alone (a root `plugin.json` with the `$schema` of Agent Plugins) names no
  tool. A row that leaves out such a reader is `wrong_tool`, with the readers in `tool`. Codex
  reads the `skills/` and `mcp.json` of a portable plugin that `.agents/plugins/marketplace.json`
  lists as a local source, and Copilot CLI reads the plugin folders that its marketplace lists.
  A `.claude-plugin/marketplace.json` names Claude Code alone.
- An OpenCode MCP server with `"enabled": false` or `"disabled": true` counts, because OpenCode
  lists such a server and connects it on request.
- A `SKILL.md` whose YAML front matter does not parse is not Cursor's skill, because Cursor CLI
  drops it and the IDE fails a request with it attached. An Antigravity rule counts only when its
  front matter decodes, after Antigravity CLI quotes a `globs` value that starts with `*`, so that
  an unquoted `globs: *` or `globs: **/*.html` is a rule.
- The file that the `prompt` of an OpenCode agent names as `{file:...}` is part of that
  subagent (`subagents`), not an `other` row. The file that `model_instructions_file` of a Codex
  agent role names is part of that subagent too.
- A folder `.cursor/rules/<name>/` with a `RULE.md` is no format that Cursor read, and an `other`
  row for it is `correct`.
- The `chatAgents`, `chatInstructions` and `chatPromptFiles` of a VS Code extension's
  `package.json` are content of an extension that its users install, not configuration of the
  repository. Path recall does not list them.
- A disclosure credits no tool when the tool only reviewed, checked, verified, analyzed,
  investigated, searched for a bug, debugged, brainstormed, was consulted or ran checks or tests,
  or made only the description, a summary, the commit message or comments on the pull request.
- `Drafted with Claude Opus 5 via Kōan` credits Claude, and Kōan's footer
  `_Generated by [Kōan](https://koan.anantys.com)_` credits Kōan among the other tools.
