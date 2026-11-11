# Two decisions for the maintainer

`TODO.md` names two items of the evaluation of 2026-10-26 that wait for the maintainer, and both
cases are in the outputs of the new collection as well: the agent file of
pingidentity/terraform-provider-pingone is still a `subagents` row of Copilot, and the `mcp.json`
of marionettejs/marionette is still a `mcp` row of Cursor alone. The counts below come from the
checkout's current outputs and validated tree (the collection of 2026-10-26, regenerated on
2026-11-05), the files that support them are `links_into_submodules.csv` and
`codex_marketplace_parts.csv` in this directory, and GitHub was read on 2026-11-07.

## pp-0475: a link into a submodule on a host that the collection does not record

`.github/agents/LocalCodeReview.agent.md` of pingidentity/terraform-provider-pingone is a
symbolic link, 69 bytes at the collected commit 110438ce, to
`../.././shared-configs/common/ai/code-review/LocalCodeReview.agent.md`. `shared-configs` is a
submodule with the URL `git@github-emu:ping-internal/cdi-shared-configs.git` (its `.gitmodules`),
an SSH alias of a host, and `gh api repos/ping-internal/cdi-shared-configs` returns 404 Not
Found. `submodules.csv` records the gitlink with an empty `submodule_repo`, because
`clone.parse_gitmodules` records a repository only for a URL on github.com. The detection lists
the link as a Copilot subagent. In a checkout that has the submodule, VS Code reads it in
`.github/agents/`.

The evaluation of 2026-10-26 labeled it `correct` and uncertain in its first pass. Its
resolution says `not_configuration`, "as for not_configuration.csv rows 40 and 41", which are
link markers of directories with a target that was never collected, such as `.codex/skills`.
For a directory, a skill is known only by the `SKILL.md` inside it. The agent file is a file at
a path where the tool reads an agent.

The collection fetches no submodule, so it holds the target of no link into a submodule. In the
checkout's validated tree, 22 link markers in 12 repositories lead into a gitlink that
`submodules.csv` records, and 8 of them are rows of a mechanism other than `other`, in 5
repositories: the `CLAUDE.md` of LedgerHQ/app-ethereum, the `CLAUDE.md` and `.gemini/GEMINI.md`
of crossbario/autobahn-js and of crossbario/autobahn-testsuite, two `.claude/commands/` files of
sirius-db/sirius, and the agent of pingone. The other seven rows lead into public repositories on
GitHub (LedgerHQ/ledger-app-ai-instructions, wamp-proto/wamp-ai and sirius-db/claude-tools).
`docs/dataset.md` names the commands of sirius-db/sirius and the agent of pingone as links into
a submodule, whose `language` the detection leaves blank.

The options:

- (a) Count every link into a submodule as the configuration artifact at its location, and
  resolve pp-0475 as `correct`. The code stays as it is, the resolution of 2026-10-26 changes,
  and the brief states the rule.
- (b) Drop a link into a submodule whose repository the collection does not record, one row
  today. The detection would read `submodules.csv` for links. An empty `submodule_repo` also
  stands for a relative URL such as `../tools.git`, which names a repository on GitHub, and telling
  the two apart changes `clone.parse_gitmodules`, which is collector code and needs a version in
  `agentcontext/code_versions.py`.
- (c) Drop every link into a submodule: 8 rows in 5 repositories, 7 of them into public
  repositories, and the paragraph of `docs/dataset.md` changes.

I recommend (a). The detection decides by what a repository commits at a path that a tool reads,
and the link is that file. The collection checks the content behind no submodule link, public or
private, so (b) and (c) would separate the rows by the visibility of a host rather than by what
the tool reads. Option (a) keeps one rule for the 8 rows, needs no change of code, and its limit,
a row without content and with a blank `language`, is already in `docs/dataset.md`.

## pp-0515: a reader that a row leaves out

`plugins/marionette/` of marionettejs/marionette holds a root `plugin.json` with the `$schema` of
Agent Plugins 1.0.0 and an `extensions.com.openai` block, a `.claude-plugin/plugin.json`,
`.mcp.json`, `mcp.json` and `skills/marionette/SKILL.md`. Three marketplaces at the root list the
folder: Codex's `.agents/plugins/marketplace.json` (`"source": "local", "path":
"./plugins/marionette"`), `.claude-plugin/marketplace.json` and `.cursor-plugin/marketplace.json`.
Codex's page states "Point each plugin entry's `source.path` at the plugin directory you want the
local host to load", "A portable plugin has a `plugin.json` manifest at its root", "Configure
portable MCP servers in root `mcp.json`" and "Portable packages discover skills from the root
`skills/` directory" (https://developers.openai.com/plugins/build/plugins, read 2026-11-07). The
detection gives `mcp.json` the tool `cursor` and the skill `claude|cursor`.

The evaluation of 2026-10-26 labeled pp-0515 `wrong_tool`, with Codex and Cursor as its readers.
It labeled pp-0231, a skill of keplergl/kepler.gl with `claude|cursor`, `correct`, although
`plugins/kepler.gl/` has the same layout: a portable `plugin.json`, which its
`.agents/plugins/marketplace.json` lists as a local source. pp-0255 and pp-0262 (skills of
exa-labs/exa-mcp-server, `claude|gemini`) and pp-0278 (sourcey/sourcey, `claude|gemini|cursor`)
are labeled `correct`. Each has a portable `plugin.json` beside vendor manifests and no Codex
marketplace. The same evaluation labeled pp-0660 `wrong_tool`: a `gemini` row of comet-ml/opik
that Cursor also reads through its `.cursor` link to `.agents`. That label led to the fix that now
gives the row `gemini|cursor`. The criterion of `wrong_tool` reads "configuration of another
tool, or the named tools are not the tools that read it".

`docs/heuristics.md` states "A portable manifest names no tool: Its shared parts are attributed
to no tool". In the code, `paths.plugin_roots` adds the local sources of the marketplaces of
Claude Code and of Cursor to their plugin folders, and nothing for Codex's marketplace. Codex's
page states "Keep `source.path` relative to the marketplace root, start it with `./`, and keep it
inside that root", so the source `./` names the repository root as the plugin folder.
`codex_marketplace_parts.py` lists the rows below every local source of a Codex marketplace, the
root included, in `codex_marketplace_parts.csv`, and `codex_marketplace_parts.log` gives its
counts. In the checkout's outputs, the portable plugins that a Codex marketplace lists have 27
rows of `skills/` and `mcp.json` in 9 repositories. 5 rows in 3 repositories have the tool `any`,
because no vendor manifest is beside them, and 22 rows in 6 repositories name vendor tools and
leave out Codex:

- the 13 root skills of bex-co/beancount-io (`claude`). Its `.agents/plugins/marketplace.json`
  lists the source `./`, its root `plugin.json` has the `$schema` of Agent Plugins, it has no
  `.codex-plugin`, and its `.claude-plugin/marketplace.json` lists the same skills from
  `./skills`.
- 3 skills of chaxus/ran and 1 skill each of intelligentnode/IntelliNode and
  mountain-loop/yaak (`claude`)
- 2 skills of keplergl/kepler.gl (`claude|cursor`)
- the skill (`claude|cursor`) and the `mcp.json` (`cursor`) of marionettejs/marionette

The options:

- (a) A row is correct when every tool that it names reads the file, and a reader that it leaves
  out is no error of precision. pp-0515 becomes `correct`, the code stays, and the criterion of
  `wrong_tool` narrows to "a named tool does not read it". Path precision would then no longer
  find a reader that a row leaves out, such as the one of pp-0660.
- (b) A row names every tool that a file of the repository identifies as a reader: a vendor's
  manifest, a vendor's marketplace that lists the folder, a tool directory or a link. A portable
  manifest alone names no tool, as documented. pp-0515 stays `wrong_tool`, pp-0231 becomes
  `wrong_tool`, and pp-0255, pp-0262 and pp-0278 stay `correct`. The fix makes the folder of a
  local source of `.agents/plugins/marketplace.json` a Codex plugin for the portable parts that
  Codex reads (`skills/`, `mcp.json`), the repository root for the source `./`, as the code
  already does for the marketplaces of Claude Code and Cursor, and the 22 rows gain `codex`. `paths.plugin_roots` is collector code, so the
  change needs a version in `agentcontext/code_versions.py` whose reach is the file lists with
  `.agents/plugins/marketplace.json`. The rows show that the collection holds the files.
- (c) Every part of a portable plugin gets every tool that reads the portable layout, beside the
  vendor's tools. The documented rule changes, and the rows of exa-labs/exa-mcp-server and
  sourcey/sourcey change too. The row of the portable `plugin.json` states that Cursor and Codex
  load the layout, and pp-0515 left open whether Copilot CLI reads a portable `mcp.json`, so
  each reader needs its grounding first.

I recommend (b). It follows the criterion as written and the label of pp-0660, keeps the
documented rule for a portable manifest, and gives Codex's marketplace the effect that the
marketplaces of Claude Code and Cursor already have. The Codex page grounds it, and it changes
22 rows in 6 repositories, 13 of them the skills of bex-co/beancount-io.

## The message to the maintainer

> Two decisions for the detection accuracy evaluation, both open since the evaluation of
> 2026-10-26 and both present in the new collection:
>
> 1. pingidentity/terraform-provider-pingone (pp-0475): `.github/agents/LocalCodeReview.agent.md`
>    is a link into the submodule `shared-configs` at `git@github-emu:ping-internal/cdi-shared-configs.git`,
>    which a public request cannot read. The collection holds the target of no link into a
>    submodule, and 8 mechanism rows in 5 repositories are such links, 7 of them into public
>    GitHub repositories. I recommend (a).
>    - (a) Keep every such link as configuration at its location, and count pp-0475 correct.
>    - (b) Drop only the links into a submodule whose repository the collection does not
>      record, 1 row, where telling a relative URL apart needs a change of the collector.
>    - (c) Drop every link into a submodule, 8 rows.
> 2. marionettejs/marionette (pp-0515): Codex's repository marketplace lists the portable plugin
>    `plugins/marionette/`, and Codex reads its `mcp.json` and `skills/`, but the rows name Cursor
>    and Claude Code without Codex. kepler.gl (pp-0231) has the same layout and was labeled
>    correct, while pp-0515 and pp-0660, a row that left out Cursor, were labeled wrong_tool.
>    I recommend (b).
>    - (a) A row is correct when every tool that it names reads it.
>    - (b) A row names every tool that a file of the repository identifies, and a portable
>      manifest alone names none. pp-0231 becomes wrong_tool, and 22 rows in 6 repositories gain
>      Codex, as the code already does for the marketplaces of Claude Code and Cursor. 13 of the
>      rows are the skills of bex-co/beancount-io, whose Codex marketplace lists the repository
>      root `./` as a portable plugin.
>    - (c) Every part of a portable plugin gets every reader of the portable layout.

Whichever options the maintainer chooses, record them in the resolutions of pp-0475, pp-0515 and,
under (b) of the second decision, pp-0231 in `data/collection/evaluation/2026-10-26/labels/resolutions.csv`
with the day of the decision, in the criteria of `docs/tools/evaluation.md`, and in the last
section of [the brief](2-labeling-brief.md) before the agents start.
