# The heuristics

The package applies the heuristics in `heuristics/` at the repository root.
`agentcontext/heuristics.py` loads them, except for the import heuristics and the two files of
the planning documents. `agentcontext/imports.py` loads the import heuristics ([Imports](#imports)), and
`agentcontext/planning.py` loads the files of the planning documents. The directory contains
these files:

- `config_path_patterns.csv`, the path patterns
- `ai_commit_patterns.csv`, the commit patterns
- `other_ai_commit_patterns.csv`, the commit patterns of the tools outside the six
- `not_configuration.csv`, the path globs that are not configuration
- `not_ai_commits.csv`, the commit attributions that belong to automation other than a coding
  agent
- `import_patterns.csv`, the import heuristics, and `not_imports.csv`, what the tools do not read
  as an import
- `planning_document_patterns.csv`, the locations of the planning documents ([Planning documents](#planning-documents))
- `not_planning_documents.csv`, the locations that are left out of the planning documents on
  purpose
- `vendored_dirs.txt`, the names of the vendored directories, from which no step reads files
- `local_state_paths.csv`, the places in the tool directories where the tools keep sessions and
  other local state, which the cloning step does not copy and the collected file cleanup deletes
  ([The local state of the tools](#the-local-state-of-the-tools))
- `doc_hashes.json`, the recorded hashes of the documentation watch, and `doc_quotes.json`,
  the quotations of the planning heuristics that it found in their pages ([Planning documents](#planning-documents))

## Keeping up with the tools

`docs` and `unmatched paths` exist so that you notice a mechanism soon after a tool adds it.

`python -m agentcontext docs`, the documentation watch, fetches every `doc_url` of a heuristics
file again, hashes the visible text of each page and lists the pages that changed since
`doc_hashes.json` was last written. It reads `config_path_patterns.csv` unless `--heuristics`
names another file. `.github/workflows/doc-watch.yml` gives the `--heuristics` and
`--label-columns` of every heuristics file, such as
`--heuristics heuristics/ai_commit_patterns.csv --label-columns tool,scope`. Open each listed
page and read it before you edit any row. If the mechanism changed, update the row and its
`doc_verified` date. Once you have read every listed page, run the same command with `--update`.
It records the hash of every page that it could fetch, the changed pages included. For the two
files of the planning documents, add `--quotes`. The watch then also checks every quotation in
their notes against the pages that the row of the quotation cites, and `--update` records the
quotations with the new hashes. To record only the pages that a new heuristic cites, run
`--update --new-only`. The changed pages then keep their previous hash, and the watch lists them
until you have read them and run `--update` without `--new-only`. With `--quotes`, a changed page
also keeps its entry in `doc_quotes.json` as it was, with the quotations that the watch found in the
earlier text of the page for both files of the planning documents. On a new page and on an unchanged
page, the run records the quotations that it found beside those that a run over the other file
recorded under the same hash. An updated `doc_verified` date does not clear a page from the list,
because the watch compares only hashes.

For each changed page, the watch writes the log message
`The page <url> changed (cited by <labels>).` to stderr through the shared [logger](logs.md), in
which the labels are the values of the `--label-columns` of each row that cites the page. It logs
`Could not fetch the page <url> cited by <labels> (<reason>).` when the request fails, when the
response ends before its stated length, or when the gzip body ends early or does not decompress. Such a page keeps its recorded hash. The watch
compares every other page and then exits 1. The Transifex article and the semantic-release page
show a relative update date, such as "Updated this week", and the watch lists them again
whenever that date changes. The notes of their rows state this. Task Master's PRD guide on
tryhamster.com serves its sign-in and theme controls in one of two orders, and the two orders
have different hashes. The watch can therefore list the page although its text did not change.

When a vendor's current page no longer states a heuristic, cite in `doc_url` the live page that
replaced the old page, so that the watch still reports changes there. In `notes`, quote the last
Wayback Machine capture that states the heuristic, with the address of the capture. Do not
cite the capture in `doc_url`: A capture never changes, and the watch would report no change of
the vendor's page. When the watch fetches a capture, it adds `id_` after the timestamp so that
the Wayback Machine serves the capture without the banner that counts the captures of the page.
`TestTheAddressFetched` in `tests/test_tools_docs.py` records the URL that `fetch` requests for a
GitHub blob and for a Wayback capture, and checks that `fetch` requests the canonical address.
The negative commit heuristics cite the READMEs of Renovate, release-please and Restyled rather
than the landing pages of their repositories, because a landing page changes whenever its star
count or release widget changes.

`.github/workflows/doc-watch.yml` runs `docs` every Monday at 07:00 UTC over the heuristics files
that cite pages, over the prices of the models of the classification,
`2_classification/config/model_prices.csv`, and over the statistics of `docs/statistics.json`
with a `vendor` source. With `--statistics docs/statistics.json --quotes`, the watch checks the
page of each such statistic and the verbatim quote from which the statistic reads its number.
The prices cite OpenAI's pricing page and OpenRouter's lists of the endpoints of each model. Such
a list is JSON with the uptime, the latency and the status of each endpoint over the last minutes,
which change between two requests. The watch hashes only the fields of each endpoint that a price
row reads: its provider slug, its provider, its quantization, its prices, its supported parameters
and its context length (`OPENROUTER_ENDPOINT_FIELDS` of `agentcontext/tools/docs.py`). It opens an issue when a cited page changed or could not be
checked. The workflow step that runs `docs` writes the
output to a file, and the script that opens the issue reads that file. Text from a vendor's
server in an error message therefore stays data in the issue and never becomes part of the
script, and output of any length reaches the script. The script
shortens the issue body below GitHub's size limit and links the workflow run, whose log contains the
whole output. With `--quotes`, the watch logs one line per page for the quotations that it could not
check because the page could not be fetched, with the number of these quotations and one of them. A
host that rejects every request, such as raw.githubusercontent.com for every GitHub link, therefore
cannot make the output longer than an issue body.

`python -m agentcontext unmatched paths` reads `output/other.csv`, rewrites each path as its path
glob, and lists the path globs in <!--s:parameters.agentcontext.checks.unmatched.paths_min_repos_default:words-->three<!--/s--> or more repositories that the heuristics do not explain.
Each listed path glob is either a mechanism to add to `config_path_patterns.csv` or a row to add to
`heuristics/not_configuration.csv` with a reason. Before you add a positive or a negative heuristic,
find the rows of the path glob in `output/other.csv`, open the files that their `github_link` names,
and compare the paths with what the vendor's documentation says that the tool loads.

The runner also runs `unmatched paths` as its `unmatched_paths_report` step after the file
history step. The step writes `output/unmatched-paths.csv` as evidence for review and exits 0, so
that the collection goes on to build its local archive and release directory. Stopping the run
on unmatched paths would protect no published dataset, because the run uploads nothing: A person
uploads the dataset to Zenodo. Before you push or merge changes, `python -m agentcontext verify`
and CI run `unmatched paths --fail-above 2`, which exits 2 when an unmatched path glob occurs in
two or more repositories.

## The page or the examples behind each heuristic

Each heuristic is grounded in a vendor page or in examples. A row that its tool documents cites
the page in `doc_url`, and the documentation watch reads that page again every week. A row
derived from repositories links its sources in `evidence`: commits or files, from
different repositories and each at a fixed commit, and the app's page for an app's account. The
loaders raise an error for a `sample` or `dropped` row without evidence and for evidence that is
not an `https` link. The path patterns have no `source` column, because each of them cites a
page. Where the page does not state the whole path, as for the files that a repository keeps
beside a plugin manifest, `evidence` links files at that path.

Use another project's list of patterns or conventions only as a source of candidates, such as
the pattern files of labri-progress/agent-impact, the GitHub search queries of AIDev and PRarena
or GitClear's help page. Such a list shows where a path or a line occurs, or what its authors
observed, and not what a tool reads or writes. Ground each candidate like any other row, in a
vendor page or in examples, and name the list in `notes` when the row comes from it. Do not cite
the list in `doc_url`, because a change of the list does not change what a tool reads.

Keep a positive heuristic that a vendor page or the vendor's code states when a collection
matches nothing with it, because the sample does not have to contain every location that a tool
reads or every line that it writes. Add a positive `sample` row only with an example, which its
`evidence` links and which grounds the row. A later count of its matches describes the
collection and is no reason to remove the row.

## The tools that the dataset covers, and the commits that count as AI contributions

Configuration artifact detection determines which files of which tools the dataset collects from a
repository. AI contribution detection determines which commits in the commit history of a repository
count as a contribution of one of those tools, and which tool each of them credits. The two
detections apply separate criteria. Every quotation below was checked against the text of the page
that it cites, fetched on 2026-09-15 unless the quotation gives a later date.

### The tools

The dataset covers six tools. The dataset paper chose five of them from the 2025
Stack Overflow Developer Survey's question on "out-of-the-box agents, copilots or assistants"
([survey](https://survey.stackoverflow.co/2025/ai)). Four are the tools that the survey names most
often, with OpenAI Codex in place of ChatGPT as the same vendor's coding agent. The fifth
is Cursor, which the survey does not list and which the paper chose for its prominence in
software engineering research.

OpenCode, the sixth tool, was chosen by a count of the tools in the repositories in the sample on
2026-10-20. `python -m agentcontext tool-selection` ([the tool selection](tools/tool_selection.md))
read the file lists of the <!--s:tool_selection_2026_10_20.file_lists-->36,461<!--/s--> repositories of the October 5–6 collection and
counted, for each of <!--s:tool_selection_2026_10_20.tools-->43<!--/s--> tools, the owners with a root-level path of the tool. The
tools are the five, the other tools that document a path in the repository, and
five review products. An owner is the account in a repository's name, so that an organization that
keeps one layout in many repositories counts once. Most tools have few owners. The tools
with ten or more owners are these:

| Tool | Owners |
|---|---|
| Claude Code | <!--s:tool_selection_2026_10_20.owners.claude_code-->3,386<!--/s--> |
| GitHub Copilot | <!--s:tool_selection_2026_10_20.owners.github_copilot-->1,375<!--/s--> |
| Cursor | <!--s:tool_selection_2026_10_20.owners.cursor-->509<!--/s--> |
| Gemini | <!--s:tool_selection_2026_10_20.owners.gemini-->308<!--/s--> |
| OpenAI Codex | <!--s:tool_selection_2026_10_20.owners.openai_codex-->182<!--/s--> |
| OpenCode | <!--s:tool_selection_2026_10_20.owners.opencode-->107<!--/s--> |
| Windsurf, now Devin Desktop | <!--s:tool_selection_2026_10_20.owners.windsurf-->41<!--/s--> |
| Junie | <!--s:tool_selection_2026_10_20.owners.junie-->28<!--/s--> |
| Cline | <!--s:tool_selection_2026_10_20.owners.cline-->24<!--/s--> |
| Pi | <!--s:tool_selection_2026_10_20.owners.pi-->22<!--/s--> |
| Kiro | <!--s:tool_selection_2026_10_20.owners.kiro-->20<!--/s--> |
| JetBrains AI Assistant | <!--s:tool_selection_2026_10_20.owners.jetbrains_ai_assistant-->14<!--/s--> |
| Aider | <!--s:tool_selection_2026_10_20.owners.aider-->13<!--/s--> |

The cutoff is the largest step between two neighbors below the five, from OpenCode's <!--s:tool_selection_2026_10_20.owners.opencode-->107<!--/s--> owners to
Windsurf's <!--s:tool_selection_2026_10_20.owners.windsurf-->41<!--/s-->, a factor of <!--s:tool_selection_2026_10_20.largest_step-->2.61<!--/s-->. Among the five, the steps are <!--s:tool_selection_2026_10_20.step.cursor_gemini-->1.65<!--/s--> to <!--s:tool_selection_2026_10_20.step.copilot_cursor-->2.70<!--/s-->, and the step from Codex
to OpenCode, <!--s:tool_selection_2026_10_20.step.codex_opencode-->1.70<!--/s-->, is within that range. Below Windsurf, no step down to Aider exceeds <!--s:tool_selection_2026_10_20.step.windsurf_junie-->1.46<!--/s-->.
Head/tail breaks over the tools outside the five agree with this cutoff. The first break, at
the mean of <!--s:tool_selection_2026_10_20.outside_mean-->13.2<!--/s--> owners, keeps the seven tools from OpenCode to JetBrains AI Assistant. The second,
at the mean of those seven tools, keeps OpenCode and Windsurf, and the third, at the mean of these
two, keeps OpenCode alone. Over all the tools, the first break lies at the mean of <!--s:tool_selection_2026_10_20.all_mean-->196.9<!--/s-->
owners, which Claude Code's owners raise. Claude Code, Copilot, Cursor and Gemini are above that
mean, and Codex is below it.

The review products are counted separately, because they review a change rather than write one.
CodeRabbit has <!--s:tool_selection_2026_10_20.owners.coderabbit-->308<!--/s--> owners, as many as Gemini. Greptile has
<!--s:tool_selection_2026_10_20.owners.greptile-->21<!--/s-->, Sourcery <!--s:tool_selection_2026_10_20.owners.sourcery-->20<!--/s--> and Qodo
<!--s:tool_selection_2026_10_20.owners.qodo-->12<!--/s-->. Pi, which GitHub Agentic Workflows runs as a built-in engine, is below
the cutoff. Hermes Agent and OpenClaw are not in the count, because neither documents a path in
the repository. OpenClaw keeps its workspace in `~/.openclaw/workspace`, and Hermes Agent keeps
its skills in `~/.hermes/skills/`. OpenCode has a tool flag and configuration artifacts in the
mechanism CSVs, and the commit patterns attribute commits to it, as for the five (the paragraph OpenCode
below).

### Configuration artifact detection

**Criterion.** A file counts when its vendor documents it as repository-level configuration of a
product of one of the six tools. The product may write changes or review them, and the files of the
review products count as well. Gemini Code Assist on GitHub, for example, reads
`.gemini/config.yaml`: "You modify Gemini Code Assist on GitHub behavior by adding a config.yaml
file to a .gemini/ folder located in the root of each of your repositories." ([Google
Cloud](https://docs.cloud.google.com/gemini/docs/code-review/customize-repo-review), read
2026-10-16) The rows of the two files of that folder, `config.yaml` and `styleguide.md`, have the
match mode `root_file` (since 2026-10-15), because the page names the folder at the root of each
repository. A row that matched them at any depth would set the `gemini` flag of
googleapis/google-cloud-java, which has a nested copy of the folder. Cursor documents
`.cursor/BUGBOT.md` for Bugbot: "Create .cursor/BUGBOT.md files to provide project-specific context
for reviews." ([Cursor](https://cursor.com/docs/bugbot)) Whether a product produces changes matters
for AI contribution detection (below), not for configuration artifact detection.

A tool's path that a repository's `.gitignore` names, such as `.claude/`, is no
configuration artifact and sets no tool flag, because the criterion counts the files that a
repository contains and not the names in its `.gitignore`. labri-progress/agent-impact counts
such a name as a sign of adoption.

A skill folder can keep its `SKILL.md` as a link to a file elsewhere in the repository, as
feast-dev/feast's `.claude/skills/feast-dev/SKILL.md` links to `../../../skills/feast-dev/SKILL.md`.
Claude Code, OpenCode and Gemini CLI read the skill in the file that the link leads to. OpenCode's
skill scan types a link to a file as a file, `info.type === "File" ? "file"`, and takes it as the
skill, `child.type === "file" && child.name === "SKILL.md"`
([`skill-scan.ts`](https://github.com/anomalyco/opencode/blob/510aa79d075c32e75f85b7774ff7390370c22b18/packages/core/src/config/plugin/skill-scan.ts)).
Gemini CLI finds its skills with `glob(['SKILL.md', '*/SKILL.md'], { ... nodir: true ... })`,
which a link to a file matches and which reads no deeper folder, and then reads the file
([`skillLoader.ts`](https://github.com/google-gemini/gemini-cli/blob/718e97c2448e0384cb582409cf4e479437bf50fe/packages/core/src/skills/skillLoader.ts)).
Claude Code 2.1.312's skill loader resolves the link and skips only a target that is no regular file,
that exceeds its size limit, or that is a device or process file, with the warning "not a regular
file, or refused for where it leads". Codex skips every linked skill file, because it skips every
link that is not a directory when it lists a skills directory, `if is_symlink &&
(!options.follow_directory_symlinks || !metadata.is_dir()) { continue; }`
([`local_file_system.rs`](https://github.com/openai/codex/blob/f9c10053fb3d9b960e78cfeae1f3ffa21e0dc00a/codex-rs/exec-server/src/local_file_system.rs)),
and `ignores_symlinked_skill_files_and_directory_cycles`
([`discovery_tests.rs`](https://github.com/openai/codex/blob/f9c10053fb3d9b960e78cfeae1f3ffa21e0dc00a/codex-rs/ext/skills/src/loader/discovery_tests.rs))
tests that a skill with a `SKILL.md` that links to a file is not discovered. Cursor reads a linked
`SKILL.md` only when the link's target lies inside the same skills directory, because Cursor CLI
2026.09.28-64d2043 keeps a `SKILL.md` only when its real path is within the real path of the
skills directory that it scans (`findSkillMarkdownFiles`, `isPathWithin`). For the same reason,
it follows no skill folder that a link in a project skills directory leads to outside that
directory, such as bestofjs/bestofjs's `.cursor/skills/find-skills`, a link to
`../../.agents/skills/find-skills`: It descends into a folder only when the folder's real path is
within the directory, and it follows links to folders only in the user's own skills directory.
Cursor still reads such a skill where it is when the skill is in a skills directory that Cursor
reads and Cursor does not skip the file, as it skips a file with a front matter that does not
parse. The table below "Skills are automatically loaded from these locations" gives
`.agents/skills/` and `.cursor/skills/` the scope "Project-level"
([Cursor](https://cursor.com/docs/skills), read 2026-11-08). Where Cursor reads the skill at its
own path, the link names Cursor as a reader of the skill, and the skill keeps Cursor
(`detection._cursor_reads_at_its_own_path`). yoopta-editor/Yoopta-Editor links each skill folder
of its `.agents/skills` from `.claude/skills`, `.codex/skills`, `.cursor/skills` and
`.opencode/skills`, and each skill is the skill of the four tools (pp-0198 of the evaluation of
2026-11-04). Where Cursor does not read the skill at its own path, detection credits no Cursor
skill through the link (`detection._links_outside_its_directory`), and the skill keeps the tools of
the directory that it is in. A link of the whole `.cursor/skills` to another skills directory
names the directory that Cursor scans, and its skills are Cursor's.

Copilot CLI and Antigravity CLI read a linked `SKILL.md` and follow a linked skill folder
wherever the link leads. Node called the native discovery of Copilot CLI 1.0.105, `skillsLoad`
in the `runtime.node` of
[@github/copilot-darwin-arm64](https://registry.npmjs.org/@github/copilot-darwin-arm64/-/copilot-darwin-arm64-1.0.105.tgz),
on test repositories on 2026-10-30. In each of `.github/skills`, `.agents/skills` and
`.claude/skills`, it loaded a skill with a `SKILL.md` that links to a file in the same skills
directory or in `docs/`, and a skill folder that links to a folder inside or outside the skills
directory, with the text of the file that the link leads to. Its changelog for 1.0.62 reads "Load
skills from symlinked directories outside the configured root"
([changelog.md](https://github.com/github/copilot-cli/blob/84ee80ea6490ce02d6000439356a609fcefbb9ae/changelog.md)).
The test covers Copilot CLI and not Copilot in VS Code. Antigravity CLI 1.4.9 lists a skills
directory with Go's `os.ReadDir` and takes an entry as a skill folder when it is a folder or a
link, `IsDir()` or `Type()&ModeSymlink` (`discoverInSubDirsFiltered`). It keeps a link when
`os.Stat`, which follows the link, finds a folder. It then takes the file of the folder named
`SKILL.md`, compared without case and without a test of its type (`findFileCaseInsensitive`,
`strings.EqualFold`), and reads it with `os.ReadFile`
([the CLI](https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.9-4962164969177088/linux-arm/cli_linux_arm64.tar.gz)).
Antigravity CLI compares no real path with the skills directory, and it reads each of its
customization roots, `.agents`, `.agent`, `_agents` and `_agent`, the same way.

The cloning step copies the file that such a link leads to, in the skills directory of every tool
(`paths.skill_files`), as it copies a linked skill folder. The collected file cleanup keeps the
file where a heuristic keeps the marker, and the detection step reads the `language` of the skill
in it. Where each tool that reads a skills directory skips the link, the link is no skill
(`detection._skipped_skill`). A linked `SKILL.md` with a target outside the directory in
`.codex/skills`, which Codex and Cursor read, or in `.cursor/skills` is then a file of the tool's
configuration directory that no mechanism covers, an `other` row of Codex or Cursor. One with a
target inside `.codex/skills` is Cursor's skill, because Codex skips it and Cursor reads it.
NotePlan/plugins's
`.cursor/skills/nplog/SKILL.md`, which links to `agents/skills/nplog/SKILL.md`, is therefore an
`other` row of Cursor. A linked `SKILL.md` stays a skill in `.claude/skills`, `.opencode/skills`,
`.gemini/skills`, `.github/skills` and the Antigravity skills directories, because their tools
read it. `.agents/skills` has the tool `any`, because Codex, Copilot, Cursor, Antigravity and
OpenCode read it. OpenCode reads it with the skill scan above,
`[...roots.claude, ...roots.agents].map((root) => path.join(root, "skills"))`
([`compatibility.ts`](https://github.com/anomalyco/opencode/blob/510aa79d075c32e75f85b7774ff7390370c22b18/packages/core/src/config/plugin/compatibility.ts)).
Of a linked `SKILL.md` there, Gemini, Copilot and OpenCode read the link, Cursor reads it where
the target lies inside `.agents/skills`, and Codex skips it. Several tools therefore still read it,
and the skill keeps `any` and sets no tool flag, as the directory's own skills do. Where one reader
is left, the skill is that tool's (`detection._tools_reading_a_skill`). A link to the directory adds
no reader outside that reader's own directory (`detection._readers_without_their_own_link`), because
the reader reads the file directly: amelioro/ameliorate's `.claude/skills/skills`, a link to
`.agents/skills`, leaves its skills with `any`, and a repository that has no OpenCode file gets no
`opencode` flag from it. A skills directory that the heuristics of one tool alone read is that tool's
own, as Antigravity's `.agent/skills`, `_agents/skills` and `_agent/skills` are, so that
sanity-io/pkg-utils's `.agent/skills/tsdown`, a link to `.agents/skills/tsdown`, credits Gemini.
microsoft/beachball's `.agents/skills/beachball-change-file/SKILL.md`, a link to
`skills/beachball-change-file/SKILL.md`, is a linked `SKILL.md` that keeps `any`. A tool that skips the
link gets no credit through a link of its directory either, such as a `.codex` or a `.cursor` that
leads to `.agents`.

Claude Code 2.1.312 lists the commands of `.claude/commands` with ripgrep's `--files --hidden
--follow --no-ignore --glob *.md`, so it also reads the commands below a link in the directory,
as GovTechSG/sgds-web-component's `.claude/commands/sgds-forms` links to
`../../skills/sgds-forms`. Of a folder with a `SKILL.md`, it reads that file as the command named
for the folder, with the folder as the base directory of the skill, and no other `.md` file
directly in the folder. The `.md` files of its subfolders are commands of their own. The cloning
step
copies the folder that such a link leads to (`paths.link_parents`), and the collected file
cleanup keeps it when it contains a `.md` file. The detection step lists the commands of the folder
at their own paths and credits them to Claude Code through the link
(`detection._linked_markdown_folders`).

Claude Code reads the rules below a link in `.claude/rules` too. Its
[memory page](https://code.claude.com/docs/en/memory) says that "The `.claude/rules/` directory
supports symlinks" and names a rules file "you reach through a symlink, whether the file or its
directory is the link". opendatahub-io/opendatahub-operator links `.claude/rules/.rules` to
`../../.rules`. The collected file cleanup keeps such a folder when it contains a `.md` file, and
the detection step lists its rules at their own paths and credits them to Claude Code through the
link (`detection.LINKED_RULES_DIRS`). No page that the heuristics cite says the same of
`.claude/output-styles`, and the step reads no output style through a linked folder.

**Gemini includes Antigravity.** Google retired Gemini CLI and the Gemini Code Assist IDE extensions
for individual users on 2026-06-18 and names Antigravity CLI their successor: "On June 18, 2026,
Gemini CLI and Gemini Code Assist IDE extensions will stop serving requests" ([Google for
Developers](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)).
Since 2026-09-29, Antigravity's configuration artifacts count under the `gemini` tool flag, which
covers Google's coding agent under each of its names. Under AI contribution detection, a commit that credits
Antigravity has `Gemini` in the `ai_tool` column of the commit CSV. Antigravity reads `GEMINI.md`
and `AGENTS.md` as Gemini CLI did, and its own files under the workspace customization roots
`.agents/`, `.agent/`, `_agents/` and `_agent/`. Antigravity CLI 1.2.14's embedded discovery guide
names all of these roots, and the customization discovery page states "Antigravity also recognizes
folders named .agent/, _agents/, and _agent/"
([customization discovery](https://antigravity.google/docs/customization-discovery), read
2026-11-05). Each root supports rules, skills, subagents, hooks, MCP servers, workflows, plugins
and their registration files. The rows cite the component references and record the CLI version
that establishes these root names as aliases.

Inside a root, the page places rules in `rules/`, skills in `skills/`, custom agents in
`agents/`, plugins in `plugins/`, hooks in `hooks.json` and MCP servers in `mcp_config.json`.
It lists `AGENTS.md` and `GEMINI.md` among the rules too, which Antigravity reads beside the
root: "At each level, it reads AGENTS.md, GEMINI.md, and the .agents/ folder". The rows also
read the legacy `workflows/` and the discovery manifests in a root. `_agents` and `_agent` are
also the names of private Python packages and documentation partials, such as
NVIDIA-NeMo/Curator's `nemo_curator/stages/audio/_agent/`, genkit-ai/genkit's
`py/packages/genkit/src/genkit/_ai/_agents/` and reactive/data-client's `docs/core/_agents/`.
Their rows therefore have the match mode `spec_dir_with_place`: such a directory is a
customization root, which the cloning step copies, the collected file cleanup keeps and
detection reads whole, only where it contains one of these places with a file or a link at or
below it (`paths.is_spec_directory`). `.agents` and `.agent` are `spec_dir` rows and count
whatever they contain. A link with the name of a root is followed as the link of any tool
directory is.

Antigravity reads the Markdown files directly in `rules/` and `workflows/`. A rule needs a front
matter with a `trigger` that Antigravity accepts. The rules page states "Every .md file inside
rules/ must start with YAML frontmatter declaring a valid trigger", lists the values `always_on`,
`model_decision`, `glob` and `manual`, and states that Antigravity silently discards a file that
"omits frontmatter or specifies an
unrecognized trigger value (such as camelCase alwaysOn or modelDecision)"
([rules](https://antigravity.google/docs/rules), read 2026-10-13). The rows with the match mode
`dir_ext_triggered:.md` count only a file with such a trigger and a front matter that parses as
YAML once Antigravity has quoted two of its values (`paths.has_trigger`). Antigravity CLI 1.4.10
reads every rule of a workspace, a plugin and a registration through `parseRuleInfo`
([the CLI](https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-arm/cli_linux_arm64.tar.gz),
read 2026-11-07). The function first puts a `globs` or `glob` value that starts with `*` in double
quotes up to the end of its line, by replacing `(?m)^(\s*globs?\s*:\s*)([*][^\n]*)` with
`${1}"${2}"`. The rules page requests these quotes: "Wrap patterns starting with * in
quotes so YAML does not treat * as an alias anchor." ([rules](https://antigravity.google/docs/rules),
read 2026-11-07). Where the front matter still does not parse, `NormalizeFrontmatterDescription`
quotes a `description` value that holds an unquoted `: ` or ends with `:`, the fix that the CLI's
changelog for 1.3.6 describes: "Fixed skills, rules, workflows, and Markdown agents failing to load
with `mapping values are not allowed in this context` when an unquoted `description:` contains a
colon". go-yaml v3 then decodes the front matter into a struct of four strings, which its type
descriptor in the binary names `yaml:"trigger"`, `yaml:"description"`, `yaml:"globs"` and
`yaml:"glob"`. go-yaml v3 decodes any scalar into such a string, refuses a list or a mapping there
and ignores the other keys. `getRules` logs a rule that does not decode, "Failed to parse rule file
%s: %v", and reads the next file. GoogleCloudPlatform/khi's `.agents/rules/html-coding-rule.md`
with `globs: **/*.html` and mum4k/termdash's `.agents/rules/012-q-prompt.md` with `globs: *` are
therefore rules. khi's `.agents/rules/description-rule.md`, which lists its globs, is no rule, and
neither is a rule with a front matter that holds a list that is not closed, a key twice at its top
level or an alias in its description. The steps parse with PyYAML (`paths.load_yaml`), and
[the YAML parsers](#the-yaml-parsers-of-the-tools) gives where it differs from go-yaml v3.
Antigravity CLI 1.4.2 applied the requirement to a plugin's `rules/`
too (below), and the `<antigravity-plugin>/rules` row therefore has the match mode
`dir_ext_triggered:.md` as well. The [Build plugins](https://antigravity.google/docs/build-plugins)
page describes a plugin's "rules/AGENTS.md (or rules/GEMINI.md)" as "Loaded automatically as an
always-on rule whenever the plugin is enabled (no YAML frontmatter required)" (read 2026-11-01).
Such a file without a trigger counts as a context file through the `basename` rows of
`AGENTS.md` and `GEMINI.md`, not as a rule. The collected tree of the run that started on 2026-10-23 holds no
`rules/` folder of an Antigravity plugin.

An Antigravity subagent is `agents/<name>.md` or `agents/<name>/agent.md` with a non-empty `name`
and `description` in its front matter (`paths.has_name_and_description`). Other Markdown files in
those folders are not agents. The pages spell the file `agent.md`, and Antigravity CLI 1.4.9 and
1.4.10 find it in any letter case: `(*subdirCustomization).findMarkerInDir` looks it up with
`customizations.findFileCaseInsensitive`, which compares each name with `strings.EqualFold`, as
for `SKILL.md` ([the CLI](https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.9-4962164969177088/linux-arm/cli_linux_arm64.tar.gz),
read 2026-11-05). opensupplyhub/open-supply-hub's `.agent/agents/oshub-test-writer/AGENT.md` is
therefore an agent (`heuristics.is_folder_agent_file`). Antigravity reads a plugin's `agents/`
the same way, and the `<antigravity-plugin>/agents` row has the match mode `dir_agent:.md`. A
plugin is at
`plugins/<name>/` under a customization root or in a directory that `plugins.json` selects. A folder
there without a `plugin.json` is not a plugin, because the plugins page marks the manifest as
required. The Build plugins page places a plugin's MCP servers "in <plugin-root>/mcp_config.json"
(read 2026-11-01), which Antigravity CLI 1.4.9 and 1.4.10 read (`processPluginMcpServers`). The
CLI reads a plugin's `mcp.json` only for a `plugin.json` with the `$schema` of Agent Plugins 1.0.0
while the flag `enable-agent-plugins-spec` is on. The `<antigravity-plugin>/mcp_config.json` row
follows the page and the CLI, and the `<agent-plugin>/mcp.json` row, which follows the Agent
Plugins specification, reads the `mcp.json` beside such a manifest.

An Antigravity skill needs front matter. The skills page states "Every `SKILL.md` file begins
with YAML frontmatter" ([skills](https://antigravity.google/docs/skills), read 2026-11-05), and
the changelog that Antigravity CLI 1.4.10 ships says for 1.4.1 that `agy plugin validate` "errors on
a `SKILL.md` with missing or invalid frontmatter and warns when `name` or `description` is
missing", and for 1.4.6 that it "reports every `SKILL.md` the plugin loader skips, including
skills listed through `plugin.json` or `skills.json`". The binary's messages include "no
frontmatter: the file must start with a --- block holding name and description" and "%s. The
loader skips this skill"
([the CLI](https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-arm/cli_linux_arm64.tar.gz)).
Where only Antigravity reads a skill, in `.agent/skills`, `_agents/skills`, `_agent/skills` and a
plugin's `skills/`, and where an Antigravity plugin's manifest or a registration file names it,
the detection step counts a `SKILL.md` only when it opens with a `---` line that a later `---`
line closes (`detection._antigravity_skill_loads`). It does not read the keys, because a skill
without `name` or `description` loads with a warning, and it does not parse the YAML.
GoogleCloudPlatform/cloud-solutions' `projects/legacy-detox-demo/.agents/skills.json` registers
the folders in `projects/legacy-detox-demo/skills/`. Each `SKILL.md` there, such as
`zero-copy-ingestion/SKILL.md`, opens with a heading, and no folder counts as a skill. The
cloning step and the collected file cleanup keep these files, because neither tests the front
matter of a `SKILL.md`. A `SKILL.md` without front matter in `.agents/skills`, which other tools
read too, stays a skill with the tool `any`.

Antigravity reads a skills folder one level deep: "Antigravity scans each customization folder one
level deep. A skill folder must sit directly inside skills/" and "To load a nested item, move it
up one level or name it in a discovery manifest"
([customization discovery](https://antigravity.google/docs/customization-discovery), read
2026-11-05). The rows of `.agent/skills`, `_agents/skills`, `_agent/skills` and a plugin's
`skills/`, which no other tool reads, have the match mode `skill_dir_flat`. A `SKILL.md` nested
deeper, such as joemccann/dillinger's `.agent/skills/game-development/2d-games/SKILL.md`, is a
file of the skill whose folder contains it, unless a `skills.json` or the `skills` of a plugin's
manifest names it. Codex reads `.agents/skills` recursively, and a nested skill there stays a
skill.

Codex loads a `SKILL.md` only when its name and its parser accept it. Discovery keeps a file when
`entry.path.basename().as_deref() == Some(SKILLS_FILENAME)`, with `SKILLS_FILENAME: &str =
"SKILL.md"`, and compares the name that the directory listing returns, on every host.
`parse_skill_frontmatter_metadata` begins with
`extract_frontmatter(contents).ok_or(SkillParseError::MissingFrontmatter)?`, where
`extract_frontmatter` needs a first line and a later line that trim to `---`, and continues with
`if description.is_empty() { return Err(SkillParseError::MissingField("description")); }` and a
name of at most 64 characters (`MAX_NAME_LEN`). The file is read with `String::from_utf8`, which
keeps a byte order mark, and `trim` does not remove U+FEFF. A skill that does not parse is a
`SkillError`, and Codex loads nothing from it
([`discovery.rs`](https://github.com/openai/codex/blob/f9c10053fb3d9b960e78cfeae1f3ffa21e0dc00a/codex-rs/ext/skills/src/loader/discovery.rs),
[`parser.rs`](https://github.com/openai/codex/blob/f9c10053fb3d9b960e78cfeae1f3ffa21e0dc00a/codex-rs/skills/src/parser.rs),
[`lib.rs`](https://github.com/openai/codex/blob/f9c10053fb3d9b960e78cfeae1f3ffa21e0dc00a/codex-rs/file-system/src/lib.rs),
read 2026-11-05). The detection step credits Codex with no `SKILL.md` that fails these tests
(`detection._codex_skill_loads`), as it credits Codex with no `SKILL.md` that is a link. It reads
lines rather than parse YAML, and a front matter that YAML rejects elsewhere still passes.
shapeshift/web links `.codex/skills` to `../.claude/skills`, and its
`.claude/skills/chain-integration-crew/skill.md`, named in lowercase and opening with a heading, is
Claude Code's alone. A skill of a Codex plugin's `skills/` that Codex refuses is no
skill, because no other tool reads that folder.

Cursor also reads `.claude/agents`: ".claude/agents/ Current project only (Claude compatibility)"
([subagents](https://cursor.com/docs/subagents), read 2026-11-06). A file there that Claude Code
loads is Claude Code's, at its path and through a link, and a file that it does not load is Cursor's
when it has a front matter at its top (`detection._cursor_agent_has_front_matter`), with `memory`
`n/a` (`detection._without_the_compatibility_reader`). The parser of Cursor's loader also accepts two
horizontal rules below the top of a document, but its page states "Each subagent is a markdown file
with YAML frontmatter", so such a document is no subagent. In
[a thread on nested Markdown files](https://forum.cursor.com/t/nested-markdown-files-inside-cursor-agents-are-incorrectly-detected-as-subagents/166296),
Cursor's staff calls their detection as subagents "a bug on our side" and states the parser's
reading: "You specifically need a pair of `---` (two horizontal rules) or a real frontmatter
block." Cursor's reader removes a byte order mark before the parser, so a mark above the block does
not count against the test. An `other` row of such a directory stays the tool of the directory
(`detection._tool_of_an_other_row`).

OpenCode defines agents in its configuration as well, under `agents` ("Define agents under agents
in any OpenCode configuration file", [agents](https://opencode.ai/docs/agents/)) and, as OpenCode 2
still reads the format of OpenCode 1, under `agent` and `mode` ("The singular agent and deprecated
mode maps become agents." and "Entries from the old mode map become primary agents.",
[migration](https://opencode.ai/docs/migrate-v1/), both read 2026-11-06). The rows
`json_servers:agents`, `json_servers:agent` and `json_servers:mode` of `opencode.json`,
`opencode.jsonc`, `.opencode/opencode.json` and `.opencode/opencode.jsonc` count a file once, named
after the file stem, as no mechanism CSV repeats a path of a repository. Detection skips a file whose
objects name only the built-in agents `build`, `plan`, `general`, `explore`, `compaction`, `title` and
`summary`, which OpenCode's `agent.ts` loads with `native: true`
(`detection._opencode_defines_an_agent`). GLOSSARY.md defines subagents as "An agent definition to
which the tool can delegate part of a task". A Markdown agent of `.opencode/agent`,
`.opencode/agents`, `.opencode/mode` or `.opencode/modes` with the name of a built-in agent is no
subagent either, because OpenCode puts it into the same map of agents. In
`packages/opencode/src/config/agent.ts` of
[e3c4eb242585](https://github.com/anomalyco/opencode/blob/e3c4eb242585dcd77be1a867391b7c25fd47ce52/packages/opencode/src/config/agent.ts)
(read 2026-11-08), `load` scans `{agent,agents}/**/*.md` and names each agent
`configEntryNameFromPath(path.relative(dir, item), ["agent/", "agents/"])`, and `loadMode` scans
`{mode,modes}/*.md` and names each `configEntryNameFromPath(path.relative(dir, item), ["mode/",
"modes/"])`. Both build an agent of `name`, `...md.data` and `prompt: md.content.trim()` and store
it under `result[config.name]`, and `packages/opencode/src/agent/agent.ts` merges an agent with the
name of a built-in agent into that agent, `item.prompt = value.prompt ?? item.prompt`. The name is
the path below the folder without `.md`, or the `name` of the front matter where it sets one
(`detection._opencode_markdown_agent_name`). debanjandhar12/logseq-astro-box's and
debanjandhar12/logseq-anki-sync's `.opencode/agents/plan.md` and equinor/design-system's
`.opencode/agent/build.md` adjust a built-in agent and are `other` rows of `.opencode` (pp-0797 of
the evaluation of 2026-11-04). The rows `.opencode/agents/**` and `.opencode/agent/**` of
`not_configuration.csv` give the reason, "Override a built-in by using the same ID"
([agents](https://opencode.ai/docs/agents/), read 2026-11-08), so that `unmatched paths` does not
list them.

The prompt of such an agent can name a file. OpenCode replaces a reference in a configuration file
with the content of the file that it names: "Use {file:path/to/file} to substitute the contents of a
file" and "File paths can be: Relative to the config file directory"
([config](https://opencode.ai/docs/config/), read 2026-11-07). OpenCode 2.2.6 resolves the path
against the folder of the configuration file, `path.isAbsolute(expandedPath) ? expandedPath :
path.resolve(configDir, expandedPath)` with `const configDir = input.type === "path" ?
path.dirname(input.path) : input.dir` (`packages/core/src/config/variable.ts` of
[anomalyco/opencode](https://github.com/anomalyco/opencode/blob/dc2ffcf8bf26f9e669b6aa22b95f50e12be15e06/packages/core/src/config/variable.ts)),
and OpenCode 1.19.5 the same way, `path.isAbsolute(filePath) ? filePath : path.resolve(configDir,
filePath)` with `return input.type === "path" ? path.dirname(input.path) : input.dir` in `dir()`
(`packages/opencode/src/config/variable.ts`). cakmoel/Scriptlog's `opencode.json` gives its subagent
playwright-test-generator the prompt `{file:.opencode/prompts/playwright-test-generator.md}`. The
file is a part of the agent that names it, as the prompt file of an agent action is a part of its
workflow (decided on 2026-11-07). `paths.opencode_prompt_files` reads the references in the `prompt`
of an agent, the key of OpenCode 1, and in its `system`, the key of OpenCode 2, under each key of
these rows and in each of their files, at the root and in every `.opencode` directory. The cloning
step copies the file, the collected file cleanup keeps it, also when it is empty, and the detection
step counts it with the configuration that names it rather than in `other`, also for a built-in
agent that the configuration adjusts. A Markdown agent in `.opencode/agents` names no prompt file,
because its body is its prompt and OpenCode substitutes no reference there: `prompt:
md.content.trim()` in `packages/opencode/src/config/agent.ts` of v1.19.5, and `prompt: body` for the
format of OpenCode 1 and `system: body` for its own in `packages/core/src/config/plugin/agent.ts` of
v2.2.6. A reference outside a prompt, such as the `{file:.secrets/project-ref}` in the url of an MCP
server of mattrossman/forecaswatch2, names no prompt file.

Gemini CLI reads only the events of its `HookEventName`. It skips any other name,
`if (!this.isValidEventName(eventName)) { ... Invalid hook event name: ... Skipping.`, so that a
`.gemini/settings.json` with an event of Claude Code, such as `PreToolUse`, is no hooks row. Antigravity
does not read that file: "Hooks in ~/.gemini/antigravity-cli/settings.json aren't loaded."
([hooks](https://antigravity.google/docs/hooks), read 2026-11-06).

Cursor reads the skills directories of Claude Code and Codex: "For compatibility, Cursor also
loads skills from Claude and Codex directories: `.claude/skills/`, `.codex/skills/`,
`~/.claude/skills/`, and `~/.codex/skills/`." ([skills](https://cursor.com/docs/skills), read
2026-11-05) Cursor CLI 2026.09.28-64d2043 lists `{configDir:".claude",subdir:"skills",thirdParty:!0}`
and `{configDir:".codex",subdir:"skills",thirdParty:!0}` among its skill roots, and below the
root, its nested service adds `"*/**/.claude/**"` and `"*/**/.codex/**"` when third-party
extensibility is on, which is the default. Its loaders need no description, and
`parseSkillMarkdown` takes the first line of the content for a missing one. The heuristics give
Cursor a row for each of the two directories, and the directory's own tool takes precedence where
it reads a skill (`detection._tools_reading_a_skill`). A skill is therefore Cursor's where Codex
does not load it, such as FlameskyDexive/Legends-Of-Heroes's `.codex/skills/aibridge/SKILL.md`,
which opens with a byte order mark, and probelabs/probe's
`test_data/skills/.codex/skills/no-desc/SKILL.md`, which has no description.

Cursor CLI drops a skill with a front matter that gray-matter 4.0.3 cannot parse.
`parseSkillMarkdown`, which reads the skills of a skills directory and, through
`loadPluginSkills`, those of a plugin, returns no skill on the error:
"catch(e){return(0,m.cY)(`Failed to parse skill frontmatter for ${s}:`,e),null}"
(`dist-package/1623.index.js` of Cursor CLI 2026.09.28-64d2043, read 2026-11-07). The IDE fails
a request with such a skill attached, as Cursor's staff states: "If that frontmatter isn't valid
YAML, the request fails with a generic server error", with "An unquoted description that contains
a colon" among the causes
([forum](https://forum.cursor.com/t/error-connecting-to-server-sending-prompts-with-skills-attached/168440),
2026-08-14, Cursor 3.14.27, read 2026-11-07). The maintainer decided on 2026-11-07 that such a
skill is not a skill of Cursor, because the CLI drops it and the IDE cannot send a request with it
attached. `detection._cursor_skill_loads` splits the front matter off as gray-matter does, up to
the first later line that starts with `---` or to the end of the file, and parses it as YAML
([the YAML parsers](#the-yaml-parsers-of-the-tools)). probe's `invalid-yaml/SKILL.md` beside
`no-desc/`, with `name: [invalid`, which Codex refuses as well, is therefore an `other` row of
Codex, and the skills of cabloy/cabloy's `.cursor/skills` with an unquoted `: ` in their
descriptions are `other` rows of Cursor. Shopify/shopify-app-js's
`.claude/skills/dependabot/fix-broken-major-upgrade/SKILL.md`, with
`description: Process a major Dependabot PR: research breaking changes, apply fixes, validate CI`,
and the `.claude/skills/repo/release/SKILL.md` of `SocketDev/socket-registry` are in category
folders that Claude Code does not read, and OpenCode 2 loads both. Both releases of OpenCode parse
a front matter with `parse` of `packages/core/src/config/markdown.ts`, which parses it again after
`sanitize` where gray-matter fails: "Other coding agents accept unquoted colons in frontmatter
values. Retry those values as YAML block scalars so existing config files keep working."
([`markdown.ts`](https://github.com/anomalyco/opencode/blob/e3c4eb242585dcd77be1a867391b7c25fd47ce52/packages/core/src/config/markdown.ts),
read 2026-11-07). OpenCode 2 makes the name optional, `name: Schema.String.pipe(Schema.optional)`,
and names a skill without one after its folder
([`skill-file.ts`](https://github.com/anomalyco/opencode/blob/510aa79d075c32e75f85b7774ff7390370c22b18/packages/core/src/config/plugin/skill-file.ts),
read 2026-11-07). OpenCode 1.19.3 keeps a skill only with a name, `typeof data.name === "string"`
in `isSkillFrontmatter` of
[`index.ts`](https://github.com/anomalyco/opencode/blob/e3c4eb242585dcd77be1a867391b7c25fd47ce52/packages/opencode/src/skill/index.ts),
and skips Shopify's file, which has none. The file stays OpenCode's, because OpenCode 2 loads it
and the steps count the current release. gray-matter reads a word after the first
`---` as the language of the front matter. The test keeps a file with a word other than `yaml` or
`yml`, because the forum states the failure of the IDE only for a front matter that is not valid
YAML, although the CLI drops the file where gray-matter has no engine for the word, such as `toml`.

A tool that reads another tool's skills directory for
compatibility is credited only with a marker named `SKILL.md` exactly, because Cursor CLI keeps a
file only when `"SKILL.md"===e.name` and OpenCode compares `child.name === "SKILL.md"`. Cursor
keeps the marker in any case in its own `.cursor/skills`, because the loader of the Cursor IDE was
not read.

`.agents/rules.json` registers rule files that the directory scan leaves out. Each entry names an
item or a directory, and for a directory, the scan reads the items directly in it. `include_only`
names the items to read instead, including nested items, and `exclude` names items to leave out.
Each value is a name and not a pattern. The rules page gives a file and a folder,
`"exclude": ["deprecated_rules.md", "legacy/"]`, and a nested file,
`"include_only": ["frontend/react.md"]` ([rules](https://antigravity.google/docs/rules), read
2026-10-18). The changelog of CLI 1.2.10 says "to load a nested item, name it in `include_only`".
The JSON Configuration Files guide that CLI 1.4.2 ships calls both lists "Patterns" and shows
`"exclude": ["experimental-.*"]`. It also states where a path resolves: "Relative `path` entries
resolve against the directory containing the dot-dir that declares them, falling back to the project
root when nothing exists there." Antigravity CLI 1.4.2 (linux_arm64, from the installer's manifest
on 2026-10-18) was run offline with `antigravity agents` over trusted workspaces with an
`.agents/agents.json`. It compared each value with the items as written, and the steps read the
values as the CLI did (`paths.registered_configuration_files`):

- `include_only` selected an item by its path below the entry's directory, with or without
  `.md`, or by the path of its `agent.md`. A trailing `/` made no difference. `al.*`, `al*`,
  `^alpha$`, `lph`, `cat` for `cat/deep`, `cat/*` and ` alpha ` selected nothing.
- `exclude` left an item out by its own name or by a longer end of its path in whole segments,
  each with or without `.md`. Both `deep` and `cat/deep` left out `cat/deep`, and both
  `shared/alpha` and `team/shared/alpha` left out `team/shared/alpha.md` of the entry
  `team/shared`. It compared case-sensitively, and `experimental-.*`, `experimental-*` and
  `xperimental` left `experimental-x` in.

The CLI looks an `include_only` value up on disk. Because the workspaces of the test were on a
macOS volume, which ignores case, the CLI also selected `alpha.md` for `ALPHA`. The steps compare
names case-sensitively, as a Linux volume of a collection does. They do not read a value as a
regular expression: With Python's backtracking `re`, a value such as `^(a+)+$` over a directory
name of a few dozen `a` takes time exponential in the length of the name, and a collector worker
has no time limit.

On 2026-10-19 the same CLI was run offline in the development image over workspaces that were
git repositories, to test where paths resolve, what an entry that names an item selects and how
the other lists apply. `antigravity plugin validate --json` counted a plugin's skills and rules.
The steps read the registrations as the CLI did:

- A plugin's registration file resolved `extra` to the plugin's `extra/`, and to the workspace's
  `extra/` only where the plugin has none. A nested `sub/.agents/agents.json`, which a session
  started in `sub/` reads and a session at the root does not, resolved `shared` to `sub/shared`
  and `sub/other` to the workspace's `sub/other`. An inherited file and its entries resolved
  against the directory of the file that starts the chain: `team.json` was the root's and not
  `.agents/team.json`, and under `sub/` it was `sub/team.json` where that file exists. An entry
  without `.md` loaded the Markdown file of that name in that directory, and a directory of that
  name took precedence over the file. A plugin's `extra` loaded no `extra.md` of the workspace.
- An entry that names an item loaded it: `shared/beta.md` and `shared/beta`, a plugin's
  directory or its `plugin.json`, and a skill's folder or its `SKILL.md`. A folder with an
  `agent.md` gave its Markdown files, including `agent.md`, but no folder below it with an
  `agent.md`, whether an entry named the folder or scanned it. A skill's folder gave no skill
  below it. The `include_only` and `exclude` of such an entry compared as `exclude` does.
  `e1`, `shared/e2.md` and `e6/` selected their files, and `x/e3`, `shared`, `E5` and `e` did
  not.
- The `include_only` and `exclude` of an `inherits` object applied to every item that the
  inherited chain selects, through every further link, and compared as `exclude` does. They did
  not apply to the entries of the file that contains the object. The object's
  `include_only: ["cat/deep"]` selected nothing that the entries did not select. A file
  inherited twice was read only the first time, with the lists of the first object. An entry's
  own `include_only` still named a path below the entry's directory, and `shared/beta` selected
  nothing.
- A file's top-level `exclude`, also in an inherited file, compared as `exclude` does. It left
  items out of every entry and out of the default places of every customization root of the
  session, such as `.agents/agents/defaultone.md` and `.agent/agents/d1.md` for an
  `.agents/agents.json`. `.agents/plugins.json` left out a plugin of `.agents/plugins/`. A
  plugin's own registration file left out its default agents, skills and rules. The exclusions
  of the workspace did not apply to a plugin, and the exclusions of a plugin did not apply to
  the workspace. A top-level `include_only` changed nothing. In a session started in `sub/`, the
  root's file left out the items of `sub/.agents/agents/`, and a file in `sub/` left out the
  items of the root's `.agents/agents/`. A session at the root loads the root's
  `.agents/agents/`, and the steps apply a top-level `exclude` to the directory of the file that
  contains it and the directories below (`paths.antigravity_left_out`). They do not apply it to
  `.agents/skills`, which other tools read as well.

A second test on 2026-10-19, with the same commands, showed how the CLI reads the root of a
workspace and the files that a chain inherits. The steps read the registrations as the CLI did:

- `.` and `./` in an `.agents/agents.json` at the workspace root resolved to the root. The CLI
  listed `x.md` and not `sub/y.md`, and it listed both when an `agent.md` was beside `x.md`. A
  plugin at the workspace root with `.` or `SKILL.md` in its `skills.json` had one skill, the
  root skill, and not `more/a`. With an empty `entries` list, the plugin had no skill. With `.`
  and no root `SKILL.md`, it had the two skill folders directly in the root. A plugin under
  `.agents/plugins/p` counted the same.
- The steps read such a skill, in a folder that contains the registration file that selects it,
  as a root skill, as they read a plugin's root skill. They take its `SKILL.md` with
  `scripts/`, `references/` and `assets/` (`paths.registered_root_skills`). The folder can be
  the repository or a package of it. A copy of the whole folder would put its source code and
  its `.env` into the collection, and the file history step would run a `git log` for each of
  its files.
- The CLI read an inherited file by its extension. It accepted comments and trailing commas in
  a `.json` file. A name such as `Entries` or `ENTRIES` matched its key, and of two equal names,
  the later took effect. A name that is no key, such as `name` in a package.json, was ignored. A
  file that did not parse, a list, a file that began with a byte order mark, and a file with a
  value of another type for a key, such as a number for a `path` or a string for `inherits`,
  gave nothing. The CLI logged "Failed to load inherited paths" and read the next inherited
  file. A `.txt` file gave one path entry per trimmed line, without blank lines and lines that
  start with `#`, and the lists of its `inherits` object applied. A file of any other type,
  such as `README.md`, `team.yaml`, `team.jsonc` or `team.JSON`, and a directory were an
  "unsupported file type". The CLI read the file that starts a chain the same way, and such a
  file with a value of another type gave nothing too (`paths._registration_document`).
- A package.json without `entries`, `inherits` or `exclude` changed nothing. The CLI applied
  the `entries` list or the top-level `exclude` of a package.json that has one. An inherited
  file is therefore a configuration artifact of the registration's mechanism where it sets one
  of these keys. The cloning step, the collected file cleanup and the detection step skip any
  other inherited file.

A third test on 2026-10-19, with the same commands, showed which Markdown files the CLI reads
as rules and agents, and how it reads a workspace through symbolic links. The steps read the
registrations and the plugins as the CLI did:

- `.` in an `.agents/agents.json` at the workspace root listed `x.md` and not `README.md` or
  `CONTRIBUTING.md`, which have no front matter. An entry `docs` listed `docs/x.md` and not
  `docs/README.md` or a `docs/guide.md` with a `title` alone. An entry that names a folder with
  an `agent.md` listed its `agent.md` and an `extra.md` with a name and a description, and not
  a `notes.md` without front matter. `antigravity plugin validate` counted only such agents,
  both among the items that a plugin's `agents.json` registers and in a plugin's `agents/`. In
  `agents/`, it read the Markdown files directly in the folder and each `<name>/agent.md`, as in
  `.agents/agents`. It counted a `README.md` with a name and a description and an agent named
  `_x.md`, and it reported "the frontmatter has no name or description. The loader skips this
  agent" for an agent without them.
- `antigravity plugin validate` counted only the rules with a trigger that the rules page lists,
  both among the items that a plugin's `rules.json` registers and in a plugin's `rules/`. In
  `rules/`, it read the Markdown files directly in the folder, including a `README.md` with a
  trigger. It reported "no valid trigger in the frontmatter, so the rule never applies" for a
  rule without front matter and for a rule with `trigger: alwaysOn`. Without a login, the CLI
  lists no rule of the workspace. The steps apply the same test to the rules that an
  `.agents/rules.json` registers, as the rules page requires of every rule (`paths._ITEM_TESTS`).
- Where `.agents` was a symlink to `config/agents`, the CLI listed through the link the agents
  of `.agents/agents/` and the agents that `config/agents/agents.json` registers. It resolved the
  entry `shared` of that file against the directory that contains the link. It listed
  `shared/a.md` and not `config/shared/b.md`, and in a session started in `pkg/` with
  `pkg/.agents -> config`, it listed `pkg/shared/a.md`. An `.agents/agents.json` that links to
  `config/team/agents.json` resolved its entries and its inherited files against the workspace
  root in the same way. The steps follow such a linked customization root for its registration
  files and report those files at their paths in the repository (`paths._registration_sources`).
  The cloning step stores the link as a marker, which the collected file cleanup and the
  detection step follow (`paths._linked_directory`). The steps read the root's default places,
  such as `.agents/agents/`, through the link too, as they read every tool directory that is a
  link (`docs/steps/repository_cloning.md`).
- The CLI listed an agent that is a symlink to an agent elsewhere in the workspace, in
  `.agents/agents/`, in a plugin's `agents/` and where an entry selects it. It also listed the
  agents of an `.agents/agents` that links to another directory and of a registered directory
  `shared` that links to `real`. The steps read a registered agent's front matter through its
  link, and the cloning step stores the agent with that content (`paths.store_declaring_files`).
  The steps follow an `.agents/agents` or `.agents/rules` that links to a directory of the
  repository as they follow any linked mechanism directory. They follow a rule or an agent of a
  default place that is a link as the next test shows, and they follow no link in an entry's
  path.

A fourth test on 2026-10-19, with the same commands, showed how the CLI reads a rule or an
agent that is a link to a file, and a chain of links to a customization root. The steps read
them as the CLI did:

- `antigravity plugin validate` counted a plugin's rule that links to the workspace's
  `AGENTS.md`, which sets `trigger: always_on`, beside the plugin's own rule. It reported "no
  valid trigger in the frontmatter, so the rule never applies" for a rule that links to a file
  without front matter. It counted an agent of a plugin that links to `shared/x.md`.
  `antigravity agents` listed an agent of `.agents/agents` that links to `shared/x.md`, and not
  an agent that links to a file without front matter. Because the rules page requires the
  trigger of every file in `rules/`, the steps read a linked rule of a default place the same
  way, such as angular/angular's `.agent/rules/agents.md`, a link to `../../AGENTS.md`.
- When a link lies directly in a folder of rules, agents or other items that are files, in a
  plugin's folders as well, the cloning step copies the file that the link leads to
  (`paths.link_parents`). It copies no such file in `.git` (`paths.in_git_directory`). The
  collected file cleanup keeps that file where a row of the folder matches the link as a skill,
  an agent, a command or a rule. The detection step tests the file's front matter through the
  link's marker, reports the item at the link's path (`detection._file_text`) and reads its
  `language` in that file (`detection.measured_artifact`). A context file that is a link stays a
  reference to its target, measured at its own path. The content test of an Antigravity
  `hooks.json` also reads a linked file through its marker.
- For `.agents -> lnk` and `lnk -> config/agents`, the CLI listed the agent in `shared` that
  `config/agents/agents.json` registers. The cloning step stores such a linked customization root
  as a marker that names the directory that the chain leads to, relative to the directory that
  contains the link, because the collected tree contains no `lnk` to follow
  (`paths.collect_declared_rules`).

Without a login, the CLI lists no skills, rules or workflows of the workspace. The steps read the
registration files of those mechanisms the same way, based on the guide, which gives one schema
for all of them. The cloning step and the collected file cleanup keep the inherited files that
the CLI reads, so that the detection step can follow the same chain. A rules row matches the
registration file, and `paths.declared_rule_files` reads for every step what the file registers,
as `paths.declared_context_files` reads a declared context file. No repository in the October 5–6
collection has a `.agents/rules.json`.

**OpenCode.** OpenCode reads `opencode.json` and `opencode.jsonc` in every directory from the
directory that it starts in up to the worktree root, and an `.opencode` directory in each of them
(`ConfigPaths` in [`config/paths.ts`](https://github.com/anomalyco/opencode/blob/e3c4eb242585dcd77be1a867391b7c25fd47ce52/packages/opencode/src/config/paths.ts) of OpenCode 1.19.3, of 2026-10-16). The
rows match the two configuration files, and `tui.json` and `tui.jsonc`, the settings of its
terminal interface, only at the repository root. In the file lists of the October 5–6
collection, the `opencode.json` and `opencode.jsonc` files below the root are translation files,
examples, documentation, test fixtures and templates, such as the translation files under
xpf0000/FlyEnv's `src/lang/`, and a configuration under `.github/` that a workflow has to name. The one
`tui.json` below the root is not OpenCode's. `.opencode` counts at any depth, as `.cursor` does.

In `.opencode`, the agents of `agents/`, the commands of `commands/` and the skills of `skills/`
count under those mechanisms at any depth below the folder, as OpenCode reads them. The folders with
the singular names, which OpenCode reads too, count the same way. OpenCode 2 also reads a Markdown
file directly in `skills/` or `skill/` as a skill: "Markdown files must be at the source root, such
as skills/review.md." and "Each source can contain either form"
([skills](https://opencode.ai/docs/skills/), read 2026-11-06). The rows with the match mode
`dir_ext_described:.md` count such a file, named after the file, where its front matter has a
description, because "skills without one are not advertised". A `README.md` or a list of credits in
the folder therefore stays an `other` row. OpenCode's scan keeps the Markdown files of the top level
only and none where the folder has a `SKILL.md` directly in it, and it leaves out a hidden file
([`skill-scan.ts`](https://github.com/anomalyco/opencode/blob/649f0d542671e79cdd748ff695605e5ccf050d6c/packages/core/src/config/plugin/skill-scan.ts)).
The rows name OpenCode's own two folders. OpenCode 2 reads the same form in `.claude/skills` and
`.agents/skills`, where a tool that reads another tool's skills directory is credited only with a
marker named `SKILL.md`. The maintainer decided on 2026-11-07 that these files stay uncounted for
OpenCode ("this makes sense"), because counting them would set the `opencode` flag on the
repositories with a Markdown file in a skills directory of Claude Code or of several tools, most
of which show no other sign of OpenCode, such as a `README.md` of `.claude/skills`. The configuration
defines commands as well, under `commands` ("Define commands under commands in any OpenCode JSON or
JSONC configuration file.", [commands](https://opencode.ai/docs/commands/)) and under `command`, the
key of OpenCode 1 ("Rename the singular command map to commands" and "The V1 config format remains
supported.", [migration](https://opencode.ai/docs/migrate-v1/)). The rows `json_servers:commands` and
`json_servers:command` of the four configuration files count a file once, named after the file stem,
where one of the two is an object with an entry. A mode of `modes/` is an agent
that a person selects as the primary agent. It counts as a subagent, as Copilot's chat modes do. A
plugin of `plugins/` and a custom tool of `tools/` are TypeScript or JavaScript modules directly in
the folder, and OpenCode loads them at startup. Both count as plugins, because the file is the
plugin itself rather than a manifest that names a plugin, and the plugins page describes adding a
tool through a plugin in the same way. OpenCode 2, published as `@opencode/cli`, also loads each
folder directly in `plugins/` as a plugin package: "OpenCode also loads direct .ts and .js files and
immediate plugin package directories from every discovered .opencode/plugins/ directory."
([plugins](https://opencode.ai/docs/plugins/)). It reads `plugin/` as well
([migration](https://opencode.ai/docs/migrate-v1/)). A folder is a plugin when it has an entry
module, a file directly in the folder that `Host.resolve` of release 2.1.1 from 2026-10-20 looks for
([`host.ts`](https://github.com/anomalyco/opencode/blob/ba546ec1598a70db2570e9c6258028d955ba8f58/packages/plugin/src/host.ts)):
`server` and then `index` for the server, and `tui` for the terminal interface. The server leaves
out a folder without a server entry
([`source.ts`](https://github.com/anomalyco/opencode/blob/ba546ec1598a70db2570e9c6258028d955ba8f58/packages/core/src/config/plugin/source.ts)),
and the terminal interface lists only the folders of `plugins/`
([`discovery.ts`](https://github.com/anomalyco/opencode/blob/ba546ec1598a70db2570e9c6258028d955ba8f58/packages/tui/src/plugin/discovery.ts)).
A folder in `plugin/` with only a `tui` entry is therefore no plugin. The two rows with the match
mode `package_entry:` match a folder by an entry module with any of the eight extensions that both
runtimes of the command line, Bun and Node, try for a path without an extension. The steps try the
extensions in the order of Bun, which runs the published binary: `.tsx`, `.jsx`, `.mts`, `.ts`,
`.mjs`, `.js`, `.cts` and `.cjs`. A folder is one plugin, counted at its first entry module, and its
other files belong to that plugin. In the collection of the run that started on 2026-10-23,
aoda-zhang/PawHaven-FullStack-React-NodeJS keeps four such folders in `.opencode/plugins/`, each
with an `index.ts`, and detection counts each of them as one plugin.

The themes of `themes/` count as settings, as `tui.json` does. The configuration's MCP servers
count where its `mcp` is an object with an entry. OpenCode 1 names a server directly under the key,
and OpenCode 2 under `mcp.servers` ("To configure a server by hand, give it a unique name under
mcp.servers. V2 does not place server names directly under mcp.",
[MCP servers](https://opencode.ai/docs/mcp-servers/)), and it reads both forms ("OpenCode recognizes
mixed V1 and V2 members within mcp", [migration](https://opencode.ai/docs/migrate-v1/)). The rows
test the entry and not its name, so that they would also count an `mcp` that holds only a setting,
such as a timeout, or an empty `servers`. Its LSP servers count where its `lsp` is `true`
or an object, because both values enable the built-in servers, and not where it is `false`.
OpenCode writes a `.gitignore`, a `package.json` and lock files into `.opencode` for the
dependencies of its plugins, and these files are no configuration (`not_configuration.csv`).
The plans of `.opencode/plans/` are planning documents ([Planning documents](#planning-documents)).

OpenCode reads `AGENTS.md`, `CLAUDE.md` where a directory has no `AGENTS.md`, and the skills of
`.claude/skills` and `.agents/skills`, which their own rows attribute. A file that only the
`instructions` of its configuration name is OpenCode's context file ([A context file that configuration names](#a-context-file-that-configuration-names)). OpenCode's code also falls back to a `CONTEXT.md`, which the code marks as
deprecated and the rules page does not name. No row matches that file, because
<!--s:collection.root_context_md_repositories-->123<!--/s--> repositories of the development dataset have a `CONTEXT.md` at the root, a name
that projects use for their own documentation.

#### The YAML parsers of the tools

Antigravity CLI 1.4.10 decodes a rule's front matter with go-yaml v3, which it bundles as
`google3/third_party/golang/yamlv3`, and Cursor CLI 2026.09.28-64d2043 parses a skill's front
matter with js-yaml 3.14.1 through gray-matter 4.0.3. The steps parse both with the safe loader
of PyYAML 6.0.3, which `locks/pipeline.txt` pins (`paths.load_yaml`). The three parsers were run
on 2026-11-07 on the same front matter, go-yaml v3 as gopkg.in/yaml.v3 v3.0.1 and
go.yaml.in/yaml/v3 v3.0.4. They agree on the front matter of every rule that the development
dataset credits to Antigravity and of every skill that it credits to Cursor. Each parser refuses
an unquoted `*` and `**/*.html`, which YAML reads as aliases, an unquoted `: ` inside a value and
`name: [invalid`, and each reads the same values in quotes. go-yaml v3 and js-yaml 3 refuse a
mapping with a key twice, which PyYAML's safe loader keeps, and `paths.load_yaml` refuses it as
well. `paths.load_yaml` reads a timestamp that is no date, such as `2025-02-30`, as text, because
PyYAML's constructor raises an error for it. The parsers differ on forms that front matter seldom
holds:

- PyYAML refuses a tab after a key's colon, which go-yaml v3 and js-yaml 3 read.
- PyYAML and go-yaml v3 refuse an indentation with a tab, an anchor name with a dot and a long
  implicit key, which js-yaml 3 reads.
- PyYAML and js-yaml 3 refuse an unknown tag, such as `!foo`, which go-yaml v3 reads.

Where the tool's own parser reads such a form, the steps still count the rule or the skill as
one that does not parse.

`paths.load_yaml` and the tools also test a repeated key differently. go-yaml v3 compares the kind
and the text of two keys, `ni.Kind == nj.Kind && ni.Value == nj.Value` (`mapping()` in `decode.go`
of gopkg.in/yaml.v3 v3.0.1), and js-yaml 3 compares the keys as strings,
`keyNode = String(keyNode)` (`storeMappingPair` in `lib/js-yaml/loader.js` of js-yaml 3.14.1),
while `paths.load_yaml` compares the constructed keys. Both parsers therefore refuse `1: a` with
`"1": b`, for which `paths.load_yaml` returns `(True, {1: 'a', '1': 'b'})`, and the steps would
count a rule or a skill with these keys that its tool refuses. In the other direction, go-yaml v3
reads `true: a` with `True: b`, which js-yaml 3 and `paths.load_yaml` refuse, and the steps would
leave out a rule with these keys that Antigravity loads. Antigravity decodes a rule into a struct
of the strings `trigger`, `description`, `globs` and `glob`, and go-yaml v3 decodes the value of a
key only where the struct has a field for the key (`mappingStruct` in `decode.go`), so it does not
test the keys of a mapping under any other key. A rule with a `metadata:` that holds `owner` twice
therefore loads in Antigravity, but `paths.has_trigger` returns False for it. None of these forms
occurs in the development dataset. No front matter of a Markdown file in its validated tree, read
as it is, as Antigravity quotes it and as gray-matter splits it off, has two keys that one of the
three comparisons finds equal and another does not, or a key twice in a mapping below the top
level (scan of 2026-11-07).

### AI contribution detection

**Purpose.** The commit CSV lists the commits to which one of the six tools contributed, as
evidence that a repository uses the tool, with the tool that each commit credits.

**Attribution criterion.** A commit counts as a contribution of a tool when a product of that tool
made the commit or produced the change that it records: The product wrote the change itself, or
applied it once a person confirmed. A commit that records no change counts as well, such as the
empty "Initial plan" commit with which Copilot's cloud agent opens a pull request, because it shows
just as well that the repository uses the tool. A reviewer's suggested change that a person commits
with one click counts, because the change is applied as the reviewer wrote it: "To apply the change
in its own commit, click **Commit suggestion**." GitHub credits the reviewer as co-author of that
commit: "Each person who suggested a change included in the commit will be a co-author of the
commit."
([GitHub](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/incorporating-feedback-in-your-pull-request))
A change that a person copies into the repository by hand from a review comment is the person's.
When a reviewer delegates a fix to an agent, the commit is the agent's. A credit that a message only
quotes from another commit does not count ([Detection scopes](commits.md#detection-scopes)).

| Product | Its changes count | From its page |
|---|---|---|
| Claude Code in the terminal, the IDE, the desktop app and the web | yes | "It stages changes, writes commit messages, creates branches, and opens pull requests." ([overview](https://code.claude.com/docs/en/overview)) |
| Claude Code GitHub Actions, as the `claude` app | yes | "Mention `@claude` in a pull request or issue comment to have Claude analyze code, implement changes, and push commits." ([GitHub Actions](https://code.claude.com/docs/en/github-actions)) |
| GitHub Copilot agent mode | yes | It works "through an agentic loop, including planning, applying changes, testing, and iterating." ([GitHub blog](https://github.blog/ai-and-ml/github-copilot/agent-mode-101-all-about-github-copilots-powerful-mode/)) |
| GitHub Copilot cloud agent, as the `copilot-swe-agent` app | yes | "Copilot can research a repository, create an implementation plan, and make code changes on a branch." ([GitHub](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent)) |
| GitHub Copilot CLI, as the `github-copilot-cli` app | yes | "When you use Copilot CLI, Copilot can perform tasks on your behalf, such as executing or modifying files, or running shell commands." ([GitHub](https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli)) |
| GitHub Copilot code review, as the `copilot-pull-request-reviewer` app | yes, once a person applies a suggestion | "Where possible, Copilot's feedback includes suggested changes which you can apply with a couple of clicks." ([GitHub](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review)) |
| Copilot Autofix for code scanning, as the `github-advanced-security` and `github-code-quality` apps | yes, once a person accepts a suggestion | "Copilot Autofix presents all suggestions as proposed code changes that require explicit developer review and acceptance before being applied." ([GitHub](https://docs.github.com/en/code-security/code-scanning/managing-code-scanning-alerts/responsible-use-autofix-code-scanning)) |
| Agentic autofix for code scanning, in public preview since 2026-07-10 | yes | It "explores relevant files, proposes a fix, and reruns the original analysis to confirm the fix closes the alert before opening a pull request for your review." ([GitHub changelog](https://github.blog/changelog/2026-07-10-agentic-autofix-for-code-scanning-alerts-in-public-preview/)) |
| Gemini CLI | yes | Its key cases include "Query and edit large codebases". ([README](https://github.com/google-gemini/gemini-cli)) |
| Gemini Code Assist agent mode in the IDE | yes | "Agent mode has access to your built-in tools like file search, file read, file write, terminal commands, and more." ([Google Cloud](https://docs.cloud.google.com/gemini/docs/codeassist/use-agentic-chat-pair-programmer)) |
| Gemini Code Assist on GitHub, as the `gemini-code-assist` app | yes, once a person commits a suggestion | Its review comments contain a "Code suggestion that can be committed directly from GitHub". ([Google Cloud](https://docs.cloud.google.com/gemini/docs/code-review/use-code-assist-github)) |
| Codex CLI | yes | "Inspect code, make changes, run commands, and automate repeatable work without leaving your terminal." ([Codex](https://learn.chatgpt.com/docs/cli)) |
| Codex cloud tasks | yes | "Give Codex coding tasks to work through in the cloud, from investigating bugs to building features." "Review the changes and test results, request follow-ups, and commit or open a pull request when ready." ([Codex](https://learn.chatgpt.com/docs/cloud)) |
| Codex in GitHub, reviewing | no, but a suggestion that a person commits counts as for any reviewer | "Codex posts a review on the pull request, just like a teammate would." ([Codex](https://learn.chatgpt.com/docs/third-party/github)) |
| Codex in GitHub, fixing on request | yes | "Codex starts a legacy cloud chat with the pull request as context and can push a fix back to the branch when it has permission to do so." ([Codex](https://learn.chatgpt.com/docs/third-party/github)) |
| Cursor Agent, in the editor, the CLI and the cloud | yes | "Agent is Cursor's assistant that can complete complex coding tasks independently, run terminal commands, and edit code." ([Cursor](https://cursor.com/docs/agent/overview)) |
| Cursor Bugbot, reviewing | no, an agent makes its fixes, but a suggestion that a person commits counts as for any reviewer | "Bugbot analyzes PR diffs and leaves comments with explanations and fix suggestions." "Fix in Cursor links open issues directly in Cursor" ([Cursor](https://cursor.com/docs/bugbot)) |
| Cursor Bugbot Autofix, through a Cloud Agent | yes | "Bugbot Autofix automatically spawns a Cloud Agent to fix bugs found during PR reviews." ([Cursor](https://cursor.com/docs/bugbot)) |
| OpenCode in the terminal, the desktop app and the IDE | yes | "OpenCode is an open source AI coding agent. It's available as a terminal-based interface, desktop app, or IDE extension." ([OpenCode 1](https://docs.opencode.ai/docs/)) |
| OpenCode's GitHub integration, as the `opencode-agent` app | yes | "OpenCode will implement the requested change and commit it to the same PR." ([OpenCode](https://opencode.ai/docs/github/)) |

The app names come from GitHub's own account records, read through `GET /user/{id}` for the
numeric identities that the commit trailers contain: 175728472 is `copilot-pull-request-reviewer`, 223556219
`github-copilot-cli`, 198982749 `copilot-swe-agent`, 62310815 `github-advanced-security`, 223894421
`github-code-quality`, 176961590 `gemini-code-assist`, 209825114 `claude` and 206951365 `cursor`.

A reading of every commit of the commit CSV of release 1.0.0 that only an author or committer
identity credited labeled it by the identity that GitHub's API returned for it: `vendor_account`,
`tool_identity`, `human`, `other_automation`, `undecidable` or `unreachable`. A `tool_identity` is
an identity that runs the tool without being the vendor's account, such as a project's own GitHub
App that runs the Claude Code action. The commits labeled `vendor_account` or `tool_identity` count
as the tool's. The tracked files keep what names no person. `output/identity-labels.csv` has the
commits with every label except `human` and `undecidable`, and a login of a person is left out of
it. `output/identity-readings.csv` gives each distinct identity with its label, its counts and the
`reading` that supports the label, and it leaves the identity empty for a person and for the
undecidable identity. `output/identity-pilot.csv` gives a seeded sample of <!--s:collection.identity_pilot.commits-->30<!--/s--> of those
commits with the identity heuristics that each commit matches, and it leaves a committer who is a
person empty. The private repository `se-uhd/agenttorrent-pipeline-internal` holds the full files in its
history.

**Attributions.** Because the lines that credit a tool mean different things, the commit CSV
records in `attributions` how a commit credits each of its tools, as `<tool>:<attribution>`. The
`attribution` column of the two heuristics files gives the attribution of each heuristic:

| Attribution | What credits the tool | Example | Grounding |
|---|---|---|---|
| `account` | The tool's own account authored or committed the commit, or the identity that the tool's code or environment writes, name and address together, or an agent account or a GitHub App that its own page or its project's own configuration grounds as running the tool, such as a workflow that runs the tool's action with the app's token or an organization's AGENTS.md that names the app as the tool's identity. An address that GitHub links to a vendor's account is `account` only when the tool's code or environment writes it. Only an author heuristic reads it | `copilot-swe-agent`, the `claude` and `cursor` apps, `Gemini CLI <gemini-cli@google.com>`, `codex@openai.com`, Cloudflare's `ask-bonk[bot]` for OpenCode, Lodekeeper of ChainSafe/lodestar for OpenClaw, `claude-code-aisi[bot]` of UKGovernmentBEIS/inspect_evals, `elsa-workflows-codex[bot]` | GitHub's account records, the tools' code, the agent's own page, such as the README of Cloudflare-Studio/ask-bonk, the project's workflows and AGENTS.md, and `output/identity-labels.csv` |
| `reviewer` | The co-author trailer that GitHub writes when a person commits a review app's suggestion, and the footer that GitHub writes into a pull request that a person creates from a suggestion of Copilot Autofix | Copilot code review, Copilot Autofix, Gemini Code Assist on GitHub | "Each person who suggested a change included in the commit will be a co-author of the commit." ([GitHub](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/incorporating-feedback-in-your-pull-request)) |
| `standard` | The line that the tool writes itself, with its name and its own address or account, or a marker that it writes into a commit or a pull request | Claude Code: "unset, so Claude Code adds `Co-Authored-By: <name> <noreply@anthropic.com>`"; Codex: "Commit messages must end with `Co-authored-by: Codex <noreply@openai.com>`"; VS Code: `Co-authored-by: Copilot <copilot@github.com>` | [settings reference](https://code.claude.com/docs/en/settings-reference); the vendors' code, `codex-rs/ext/git-attribution` of Codex, `extensions/git/src/repository.ts` of VS Code, `src/github/constants.ts` of Claude Code's GitHub Action and `packages/opencode/src/tool/bash.txt` and `packages/opencode/src/cli/cmd/github.handler.ts` of OpenCode; GitHub's account records |
| `convention` | Any other line that names the tool, which a person writes, or which an agent writes because a repository's instructions require it, and any other name or address that names the tool and that users chose for an author or a committer | `Assisted-by: Claude`, `Co-authored-by: Claude <claude@anthropic.com>`, `Edgar (OpenClaw) <edgar@openclaw.com>` as the author | The heuristics' notes on the sample heuristics, such as "A hand-written convention" |

Heuristics with different attributions can match one line. The heuristic of Copilot's name, the
heuristic of the Copilot apps' address and the heuristic of the code review app all match
`Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>`. Of the heuristics that match
a line, the line takes the attribution that comes first in the order `reviewer`, `standard`,
`convention`, and this line is therefore the reviewer's. `attributions` records how the line credits
the commit, and `matched_heuristics` lists every heuristic that matched it by its `id`. A line does
not prove who wrote it. A person can type the trailer that Claude Code writes, and the line then
counts as `standard`. The attribution is called standard rather than default because VS Code writes
its line only when `git.addAICoAuthor` is turned on, and the setting is off by default.

A line that names the tool counts as the tool's own only with the tool's own address. Before
2026-10-14, a co-author line that named the tool counted as the tool's own whatever its address.
The addresses beside a tool's name in the October 5–6 collection show what else writes such a
line:

- A changed setting replaces the line. Claude Code's `attribution.commit` replaces its trailer,
  as with `Claude Sonnet 5 <first156.last+claude-code@example.org>` in renovatebot/renovate, or with a
  company's address such as `svc-devxp-claude@slack-corp.com`.
- A vendor's address appears beside another tool's line. `claude@anthropic.com` is in none of
  Anthropic's code, and GitHub's API links it to Anthropic's account `claude`. The address also
  appears under a model's name beside another tool's line, as `Co-authored-by: Anthropic Claude
  Opus 4.6 <claude@anthropic.com>` beside `Co-authored-by: Cursor AI <ai@cursor.sh>` in
  pypa/setuptools-scm.
- A repository's instructions prescribe the line. openlineage's AGENTS.md prescribes
  `Co-Authored-By: Cursor <noreply@cursor.com>`.
- GitHub's squash merge turns the git identity of an agent's commits into a co-author line, such
  as `Codex <codex@localhost>`.

A line at the address of the tool's own GitHub account is the tool's own, whatever name stands
before it, as with the noreply addresses of the accounts of Copilot, Cursor and Gemini, of
OpenCode's GitHub agent, `opencode-agent[bot]`, and of OpenAI's app `chatgpt-codex-connector[bot]`,
as in `Co-authored-by: Codex <199175422+chatgpt-codex-connector[bot]@users.noreply.github.com>`.
`codex@openai.com` is the identity that the environment of Codex cloud writes for its commits,
as a user reports its defaults in openai/codex issue 1281 (`codex-author-05`), and GitHub's API
links the address to OpenAI's account `codex`. A line such as
`Co-authored-by: GPT 5.6 <codex@openai.com>` therefore names the tool at its own address and
counts as `standard` ([The branch merges and the apps of 2026-10-21](commits.md#the-branch-merges-and-the-apps-of-2026-10-21)), and so does a line
at the account's GitHub noreply address. GitHub's link alone does not make an address the
tool's: GitHub also links `claude@anthropic.com` to Anthropic's account `claude`, which no code
or environment of Anthropic writes, and a line at that address is a convention. GitHub also links
`noreply@openai.com`, the address of the trailer that Codex's code writes, to OpenAI's account
`codex`. A line that names Codex at that address counts as `standard` (decided on 2026-10-29) in
every `Assisted-by`, `Generated-by` and `Co-authored-by` form that the Codex heuristics read,
such as `Assisted-by: OpenAI Codex <noreply@openai.com>` and
`Co-Authored-By: GPT-6.1-Sol via Codex <noreply@openai.com>`. A line that names only a model at
that address names no tool, because other agents write OpenAI's address under a model's name.

A `Co-developed-by`, `Signed-off-by`, `Authored-by`, `Created-by` or `Written-by` line is a
convention whatever its address, because no tool writes such a line, and so is a `Made-with` line
other than Cursor's own `Made-with: Cursor` (`cursor-trailer-04`)
([The Authored-by, Created-by, Written-by and Made-with lines, read 2026-11-03](commits.md#the-authored-by-created-by-written-by-and-made-with-lines-read-2026-11-03)).
Copilot CLI's `Copilot-Session:` trailer and the claude.ai session link that Claude Code puts
into a commit or a pull request description are standard, and so are the
link to its task that a Codex cloud task writes into the description of its pull request and the
description and the buttons that a Cursor cloud agent writes into its own pull request. The
`claude`, `chatgpt-codex-connector`, `cursor`, `devin-ai-integration` and `coderabbitai` apps all
review pull requests and make changes. A line that names one of these apps counts as `standard`,
because the line alone does not show whether it records a review or a change. A suggestion of
Devin's review that a person committed, for example, has the same trailer as Devin's own commit. The
`claude` app likewise puts suggestions into its review comments and commits the changes that a
mention of `@claude` requests, and CodeRabbit's app commits the changes that its chat makes and
the docstrings that it generates. A suggestion that a person commits through GitHub's web
interface shows the review in the subject that GitHub gives the commit: `Update` and the path of the
file, `Apply suggestion from @<app>` or `Apply suggestions from code review`. In such a commit, the
line of the `claude` app, CodeRabbit or Devin counts as `reviewer`, and so does their line below
such a subject in the list of commits of a squash merge. A co-author line that a squash merge
repeats below its line of nine hyphens credits nothing of its own, and neither does a line that a
squash merge in the list repeats below its own separator. When the list also contains a commit that
the app authored, such as CodeRabbit's docstrings, which carries no co-author line of the app, the
one line of the app below the separator stands for that commit too, and the squash merge credits the
app only as `reviewer`.
[The detection accuracy evaluation of 2026-10-26, read 2026-10-29](commits.md#the-detection-accuracy-evaluation-of-2026-10-26-read-2026-10-29)
gives the number of such squash merges. The commit logs of the run that started on 2026-10-23
contain no committed suggestion of the Codex and Cursor apps in this form.

The reclassification of 2026-10-14 under these attributions changed the attributions of
commits in the commit CSV and the tools of none. A Claude line at another address may name the
model that another tool ran. In <!--s:collection.claude_convention_beside_another_tool.commits-->229<!--/s--> commits of the commit CSV, in <!--s:collection.claude_convention_beside_another_tool.repositories-->106<!--/s--> repositories, Claude's only attribution is a
convention beside another tool's account, standard line or review. An analysis can leave these
commits out by their attributions.

**Commits that credit a tool only as a reviewer.** In the commit CSV,
<!--s:collection.reviewer_only.copilot.commits-->39,047<!--/s--> of the <!--s:collection.commits.copilot-->184,386<!--/s--> commits that credit Copilot, in <!--s:collection.reviewer_only.copilot.repositories-->3,494<!--/s--> repositories, credit it only as a
reviewer. Of these, <!--s:collection.reviewer_only.copilot_code_review.commits-->36,101<!--/s--> in <!--s:collection.reviewer_only.copilot_code_review.repositories-->3,107<!--/s--> repositories credit it through Copilot code review and <!--s:collection.reviewer_only.copilot_autofix.commits-->3,066<!--/s--> in <!--s:collection.reviewer_only.copilot_autofix.repositories-->855<!--/s--> through
Copilot Autofix, some through both. Of the <!--s:collection.commits.gemini-->6,360<!--/s--> commits that credit Gemini, <!--s:collection.reviewer_only.gemini_code_assist.commits-->5,261<!--/s--> in <!--s:collection.reviewer_only.gemini_code_assist.repositories-->273<!--/s-->
repositories credit it only through Gemini Code Assist on GitHub. These reviewer commits count,
and `attributions` identifies them. Narrowing the identity heuristics against the commit logs of
October 2026 removed the commits that the reading labeled as people's. Of the <!--s:collection.identity_labels.other_automation-->38<!--/s--> commits
that the file labels as other automation, <!--s:collection.identity_labels.other_automation_remaining-->4<!--/s--> are still in the commit CSV.

## Evidence from current handovers

The handover that [dataset.md](dataset.md) names contains the current
configuration files, the complete file lists of the collection and the recorded commit evidence,
including the full commit logs. In its working copy, `python3 -m agentcontext unmatched paths`
and `python3 -m agentcontext unmatched commits` list what the heuristics miss across the sample,
and the output CSVs show what they match. Vendor documentation establishes what a tool loads, and
links to fixed commits of repositories ground the conventions observed in them. Add positive and
negative regression tests for a changed heuristic, regenerate the outputs as
[the development procedure](development.md#continuous-improvement-from-handovers) describes, and
inspect them. The counts of earlier dataset releases do not constrain the change. A zero count is
meaningful only within the input that the collection preserved. Over a handover without the commit
logs that a complete reclassification requires, `commit-reclassification` reports `complete:
false` in `output/commit-reclassification.json`.

## The path patterns

`config_path_patterns.csv` records once which path belongs to which tool and which mechanism,
and the cloning step, the collected file cleanup and the detection step read it through
`heuristics.py`, so that no step keeps a copy of the paths that the others lack.

Each row is one location of a configuration artifact that a vendor documents:

| Column | Meaning |
|--------|---------|
| `tool` | `claude`, `gemini`, `codex`, `copilot`, `cursor`, `opencode`, or `any` for a cross-tool convention |
| `mechanism` | one of the detected mechanisms, `other` for the unclassified files, or `collect_only` for a path that is copied but classifies nothing |
| `path` | a path relative to the repository root, a bare directory or a bare filename |
| `match_mode` | how the path is matched, and for directory modes which extensions count (the module docstring of `heuristics.py` lists every mode) |
| `content_format` | how a file is read when its content is parsed: empty for strict JSON, `jsonc` for comments, and `jsonc_trailing` for comments and trailing commas. Antigravity's MCP files use `jsonc_trailing`, which the CLI 1.1.24 [changelog](https://antigravity.google/docs/changelog) documents, and so do its registration files, which CLI 1.4.2 read with comments and trailing commas ([Configuration artifact detection](#configuration-artifact-detection)), and its plugin manifests, which CLI 1.4.9 reads through `hujson.Standardize` ([The places that a plugin's manifest names](#the-places-that-a-plugins-manifest-names)). Because the format is a property of the file rather than of the row, every row that names the file has it, including a row that matches the path alone. Each value is grounded in the tool. Gemini CLI's settings file is `jsonc` because of `JSON.parse(stripJsonComments(content))` in its source, and Claude Code's is empty because its documentation calls a comment there a syntax error. `test_heuristics.py` checks the detector against the column in both directions |
| `doc_url` | the vendor documentation that defines the mechanism |
| `doc_verified` | the ISO date on which that documentation was last read and confirmed |
| `evidence` | where the page does not state the whole path, links to files at that path, separated by spaces, each at the commit that a collection read, and blank where the page states it |
| `notes` | anything that the other columns cannot state. A person's name or address in a note is fictional, in a form that keeps what the row reads, and the link in `evidence` identifies the case |

To add a mechanism, add its row first, then update the step that reads it, and run
`python -m unittest tests.test_heuristics` from the repository root. The suite fails in both
directions: when the code of a step contains a path that the heuristics do not list, and when no
step reads a heuristic. The order of the rows matters, because the detection step's
constants keep the file's order, and that order determines the order of the rows in the output
CSVs.

In `agentcontext/heuristics.py`, `context_declaration_heuristics` selects the rows that name
context files, and `MECHANISM_CSVS` maps each mechanism to its output CSV filename and path
column. In the names of the code, "heuristic" stands for a detection criterion, and "rules" only
for the configuration mechanism.

Cursor's `.cursor/worktrees.json` counts as settings. The [worktree
reference](https://cursor.com/docs/configuration/worktrees) documents commands
or a script path that run when an agent's worktree is created.

## The commit patterns

`other_ai_commit_patterns.csv` has the columns and checks of `ai_commit_patterns.csv` for the
tools outside the six. Its loader raises an error for a row that names one of the six,
because the cloning step writes the commits that this file matches to a CSV of their own,
`output/other_ai_commits.csv`, and such a row would put a commit of the six there and not in the
commit CSV. [The tools outside the six](commits.md#the-tools-outside-the-six-2026-09-25) describes what its rows match.

`ai_commit_patterns.csv` contains all <!--s:heuristics.ai_commit_patterns.rows-->431<!--/s--> commit patterns of the six tools, with the columns `id`,
`tool`, `scope` (`author`, `trailer` or `full_body`), `attribution` (see Attributions above),
`pattern`, `source`, `doc_url`, `doc_verified`, `evidence` and `notes`. The `id` is the tool and the
scope as words and a number, such as `claude-trailer-02`. Never give an `id` to a second row,
because `matched_heuristics` in the commit CSV names a heuristic by its `id`. When you remove a row,
add its `id` to `REMOVED_COMMIT_HEURISTIC_IDS` in `agentcontext/heuristics.py`. The loader raises an
error for a row with one of these ids, and `tests/test_heuristics.py` fails on an id that occurs in
the commit history of the two commit heuristics files but in neither file nor the list.

`source` separates heuristics that stay current in different ways. A `doc` heuristic matches
what the vendor documents that the tool writes. It cites the page, and `docs` reads that page
again weekly. The path patterns have the same check. A `sample` heuristic is a convention
derived from the commits themselves. No vendor page will ever change to show that such a
heuristic is wrong, and `unmatched commits` is the only check that keeps it current. Its `notes`
describes what was observed, and its `evidence` links the commits that show it, as [The page or the examples behind each heuristic](#the-page-or-the-examples-behind-each-heuristic) describes.

<!--s:heuristics.ai_commit_patterns.doc_rows:Words-->Eleven<!--/s--> of the <!--s:heuristics.ai_commit_patterns.rows-->431<!--/s--> heuristics are `doc`, because the vendors do not document the other attributions. A
search of Cursor's documentation pages found no bot account name. OpenAI's
Codex documentation export mentions neither `chatgpt-codex-connector` nor a commit trailer, and Google
documents `gemini-code-assist[bot]` as a pull request reviewer rather than as a commit author.
Cursor's git page documents the `Made with Cursor` trailer and pull request footer that its
agent adds, which <!--s:heuristics.ai_commit_patterns.made_with_cursor_rows:words-->two<!--/s--> rows match.

Earlier, the `notes` of each message heuristic recorded how many commits of the commit CSV of
release 1.0.0 it explained. These notes describe that CSV and the heuristics of that time, and the
commit history keeps them:

- Some of those message heuristics explained no commit in that CSV, among them `head:copilot/`,
  a search qualifier that no commit message contains, and `from copilot/` explained few. No row matches
  either string now, because `copilot-full-body-01` matches the message of the merge commit that
  GitHub writes instead ([The other two outside lists, read 2026-09-22](commits.md#the-other-two-outside-lists-read-2026-09-22)).
- That CSV records no author field. It therefore gives no count of the commits that the identity
  heuristics match, but it gives the commits that depend on them alone. In that CSV, many Copilot
  commits match no Copilot message heuristic and depend on the Copilot identity heuristics of that
  time, the broadest of which was the bare word `copilot`. Claude, Cursor and Gemini commits
  depend on their identity heuristics alone as well, and no Codex commit does. Some of those Claude
  commits are April labels from trailers that the Claude heuristics no longer match (see [The Claude trailers, narrowed 2026-09-25](commits.md#the-claude-trailers-narrowed-2026-09-25)).

The current commit CSV lists every heuristic that matched a commit in `matched_heuristics`, the
author heuristics included. From that column, `commit_heuristic_attribution.csv` of the detection
accuracy evaluation gives for each heuristic the commits that it matches (`commits`) and their
repositories (`repos`), the commits that no other heuristic credits to the same tool
(`sole_commits`), and the repositories that would have no commit of the tool without it
(`repos_lost_if_removed`) ([the tool](tools/evaluation.md)).

The <!--s:heuristics.ai_commit_patterns.rows_read_from_unmatched:words-->thirteen<!--/s--> rows `claude-trailer-06`, `copilot-trailer-04`, `codex-trailer-04`,
`codex-trailer-05`, `codex-author-06`, `cursor-trailer-05`, `gemini-trailer-01`,
`copilot-trailer-05`, `cursor-trailer-06`, `codex-trailer-06`, `gemini-trailer-02`,
`opencode-author-01` and `opencode-trailer-01` were derived from a different CSV,
`output/all_ai_commits-unmatched.csv` in the handover of the collection started on 2026-09-24. That CSV lists the attributions that no
heuristic matched in that collection, with their commit counts. The notes of these rows name
that CSV as their source, and `docs/commits.md` describes what each row matches and what it
does not match.

The watch reads the commit patterns with `python3 -m agentcontext docs --heuristics
heuristics/ai_commit_patterns.csv --label-columns tool,scope`, and
`.github/workflows/doc-watch.yml` runs that command every Monday, beside the commands for the
other nine heuristics files.

## A workflow that runs an agent

<!--s:heuristics.config_path_patterns.workflow_files:Words-->Six<!--/s--> rows match a workflow file by its name, which is the weaker evidence. The Gemini CLI
action ships example workflows that each run it under a name that the repository may change, and
Codex's action page names no file at all. A heuristic that reads file names counts a renamed
workflow as ordinary CI. The vendors publish the action's identifier instead, and a
workflow runs an action by naming it in a `uses:` line.

`config_path_patterns.csv` therefore has <!--s:heuristics.config_path_patterns.workflow_uses_rows:words-->six<!--/s--> rows with the match mode
`workflow_uses:<owner>/<repo>`, one per documented action: `anthropics/claude-code-action`,
`anthropics/claude-code-base-action` (a mirror of the `base-action` folder of
`anthropics/claude-code-action`),
`openai/codex-action`, `google-github-actions/run-gemini-cli` and OpenCode's
`anomalyco/opencode/github`, with its earlier address, `sst/opencode/github`, which GitHub has
forwarded since the repository moved. The rows match an action from the start of its identifier,
which lets a row name an action in a folder of a repository, such as OpenCode's `github` folder.
These rows are `settings` rows, as are the rows that match workflow files by name. They share one
path, `.github/workflows`, because the tool comes from the action that a file runs rather than from
where the file is. Together with the `workflow_engine:` and `workflow_command:` rows below, these
<!--s:heuristics.config_path_patterns.workflow_uses_rows:words-->six<!--/s--> rows are the only heuristics that attribute a configuration artifact by its own content. A
Claude workflow attributed by its path alone would set the Copilot flag and not the Claude flag,
because `tool_for_path` attributes `.github` to Copilot.

The cloning step otherwise copies `.github` only when the directory contains a Copilot path, but
it copies such a file even when nothing else in `.github` is configuration. `paths_under` also
leaves a `workflow_uses:` row out of the list of Copilot paths to keep, because the row matches a
file in that directory and not the directory itself. Counting the row there would make an
ordinary `ci.yml` look like Copilot configuration.

The cloning step sets the tool flag for the action that a `uses:` line runs, through the same
`paths.uses_action` that the detection step calls, and not for every action that the file names
anywhere. A Claude workflow that names `openai/codex-action` in a comment would otherwise set the
Codex flag, which the detection step does not confirm.

GitHub Agentic Workflows is the second way that a repository runs an agent from Actions, and the
file's own front matter identifies the agent. A GitHub Agentic Workflow is a Markdown file directly
in `.github/workflows/`, with a YAML front matter above the prompt, which `gh aw compile` turns into
the `.lock.yml` that Actions runs. `engine:` names the agent, as a string or as a mapping's `id:`,
and gh-aw's table of built-in engines lists "GitHub Copilot (default) | engine: copilot or omit engine". <!--s:heuristics.config_path_patterns.workflow_engine_builtin_rows:Words-->Five<!--/s--> `settings`
rows with the match mode `workflow_engine:<engine>` match `copilot`, `claude`, `codex`, `gemini` and
`agy`. gh-aw added `agy`, Antigravity CLI, on 2026-10-08 and lists "Gemini CLI (deprecated) |
engine: gemini" ([engines](https://github.github.com/gh-aw/reference/engines/), read 2026-10-28).
"The engine is deprecated but remains supported, with no removal date or automatic migration."
([the Gemini engine](https://github.github.com/gh-aw/reference/engines/gemini/), read 2026-10-28).
Both engines count as Gemini. `pi`, the sixth built-in engine, is not one of the six tools. A file counts when its
front matter has `on:`, which a README beside the workflows does not have. `paths.workflow_tool`
applies this test for the cloning step, the collected file cleanup and the detection step alike. The
compiled `.lock.yml` is generated and has no row. No workflow is counted twice, because no compiled
`.lock.yml` in the collection started on 2026-09-24 runs a documented action. The collection that
the checkout tracks contains GitHub Agentic Workflows in <!--s:collection.agentic_workflow_repositories-->123<!--/s--> repositories, most of them on
Copilot.

A workflow can also take its engine from a file that it imports. `imports:` lists Markdown files
that `gh aw compile` merges into the workflow, such as `shared/engine.md`. Where the workflow
names no engine, the compiler takes "the first real included engine specification" from these
files (`validateSingleEngineSpecification` in `pkg/workflow/engine_validation.go` of
github/gh-aw, read 2026-10-14). The imports reference states that the importing workflow always
provides the engine, but the code of the compiler determines the engine, and
`paths.agentic_workflow` resolves an import as the compiler does.

`paths.agentic_workflow` resolves an import path under `.github/` or starting with `/` against
the repository root, and any other import path against `.github/workflows/`, except that an
import without a `/` in an imported file resolves against that file's directory. It reads only
files inside `.github/` and `.agents/`, and it follows the imports of imported files. It skips a
missing import that ends in `?`, which marks the import as optional. The engine is unknown where
the files in the repository do not determine it: a required import that the repository does not
contain, a remote import such as `owner/repo/path@ref`, imports that select two engines, which
gh-aw does not compile, or more than <!--s:parameters.agentcontext.paths.MAX_WORKFLOW_IMPORTS-->100<!--/s--> imported files. An `engine:` mapping that contains
only preferences, such as `model:`, selects no engine.

The cloning step copies the imported files with the workflow, the collected file cleanup keeps
them and the detection step counts them as part of the workflow rather than in `other`. In the
October 5–6 collection, workflows of pydantic/pydantic-ai, pulumi/pulumi-azure-native and
pulumi/pulumi-kubernetes-operator that the detection step had counted as Copilot's take Claude's
engine from their imports.

A workflow can also load a file into the prompt from its body. "In markdown, use
`{{#runtime-import filepath}}` to inject the content of another file directly into the body at
that position", and "Use `{{#runtime-import? filepath}}` to silently skip a missing file instead of
failing" ([imports](https://github.github.com/gh-aw/reference/imports/), read 2026-10-31). The
older form "`{{#import filepath}}` (without `runtime-`) is a deprecated body-level shorthand. It
normalizes to `{{#runtime-import filepath}}` at runtime", and "Runtime imports automatically strip
YAML front matter and HTML/XML comments"
([templating](https://github.github.com/gh-aw/reference/templating/), read 2026-10-31).
`paths.agentic_workflow` follows `processRuntimeImports` in `actions/setup/js/runtime_import.cjs`
of github/gh-aw a454a6b30e, read 2026-10-31. It reads the macros in the body of the workflow, of
each file that the workflow imports and of each file that a macro loads, after the front matter
and the HTML comments are removed. A macro in a fenced code block counts too, and only a
`{{#import}}` in a code span on one line does not. The templating page states "All file paths
resolve within `.github`, with or without the `.github/` prefix", but the code reads a path that
starts with neither `.github/` nor `.agents/` from `.github/workflows/`, as the comment in
`resolveRuntimeImportFilePath` states: "If path doesn't start with .github or .agents, prefix with
workflows/". gh-aw has read such a path there since its commit 532ca33448 of 2026-02-11, "Fix
runtime-imports path resolution to default to .github/workflows/". opsmill/infrahub's workflows
load `shared/issue-taxonomy.md`, and their lock files, which gh-aw v0.81.6 compiled, name
`.github/workflows/shared/issue-taxonomy.md`. A file that a macro loads is part of the workflow
and selects no engine, because gh-aw removes its front matter.

The compiler also reads the `engine:` of a file that a line `{{#import path}}`, `@include path` or
`@import path` of the body names (`ExpandIncludesForEngines` in `pkg/parser/include_expander.go`,
which `pkg/workflow/compiler_orchestrator_engine.go` calls). `paths.agentic_workflow` reads no
engine there, because these forms are deprecated and no GitHub Agentic Workflow in the collection
of the run that started on 2026-10-23 has such a line. In that collection, the workflows of
shopware/shopware and opsmill/infrahub load files that no other heuristic keeps.
shopware/shopware's Claude workflows each load a policy from `.github/aw/`, which loads a second
file from `.github/aw/shared/`, and opsmill/infrahub's Claude workflows load
`.github/workflows/shared/issue-taxonomy.md`. The cloning step copies these files with the workflow,
the collected file cleanup keeps them, and the detection step counts them as part of the workflow
rather than in `other`.

The engine's `command:` replaces the program that the workflow runs: "engine.command replaces
the executable and skips the engine installer, not AWF installation." ([gh-aw's engines
reference](https://github.github.com/gh-aw/reference/engines/), read 2026-10-28). Each of gh-aw's
engine ids, `claude`, `copilot`, `codex`, `gemini`, `cursor-agent` and `opencode`, is the
beginning of the name of its tool's program. A workflow therefore counts for its engine's tool
when the file name of the command begins with the engine's id, as `/usr/local/bin/copilot-dev`
does, and for no tool otherwise. pydantic/pydantic-ai
imports `engine: {id: claude, command: /tmp/gh-aw/bin/pydantic-ai-runner-launch}`, which runs
pydantic-ai's own agent runner on Z.AI's models, and Claude Code does not run. Since 2026-10-14,
its GitHub Agentic Workflows, which the detection step had counted as Claude's, count for no tool.
No other repository in the October 5–6 collection sets `command:`.

A workflow can also run an agent's command-line tool in a shell step, and every tool documents
the invocation that runs it without a person present: `claude -p`, `codex exec`, `gemini -p`,
`copilot -p`, `opencode run` and Cursor's `agent -p`. Cursor documents no action. The workflow on
its GitHub Actions page installs the tool with `curl https://cursor.com/install -fsS | bash`
before it runs `agent -p`, and with `irm 'https://cursor.com/install?win32=true' | iex` on a
Windows runner. Because `agent` is too common a name to match alone, Cursor's two rows match the
installers. <!--s:heuristics.config_path_patterns.workflow_command_rows:Words-->Seven<!--/s--> `settings` rows with the match mode
`workflow_command:<invocation>||<invocation>` list these invocations and the `npx` form of each
tool's npm package. `paths.runs_command` matches an invocation where a shell command starts: at
the start of a line or of a `run:` value, after `;`, `&`, `|`, `(` or `` ` ``, inside `$(`, or
after `npx`, with the invocation's other words later on the same line. At each of these
positions, the invocation may follow `timeout <duration>` or a reserved word of the shell that
can precede a command, such as `if`, `do` or the `!` of a pipeline. In the October 5–6
collection, RayforceDB/rayforce ran `timeout 12m claude -p`, dotnet/runtime
`timeout 30 copilot --prompt` and nationalsecurityagency/datawave `if ! gemini --prompt`. No workflow in that
collection ran a tool after `sudo`, `env`, an assignment, `bunx` or `pnpm dlx`, and the rows do
not match these prefixes. `tests/test_paths.py` tests each shell separator and checks that
`paths.runs_command` rejects a command name that only begins with the tool name. An invocation with
its other words on a continuation line, as in hyperdxio/hyperdx's `.github/workflows/release.yml`,
`npx --yes @anthropic-ai/claude-code@2.1.220 \` with `--print` on the next line, is not matched. The
cloning step copies such a workflow only where another row copies `.github` whole, as hyperdx's
`.github/prompts` does. Reading continuation lines would change what the step copies in every
repository with a workflow, and the file list cannot narrow that reach.

A workflow whose job calls a reusable workflow of another repository, `jobs.<id>.uses:
<owner>/<repository>/.github/workflows/<file>@<ref>`, is not matched either, although the called
workflow can run an agent on the repository, as mui/material-ui's `claude-review.yml` calls
mui/mui-public's workflow that runs `anthropics/claude-code-action`. The collection reads no
other repository, and the detection accuracy evaluation of 2026-10-26 counts such a file as a
miss (path_recall pr-0276).

A workflow that runs the tool only through a script of the repository is not matched either,
because `paths.runs_command` reads the workflow file and not the scripts that its steps run.
fedimint/fedimint's `codex-agent.yml` and `codex-ci-debug.yml` run `bash
.github/scripts/run-codex-agent.sh` and `bash .github/scripts/run-codex-ci-debug.sh` after the `-c`
of a Nix shell, and each script ends in `exec codex exec`. Reading the scripts that a step runs
would change what the cloning step copies in every repository with a workflow, as reading
continuation lines would, and the file list cannot narrow that reach. The limit is decided, and
the detection accuracy evaluation counts both workflows as misses (path recall pr-0252 of the
evaluation of 2026-11-04).

A workflow that mentions `claude -p` only in a comment is not Claude's, because the rows skip
comment lines. An action takes precedence over a command: A file that both runs
`anthropics/claude-code-action` and calls `codex exec` is Claude's. A workflow counts for one tool.
supabase/cli's `ai-review.yml` runs `claude -p` in one job and `openai/codex-action`, named through
a YAML anchor, in another, and it counts as Codex's. No row reads a compiled `.lock.yml` for
commands, because the rows read its Markdown source. Copilot's cloud agent has neither an action
nor a command, and a row matches the name of the one workflow that it runs,
`copilot-setup-steps.yml`.

A row that matches a workflow by its name counts the file only when it has a line that is
neither blank nor a comment (`detection._workflow_has_content`). A YAML file of comments alone is
an empty document without jobs, and "A workflow is a configurable automated process made up of one
or more jobs" ([workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
read 2026-11-05). Every line of auth0/laravel-auth0's `claude-code-review.yml` and of utopia-php/database's
`claude.yml` is a comment, and each file is an `other` row. A name row of Claude Code, Copilot or
OpenCode tests nothing else. It counts cloudposse/atmos's `claude.yml`, which runs only a size check
of its `CLAUDE.md` files and no Claude Code. A test of what a Claude workflow runs would also drop
evcc-io/evcc's `claude-code-review.yml`, which only records the number of the pull request for the
workflow that runs the review, and dosemu2/dosemu2's `claude.yml`, which starts Claude Code through
Anthropic's API with `curl` ([evidence limits](dataset.md#evidence-limits)).

The name row of Gemini, `gemini-dispatch.yml`, also tests what the file calls. The Gemini CLI
action's README describes it as "a central dispatcher for Gemini CLI, routing requests to the
appropriate workflow based on the triggering event and the command provided in the comment"
([run-gemini-cli](https://github.com/google-github-actions/run-gemini-cli/blob/387c8ddb7a72078825da941758b811b9153c71e8/README.md),
read 2026-11-08), and the action's example workflows that it calls declare `workflow_call`. A job
calls a reusable workflow of the same repository with `jobs.<job_id>.uses:
./.github/workflows/<file>` or `$/.github/workflows/<file>`, and "Use on.workflow_call to define
the inputs and outputs for a reusable workflow" ([workflow
syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax), read
2026-11-08). The dispatcher counts only where a job calls a workflow of the repository that a
Gemini workflow row matches and that declares `workflow_call`
(`detection._dispatches_a_gemini_workflow`), and it is an `other` row otherwise.
pantavisor/pantavisor's dispatcher calls a `gemini-review.yml` that declares only
`workflow_dispatch`, so no Gemini runs through it (pp-0688 of the evaluation of 2026-11-04),
and NationalSecurityAgency/datawave's calls a `gemini-review.yml` that runs `gemini --prompt` and
declares `workflow_call`.

gh-aw also ships sample engines for tools that it does not support, Cursor and OpenCode among them.
The sample definitions, `.github/workflows/shared/cursor.md` and
`.github/workflows/shared/opencode.md`, declare `id: cursor`, which runs `cursor-agent`, and
`id: opencode`, which runs `opencode run`. A workflow imports one of these definitions and names the
engine in the nested form: "Custom engines require object form with engine.id." ([custom
engines](https://github.github.com/gh-aw/reference/third-party-agent/), read 2026-10-28). The
rows `workflow_engine:cursor` and `workflow_engine:opencode` attribute such a workflow to Cursor and
to OpenCode, as the other `workflow_engine:` rows attribute a workflow to its built-in engine. The
collection started on 2026-09-27 contains no copy of either definition.

The Codex action takes its prompt inline or from a file of the repository: "You can provide inline
text via prompt or point to a file committed in the repo with prompt-file", and "Consider storing
prompts in .github/codex/prompts/" ([the action](https://learn.chatgpt.com/docs/github-action), read
2026-10-21). The action reads the file that its `prompt-file` input names, "Path to file that
contains the prompt to pass to Codex" (`action.yml` of openai/codex-action c05cfd74ad95, read
2026-10-21), relative to the step's working directory, the checkout of the repository. The folder
that the page suggests is no configuration artifact of its own, because the action reads only the
file that a workflow names, wherever it is. That file is the prompt of the agent workflow that names
it and therefore part of that workflow, as the imports of a GitHub Agentic Workflow are. The Claude
Code Base Action takes its prompt in the same two ways, "prompt: The prompt to send to Claude Code
(mutually exclusive with prompt_file)" and "prompt_file: Path to a file containing the prompt to
send to Claude Code (mutually exclusive with prompt)" (`action.yml` of
anthropics/claude-code-base-action 33edbb44fb53, read 2026-11-01). The folder `base-action` of
anthropics/claude-code-action has the same input (`action.yml` at d535f5172dec, read 2026-11-01),
and the Claude Code Action itself has `prompt` and no such input. The step that runs the Base Action
works in the checkout unless the workflow sets `CLAUDE_WORKING_DIR` ("Change to CLAUDE_WORKING_DIR
if set"), and `src/prepare-prompt.ts` tests the path with `existsSync`, which resolves a relative
path against the working directory. A relative path therefore names a file of the checkout.
`paths.action_prompt_files` reads the `prompt-file` of each step that runs the Codex action and the
`prompt_file` of each step that runs the Base Action, also of a step that names either action
through a YAML anchor or its alias, as supabase/cli's `ai-review.yml` does. It reads the path
relative to the repository, also after `${{ github.workspace }}/`, and leaves out a path that
another expression builds, such as `${{ runner.temp }}/prompt.md`, an absolute path and a path that
names no file of the repository. The cloning step copies the file with the workflow, the collected
file cleanup keeps it and the detection step counts it as the workflow's rather than in `other`.
In the October 5–6 collection, workflows that run the Codex action name
prompt files of the repository, such as baimard/gestion's
`.github/codex/prompts/issue.md`. In the development dataset, workflows that run the Base Action
name a prompt file of the repository only in facebook/rocksdb, where a step names `claude_md/ci_recovery_prompt.md`. The other steps of
facebook/rocksdb that run the Base Action, and the workflows of mattgodbolt/jsbeeb,
facebookincubator/velox and facebook/hermes, name a file in `/tmp` that an earlier step of the
workflow writes, such as velox's `/tmp/prompt.txt`.

## A context file that configuration names

Gemini CLI and Codex read a context file under a name that their configuration gives. Gemini CLI reads
the files that `context.fileName` of `.gemini/settings.json` names, one name or a list, beside
`GEMINI.md`, wherever it would look for `GEMINI.md`. For an extension, Gemini CLI reads one
filename or a list from `contextFileName` in `gemini-extension.json`, relative to the extension
directory. The extension
[loader](https://github.com/google-gemini/gemini-cli/blob/38700b4b38bf387dafded6c97c3f190d084b49e9/packages/cli/src/config/extension-manager.ts#L1279-L1399)
rejects POSIX and Windows absolute paths, null bytes and `..` in the original or URL-decoded
name. Each path must stay strictly inside the extension directory, and a nonempty filename keeps
its spaces. No row reads a top-level `contextFileName`, because Gemini CLI read that key only in
its version 1 settings, until it removed the migration from that format in January 2026. The
repositories in the collection started on 2026-09-24 that set the key all name `AGENTS.md`.

Codex reads the file that `model_instructions_file` of `.codex/config.toml` names in place of its
built-in instructions. In each directory from the project root down, it tries
`AGENTS.override.md`, then `AGENTS.md`, then the names that `project_doc_fallback_filenames`
lists, and reads one file per directory at most. Codex ignores a fallback entry that is not a
filename, that is, an entry that contains a `/` or a NUL or that is `.` or `..`, with the warning
"ignoring project_doc_fallback_filenames entry that is not a filename" (`candidate_filenames` in
`codex-rs/core/src/agents_md.rs`, openai/codex 6ae8b8aa7c60, read 2026-10-16). The heuristic
ignores such an entry too. The Codex page does not state what a relative path resolves against.
Codex's loader resolves the paths of a project config against the `.codex` folder, and
`CODEX.md` there means `.codex/CODEX.md`.

OpenCode reads the files that `instructions` lists in its configuration together with the
`AGENTS.md` files: "Declare additional instruction files, globs, or HTTP(S) URLs. OpenCode loads
them alongside discovered AGENTS.md files." ([config](https://opencode.ai/docs/config/)). An entry is a path or a glob
pattern, such as `.cursor/rules/*.md`. OpenCode 1.14.24 fetches an entry that starts with
`http://` or `https://`. It reads an entry that starts with `~/` in the home directory and an
absolute entry where it points. It globs every other entry with node-glob, files only and dotted
names included, from its working directory up to the worktree root (`Instruction.systemPaths` in
[`packages/opencode/src/session/instruction.ts`](https://github.com/anomalyco/opencode/blob/da6683fedcbb57a36c4ba54ba5ad00dd8bc2da65/packages/opencode/src/session/instruction.ts)). It concatenates the
entries of all its configuration files. The rows match the four configuration files at the root
and glob their entries from there, where the other OpenCode rows match the configuration. In the
October 5–6 collection, the entries of the OpenCode configuration files that list instructions
name `AGENTS.md`, `CLAUDE.md`, Markdown files under `.opencode` and `.agents/rules`,
documentation such as `docs/standards/*.md` and the reStructuredText documents of
mathiasertl/django-ca.

Because the configuration, not a path, determines which file these keys name, the <!--s:heuristics.config_path_patterns.declared_rows:words-->eight<!--/s--> rows
have the declaring file as their `path` and the key in the match mode: `declared_names:`,
`declared_fallback:`, `declared_path:`, `declared_local:` and `declared_globs:`. `declared_local:`
accepts the extension filenames under the restrictions above. `declared_path:` accepts one Codex
path, which may leave `.codex/` while staying inside the repository. `declared_globs:` accepts
OpenCode's relative entries that stay inside the repository. `paths.declared_context_files` reads
these rows for every step. The cloning step copies the named file out of the clone. The
collected file cleanup keeps the file, where it would otherwise delete it as not a spec file or,
for the lowercase `gemini.md` that `google/clasp`'s extension names, as a misspelled `GEMINI.md`.
The detection step lists the file as a context file. The tool flag needs nothing more, because
the declaring file already is configuration of that tool.

In the collection started on 2026-09-24, most declarations name `AGENTS.md` or `GEMINI.md`, which
the path patterns find anyway, and a few name a file that no path pattern copies. In
the March sample, in which the trees contain only what the cloning step of March copied, the rows
reclassify files of `other.csv` as context files: `.codex/CODEX.md` and the fallback names
`.claude/IDENTITY.md` and `.codex/IDENTITY.md` in `ossrs/srs`, and a compliance document under
`.gemini` in `devfile/devworkspace-operator`. The
analysis figures show such a file under the category `Named in Configuration`, because its path
does not show which configuration named it. A context file that no path pattern matches can only
be a file that a configuration named.

### A subagent file that configuration names

A Codex agent role is a TOML config layer. A role file is a TOML file below `.codex/agents`, at
any depth, or the file that the `config_file` of an `[agents.<name>]` table in
`.codex/config.toml` names: "Path to a TOML config layer for that role; relative paths resolve
from the config file that declares the role"
([config reference](https://learn.chatgpt.com/docs/config-file/config-reference), read
2026-11-07). Codex reads the `agents` table of each config layer, keeps the file of each role,
`if let Some(config_file) = role.config_file.clone() { declared_role_files.insert(config_file); }`,
and resolves its path against the folder of the layer, the `.codex` folder of the project
(`load_agent_roles` and `agents_toml_from_layer` in
[`agent_roles.rs`](https://github.com/openai/codex/blob/758ef40f50c1a458425c7cfbf1eb12cbc07af0b0/codex-rs/core/src/config/agent_roles.rs#L53-L55)
at rust-v0.149.0). The row `declared_path:agents.*.config_file` of `.codex/config.toml` counts
such a role file as a subagent of Codex, where `*` stands for the name of each role.

A role file names the base instructions of its subagent with `model_instructions_file` too. Up to
rust-v0.148.0, Codex loaded a role as a config layer, resolved its relative paths against the
folder of the role file, `resolve_relative_paths_in_config_toml(role_config_toml,
role_config_base,)`, and read the role's `model_instructions_file` as the base instructions of the
subagent: `let preserve_current_base_instructions = role_layer_toml.get("instructions").is_none()
&& role_layer_toml.get("model_instructions_file").is_none();` in
[`role.rs`](https://github.com/openai/codex/blob/3ba0f711642a888aec92a611a3f3b2211157ff89/codex-rs/core/src/agent/role.rs#L180-L181).
rust-v0.149.0 stopped reading the key in a role: "Apply only supported role overrides for model
behavior, developer instructions, personality, service tier, and capability reductions"
([#39299](https://github.com/openai/codex/pull/39299)). The dataset counts the retired format, as
it counts other deprecated configuration. The row `declared_path:model_instructions_file` has the
directory `.codex/agents` as its `path` and reads each role file, those below the directory and
those that the row of `.codex/config.toml` names. `paths.declared_subagent_files` reads both rows
for every step. The cloning step copies the file that a role names, and a role file that
`config_file` names outside `.codex/agents`, which no other pass copies outside `.codex`. The
collected file cleanup keeps both. The instructions file is a part of the role that names it
(decided on 2026-11-08), as the prompt file of an OpenCode agent is a part of the agent. A
subagent is an agent definition to which the tool can delegate part of a task (GLOSSARY.md), and
Codex delegates to the role, the TOML config layer, of which the instructions file is the prompt.
The detection step therefore lists the role file as a subagent of Codex, and the instructions
file is neither a row of its own nor an `other` row (`detection._declared_subagents`).
areal-project/AReaL's `.codex/agents/planner.toml` names `./planner.md`, and each role of its
`.codex/agents` is one subagent row of Codex, the TOML file.

## An AGENTS.md under .claude

"By default, Claude reads `AGENTS.md` only when you have no `CLAUDE.md` in your working
directory or above it", where "a `CLAUDE.md`, `.claude/CLAUDE.md`, or `CLAUDE.local.md`" count,
and "When none count", Claude Code reads "every `AGENTS.md` and `.claude/AGENTS.md` in your
working directory and the directories above it" ([memory](https://code.claude.com/docs/en/memory),
read 2026-11-05). The value of the setting that loads both kinds of file is ignored "in project
and local settings files", and a repository cannot select it. The `AGENTS.md` row names no tool,
because every tool reads that name. A `file` row attributes `.claude/AGENTS.md` to Claude Code,
and `detection.tool_for_path` checks that row before it attributes the name to no tool. The row
reads the file only where Claude Code reads it (`detection._claude_reads_agents_md`), by the
rules of the [import detection](#imports): where no CLAUDE file is at the root or in the
directory above the `.claude`, the condition `no_claude_md` of the row of `AGENTS.md`, or where a
chain of Claude Code's imports reaches the file. <!--s:collection.claude_agents_md_repositories:Words-->Four<!--/s-->
repositories of the development dataset have a `.claude/AGENTS.md`. ValidatorsDAO/slv has no
CLAUDE file, and keycloakify/keycloakify-angular's `.claude/CLAUDE.md` imports the file with
`@AGENTS.md`, so that both files are Claude's. ng-alain/delon has a `.claude/CLAUDE.md` and
tsedio/tsed a root `CLAUDE.md`, neither imports the file, and both files name no tool.

## A context file in another case, at the root

The path patterns match `AGENTS.md`, `CLAUDE.md` and `GEMINI.md` by their exact name, and the
collected file cleanup deletes a file that a repository spells `agents.md` or `Claude.md` as a
misspelling, except at the root. Codex and Gemini CLI look the name up by joining it to a directory
and querying the file system. Codex reads the metadata of that path
(`codex-rs/core/src/agents_md.rs`), and Gemini CLI calls `fs.access` on it
(`packages/core/src/utils/memoryDiscovery.ts`). On a case-insensitive file system, the default on
macOS and Windows, that lookup finds `agents.md`. Each such file in the collection started on
2026-09-24 was read to determine whether it contains an agent's instructions. At the repository
root, every file did except a file that held a single line reporting a finished refactoring.
Below the root, most did not, and most of those were documentation about agents in an application's source tree.

The <!--s:heuristics.config_path_patterns.root_any_case_rows:words-->three<!--/s--> `root_any_case` rows accept the name in any case at the root only, and a nested
file in another case stays deleted. In the collection that the checkout tracks, <!--s:collection.root_other_case_repositories-->87<!--/s--> repositories
have such a file at the root, and in <!--s:collection.root_other_case_only_config_repositories-->40<!--/s--> of them it is the only configuration artifact,
so that without these rows the collected file cleanup would remove the repository. No file in the March sample is affected, because the cloning step of
March copied no such file at the root. The chains of Claude Code and Gemini CLI from such a file
read its imports as a file system that ignores case reads them
([the rows of each tool](#the-rows-of-each-tool)).

## Copilot CLI's places below the root

GitHub reads a repository's `.github` only at the root. Copilot CLI also reads some of its places
in the directory that it works in and in each directory between that directory and the root:
"Unless noted in the table below, Copilot CLI discovers repository and agent instruction files in
the standard locations: the repository root, the current working directory, intermediate
directories between them, and any directories nested in the path of a file it is working on."
([custom instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions),
read 2026-10-21) The loading order of the [plugin
reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)
lists "<parents>/.github/agents/ (inherited, monorepo)" and "<parents>/.github/skills/ etc.
(inherited)". On 2026-10-21, Node called the native discovery of Copilot CLI 1.0.101
(`@github/copilot-darwin-arm64`), code in its `runtime.node`, on test workspaces with the working
directories `pkg/` and `pkg/sub/` of a git repository. The CLI read six places of `pkg/.github`:

| Place | What the CLI read |
|---|---|
| `copilot-instructions.md` | the file for both working directories, beside the root's (`instructionsDiscover`), and its imports against its own directory |
| `instructions/` | the `*.instructions.md` at any depth for `pkg/` and not for `pkg/sub/`, as the page states "discovered in the standard locations but not intermediate directories" |
| `agents/` | the Markdown files with a description, at any depth, as project agents for `pkg/` and inherited agents for `pkg/sub/` (`customAgentsDiscoverJson`) |
| `skills/` | the skills at any depth, as project skills for `pkg/` and inherited skills for `pkg/sub/` (`skillsLoad`) |
| `lsp.json` | its servers, merged with the servers of the root's file, for both working directories (`lspConfigsLoadRawProjectConfig`) |
| `mcp.json` | its servers beside the servers of the root's file, for both working directories, and the `.mcp.json` of `pkg/` instead where `pkg/` has both files, as at the root (`mcpConfigLoadMerged`) |

The CLI reads no other place of such a `.github`. Its JavaScript takes the repository settings,
`.github/copilot/settings.json` and `settings.local.json`, and the hooks of `.github/hooks/` from
the root of the git repository (`repoSettingsLoadWithWarning` and `getHooksDir` in `cli-main.js`).
The CLI reads no prompt file or chat mode at all, although VS Code reads both at the root of its
workspace folder. GitHub reads the workflows only in the root's `.github`.

The CLI reads the `.mcp.json` of the same directories, outside `.github`. The [MCP
servers](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers)
page (read 2026-10-21) lists ".mcp.json (in any directory from your working directory up to the
repository root)". It states that the CLI reads the files of both names "from your current working directory
up to the repository root, loading MCP configuration files along the way", and "If both .mcp.json
and .github/mcp.json exist in the same directory, .mcp.json takes precedence."
`mcpConfigLoadMerged` of 1.0.101 loaded the servers of `pkg/.mcp.json` for the working
directories `pkg/`, `pkg/sub/` and `pkg/sub/deeper/`, and the servers of `pkg/sub/.mcp.json` for
`pkg/sub/` and not for `pkg/`, in the format with `mcpServers` and in "the bare top-level format
where each key is an MCP server name". The `root_file` row of `.mcp.json` matches the root's
file and has the tool `any`, because several tools read that file. The
[MCP page](https://code.claude.com/docs/en/mcp) of Claude Code (read 2026-10-21) puts a project's
`.mcp.json` at the project's root directory and names no `.mcp.json` below it, but Claude Code
2.1.314 reads the `.mcp.json` of each directory from its working directory up to the file system
root and merges their `mcpServers`:
`while(w!==SYr(w).root)y.push(w),w=gYr(w);for(let L of y.reverse()){let G=goe(L,".mcp.json")`.
The `nested:` row of `.mcp.json` attributes the file in a directory below the root to Copilot,
and detection adds Claude Code where the file names, under `mcpServers`, the one key that Claude
Code reads there, a server that Claude Code loads (`detection._claude_reads_nested_mcp`). Claude
Code 2.1.319 takes an entry without a `type` for a stdio server,
`"type"in Ce&&typeof Ce.type==="string"?Ce.type:"stdio"`, and the schema of a stdio server
requires the command, `type:T("stdio").optional(),command:o().min(1,"Command cannot be empty")`.
A `type` that is not a string, such as a list, selects that schema too, which rejects it. Claude
Code therefore loads a server with a `command` and no `type` or the type `stdio`, and a server
with a `url` and the type `http`, `streamable-http`, `sse` or `ws`, where each field of a plain
JSON type that the schema of its type declares has that type, such as a list of strings in
`args` and an object of strings in `env` and `headers` (`paths.claude_loads_a_server`). The test
does not read the objects `oauth`, `tools` and `toolPermissions`. Given such servers inline in
a manifest, which it parses with the same schemas, `claude plugin validate` of Claude Code
2.1.319 reports "Invalid input" for a boolean in `env` and "Too small: expected number to
be >0" for a `timeout` of 0. boardsesh/boardsesh's `packages/web/.mcp.json`, whose one server
has a `url` and no `type`, is Copilot's alone. A
`.mcp.json`, at the root or below it, and the `.cursor/mcp.json`, `.github/mcp.json` and
`.vscode/mcp.json` count only when they configure a server, that is, when they contain an entry
with a `command` or a `url` (decided on 2026-10-29). A server needs no `type`, because VS Code's
`sanitizeServer` infers it from the command or the url and Visual Studio's own example is
`{"servers": {"github": {"url": ...}}}`, although Claude Code skips such a server: "A JSON entry
that has a url but no type is a configuration error ... Claude Code skips that server". Such a
server does not add Claude Code to a file below the root or in a plugin. The root `.mcp.json`
keeps the `any` of its `root_file` row with such a server, as it does in the bare form. VS Code
reads the servers of `.vscode/mcp.json` under `servers` alone (`fromWorkspaceFolderMcpServers` of
[`mcpResourceScannerService.ts`](https://github.com/microsoft/vscode/blob/419dc8780af94fb0f8c02f8ae52bd2e31ac3b99d/src/vs/platform/mcp/common/mcpResourceScannerService.ts)),
so lightspeedwp/tour-operator's file, with its servers under `mcpServers`, configures none.
`.github/mcp.json` counts with `mcpServers` or in the bare form, because its reader, Copilot CLI,
calls `servers` "the unsupported top-level key `servers`". A `.mcp.json` counts with either key or
in the bare form. Below the root, Copilot CLI reads `mcpServers` and the bare form, and Visual
Studio reads `servers` in the `.mcp.json` of a solution directory: "`<SOLUTIONDIR>\.mcp.json` Works
well if you're looking for an MCP configuration that you can track in source control for a
repository" ([Visual Studio](https://learn.microsoft.com/en-us/visualstudio/ide/mcp-servers), read
2026-10-29, whose example keeps the servers under `servers`). Detection does not check that a
solution file is beside the file, because it reads the collected tree, which holds no solution
file. In the collection of the run that started on 2026-10-23, the file lists show a solution file
beside each `.mcp.json` below the root with `servers`, and at the root of Azure/Industrial-IoT,
CommunityToolkit/Aspire, microsoft/aspire and OrchardCMS/OrchardCore, whose root `.mcp.json`
keeps its servers under `servers`. MirrorNetworking/Mirror and valbok/QtAVPlayer have such a root
`.mcp.json` without a solution file at the root. The `.mcp.json` files below the root with
`servers` are beside a `.slnx`: ARKlab/Ark.Tools' `samples/Ark.ResourceWatcher/.mcp.json` and
`samples/Ark.ReferenceProject/.mcp.json`, and rmcrackan/Libation's `Source/.mcp.json`. The root
`.mcp.json` and `.vscode/mcp.json` are read with comments and trailing commas (`jsonc_trailing`),
as VS Code parses both files, and a `.mcp.json` below the root or in a plugin as strict JSON, the
content format of its rows. A plugin's MCP file
and a file of a `json_mcp_servers` row count with a server too, and storybookjs/storybook's
`{"mcpServers": {}}` is none.

A plugin's directory below the root is such a directory too. Claude Code reads the `.mcp.json`
beside `.claude-plugin/plugin.json` as the plugin's MCP servers, and Codex reads the `.mcp.json`
beside `.codex-plugin/plugin.json` ([The parts of a plugin, wherever the repository puts it](#the-parts-of-a-plugin-wherever-the-repository-puts-it)).
Copilot CLI 1.0.101 read the `.mcp.json` of a directory with `.claude-plugin/plugin.json` when
that directory or a directory below it was the working directory, as it reads the file of any
other directory. The page ties the file to the directories between the working directory and the
root, not to a manifest. Such a file therefore has the tools of both rows, and neither row takes
precedence. The detection step records OpenAgentsInc/openagents's `plugins/claude/.mcp.json`
with the tool `claude|copilot` and the repository with both flags. A plugin's MCP file is Claude
Code's only where Claude Code loads a server of it, by the same test, because Claude Code 2.1.319
parses each server of the file with the schemas of its types (`Bie().safeParse(Ee)`). Codex drops
the `type` of a plugin's server and infers the transport from the `command` or the `url`
(`object.remove("type")` in `normalize_plugin_mcp_server_value` of
[`plugin_config.rs`](https://github.com/openai/codex/blob/758ef40f50c1a458425c7cfbf1eb12cbc07af0b0/codex-rs/codex-mcp/src/plugin_config.rs#L254)
at rust-v0.149.0), and a server with a `url` alone in the `.mcp.json` of a folder with both
manifests is therefore Codex's and not Claude Code's (`detection._plugin_mcp_servers`). The
`.mcp.json` of a plugin at the root keeps the `any` of the `root_file` row and takes no tool from
the plugin, because every tool that reads the repository's MCP configuration reads that file.
Where a manifest replaces the plugin's default MCP file, as `mcpServers` of a Codex plugin in the
compatibility layout does, the `.mcp.json` of its directory is only Copilot's.

The rows with the match mode `nested:` match these places, and `paths.nested_places` finds them
for every step. A place of a `.github` is in a directory named `.github` below the root, outside
the vendored directories and outside the directories that their own rows read whole, such as
`.claude`. The first segment below that `.github` may have any case, as the cloning step matches
it in the root's `.github`. A place of `.mcp.json` is a file with that name in a directory below
the root, outside the same directories, spelled as the cloning step copies the root's file. A
place can be a link to a file or a folder of the repository, such as dotnet/dotnet's
`src/msbuild/.github/copilot-instructions.md`, which links to `../AGENTS.md`, because Copilot
CLI reads the place through the link, as it reads the root's. A link out of the repository, to
its root, into `.git` or to a folder that contains the link is no place. The cloning step
copies each place, a folder as a whole and a link as its marker, with the folder that a link
at the place of a folder leads to (`paths.collect_nested_places`). The collected file cleanup
keeps the place whatever the root's `.github` contains, and the detection step reads it with the
mode after the prefix and attributes it to Copilot. A file in such a folder that no row matches,
such as an agent without a description, is an `other` row, as in the root's `.github`. The
import heuristic `copilot-import-06` reads the imports of such a `copilot-instructions.md`
([Imports](#imports)). In the file
lists of the October 5–6 collection, many places of a `.github` below the root are in a package
of a monorepo, such as the `.github/skills`
of the packages of the Azure SDKs, or in a template, such as bitfoundation/bitplatform's
`src/Templates/Boilerplate/Bit.Boilerplate/`. In the `mcp.csv` of the collection that the
checkout tracks, <!--s:collection.nested_mcp_json.repositories-->106<!--/s--> repositories have a `.mcp.json` below the root, some of
them in the directory of a plugin.

## The parts of a plugin, wherever the repository puts it

A plugin is a directory that contains a manifest, and each vendor's reference names the parts at
fixed places inside that directory. For Claude Code these are `skills/`, `commands/`, `agents/`,
`hooks/hooks.json`, `.mcp.json`, `output-styles/`, `workflows/` and `settings.json` beside
`.claude-plugin/plugin.json` ([plugins
reference](https://code.claude.com/docs/en/plugins-reference)). Cursor puts `rules/`, `skills/`,
`agents/`, `commands/`, `hooks/hooks.json` and MCP configuration beside
`.cursor-plugin/plugin.json`. Codex's compatibility layout puts `skills/`, `.mcp.json`,
`.app.json` and `hooks/hooks.json` beside `.codex-plugin/plugin.json`. The portable Agent
Plugins layout defines `skills/` and `mcp.json` beside a `plugin.json` that declares its schema,
and in that layout, hooks at `hooks/hooks.json` are a Codex component. A Gemini CLI
extension contains `commands/`, `skills/`, `hooks/hooks.json` and `agents/` beside
`gemini-extension.json`. A Copilot CLI plugin contains `agents/`, `skills/`, `hooks.json` or
`hooks/hooks.json` and `.mcp.json`, and the CLI looks for its manifest at `.plugin/plugin.json`,
`plugin.json`, `.github/plugin/plugin.json` or `.claude-plugin/plugin.json` ([CLI plugin
reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)).

A Claude Code mod is a plugin with hooks that are JavaScript functions. Its `hooks/hooks.json`
names the module under `modules` and need not have a `hooks` key: "modules: an array with one path, relative to
this file, to the hooks module, as in "modules": ["./register.js"]. Can also hold settings hooks
under hooks." ([mods reference](https://code.claude.com/docs/en/plugins/mods/reference), read
2026-10-16). The match mode of that file, `json_hooks_or_modules`, accepts either key. The module
is code, as a hook's command is, and has no row of its own. Cursor also reads a repository's
plugins from its marketplace at `.cursor-plugin/marketplace.json`. "For a marketplace entry with
"source": "my-plugin": The parser looks for my-plugin/.cursor-plugin/plugin.json If found, the
per-plugin manifest is merged with the marketplace entry (manifest values take precedence)"
([plugins reference](https://cursor.com/docs/reference/plugins), read 2026-10-16). A directory
that an entry names is therefore a plugin with or without a manifest. `metadata.pluginRoot`
prefixes every source, and an entry's component fields count where the plugin's manifest does
not set them. No repository in the October 5–6 collection has a mod or a Cursor marketplace
entry without a manifest.

Codex reads the plugins that a Codex marketplace lists, an `.agents/plugins/marketplace.json`
below a marketplace root, which is the repository root for the repository's own list: "Point each
plugin entry’s source.path at the plugin directory you want the local host to load", "Keep
source.path relative to the marketplace root, start it with ./, and keep it inside that root" and
"Codex resolves source.path relative to the marketplace root, not relative to the .agents/plugins/
folder" ([plugins](https://developers.openai.com/plugins/build/plugins), read 2026-11-07). The
source `./` names the marketplace root, as the list of bex-co/beancount-io names its repository
root, and Jij-Inc/Qamomile's list in `qamomile/agent_plugins/.agents/plugins/` resolves against
`qamomile/agent_plugins/`. `resolve_local_plugin_source_path` in
`codex-rs/core-plugins/src/marketplace.rs` of
[rust-v0.167.0](https://github.com/openai/codex/tree/rust-v0.167.0) refuses a path without the
`./` prefix or with `..`. Codex reads none of the plugins of a file that does not parse as its
`RawMarketplaceManifest` (`paths.codex_marketplace_plugins`), and it installs no plugin of an
entry that `find_installable_marketplace_plugin` refuses, an entry with the installation policy
`NOT_AVAILABLE` or with `products` that leave out Codex, or of an entry with a name that
`PluginId::new` refuses (`paths.codex_installs`, `codex-rs/core-plugins/src/marketplace.rs` and
`codex-rs/core-plugin-common/src/plugin_id.rs` of
[c9b4267c370a](https://github.com/openai/codex/tree/c9b4267c370a755da12ec1c8648c2451163212b7), read
2026-11-08). The marketplace is a `plugins` row only where Codex can install a plugin from one of
its entries, with a source that Codex resolves (`paths.codex_marketplace_installs`). Otherwise it
gives Codex no plugin to load and is an `other` row, as UMEP-dev/SUEWS's marketplace, which lists
its only plugin with `"installation": "NOT_AVAILABLE"` (pp-0374 of the evaluation of
2026-11-04). A listed folder with a portable manifest is a Codex plugin for the portable parts that
Codex reads, "Portable packages discover skills from the root skills/ directory" and
"Configure portable MCP servers in root mcp.json". Codex reads any other listed folder in the
compatibility layout, through the first of `.codex-plugin/plugin.json`,
`.claude-plugin/plugin.json` and `.cursor-plugin/plugin.json`
(`DISCOVERABLE_PLUGIN_MANIFEST_PATHS` in `codex-rs/exec-server-protocol/src/protocol.rs`), and
through the manifest that it writes from the entry where the folder has none of them and the
entry has a field other than `name`, `source` and `policy` or a category
(`marketplace_plugin_manifest_fallback`). obsei/obsei's list names `integrations/claude/obsei`,
which holds the manifests of Claude Code and Cursor, and Codex reads that folder through its
`.claude-plugin/plugin.json`. Below a root, Codex reads one marketplace, the first entry of
`MARKETPLACE_MANIFEST_RELATIVE_PATHS` that exists: `.agents/plugins/marketplace.json`, then
`.agents/plugins/api_marketplace.json`, `.claude-plugin/marketplace.json` and
`.cursor-plugin/marketplace.json` (`find_marketplace_manifest_path` in
`codex-rs/core-plugins/src/marketplace.rs` of rust-v0.167.0, read 2026-11-08). It reads a later
entry only where every earlier one is missing, and at a root with a Codex marketplace beside a
Claude Code or Cursor marketplace it reads only the Codex marketplace. Where a root has none of
them, Codex tries them at the root of the git repository that contains the root
(`discover_marketplace_paths_from_roots`). The cloning step and the detection read only the first
entry as a marketplace of Codex. They keep a Claude Code marketplace Claude Code's and a Cursor
marketplace Cursor's, and read no `api_marketplace.json`, as long as it is undecided which of them
name Codex as a reader of the plugins that they list.

Copilot CLI reads the plugins that a Copilot CLI marketplace lists. In a marketplace's directory,
the CLI looks for the marketplace at "marketplace.json, .plugin/marketplace.json,
.github/plugin/marketplace.json, or .claude-plugin/marketplace.json (checked in this order)", and
the last of them is Claude Code's marketplace, which a row of Claude Code matches. A plugin's
source is "the path to the plugin's directory, relative to the root of the repository. It is not
necessary to use ./ at the start of the path", and an object names a GitHub repository or a Git
URL ([CLI plugin
reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference),
read 2026-11-07). The repository root is a marketplace's directory, and so is a directory below
it that holds `.plugin/marketplace.json` or `.github/plugin/marketplace.json`. A bare
`marketplace.json` makes only the root a marketplace's directory, but the CLI reads it first in
every marketplace's directory. `Dqe` in `app.js` of Copilot CLI 1.0.63, a release that ships its
code as plain JavaScript, tries the places in their order and returns an error, "Invalid
marketplace.json", where the first file that exists is not a valid marketplace. The reference
lists `pluginRoot` among the fields of `metadata` and does not describe it. `dFt` in the same
file prefixes the source with it, `./${u}/${d}` with the `./` of each removed, resolves the
result without its `./` against the marketplace's directory, and refuses a folder outside that
directory, "Plugin path escapes marketplace directory". Without `pluginRoot`, the source `./`
therefore names the marketplace's directory, as `.` does. A listed folder is a Copilot CLI plugin
where it has a manifest that the CLI reads: "All plugins consist of a plugin directory containing
a manifest file named plugin.json", the portable manifest or one of the legacy manifests, the last
of which is Claude Code's `.claude-plugin/plugin.json`. Azure/azure-sdk-tools's `.github/plugin/marketplace.json` sets
`"pluginRoot": "./plugins"` and lists `./azure-sdk-tools`, a Claude Code plugin, and Copilot CLI
reads that folder through its `.claude-plugin/plugin.json`. ansys/pyaedt's marketplace lists
`.github/plugin/pyaedt-cli`, which holds a skill and no manifest, and the steps count no plugin
there. The code of Copilot CLI 1.0.63 (`installFromMarketplace` and `install` in `app.js`)
installs a listed folder without a manifest, because `install` copies the folder and reads a
manifest only for the plugin's version, `UP(r)?.version`. Which parts of such a folder the native
loader of the current release reads has not been checked. The cloning step copies the
marketplace with the plugin manifests, also from a `.github` below the root, such as
dotnet/dotnet's `src/msbuild/.github/plugin/marketplace.json`, and the marketplace is attributed
to Copilot there as well.

LSP servers, which give an agent a language's diagnostics and code navigation, are the `lsp`
mechanism, with `lsp.csv` and a column in `repos.csv`. The maintainer decided on 2026-11-08 to keep
them a mechanism of their own rather than merge them into `settings.csv`, because each vendor
documents them as a configuration surface of its own, beside MCP servers and hooks, and a
mechanism of its own shows how their adoption grows. <!--s:vendor_docs.lsp_reading_tools:Words-->Three<!--/s--> of the six tools read them. Copilot
CLI reads a repository's `.github/lsp.json`, which "applies to everyone working on that project"
([add LSP servers](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/add-lsp-servers)),
a legacy plugin's LSP file and the `com.github.copilot/lsp.json` of an Agent Plugins plugin
([CLI plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)).
Claude Code reads LSP servers from a plugin only, from the `.lsp.json` at its root and the files
that its manifest's `lspServers` names ([plugins
reference](https://code.claude.com/docs/en/plugins-reference)). A file at one of these paths
counts whatever it contains. Codex's configuration reference names no LSP configuration. Gemini
CLI has no documented native LSP configuration, and the
[issue that requests LSP support](https://github.com/google-gemini/gemini-cli/issues/2465) is
closed. OpenCode reads LSP servers from the `lsp` key of its configuration, "To enable all
built-in LSP servers, set lsp to true." and "Use an object to keep built-ins enabled while
configuring overrides or custom servers." ([LSP servers](https://opencode.ai/docs/lsp/)). Its
rows count the configuration file where `lsp` is `true` or an object. The Agent Plugins
specification leaves LSP servers "outside the v1 format". <!--s:collection.github_lsp_json_repositories:Words-->Ten<!--/s--> repositories of the
development dataset have a `.github/lsp.json` at the root. In the collection started on 2026-09-27, a repository had a
`.lsp.json` at its root with no plugin manifest beside it. No row counts that file, because both tools read it only
at a plugin's root.

### The files of a plugin that Copilot CLI reads

For the alternative files of a plugin's hooks and LSP servers, Copilot CLI requires a file that
passes the component's schema, and valid JSON is not enough. When the CLI rejects the first
file, it reads the next alternative. `{"hooks": {}}` and `{"lspServers": {}}` are valid and stop
the fallback, while `{}` satisfies neither schema. The MCP loader accepts an empty JSON object.
The CLI keeps the default hook files when the inline hook entries are invalid, and valid empty
event lists replace those files. The pipeline's predicates follow the schema of the shipped CLI
and what its loader returned for local test files. They leave out the policies for HTTP hooks
that depend on the host.

GitHub's pages and the behavior of Copilot CLI differ for a plugin's files, and the rows follow
the CLI. The code of the CLI was read on 2026-09-30 in the npm releases 1.0.6 and 1.0.63, which
ship plain JavaScript. It was also read in the Linux runtime of 1.0.89 and of the prerelease
1.0.90-5, which ship a compressed payload that has to be unpacked. Where the code was ambiguous,
the current CLI was then run against small test plugins. The listing commands below call no
model:

```bash
npm pack @github/copilot-darwin-arm64@1.0.89    # or the package for your platform
tar xzf github-copilot-darwin-arm64-1.0.89.tgz  # the CLI is package/copilot
export COPILOT_HOME="$(mktemp -d)"              # keeps your own ~/.copilot unchanged
package/copilot plugin install ./test-plugin    # one plugin per case, with a .plugin/plugin.json
package/copilot skill list                      # also lsp list, mcp list, instruction list
```

In CLI 1.0.90, agent discovery uses the native loader `configLoaderLoadPluginFeatureForInstalled`
with the feature `agents`. The prompt-mode command `copilot --agent does-not-exist -p x` checks
authentication before it lists agents and cannot list agents without authentication.

The rule loader of 1.0.63 is native. Node can load that release's
`prebuilds/<platform>/runtime.node` and call `configLoaderLoadPluginRules` on a folder of test
files. The function returns the rules that it loaded and a warning for each file that it
rejected.

The CLI checks a legacy plugin's manifests in the order that the
[reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)
gives, "Legacy plugins: .plugin/plugin.json, plugin.json, .github/plugin/plugin.json, or
.claude-plugin/plugin.json (checked in this order)" (read 2026-10-28). A `plugin.json` at the
plugin's root makes a Copilot plugin in this pipeline only where its content is that of a legacy
manifest (below). Where `.plugin/plugin.json` or `.github/plugin/plugin.json` makes the directory
a Copilot plugin, its manifest is the first of `.plugin/plugin.json`, `plugin.json` and
`.github/plugin/plugin.json` that exists (`paths._manifest`), and the root `plugin.json` is a row
of `plugins.csv` only with that content. Copilot CLI 1.0.101, run with `--plugin-dir` on
2026-10-21, read the default `skills/` of a plugin with a root `plugin.json` that sets no
`skills` key, and not the skills that its `.github/plugin/plugin.json` names. If the first manifest is invalid, the
installation fails, and the CLI does not use the next manifest's keys. The lookup therefore stops at
the first manifest that exists. In `tests.test_paths.TestHowAManifestKeyCombines`,
`test_copilot_prefers_the_plugin_manifest_to_the_github_manifest` covers a valid and an invalid
second manifest, and `test_copilot_does_not_read_keys_from_a_second_manifest_after_a_parse_failure`
an invalid first manifest.

A root `plugin.json` that declares the Agent Plugins schema determines how Copilot CLI reads the
plugin: "A root plugin.json that targets Agent Plugins takes precedence over .plugin/plugin.json and
.claude-plugin/plugin.json per spec §5.1". The CLI then reads the portable `skills/` and `mcp.json`,
and it "reads its client-specific components from the com.github.copilot directory": `agents/`,
`commands/`, `rules/`, `hooks/hooks.json` and `lsp.json` ([CLI plugin
reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference),
read 2026-11-07). The `<agent-plugin>` rows match these parts. `paths.plugin_components` reads
no `<copilot-plugin>` place under such a root, just as it reads no `<codex-plugin>` place there,
and it returns the portable `skills/` and `mcp.json` as parts of Copilot as well. Copilot CLI
1.0.101 read `skills/` and
`com.github.copilot/commands/` of such a plugin, with `.plugin/plugin.json` or
`.github/plugin/plugin.json` beside the root manifest, and not `commands/` or the skills that the
legacy manifest names (`TestACopilotPluginWithAnAgentPluginsManifest`). The legacy manifest stays
a row of `plugins.csv`, because the detection step counts a manifest by its path, as it counts a
manifest that does not parse. No repository in the file lists of the October 5–6 collection has a
root `plugin.json` beside either Copilot manifest.

A legacy plugin's LSP servers come from the first of `.lsp.json`, `lsp.json` and
`.github/lsp.json` that passes the LSP schema, and from none after it. The changelog of 1.0.6
states "Improve compatibility with the Open Plugins spec: support `.lsp.json`", and the code of
1.0.6 tries the three files in that order. The reference names only `lsp.json` and
`.github/lsp.json`. It also instructs authors to "create a lsp-config/servers.json file in the
plugin directory", a file that the CLI does not read and that no release of the CLI names. A
legacy plugin's MCP servers come from the first of `.mcp.json` and `.github/mcp.json` that
parses, even a file that declares no server, and never from a plain `mcp.json`. Its hooks come
from the first of `hooks.json` and `hooks/hooks.json` that passes the hooks schema. A row lists
such alternatives after the placeholder, such as `<copilot-plugin>/hooks.json|hooks/hooks.json`,
and `paths.plugin_components` keeps only the first of them that exists and satisfies its
component predicate.

In a legacy plugin's `agents/` and in `com.github.copilot/agents/` alike, the agents are the
Markdown files directly in the directory, including a plain `.md` beside a `.agent.md` and a
`README.md` with valid agent front matter, although the reference says "Path(s) to agent
directories (.agent.md files)". The CLI reads a legacy plugin's `commands/` without a manifest
key, that is, the Markdown files directly in it, including a `README.md`, although the reference
gives the key no default.

A plugin's rules are the `.mdc` files directly in its `rules/` or `com.github.copilot/rules/` that
open with a YAML front matter with a non-empty `description` string, beside Cursor's `alwaysApply`
and `globs`. The CLI's rule loader returns "description: Required" for a front matter without the
key, "Expected string" for a number, a boolean, null, a list or a mapping, and "missing or malformed
YAML frontmatter" for a file that does not open with one. It does not load a `.md` or
`.instructions.md` file there at all. The rows with the match mode `dir_ext_described:.mdc` count
only such files, and the detection step reads the front matter line by line in
`detection._has_description`. No GitHub page names the rules of a legacy plugin, and the code of
1.0.63 reads them from `rules/` unless the manifest names other places. vtex/skills keeps its
plugin's rules as `.mdc`. A plugin that keeps `.md` rules in `com.github.copilot/rules` is no
evidence for that row, because the CLI ignores those files.

A legacy plugin with no `skills/` directory and no `skills` key is a single skill, the `SKILL.md`
at its root. The reference says "falling back to a root SKILL.md when no skills/ directory
exists", and the code of 1.0.63 falls back only where the manifest sets no `skills` key.

The manifest keys `skills`, `agents`, `commands` and `rules` take a string, a list, or an object
with `paths` and `exclusive`. A string or a list replaces the default place, and the object adds
its paths to it unless `exclusive` is set. An empty or false value means the default place,
because the resolver of 1.0.63 opens with `if(!t)return[YB.join(r,n)]`, which also keeps the
root `SKILL.md` of a plugin with an empty `skills`. The resolver rejects an object with a `paths`
value other than a list of strings, with "Invalid ${n} object form: expected { paths: string[] }",
and such an object names nothing. A listed skills directory that contains a `SKILL.md` is that
one skill, and discovery stops beneath it. A listed directory without its own `SKILL.md` is
searched for skills, and child skills listed separately remain explicit declarations. A listed
agent file is read on its own, and a path that leaves the plugin makes the installation fail.

Where a `hooks` file that the manifest names exists, it is read instead of the default files. A
valid inline hooks object also replaces the default files, and a hooks path that leaves the
plugin prevents any hooks from loading. Invalid inline hooks or a missing file keep the defaults.
An `mcpServers` or `lspServers` file is read only where no default file satisfies the component
predicate.

Cursor's [plugin reference](https://cursor.com/docs/reference/plugins) specifies agents, commands,
hooks and a root skill in addition to rules, skills and MCP configuration, and its manifest
component fields replace the default discovery. Cursor reads rule and agent files with `.md`, `.mdc`
and `.markdown`, and plugin commands also with `.txt`. It treats a root `SKILL.md` as a skill only
when the plugin has no `skills/` directory and no `skills` field in its manifest. Cursor CLI
2026.09.28-64d2043 merges both `.mcp.json` and `mcp.json` at the plugin root, reads project plugin
settings from `.cursor/settings.json`, and accepts the same three extensions for project subagents.
The CLI's project command loader reads only the `.md` files directly in `.cursor/commands`, while
the IDE reads them at any depth. A Cursor staff member wrote on the forum on 2026-03-16 that "the
IDE recursively scans subdirectories within .cursor/commands/, but the CLI currently only reads
top-level .md files" ([forum](https://forum.cursor.com/t/154719)). The row matches them at any
depth, because the IDE, a product of the tool, reads them there. The rows grounded in the CLI's code
link the immutable official CLI package in `evidence`. In that package, `index.js` contains the
plugin and settings loaders, and `1623.index.js` contains the command loader.

### The places that a plugin's manifest names

Claude Code, Copilot CLI, Cursor, Codex and Antigravity plugin manifests can name places of their
components beyond or instead of the default places. Claude Code's reference states "Every
component key accepts a path relative to the plugin root" and "Every component path must resolve
inside the plugin root and must exist"
([plugins reference](https://code.claude.com/docs/en/plugins-reference)). In a
Claude Code manifest, `skills` "Adds to the default", and `hooks`, `mcpServers` and `lspServers`
merge with the default file. `commands`, `agents`, `outputStyles` and `workflows` replace the
default place. The `agents` key takes "Agent .md files. Directories aren't accepted", and
`claude plugin validate` of Claude Code 2.1.285 rejects a directory there with "agents: Invalid
string: must end with ".md"". A plugin "with SKILL.md at its root, no skills/, and no skills key
loads as a single skill", as a Copilot CLI plugin does.

In a row with the match mode `manifest_field:k:h`, the key `k` names places of a component, and
the path of the row is the default place of the component. The steps read the named places as
the row of the default place matches that place. `h` specifies how the named places combine with
the default place:

| `h` | What the tool reads once the key is set | Keys |
|---|---|---|
| `adds` | the default place and the named places | Claude Code's `skills`, Antigravity's `skills` |
| `merges` | the default file and the named files | Claude Code's `hooks`, `mcpServers`, `lspServers` |
| `replaces` | the named places alone | Claude Code's `commands`, `outputStyles`, `workflows`; Cursor's `skills`, `rules`, `agents`, `commands`, `hooks`, `mcpServers`; Codex's `skills`, `mcpServers`, `apps`, `hooks`, and `hooks` of a portable plugin |
| `files` | the named files alone, and no directory | Claude Code's `agents` |
| `paths` | the named places alone for a string or a list, both for `{"paths": [...]}` unless `exclusive` | Copilot CLI's `skills`, `agents`, `commands`, `rules` |
| `precedes` | a named file that exists, or valid inline hooks, instead of the defaults, and a path that leaves the plugin loads no hooks | Copilot CLI's `hooks` |
| `follows` | a named file, only where no default file parses | Copilot CLI's `mcpServers`, `lspServers` |

Tests cover every replacement key of Claude Code (i.e., `commands`, `outputStyles` and
`workflows`) with both default and declared files present. A schema-valid `plugin.json` inside a
vendor manifest directory remains that vendor's manifest, not an additional portable plugin.

An Antigravity plugin's manifest can list skills under `skills`. The changelog for CLI 1.3.2
reads "list skills in plugin.json", and the changelog for App 2.27.0 reads "Fixed an issue where
plugins that list their skills as a single folder path failed to load"
([changelog](https://antigravity.google/docs/changelog), read 2026-10-28). The manifest schema
on the [Build plugins](https://antigravity.google/docs/build-plugins) page gives the key the type
"String or String[]" and reads "Extra skill directories, relative to <plugin-root>, loaded in
addition to the default skills/ folder" (read 2026-11-01). The page does not state whether an
entry can name a `SKILL.md` or a folder of skill folders, or where an entry resolves when the
plugin root has no such path. Antigravity CLI 1.4.9 decides them. It is the release that the installer's manifest for linux_arm64 named on 2026-10-28. Its
archive, `cli_linux_arm64.tar.gz`, has the SHA-512 that the manifest states and the SHA-256
`0e75190d5aa48b0f7623b201cfef542cd36a1279b3d28afa7fa099983a073493`. It contains a stripped Go
program, which was read as data and not run. The program's function table names its functions,
and their disassembly shows the strings that each function uses and the order in which it reads
the places:

- `customizations.(*stringOrSlice).UnmarshalJSON` decodes `null` as no entry, a value that
  starts with `"` as one entry, and any other value as a list of strings. The changelog that
  the CLI ships reads, for CLI 1.4.5, "Fixed plugins failing to load when `plugin.json` gives
  `skills` as a single path string".
- `customizations.readPluginItem` reads the manifest through `hujson.Standardize`, which accepts
  comments and trailing commas. It applies `strings.TrimSpace` and then `strings.TrimRight` with
  `/` to each entry, and it leaves out an empty entry with "Ignoring empty skills[%d] in %s".
- `customizations.discoverPluginCustomizationPaths` reads the skills of a plugin, as it reads its
  agents and rules. It passes the entries to `resolveJSONConfig` with the plugin root as the
  base, and that function also resolves the entries of the plugin's `skills.json`, which
  `loadConfigFile` reads. It then reads `skills.json` and the `skills/` directory whatever the
  manifest lists, removes the duplicates by their canonical path, and leaves out what the
  top-level `exclude` of `skills.json` names. The key therefore adds to `skills/`, and an entry
  resolves as an entry of the plugin's `skills.json` does ([Configuration artifact detection](#configuration-artifact-detection)):
  against the plugin root, against the workspace root where nothing exists there, and as a
  skill folder, its `SKILL.md` or the skill folders directly in another folder. `agy plugin
  validate` names the same forms, "skills[%d]: %q adds no skills: list a SKILL.md, a folder with
  one, or a folder of such folders" (`plugins.(*pluginValidator).checkManifestSkill`).
- `customizations.(*Manager).processPluginSkills` ignores the key of a manifest that declares
  the Agent Plugins 1.0.0 `$schema` while the `enable-agent-plugins-spec` flag is on, with the
  message "listing skill directories in plugin.json is not compliant with the Agent Plugins
  spec 1.0.0". The validator adds "Put skills in skills/<name>/SKILL.md, which loads either
  way". `customizations.(*Manager).checkAgentPluginsSchema` reads the flag at run time, and the
  steps read the key of every manifest, as the CLI does while the flag is off.

The row `<antigravity-plugin>/skills` with `manifest_field:skills:adds` reads the key in this way
(`paths._antigravity_manifest_skills`). A listed skill with the plugin root or a folder above it
as its folder is a root skill, its `SKILL.md` with its support directories, as a registered root
skill is. The three rows of the manifest, `<antigravity-plugin>/plugin.json`,
`<antigravity-plugin>/.antigravity-plugin/plugin.json` and
`<antigravity-plugin>/.gemini-plugin/plugin.json`, have the format `jsonc_trailing`. The plugins CSV
lists <!--s:collection.antigravity_plugin_manifests:words-->four<!--/s--> manifests of Antigravity plugins, in
<!--s:collection.antigravity_plugin_manifest_repositories:words-->two<!--/s-->
repositories. The file lists and the collected files of the October 5–6 collection contain no
`plugins.json` of a customization root, and none of the manifests of that collection sets
`skills`.

A `plugin_root_skill` row matches the root `SKILL.md` of a plugin with no `skills/` directory and
no `skills` key. The skill includes the `scripts/`, `references/` and `assets/` directories beside
it and nothing else of its folder. The [Agent Skills
specification](https://agentskills.io/specification) names these directories as optional, and
they are the columns of `skills.csv`. That folder is the plugin root, and its other files are the
plugin's other parts. For a plugin at a repository's root, the folder is the whole repository.
When `SKILL.md` is at the plugin root, an explicit `skills` entry that names `.` or `./` is
collected the same way: The cloning step copies that file and its support directories and no
unrelated source files or dotfiles. The collected file cleanup keeps the root skill, and the
detection step writes one skill row. The declaration can also name child skills, which keep their
own directories. A plugin inside a skills directory is also a skill of that directory and
includes its whole folder, as with the plugins that kvcache-ai/Mooncake keeps under
`.claude/skills/<name>/`.

`paths.plugin_components` applies these readings for every plugin, and every step calls it. It
returns the default places that a tool reads, the first of a list of alternatives, what a
manifest key names, and the root skill. It splits a named place into what the row of the default
place matches: the skill folders at or below a skills directory, the files of a directory with
the extensions and the depth of that row, or one file that has to pass the content test of that
row. The cloning step copies those places, the collected file cleanup keeps them, and the
detection step lists them under the mechanism of the component. `paths.plugin_components` does
not follow a place that leaves the plugin root, that a symlink leads out of the repository, or
that does not exist, nor a URL or an `.mcpb` or `.dxt` bundle. Inline configuration names no
place. Where a Claude Code or Codex manifest configures servers or hooks inline, or a Cursor manifest
configures MCP servers inline, `paths.plugin_components` returns the manifest with the kind `inline`,
and the detection step lists the manifest under the mechanism of the component.

`TestDeclaredPluginParts` checks absolute and parent-relative paths in a plugin at the
repository root, a sibling with a name that starts with the plugin's name, and the collection of
a file outside the plugin. It checks the constraints on path resolution separately from the final
symlink check, so that neither check can hide the removal of the other.

In the collection started on 2026-09-27, manifest keys name component places only in Claude Code
manifests. In jasonxudeveloper/jengine, the named places are skills, one
folder each under `.claude-plugin/skills`, which were `other` rows. In melonjs/melonjs, they are
the skills under `packages/melonjs/skills`, and in vercel/next.js, which has its `skills/`
directory as the plugin root, they are skills too. In ecomfe/tempad-dev and
afnanenayet/diffsitter, they are hooks files. The plugins of TrafficGuard/typedai name `../hooks/hooks.json`. These
declarations fail Claude Code validation, and none of the components of these plugins load, not
even their defaults. No manifest in that collection sets a key that replaces a default place. The skill of
the plugin under `src/harlequin/hsql/skill` in tconbeer/harlequin is its root `SKILL.md`.

Claude Code's plugins also contain themes, monitors and executables in `bin/`. Gemini CLI reads a
custom theme from its settings, which are collected already, and none of the other tools
documents monitors or plugin executables. These components have no rows.

The rows for the parts of a plugin start with a placeholder segment, `<claude-plugin>`,
`<cursor-plugin>`, `<codex-plugin>`, `<copilot-plugin>`, `<gemini-extension>`,
`<antigravity-plugin>` or `<agent-plugin>`, which stands for every directory that contains the
corresponding manifest. An Antigravity plugin can be discovered in a default directory or
registered through `plugins.json`. Its default directories are `plugins/<name>/` under each of the
Antigravity customization roots listed in `heuristics.PLUGIN_PARENTS`.
`heuristics.PLUGIN_ROOTS` names the manifests, and `paths.plugin_roots` finds the directories,
including the directory of a `plugin.json` with the content of a legacy Copilot CLI
manifest (below).
The cloning step copies each plugin's manifest and parts, and the collected file cleanup keeps
them. The detection step lists each part under its mechanism and gives its repository the tool
of the manifest that the plugin contains. The parts of a directory that contains the manifests of
two tools are attributed to both tools. A portable manifest names no tool: Its shared parts are
attributed to no tool by the manifest alone, and the Copilot parts under `com.github.copilot/` are
attributed to Copilot. Codex and Copilot CLI read a portable plugin through its portable manifest,
and its `skills/` and `mcp.json` are also attributed to each of them where another file of the
repository makes the plugin that tool's: the tool's manifest beside the portable one, as
saadeghi/daisyui's root holds `.codex-plugin/plugin.json`, or a marketplace of the tool that lists
the folder, as marionettejs/marionette's `.agents/plugins/marketplace.json` lists
`plugins/marionette`. The row of the portable manifest keeps the tool `any`. Where Codex or
Copilot CLI reads another vendor's manifest as its own, in a folder that the tool's marketplace
lists and that has no manifest of the tool's own and no portable manifest, that manifest is
attributed to the tool as well (`paths.borrowed_manifest`), such as obsei/obsei's
`integrations/claude/obsei/.claude-plugin/plugin.json` to Codex. Every other manifest that the
detection step lists with its plugin's tool is attributed to that tool alone, also where it is in
a part of another plugin. In a skill folder of a Claude Code and
Cursor plugin, the root `SKILL.md` of a legacy Copilot CLI plugin is a skill of the three plugins,
and its `plugin.json` is Copilot's. A file at the root that a repository-level row matches keeps
the tools of that row and gets the tool of a plugin at the root beside them, unless a `root_file`
row with the tool `any` matches it. Every tool that reads the repository's MCP configuration
reads the root `.mcp.json` under `mcpServers` or in the bare form, and the file therefore keeps
`any` in CopilotKit/CopilotKit, a Claude Code plugin at the root, and in
wonderwhy-er/DesktopCommanderMCP, a Gemini extension and a legacy Copilot CLI plugin at the root.
A root `.mcp.json` with its servers under `servers` alone is Copilot's, because VS Code reads the
root file wrapped in `mcpServers` or in the bare form (`parseWorkspaceRootMcpConfiguration` of
[`mcpWorkspaceConfiguration.ts`](https://github.com/microsoft/vscode/blob/419dc8780af94fb0f8c02f8ae52bd2e31ac3b99d/src/vs/platform/mcp/common/mcpWorkspaceConfiguration.ts)),
Copilot CLI and Claude Code read `mcpServers`, and only Visual Studio reads `servers` there.
The skills of DesktopCommanderMCP's `skills/` are attributed to both tools, because Gemini CLI
reads an extension's `skills/` and Copilot CLI reads a legacy plugin's, "Legacy plugins: skills/
(default, overridable in manifest)".

A `skills/` directory without a manifest beside it belongs to no plugin. A `plugin.json` without
the Agent Plugins schema is the default layout of a legacy Copilot CLI plugin: "All plugins
consist of a plugin directory containing a manifest file named plugin.json", and the
[reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)
(read 2026-10-28) shows `my-plugin/` with `plugin.json`, `agents/`, `skills/`, `hooks.json` and
`.mcp.json`. Unrelated programs use the filename too, and Copilot CLI installs any directory with
a `plugin.json` that parses. The code of 1.0.63 reads the first manifest that exists,
`s=JSON.parse(o)`, with `strict:!1`, and takes a missing name from the directory. Whether a
`plugin.json` makes its directory a legacy Copilot CLI plugin therefore depends on its content,
which `paths.copilot_legacy_manifest` tests:

- The manifest is a JSON object without the Agent Plugins schema, and it is not in a vendor's
  manifest directory or in an Antigravity plugin, where a `plugin.json` is the manifest of that
  vendor or of Antigravity.
  A directory named `.<name>-plugin`, such as `.claude-plugin`, `.cursor-plugin` or
  `.grok-plugin`, is a vendor's manifest directory, also for a vendor that the dataset does not
  cover, and the plugin root is its parent (`paths._VENDOR_MANIFEST_DIRECTORY`). The reference
  puts a legacy manifest directly in the plugin directory, as in its layout of `my-plugin/`, or
  in a manifest directory of the plugin root, "Legacy plugins: .plugin/plugin.json,
  plugin.json, .github/plugin/plugin.json, or .claude-plugin/plugin.json". saadeghi/daisyui keeps
  a Grok plugin in its `skills/` directory. The plugin's manifest,
  [`skills/.grok-plugin/plugin.json`](https://github.com/saadeghi/daisyui/blob/bf3bac3a397b543e55641ecb2d60a2ed961f09ec/skills/.grok-plugin/plugin.json),
  sets `"skills": "./"`, and the root
  [`.grok-plugin/marketplace.json`](https://github.com/saadeghi/daisyui/blob/bf3bac3a397b543e55641ecb2d60a2ed961f09ec/.grok-plugin/marketplace.json),
  "Official daisyUI plugins for Grok Build", gives the plugin the source `"path": "./skills"`.
- `name` is the required field, "Kebab-case plugin name (letters, numbers, and hyphens only).
  Maximum 64 characters."
- A component path field names a place of the plugin that exists, as the reference describes
  these fields: "These tell the CLI where to find your plugin's components." The field is
  `agents`, `skills`, `commands` or `rules` as a path, a list of paths or `{"paths": [...]}`, as
  the CLI's resolver reads them, or `hooks`, `mcpServers` or `lspServers` as a path. The
  reference's table of fields leaves out `rules`, which the CLI reads as it reads `commands`. `extensions` is left out, because Grafana's `plugin.json` has an object of
  its own under that key and no row reads an extension of Copilot CLI. An inline hooks, MCP or
  LSP object names no place, and a manifest with only such an object is left out, because the
  key alone does not distinguish it from another program's key of the same name.

The test is the content that the reference documents, as the `$schema` is for the Agent Plugins
manifest. The row `<copilot-plugin>/plugin.json` counts the manifest, and the rows of the
placeholder then read the plugin's parts. Of the `plugin.json` files in the file lists of the
October 5–6 collection that are outside the vendors' manifest directories, the vendored
directories and Antigravity's plugin directories, with no other manifest beside them, these pass
the test at each repository's collected commit:
`plugins/mt-migration/plugin.json` in dotnet/msbuild, which its `.github/copilot/settings.json`
enables as `mt-migration@dotnet-msbuild`, its copy under `src/msbuild/` in dotnet/dotnet,
`.github/plugin.json` in dotnet/maui and the two plugins under `ts/packages/` in
microsoft/TypeAgent, which `ts/.github/plugin/marketplace.json` lists. The October outputs list
none of these manifests, and of their parts only the two `.mcp.json` of TypeAgent. The
skills and agents of dotnet/maui's plugin are its repository's own `.github/skills` and
`.github/agents`. Other files set a key of the same name in another form, which the test
refuses: Grafana's `extensions` objects, the `hooks` lists of NodeBB and ChurchCRM, Explorer++'s
list of `commands` and a list of `skills` objects in keplergl/kepler.gl. google/perfetto's
`ai/extensions/codex/plugin.json`, a Codex manifest beside no `skills` directory, names a place
that does not exist. `not_configuration.csv` records the other programs' files.

Copilot CLI reads Claude Code's manifest directory too, as the last of its legacy manifests. A
Claude Code plugin is also Copilot's where a Copilot CLI marketplace of the repository lists it.
Azure/azure-sdk-tools's `plugins/azure-sdk-tools`, which only its `.github/plugin/marketplace.json`
lists, is Copilot's for the parts that Copilot CLI reads, and its `.claude-plugin/plugin.json` is a
row of Claude Code and Copilot. Without a marketplace of Copilot CLI or Codex that lists it, a
Claude Code plugin is attributed only to Claude Code, the tool of its manifest. A Claude Code
marketplace does not make the plugin a Copilot plugin, because `.claude-plugin/marketplace.json`
is Claude Code's file, although Copilot CLI reads it where it finds no marketplace of its own.

No page names `.claude/plugins/*/` or `.agents/plugins/*/` as a place of a plugin's parts, and no
row names them. Every part there that the March collection matched is beside a manifest, where a
placeholder row matches it as well. The skills of `.agents/plugins/cluster-provisioning/` in
opendatahub-io/notebooks have no manifest beside them, and the repository has no marketplace file.
Codex does not load them, and neither does Antigravity, which needs a `plugin.json` for a plugin.

In the collection that the checkout tracks, `plugins.csv` lists <!--s:collection.plugin_and_extension_manifests-->433<!--/s--> `plugin.json` and
`gemini-extension.json` manifests in <!--s:collection.plugin_repositories-->252<!--/s--> repositories, and <!--s:collection.plugin_and_extension_manifests_elsewhere-->423<!--/s--> of them are outside
`.claude/plugins/` and `.agents/plugins/`, such as under `plugins/<name>/`.

## The two negatives

`config_path_patterns.csv` defines what configuration is, and `not_configuration.csv` records
what it is not, for the path globs of the files in `other.csv`. `ai_commit_patterns.csv` defines
which commits credit one of the six tools, and `not_ai_commits.csv` lists the attributions that
belong to other automation. `agentcontext/checks/unmatched.py` reads the two negatives and lists
everything else.

`not_configuration.csv` has `path`, `tool`, `source`, `doc_url`, `doc_verified`,
`evidence` and `notes`. `not_ai_commits.csv` has the same columns without `tool`, keyed on
`pattern`, which is matched inside the attribution that the cloning step recorded.

`source` is `doc` where a page states that the tool does not read the file, `sample` where the
path glob was derived from the collected files, and `dropped` where it was derived from a
repository that the collected file cleanup removed because it matched no mechanism. `dropped`
exists because such a repository reaches no output CSV. `unmatched paths` finds the glob in the
collected tree, and `other.csv` never lists it.

A row belongs in `not_configuration.csv` when the reason is permanent: a page that states that
the tool does not read that extension or that location, a name that imitates a mechanism where
no tool looks, or version-control housekeeping. A glob that is one project's own directory name
does not belong there, however small it is. `.claude/thoughts/`, `.claude/workflow/` and the
other paths that occur in only one repository in the March sample get no row in
`not_configuration.csv` on purpose, so that `unmatched paths` lists them again if a second
project adopts the same name.

A row can also hold a file that a tool reads but that a maintainer decision leaves uncounted.
`.claude/skills/**` and `.agents/skills/**` hold the Markdown files with a description in their
front matter directly in those directories, which OpenCode 2 reads as skills, under the decision of
2026-11-07 ([Configuration artifact detection](#configuration-artifact-detection)). The path recall
of the detection accuracy evaluation lists such a file in `paths`
([the labels](tools/evaluation.md#the-labels)), and not a file there without a description, such
as a `README.md`, which the rows of `.opencode/skills` do not count either.

A row names a place in a tool's directory, such as `.claude/memory/**`, and `unmatched paths`
applies it wherever a repository keeps that directory, also below the root, as in readest/readest's
`apps/readest-app/.claude/memory/`. A row therefore names no directory of one project above a
tool's directory, such as `frontend/.claude/**`, and `tests/test_checks_unmatched.py` checks
that none does ([the inputs of the check](checks/unmatched.md#inputs-and-findings)).

`not_configuration.csv` has no row for a folder below `.github/workflows/`, such as the
`.github/workflows/copilot/` that labri-progress/agent-impact matches. Because a path glob keeps
<!--s:parameters.agentcontext.checks.unmatched.glob_for.segments:words-->two<!--/s--> segments, the row would be `.github/workflows/**`. `unmatched paths` would then no
longer list any workflow file that no path pattern matches in the repositories that the cleanup
dropped, including an agent workflow that a heuristic misses. `deleted-files-report` would count
such a file as named by a heuristic.

The two negatives are files rather than lists in the code for the same reason as the positive
heuristics. A list in the code records each path with a sentence beside it, and nothing shows
whether a vendor page grounds the sentence, when anyone last read that page, or whether the
entry still matches anything.

`source` splits the negatives the way it splits the commit patterns. A `doc` row cites the page
that states that the tool does not read the file, and that claim can become outdated.
`.cursor/rules/**` is a negative because Cursor's rules page requires `.mdc`, and if that page
stops requiring it, the row is wrong. Of the <!--s:heuristics.not_configuration.rows-->238<!--/s--> path rows, <!--s:heuristics.not_configuration.doc_rows-->196<!--/s--> are `doc`. A `sample` row was
derived from the collected data, and its `notes` is the only provenance that it will ever have.
Examples of `sample` rows are a repository's own `.claude/docs/`, the scratch plans that an agent
writes and the symlink markers that the March collection could not follow.

In `not_ai_commits.csv`, a `doc` row cites the page that describes what the automation behind
the attribution does, because what it does shows that it is not a coding agent. Dependabot,
for example, "raises a pull request to update the manifest to the latest version of the
dependency", pre-commit.ci "fixes the issues automatically", and Crowdin "sends the pull request
with translations". Each row's `notes` quotes the sentence, so that you can read what the page
stated without fetching it. Of the <!--s:heuristics.not_ai_commits.rows-->21<!--/s--> rows, <!--s:heuristics.not_ai_commits.doc_rows-->12<!--/s--> are `doc`. The other <!--s:heuristics.not_ai_commits.sample_rows:words-->nine<!--/s-->,
such as `github-actions[bot]` and `openhands-pr-arena[bot]`, are `sample` rows, because the
collected commits show what the attribution marks. The cloning step recorded `github-actions[bot]` on
commits that a repository's own workflows push. GitHub's pages document the `GITHUB_TOKEN` that a
workflow authenticates with and the app that it authenticates as, and they name no account. No
page can state what those commits contain, because the repository's own workflows determine their
content. The attribution therefore names no coding agent. `openhands-pr-arena[bot]` is the app
of neulab's PR Arena, which benchmarks coding assistants and is not the app of All Hands AI,
OpenHands' vendor. Its page states that installing it adds a workflow file, and in the commit logs
of the October 5–6 collection, the app's only commit adds that file: an installation, not the
change of a coding agent.

Because their `doc` rows cite pages, the watch reads both negatives, with
`python3 -m agentcontext docs --heuristics heuristics/not_configuration.csv --label-columns
tool,path` and `--heuristics heuristics/not_ai_commits.csv --label-columns pattern`.
`tests/test_checks_unmatched.py` checks that every path row has a note, a page or evidence and
that its path has the form of a path glob. A path row need not match the current collection,
but each commit row must match an attribution in the tracked archive of the current collection.

A collection's `output/all_ai_commits-unmatched.csv` is <!--s:collection.unmatched_csv_mb-->54<!--/s--> MB, every run rewrites it, and most of its
lines name people, so it stays in the handover of the run. The tracked unmatched attributions
contain only the lines of it that name a bot or a tool and no person
([what the archive keeps](checks/unmatched.md#what-the-archive-keeps)), with the <!--s:parameters.agentcontext.checks.unmatched.UNMATCHED_COLUMNS_READ:words-->four<!--/s--> columns that
`unmatched commits` reads, as `output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z`, named for the
start day of the collection. The tracked archive is <!--s:collection.unmatched_archive_kb-->36<!--/s--> KB and contains <!--s:collection.unmatched_attributions-->2,960<!--/s-->
unmatched attributions. The test matches each row against that archive as the check does, as a
substring of the attribution, and each of the <!--s:heuristics.not_ai_commits.rows-->21<!--/s--> rows matched at least one attribution.
After each collection, run `python3 -m agentcontext unmatched reduce`, which replaces the archive,
and commit the new archive, as `docs/checks/unmatched.md` describes. Until the archive is
replaced, a row derived from a later collection fails the test if the tracked archive lacks its
attribution.

## Imports

An import names a file that a tool loads into context automatically with the configuration artifact
that contains it, such as `@docs/style.md` in a CLAUDE.md. An imported file can import further
files. The imports are an extension of the dataset beside the configuration artifacts. Because an
imported file sets no tool flag, adds no repository and is in no mechanism CSV, the import
heuristics are files of their own: `import_patterns.csv` with <!--s:heuristics.import_patterns.rows-->30<!--/s--> rows and `not_imports.csv` with <!--s:heuristics.not_imports.rows-->17<!--/s-->.
`agentcontext/imports.py` loads both and parses the imports, and its module docstring defines the
columns. The module is kept out of `agentcontext/heuristics.py`, because the code of that file is
part of the `classified_by` digest of the commit heuristics.

A row names its importing files with the path and the match mode of a row of
`config_path_patterns.csv`, and a chain of imports therefore starts only at a collected file. The
row also states the syntax, what a relative path resolves against, how many hops the tool reads, the
limits of one chain and the extensions of the files that the tool reads. Each row is grounded as a
path pattern is. `doc_url` cites the vendor page, and `notes` quotes the text that grounds the row.
Where the page is silent, `notes` gives what the vendor's shipped code does or what the tool read
when it was run on test files, with the package or the commit in `evidence`. The documentation watch
reads both files with `--label-columns id`. `tests/test_imports.py` checks the grounding of every
row, that every importing file is a configuration artifact, and that no negative starts a chain of
its tool and syntax.

### The rows of each tool

Claude Code reads `@path` in every CLAUDE.md, `.claude/CLAUDE.md`, CLAUDE.local.md and rule of
`.claude/rules`, and in an AGENTS.md "only when you have no `CLAUDE.md` in your working directory or
above it" ([memory](https://code.claude.com/docs/en/memory), read 2026-10-19). Below the working
directory, a CLAUDE file leaves out the AGENTS.md of its own directory but not the AGENTS.md of a
directory below it. The rows therefore read the imports of an AGENTS.md where neither the root nor
its own directory has a CLAUDE file. The page says that Claude Code reads nothing "under a
`.agents/` directory". Claude Code 2.1.303, run offline against a mock of the API, loaded an
AGENTS.md and a CLAUDE.md there with their imports, and the rows follow the code. The page states
four hops, relative paths against the importing file, and that "Import parsing skips Markdown code
spans and fenced code blocks". The rest is what Claude Code 2.1.303 does. It lexes the file with the
`marked` that it bundles, `gfm` off, and reads the text of each token outside code and HTML. Inside
a list item it also reads the item's text as written, code spans included. It cuts a `#fragment`,
resolves against the directory of the file that a link leads to, and reads only the extensions of an
allow-list. `claude_at` follows this code and reproduces the lexer's blocks and inline spans without
a Markdown library.

Claude Code Review includes a file where a line of the root REVIEW.md holds "only @ and the file
name" ([customize Code Review](https://code.claude.com/docs/en/code-review-customize), read
2026-10-28). The file has to be at the root beside REVIEW.md, and the name has to match the
file's name in case and contain no space. Code Review reads one level and at most <!--s:imports.code_review.max_files:words-->eight<!--/s-->
different files, and it keeps REVIEW.md and the files that it includes "under <!--s:imports.code_review.byte_limit-->150,000<!--/s--> bytes
combined". A file that "would reach the byte limit is skipped whole". `code_review_include` reads
such a line, and `claude-import-07` reads one hop. Its condition `importing_file_bytes` counts
REVIEW.md toward `max_bytes`. `max_bytes` is one byte below the page's limit, because the bytes
have to stay below that limit. The chain compares a name with the entries of the directory, so
that a name in another case than the file's is `missing` also on a file system that ignores case,
such as the default file system of macOS.

A line can also include a skill: "Add @ and the path to SKILL.md, in the form
.claude/skills/<folder>/SKILL.md, on a line of its own", "with the skill's folder directly under
skills/" and with a folder name without spaces. Any other file under `.claude` is no include:
"You can't include those directly." Unlike the line of a file, a skill line includes nothing
where it is inside a code fence, or inside an HTML comment or a `<pre>`, `<script>`, `<style>` or
`<textarea>` block that a line above it opened and did not close.
`code_review_include` delimits these blocks as CommonMark delimits a fenced code block and the
HTML blocks of the kinds 1 and 2, which end only at their closing line. The skill file counts
toward both limits above, and it "has its own limit of <!--s:imports.code_review.skill_byte_limit-->30,000<!--/s--> bytes."
Code Review reads at most <!--s:imports.code_review.skill_lines:words-->eight<!--/s--> skill lines, and "A repeated line counts again toward the eight".
A skill beyond either limit is `over_limit`. Code Review "doesn't follow a symbolic link, whether
the link is the file or a folder in its path", and a skill with a link on its path is therefore
`not_read`. The chain compares each name of the path in its case, and "a path of this form that
matches no file" is `missing`. Code Review reads none of the skill's own imports, because it
"doesn't load scripts, linked files, or `@file` references". These limits are constants of
`imports.py` that the parser sets on each skill that it finds (`Import`), because they hold for the
skills alone and the row's limits hold for all of its files. No root REVIEW.md of the October
5–6 collection includes a file or a skill.

Where the page leaves a question open, the row takes these readings:

- Code Review includes the files and the skills only when it reads REVIEW.md from a protected
  branch, and on an unprotected default branch the line stays as text. The dataset does not record whether a branch is
  protected. The row therefore reads the included files as Code Review reads them from a protected
  branch, just as the rows read the context files of a directory below the working directory, which
  a tool reads only when it works there ([The copy and the step](#the-copy-and-the-step)).
- Past <!--s:imports.code_review.max_links-->64<!--/s--> Markdown links across REVIEW.md and the files that it includes, the review skips
  the whole root REVIEW.md. The row leaves this limit out, as the rows of Antigravity leave out
  its truncation of a long rule. The page does not say which Markdown counts as a link, such as a
  link in code, an image or a reference link. The limit also determines whether Code Review reads
  the configuration artifact at all, and not whether it reads one included file.
- A file that two lines include counts once toward both limits, because the page counts
  "different files" and "the files it includes". The page skips the file that would reach the
  byte limit, and a later, smaller file can still fit.
- For a file beside REVIEW.md, the page names no exception for code and no extension. Such a line
  in a code block or in an HTML comment therefore includes a file, and the row reads a file of any
  extension. A carriage return before a line feed belongs to the line break, and a byte order mark
  at the start of REVIEW.md to no line.
- A skill line that matches no file counts toward the <!--s:imports.code_review.skill_lines:words-->eight<!--/s--> skill lines, because the limit
  counts lines and the page counts a repeated line again. A skill line inside a code fence or an
  open HTML block counts toward no limit, because it includes nothing.

Copilot CLI reads `@path` in `.github/copilot-instructions.md`, AGENTS.md and CLAUDE.md, "Copilot
CLI reads the referenced file immediately and supports references within referenced files"
([custom instructions](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions),
read 2026-10-19). The native resolver of Copilot CLI 1.0.98, run on test workspaces, gives the
rest. It reads <!--s:imports.copilot.max_depth:words-->eight<!--/s--> levels, each file once per importing file, at most <!--s:imports.copilot.max_files-->64<!--/s--> files and
<!--s:imports.copilot.max_bytes-->524,288<!--/s--> bytes per importing file, and a target of any extension with valid UTF-8 content. It
expands no import in GEMINI.md, CLAUDE.local.md or the files of `.github/instructions`, and it
reads `.claude/CLAUDE.md` and `.claude/AGENTS.md` where it starts in `.claude`. A trailing `/`
stays part of a path, and an `@` inside a path starts another import only where that path
contains no `/` and names no file. On Linux it reads
the root AGENTS.md and CLAUDE.md in any case, which the `root_any_case` rows record. A root file
that is a link resolves against the link's own directory, and an imported link against its
target's directory, which the resolution `imported_real_dir` names. Copilot CLI 1.0.101 also
expanded the imports of the `.github/copilot-instructions.md` of a directory below the root,
against that file's own directory. `copilot-import-06` reads these imports where the `nested:`
row of the file finds the file ([Copilot CLI's places below the root](#copilot-clis-places-below-the-root)).

Copilot CLI 1.0.106 changed how it reads a fence: "@-imports in AGENTS.md, CLAUDE.md and
copilot-instructions.md now follow Markdown code fence rules, so an import shown inside a nested
code example is no longer pulled into the instructions and a real import after such an example is
no longer dropped" ([changelog](https://github.com/github/copilot-cli/blob/main/changelog.md)). The
changelog does not state the details, and the native resolver of 1.0.106 and 1.0.107 decides them.
The package of such a release, such as
[@github/copilot-linux-arm64](https://registry.npmjs.org/@github/copilot-linux-arm64/-/copilot-linux-arm64-1.0.107.tgz),
holds one binary, and the asset `copilot.tgz` of the binary holds
`prebuilds/<platform>/runtime.node`. Node called `repoDiscoverInstructionSources` of that file on
test workspaces, in a container without network, on 2026-11-07. A line closes a fence only where it
starts with at least as many of the character of the fence and has only whitespace after them, and
the end of the file closes a fence as well. A run of backticks with another backtick on its line
opens no fence, and the line reads as text with a code span. In other points the resolver does not
follow Markdown. Any whitespace can indent a fence, `>` or a list marker before a run opens no
fence, and only a line feed ends a line. Its whitespace is Unicode's `White_Space`, without the
separators U+001C to U+001F that Python's `str.isspace` also accepts. These separators are text
after a fence and part of a path.

Gemini CLI reads `@file` in every GEMINI.md, in the context files that `context.fileName` of
`.gemini/settings.json` names beside it, and in an extension's `contextFileName`
([memory import processor](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/memport.md),
read 2026-10-19). The page states that `marked` finds the code, but `memoryImportProcessor.ts`
at the commit that the rows cite does not use `marked`. Its code regions are runs of backticks
closed by a run of the same length. A tilde fence, indented code and an HTML comment are
read as text. Its set of processed files is copied for each chain. A file that two
imports reach is read twice, and only a cycle stops the reading, which the condition
`each_branch` names. The rows give the imports of each file once per chain, at the smallest depth
at which Gemini CLI reads the file. Files that import each other then give as many rows as there
are imports rather than branches, and the branches grow exponentially with the five levels.

Antigravity inlines `@[label](path)` into AGENTS.md, GEMINI.md and the rules of `.agents/rules`
and its legacy places ([rules](https://antigravity.google/docs/rules), read 2026-10-19), and the
rows add the rules of a plugin and the rules that a `rules.json` registers. Only the rules with
the trigger `always_on` or `glob` load without a request. Antigravity CLI 1.4.2, run offline over
a plugin rule, read the path as written and relative to the rule, also in code, and did not
expand an `@[label](path)` in an included file. Antigravity's bare `@path` rewrites a path and
inlines nothing, which `not_imports.csv` records. The rows have the tool `gemini`, the column that
covers Antigravity.

Cursor's Bugbot page states that "Every .md or .mdc file linked from a .cursor/BUGBOT.md file
counts as its own rule". The posts of Cursor's staff in its forum, which the row's notes quote,
state the rest: BUGBOT.md "does actually support referencing other files via standard markdown
links" by a relative path inside the repository. Because neither the page nor a post states that
Bugbot follows a link in a linked file, the row reads one level. Every link to a file has a row,
and a link of another extension is `not_read`. No post says whether Bugbot drops a `?query` and
decodes `%20`. `markdown_link` reads a link as a Markdown renderer does, without its query and
decoded.

A mail link and a mail address name a person, a team or a mailbox, and no file. The tools of the
`@path` rows read an `@` in them as any other `@`. In a test workspace, Copilot CLI 1.0.101 read a
file `octocat](mailto:octocat@example.com` for `[@octocat](mailto:octocat@example.com)`, and
Claude Code 2.1.303, which matches `@path` in the text of every token outside code and HTML, reads
the link text `@octocat` as an import of `octocat`. As an import, such an `@` would add a `missing`
row for a file that nobody named. `claude_at`, `copilot_at` and `gemini_at` therefore start no
import at an `@` in the text of a Markdown link to a `mailto:` URI, inline or by reference, or in
the label of the definition of such a reference. Nor do they start one at the `@` of a mail
address in a `mailto:` URI or in an autolink, such as `<octocat@example.com>`. In `copilot_at`, the
next `@` of the same word then starts the first import of the word, such as `d.md` in
`[@x](mailto:a@b.c)@d.md`. Where Copilot CLI would start an import at the `@` of the mail link or
the mail address, because it follows no ASCII letter, no digit and none of `._%+-`, and a `/`
follows it in its word, as in `[@x](mailto:a@b.c)@d/e.md`, `copilot_at` reads no import of the
word, because Copilot CLI starts no import inside a path with a `/`. Copilot CLI 1.0.101 read no file for
that text in a workspace with a file `d/e.md`. A mail address in other text keeps the reading of
the tool, because nothing marks it as one. Copilot CLI starts no import at an `@` after an ASCII
letter, a digit or one of `._%+-`, but it reads `x.md` for `é@x.md`.

Claude Code reads a root CLAUDE.md or AGENTS.md in another case, such as a `claude.md`, and Gemini
CLI a root GEMINI.md in another case only on a file system that ignores case
([A context file in another case, at the root](#a-context-file-in-another-case-at-the-root)).
Both tools also open each import as written. Claude Code 2.1.303 reads the resolved path with
`stat` and `readFile`, and Gemini CLI reads `path.resolve(basePath, importPath)` with
`fs.readFile`. On such a file system, the import `@AGENTS.MD` of mzmine/mzmine's root
`claude.md` reaches its `AGENTS.md`. The chains of `claude-import-02`, `claude-import-06` and
`gemini-import-02` (`imports.ignores_case`) therefore look up each name of a target as written,
and otherwise as the one entry of its directory that matches the name ignoring case. Their
`target` spells each name as the directory does. A name that two entries match names no file,
because a file system that ignores case cannot hold both. The same file system opens a root
`claude.md` as CLAUDE.md, beside which Claude Code reads no AGENTS.md. For the chain of a root
agents.md in another case, a root CLAUDE file in any case therefore counts, such as
tmandry/glide's `claude.md`, a link to its `agents.md`. A root CLAUDE file in another case leaves
a root AGENTS.md read, because Claude Code reads that file on a volume that compares case, where
`claude.md` does not open as CLAUDE.md. Copilot CLI reads the root files in any case also on Linux, and
its chains find a target only in its case. Every other chain does the same and compares each name
of a target with the entries of its directory, so that the cloning step and the import detection
step find the same files on a file system that ignores case, such as the default one of macOS, as
on the volume of the container.

### What the tools do not read

`not_imports.csv` names a file and a syntax that a tool does not read as an import, each with the
page that states it. VS Code reads the Markdown links of an instructions file only where
`chat.includeReferencedInstructions` is on. The setting has been off by default since 2026-01-22,
and it also determines whether VS Code attaches the file's `#file:` references. Cursor's rules page
and a post of its staff describe `@file` in a rule and in AGENTS.md as a pointer that the agent can
open. The same holds for the AGENTS.md and the CLAUDE.md that Cursor CLI applies as rules. Codex
reads AGENTS.md without imports. Claude Code expands no import in a subagent, an output style or a
skill. The `@{path}` of a Gemini CLI command inserts the file when the user runs the command, and an
Antigravity rule with the trigger `model_decision` or `manual` is read when the agent requests it or
the user mentions it in the Antigravity App, because "The Antigravity CLI doesn't support rule
mentions" ([rules](https://antigravity.google/docs/rules)). Neither the inserted file nor such a rule is context that a
configuration artifact loads. Claude Code Review includes no `@` inside a line and no `@` with a
path into a subdirectory other than a skill's SKILL.md, both of which Claude Code would read, and
no file of a Markdown link. It
follows no `@` line in an included file or in the REVIEW.md of a subdirectory, which the collection
does not copy.

### How the parsers were checked

The tests of each parser expect the results of its own tool, apart from the texts with the `@` of
a mail link or a mail address, which expect the reading of the dataset
([the rows of each tool](#the-rows-of-each-tool)). For Claude Code and Gemini CLI, the
test cases are what the vendors' code returned for each text: <!--s:imports.vendor_cases.claude-->74<!--/s--> texts for Claude Code and
<!--s:imports.vendor_cases.gemini-->66<!--/s--> for Gemini CLI. For Copilot CLI and Antigravity, they are what the tool read when it was
run in a test workspace with the files of each example. For Bugbot they are the examples of the
posts, and for Claude Code Review the example and the conditions of its page. `claude_at` was
compared with Claude Code's own code over the distinct texts of the October collection: every
importing file of Claude Code's rows with an `@` or an HTML comment, every other Markdown file with
an `@`, and the targets that the imports reach outside the collected files, fetched at each
repository's collected commit. `claude_at` returned another import than Claude Code in a few of
these texts, and none of those imports names a file of its repository.
`tests/test_imports.py` keeps texts of this kind with the import that each side returns.
`copilot_at` was compared with the resolver of Copilot CLI 1.0.107 over the instruction files and
the imported files of the handover of 2026-11-06 with an `@` and a fence, and the two read the same
files, apart from paths that one workspace cannot hold as both a file and a directory.

### The copy and the step

The cloning step copies every file that an import of a copied file reaches in the clone to
`data/imported/<owner§repo>/<path>`, after its other passes, whether or not the collection also
copied it, so that the tree holds the files that the tools read. A target counts where it is a
regular file inside the repository after its links, outside `.git` and every gitlink, and
outside what the release prunes, such as a session transcript. A file that the tool does not
read because of its extension, its content, its size or a link on its path becomes a stub. The stub keeps the file's
extension, its size up to the largest limit of a row and whether its text is valid UTF-8, so that
the import detection step gives it the status that it has in the clone, although the tree does
not hold its bytes. A list beside the directory of the repository, `<owner§repo>.stubs.json`,
names each stub. Where the cloning step kept the imported files of an earlier collection, a stub
of a file that the current import heuristics read is `not_copied` rather than `found`.
Copilot CLI reads `.git` as well, and the dataset leaves the clone's own files out. The import
detection step parses the importing files again with the same functions and writes
`output/imports.csv` ([import detection](steps/import_detection.md)).

The imports describe what a tool reads in the repository at the collected commit. A file outside
the repository, such as a file in the home directory, is `outside`. Claude Code and Gemini CLI
read the context files of a directory below the working directory only when they work there.
Copilot CLI reads the AGENTS.md and the CLAUDE.md of the directory in which it starts. The
dataset reads every such file wherever it is. A repository with a root AGENTS.md in two cases
starts a chain at both files, where a tool reads one of them.

## The vendored list

### Problem

A repository can vendor a third-party library or a submodule that itself contains a tool directory
or a `.claude-plugin/`, such as the full Next.js source that `utooland/utoo` keeps at `next.js/`.
Vendored content is not configuration. It is build output, dependency trees or copies of upstream
projects that the developer does not intend to configure for AI agents. The cloning step copies
the manifest directory of a plugin, the metadata siblings that the heuristics name and the
recognized plugin components, instead of the whole parent directory of the manifest.

### Goal

Exclude vendored directories in all <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages-->10<!--/s--> primary languages of the sampling frame (i.e., C,
C#, C++, Go, Java, JavaScript, PHP, Python, Rust and TypeScript), so that only genuine
configuration artifacts remain in the archive.

The fix applies in two places:

1. **Collected file cleanup (`agentcontext/steps/collected_file_cleanup.py`):** reject files
   inside vendored directories, even when another heuristic would accept them.
2. **Repository cloning (`agentcontext/steps/repository_cloning.py`):** exclude vendored
   directory names from the listing of a clone's files and from the `rsync` copies, so that later
   runs do not copy vendored content.

### Heuristic: name-based blacklist

A directory counts as vendored if it has the name of a known build output, dependency
installation or build cache directory of any of the <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages-->10<!--/s--> supported languages. Names are matched
case-sensitively at any depth within a repository tree, except for a folder that contains its own
`SKILL.md`. The Agent Skills format makes such a folder a skill and its name the skill's name, and
the cloning step of the October 5–6 collection left out unoplatform/uno's Claude Code skill
`.claude/skills/docs-build` because of its name. A folder of skills without a `SKILL.md` of its
own stays vendored. open-metadata/openmetadata's `skills/vendor`, which contains skills that
another project wrote, stays vendored, and so does a vendored tree inside a skill. The collected
file cleanup keeps a skill in it that a link at a mechanism path leads to, as
`.claude/skills/composition-patterns` leads to `skills/vendor/composition-patterns`, because
Claude Code reads the skill through the link and the cloning step copies the target. Below such
a target, a vendored directory is still left out.
`paths.is_vendored` and `paths.vendored_part` apply this test for the cloning step, the collected
file cleanup and the detection step.

| Language / ecosystem | Directory names |
|----------------------|-----------------|
| JavaScript / TypeScript | `node_modules`, `bower_components`, `.yarn`, `.pnp`, `.pnpm-store`, `.next`, `.nuxt`, `.output`, `.vercel`, `.parcel-cache`, `.turbo`, `.svelte-kit` |
| Python | `venv`, `.venv`, `__pycache__`, `site-packages`, `.tox`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.eggs` |
| Go / PHP / Ruby | `vendor` |
| Rust / Maven / Java tool dirs | `target`, `.gradle`, `.mvn` |
| C# / .NET | `.vs`, `.nuget` |
| C / C++ | `CMakeFiles`, `cmake-build-debug`, `cmake-build-release`, `vcpkg_installed`, `_deps`, `third_party`, `thirdparty`, `3rdparty`, `external` |
| Docs/site build outputs | `_site`, `_build`, `docs-build` |

#### Notes on the blacklist

- Every step reads the list through `agentcontext/heuristics.py`, the only module that loads it.
  The detection step reads it too, because it reads mechanism directories below the repository
  root.
- The canonical machine-readable list is `heuristics/vendored_dirs.txt`, which
  `agentcontext/steps/repository_cloning.py` and `agentcontext/steps/collected_file_cleanup.py`
  both read. Edit the list there, not in the modules. The table above gives the ecosystem that
  each name comes from.
- `vendor` is the dependency directory of Go, PHP and sometimes Ruby or Perl. Treating it as
  vendored whatever the repository's language is safe, because a directory named `vendor` in
  source code almost always contains third-party content.
- `target` covers both Rust (`cargo target/`) and Maven (`mvn target/`) and is unambiguous for
  the languages in scope.
- Because matching is case-sensitive, no entry matches `Build/`.

#### Ambiguous names that are not on the blacklist

These names stay off the list because repositories also use them for their own paths, and
listing them would remove legitimate configuration:

- `packages` is the monorepo root of many JavaScript and TypeScript projects (e.g., Lerna, pnpm
  workspaces, Nx, Turborepo). Repositories such as `facebook/react-native`, `expo/expo`,
  `shoelace-style/webawesome`, and `temporalio/sdk-typescript` keep the configuration of each
  package, such as `.claude/`, `AGENTS.md` and copilot-instructions, under `packages/`. The NuGet
  `packages/` convention is less common.
- `build`, `dist`, `out`, `bin`, `obj` and `coverage` name build output directories in many
  ecosystems, but also source directories (e.g., a `build/` directory containing build scripts,
  a `.claude/commands/build/` path). Excluding them removed valid configuration of the
  repositories.
- `deps` holds a copy of another project in some repositories, such as swc under `deps/swc` in
  nodejs/amaro, with swc's own `AGENTS.md` files, and the repository's own build directory in
  others. datadog/datadog-agent keeps its Bazel modules there, and `deps/cacerts/AGENTS.md`
  instructs an agent how to update the repository's pinned CA bundle. The detection accuracy
  evaluation of 2026-10-13 reviewed both.
- `env` is often a Python virtualenv, but also a directory of environment configuration. `.venv`
  and `venv` identify the virtualenv case without ambiguity.
- `.cache` holds generic caches, which are usually generated but occasionally contain
  hand-maintained references.

#### Other directories that are not on the blacklist

- `.git/`: filtered elsewhere in the pipeline, and unrelated to vendoring.
- `.github/`: kept or removed by the Copilot heuristics in
  `agentcontext/steps/collected_file_cleanup.py`.
- `.idea/`, `.vscode/`: IDE configuration, kept, because it is small and can contain settings
  similar to the settings under `.cursor`.
- `docs/` (without suffix): user-authored documentation, kept.
- `test/`, `tests/`, `spec/`: source code, kept.
- `examples/`: source code, kept.

### Placement and precedence

The collected file cleanup first removes submodule files, then files under a directory named in
`vendored_dirs.txt`, before it checks configuration paths. The `next.js/` tree in
`utooland/utoo` is a recorded submodule, and the cleanup removes it for that reason.

Outside those exclusions, the collected file cleanup keeps recognized plugin manifests and
components, standalone configuration files and the metadata filenames that `plugin_sibling`
heuristics name. It handles the manifest directories `.claude-plugin/`, `.cursor-plugin/`,
`.codex-plugin/` and `.github/plugin/`. It keeps a sibling skill or agent directory when the
directory is a recognized plugin component, and it removes unrelated siblings, because a
manifest does not make its entire parent directory configuration.

### Detection approach in code

`heuristics.VENDORED_DIRS` holds the names of `heuristics/vendored_dirs.txt`, and
`collected_file_cleanup.is_inside_vendored_dir` tests a path through `paths.vendored_part`, which
does not count a folder that contains its own `SKILL.md` as vendored.

The cloning step passes the same list to `rsync` as `--exclude` flags in `paths.copy_dir`, after an
anchored `--include` for each skill folder with a vendored name. `paths.list_repo_files` and
`paths.find_dirs_by_name` prune the same names from every listing of a clone's files.

### The file count of `repos_data.7z`

The generated release README states the archive's file count. The total of configuration artifacts
is a different count, the rows of the mechanism CSVs other than `other.csv`, and it does not change
with what the archive leaves out.

### Testing

Run the tests of the collected file cleanup from the repository root, with the interpreter that
`mise.toml` sets on `PATH`, such as in a shell started by `mise exec -- bash`:

```bash
python -m unittest tests.test_steps_collected_file_cleanup.TestIsInsideVendoredDir \
  tests.test_steps_collected_file_cleanup.TestARecordedSubmoduleIsDroppedByTheRun \
  tests.test_steps_collected_file_cleanup.TestAPluginReachesTheDetector
```

These tests create temporary trees and check directory exclusions, recorded submodules and
plugin components independently. To regenerate the working copy of the current handover in
full, follow the ordered procedure in [CLAUDE.md](../CLAUDE.md#development-dataset). Every run of
the collected file cleanup deletes `data/validated/` and copies `data/collected/` again, so that
running it replaces the validated tree and its deletion log, `deletion.csv`.

## Other path patterns

`AGENTS.override.md` is Codex configuration. Codex checks it before `AGENTS.md` and the configured
fallback names in each directory, and Claude Code's memory guide lists it among the files that
Claude Code does not read when it reads `AGENTS.md`. The `agents.md` column of `repos.csv` is
TRUE for a Codex override too, but a repository with only an override has `codex=TRUE` and
`no_specific_tool=FALSE`. The sources are
[Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) and
[Claude Code memory](https://code.claude.com/docs/en/memory), checked October 1, 2026.

The rows for Claude Code's skill directories in a project and in a plugin use `skill_dir_flat`:
Only a `<name>/SKILL.md` directly below the directory is a skill of Claude Code. The
[skills page](https://code.claude.com/docs/en/skills) names "Project |
.claude/skills/<skill-name>/SKILL.md" and no deeper path, such as one through a folder that groups
skills by category (read 2026-11-01), and the skill loader of Claude Code 2.1.316 lists the entries
of a skills directory
and reads the `SKILL.md` of each folder or link among them, without searching deeper. A manifest
can name a skill directory at any depth, or a directory with skills directly below it. A Cursor plugin's `skills/`
and a portable Agent Plugins plugin's `skills/` are flat too. Cursor's plugin reference states
"Each subdirectory containing a SKILL.md file", and the Agent Plugins specification states
"Clients MUST NOT recursively search deeper descendants for additional skills". The rows of
other vendors use the depth that their own match mode specifies.

Cursor and OpenCode read `.claude/skills` at any depth, and their rows of the directory use
`skill_dir`. Cursor states "Skill directories can be organized into subdirectories. This is useful
for grouping related skills by category, team, or domain." ([skills](https://cursor.com/docs/skills),
read 2026-11-05), and Cursor CLI 2026.09.28-64d2043 searches each skills root to a depth of 10
(`findSkillMarkdownFiles`). OpenCode's page names
"Project Claude-compatible: .claude/skills/<name>/SKILL.md" ([skills of OpenCode 1](https://docs.opencode.ai/docs/skills/),
read 2026-11-06), and OpenCode 1.19.3 scans every `.claude` from the working directory up to the
worktree with `EXTERNAL_SKILL_PATTERN = "skills/**/SKILL.md"`
([`index.ts`](https://github.com/anomalyco/opencode/blob/e3c4eb242585dcd77be1a867391b7c25fd47ce52/packages/opencode/src/skill/index.ts)).
A skill folder directly in `.claude/skills` is Claude Code's, which reads it, although Cursor and
OpenCode read it too (`detection._tools_reading_a_skill`). A `SKILL.md` in a category folder, a
folder without a `SKILL.md`, is a skill of Cursor and OpenCode, such as the skills that GitNexus
writes into `.claude/skills/gitnexus/` of RocketChat/Rocket.Chat.Electron and growilabs/growi's
`apps/app/.claude/skills/learned/fix-broken-next-symlinks/SKILL.md`. Both tools keep a file only
when it is named `SKILL.md` exactly, and GitHamza0206/simba's
`.claude/skills/simba/deploy/skill.md` is no skill. A `SKILL.md` in the folder of a skill that
Claude Code reads, such as humanlayer/humanlayer's
`.claude/skills/test-it-locally/references/local-cli-testing/SKILL.md`, belongs to that skill with
the rest of the folder, also where a link leads into it. Cursor CLI and OpenCode 1.19.3 list such a
file as a skill of its own, and OpenCode 2 does not descend below a folder with a `SKILL.md`
([`skill-scan.ts`](https://github.com/anomalyco/opencode/blob/510aa79d075c32e75f85b7774ff7390370c22b18/packages/core/src/config/plugin/skill-scan.ts)).
Copilot CLI lists `.claude/skills` among its discovery directories, and whether its native
`skillsLoad` reads a category folder there was not tested, so the heuristics give Copilot no row
for it.

A Codex skill can keep `agents/openai.yaml` in its folder, "Optional: appearance and
dependencies" ([skills](https://learn.chatgpt.com/docs/build-skills), read 2026-10-21). Codex
reads it as the metadata of the skill when the skill's `SKILL.md` is beside the `agents/` folder
(`discover_skill_metadata` in `codex-rs/ext/skills/src/loader/discovery.rs` of openai/codex
3f31c0a040ca). The file is no configuration artifact of its own and needs no row, because it is
part of that skill, and the row of the skill matches the whole folder, as it matches the skill's
`scripts/`. An `agents/openai.yaml` in a folder without a `SKILL.md`, such as the file in
exceptionless/exceptionless's `.agents/skills/dependency-upgrades/`, belongs to no skill that
Codex loads and stays an unclassified file.

The MCP file of a Claude Code plugin contains a flat map of server objects or a map wrapped in
`mcpServers`. Claude Code 2.1.286 loads both forms, including in files that a manifest selects,
and Anthropic's official GitHub plugin uses the flat form. The `json_plugin_mcp` content test
accepts the `mcpServers` wrapper, a flat map with a server that has a command or a URL, and an
empty map. It does not validate every server option. Codex reads the MCP file of a plugin in
the same two forms (`PluginMcpFile` in `codex-rs/codex-mcp/src/plugin_config.rs`), and its row
has the same test. NVIDIA/Model-Optimizer's plugin, with a Codex manifest that names a flat
`.mcp.json`, therefore has MCP servers for Codex as well as for Claude Code.

A component path in a Claude Code manifest must start with `./`, except `.` for a root skill. A
missing path, a path with `..` or a path that resolves outside the plugin makes the discovery of
the plugin's components fail. Agent declarations must name files. External MCP URLs and
bundles are not files of the repository. The pipeline checks the declared component paths, not
the whole schema of a Claude Code manifest.

`hooks`, `mcpServers` and `lspServers` take "Path, object, or array of either", and Claude Code
loads what an object configures "together with" the default file. An `mcpServers` object is an
"Inline map ... Uses the map as server configs keyed by name", an LSP server requires `command`
and `extensionToLanguage`, "Map of file extension to LSP language ID, at least one entry", and
for hooks "An inline object is the event map itself, with no wrapper"
([manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference), read
2026-11-05). The manifest is therefore a row of the mechanism where its object, or an object in
its list, configures a server that Claude Code loads, a language server with both required
fields, or a hook in a matcher group, as the manifest of a Codex plugin is
(`paths._inline_configuration`). The default file stays a row of its own. Claude Code 2.1.319
parses an inline map of MCP servers as a whole, `me(o(),Bie())`, refuses the manifest, or the
marketplace entry, when one server of a map fails the schemas of the server types, and then
loads no component of the plugin. `claude plugin validate` reports "mcpServers: Invalid input"
for a server with a `url` and no `type`, also beside a server with a `command`. A Claude Code
manifest is an MCP row only where every server of its maps fits a schema, those of the `sdk`,
`sse-ide`, `ws-ide` and `claudeai-proxy` servers that Claude Code drops from a plugin included,
and Claude Code loads one of them (`paths._claude_loads_inline_servers`).
e2e-test-quest/uuv's `.claude-plugin/plugin.json` gives its one server a boolean in its `env`,
which `claude plugin validate` refuses as well, and is no MCP row. The other components of such
a plugin stay rows, because the pipeline does not check the whole schema of the manifest.
patrickchugh/terravision gives its Claude Code and its Codex manifest the same server inline,
and DataDog/datadog-agent configures its gopls server only in the manifest of its plugin. A
plugin without a `plugin.json` takes its marketplace entry as the manifest: "No `plugin.json`:
the entry is the manifest regardless of `strict`. Every manifest field that the entry accepts
applies, including `mcpServers`, `lspServers`", and beside a `plugin.json` "Entry `mcpServers`,
`lspServers`, `userConfig`, and `channels` don't apply"
([marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference), read
2026-11-05). firebase/firebase-tools keeps a plugin without a `plugin.json` at its root, and
its marketplace file is therefore an MCP row. The server in genomoncology/biomcp's entry does
not apply, because the plugin has a `plugin.json`.

A Cursor manifest's `mcpServers` takes the same forms: "Path to MCP config file, inline MCP server
config, or an array of either. Overrides default `mcp.json` discovery." and "Specify `mcpServers` in
`.cursor-plugin/plugin.json` only when using a custom path or inline config."
([plugins reference](https://cursor.com/docs/reference/plugins), read 2026-11-06). The manifest is a row
of the mechanism where its object, or an object in its list, configures a server, as in
shadcn-ui/ui's `.cursor-plugin/plugin.json`, and so is the marketplace file with an entry that
configures one, because the manifest's values take precedence over the entry's. The CLI merges the
default files with the manifest's servers, where the page says overrides, and the
`manifest_field:mcpServers:replaces` rows follow the page. Cursor's `hooks` key stays out, because
`has_a_grouped_hook` does not fit its flat entries, and no manifest needs it.

Copilot CLI reads Markdown agents recursively under `.github/agents`, including a `README.md`
with a description in its front matter. VS Code reads the Markdown files directly in the
directory and leaves out `README.md`. The detection step accepts a file that either tool reads,
and a nested Markdown file without a description goes to `other.csv` as an unclassified file. The
CLI's
[custom-agent reference](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli)
documents how directory names form the agent ID.

The Copilot marketplace rows count a file only when it is a JSON object with a nonempty `name`, an
`owner` object with a nonempty `name`, and a `plugins` array. These fields distinguish Copilot's
catalogs from other tools' catalogs with the same filename, and the detection step does not validate
each plugin entry. The root `marketplace.json` of mem0ai/mem0 lacks `owner`, and Copilot CLI rejects
it. The file is therefore no evidence for the root path, which the reference establishes.

A Claude Code marketplace entry with a local relative source also identifies a plugin root, even
for a plugin without a `plugin.json`. The components of an entry follow the
[manifest merge rules](https://code.claude.com/docs/en/plugins-reference#marketplace-entries-and-the-manifest).
A conflicting `strict: false` entry disables the plugin, and an entry for the marketplace root
that selects skill folders leaves out the default skills that it does not list. External sources
and paths outside the marketplace directory do not identify local plugin roots.

A `REVIEW.md` at the repository root contains review instructions for Claude Code Review, "Add
REVIEW.md at your repository root and merge it." ([Code
Review](https://code.claude.com/docs/en/code-review-customize)), and for Copilot code review, which
"also reads custom instructions from CLAUDE.md, GEMINI.md, and REVIEW.md files in your repository,
if they exist" ([code
review](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-code-review), read
2026-10-16). The name contains no tool's name. Since 2026-10-16, the file is a context file with the
tool `any`, as `AGENTS.md` is, and the analysis gives it a category of its own. `CLAUDE.md` and
`GEMINI.md` stay the files of the tools that they are named for, as `.claude/skills` stays Claude
Code's although VS Code and Cursor read it as well. So do `.claude/settings.json` and
`.claude/settings.local.json`, which Copilot CLI "also reads ... for the shared cross-tool subset of
repository settings (such as ... hooks)" ([config directory
reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference),
read 2026-10-22). The files of `.claude-plugin/` stay Claude Code's too, although Copilot CLI
reads its marketplace ([The parts of a plugin, wherever the repository puts it](#the-parts-of-a-plugin-wherever-the-repository-puts-it)) and the ChatGPT
desktop app reads "a legacy-compatible marketplace at $REPO_ROOT/.claude-plugin/marketplace.json"
([plugins](https://developers.openai.com/plugins/build/plugins), read 2026-10-28). Claude Code
Review also reads a `REVIEW.md` in a subdirectory for the changes under it where the base branch
is protected. The heuristic matches only the root file. In the
October 5–6 collection, 19 repositories have a nested `REVIEW.md`, and these files are review
reports, checklists for people, rules of other reviewers or instructions for the whole repository
kept elsewhere. `.claude/REVIEW.md` stays a negative.

`.cursor/config/bugbot.yaml` configures Bugbot at the repository root
([Bugbot](https://cursor.com/docs/bugbot), read 2026-10-16). Copilot CLI's repository settings,
`.github/copilot/settings.json` and `settings.local.json`, count as hooks too where they have a
`hooks` key, as Claude Code's settings files do ([configuration
directory](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)).
`.claude/worktrees/` contains the git worktrees that Claude Code creates and is a negative
([worktrees](https://code.claude.com/docs/en/worktrees)).

Antigravity also reads `.agents/skills.json`, `.agents/agents.json`, `.agents/workflows.json` and
`.agents/plugins.json`. The match mode `registered:m` records each registration file under its
mechanism and follows the components that it selects. A registered skill is a directory with its
`SKILL.md` and support files, an agent is a Markdown file or a directory with an `agent.md`, and a
workflow is a Markdown file. A plugin requires a `plugin.json`, and it can register further rules
through its own `rules.json`. A registration file resolves its paths against the directory that
contains its customization root, or against the root of its plugin, and against the workspace
root where nothing exists there. All registration files inherit and filter as
`.agents/rules.json` does ([Configuration artifact detection](#configuration-artifact-detection)).

Since CLI 1.3.3, a plugin registers its subagents and skills the same way, through an `agents.json`
and a `skills.json` at its root. Its manifest can also list skills under `skills`, which add to
`skills/` and resolve as the entries of `skills.json` do ([The places that a plugin's manifest names](#the-places-that-a-plugins-manifest-names)). Since CLI 1.3.2, it can keep its manifest in a `.antigravity-plugin/` or `.gemini-plugin/`
folder, which makes the directory above the folder the plugin
([changelog](https://antigravity.google/docs/changelog), read 2026-10-16). The components stay in
that directory. According to the changelog for CLI 1.4.0, `agy plugin validate` warns about
"components placed inside .antigravity-plugin/ instead of the plugin root" (read 2026-10-19). CLI
1.4.2, run offline on 2026-10-19, reported the `agents/`, `rules/` and `skills/` inside either
folder as "never loaded: components are read from the plugin root". The steps count none of them
(`test_the_manifest_folder_of_an_antigravity_plugin_holds_no_component`). CLI 1.3.6 also finds a
manifest named `Plugin.json`. No row matches that spelling, because on a volume that ignores case
the two names are one file, and the only `Plugin.json` in the October 5–6 collection is outside
every customization root. A plugin counts under a customization root or where a `plugins.json`
registers it. Elsewhere in a repository, a bare `plugin.json` is also the portable Agent Plugins
manifest and a file of many other programs. Because Antigravity's `$schema` is optional, nothing
distinguishes the files. mem0ai/mem0's plugin at `integrations/antigravity-plugin/` therefore does
not count.

Google deprecated Antigravity's workflows and names no end date: "Workflows are deprecated, and a
new workflows system is on the way. Your current workflows keep working for now."
([migration](https://antigravity.google/docs/migration/workflows-to-skills), read 2026-11-06). The
workflow rows keep counting the files. A workflow in a repository shows that the repository
configured Antigravity, whatever a later release reads. Once no current page names
the files, the rows keep citing the live page and quote the last capture of a page that names
them, as the rows of `.cursorrules` and `.cursorindexingignore` do. When the migration page names
the files of the new workflows system, the documentation watch reports the change.

The detection step lists the ignore files `.cursorignore`, `.cursorindexingignore`,
`.geminiignore`, `.aiexclude` and Antigravity's `.antigravityignore` under `other` at every
depth. This listing records the files that the collection kept. It does not mean that every host
applies them at every location.

Copilot's prompt files and chat modes count only directly in their default directories. Gemini
CLI's agents are the Markdown files directly in their directory, except names that start with
`_`. Copilot CLI's hook JSON files are read recursively under `.github/hooks` and need a nonempty
`hooks` object. A `hooks` object of events counts only with a hook in one of its events, in the
format of the tool that reads the file. Claude Code, Codex and Gemini CLI read three levels of
event, matcher group and handler ([hooks](https://code.claude.com/docs/en/hooks)), and their
files count only with an event that lists a matcher group with a handler in its `hooks` list.
`{"PostToolUse": []}` in the `.claude/settings.json` of wp-media/wp-rocket,
microsoft/FluidFramework and podman-desktop/podman-desktop configures no hook. Nor does an event
that maps to an object, as `UserPromptSubmit` in itbench-hub/ITBench's `.claude/settings.json`,
which Claude Code 2.1.318 drops with "must be an array of matchers; received ...; entry ignored".
Gemini CLI skips such an event ("is not an array. Skipping.") and discards a definition without a
`hooks` list ("Discarding invalid hook definition", `hookRegistry.ts` at
f4df88a70d2ff1ed5689edb07858b55570ee202b, read 2026-11-05). Codex reads each element of an
event's list as `pub struct MatcherGroup { #[serde(default)] pub matcher: Option<String>,
#[serde(default)] pub hooks: Vec<HookHandlerConfig> }`, without `deny_unknown_fields`
(`codex-rs/protocol/src/hooks.rs` at be765f1adeecace0a2aef0700b1d13b0a2043a9f, read
2026-11-05). The handler that nevalang/neva's `.codex/hooks.json` lists under Stop is therefore
a group without handlers, and Codex runs nothing. Codex parses `.codex/hooks.json` and a
plugin's hooks file as `#[serde(deny_unknown_fields)] pub struct HooksFile` with `description`
and `hooks` (`codex-rs/config/src/hook_config.rs` at the same commit), and on another top-level
key the plugin loader reports "failed to parse plugin hooks config". razorpay/blade's
`packages/blade-plugin/hooks/hooks.json`, which its Claude Code and its Codex manifest both name,
opens with `_comment`, and it is a hooks row of Claude Code alone. In `.codex/config.toml`,
Codex reads the `hooks` table as `HooksToml`, its events flattened beside `state`, which maps
each hook that another file defines to its trust state, `enabled` and `trusted_hash`.
vinta/hal-9000's `dotfiles/.codex/config.toml` has hooks only under `[hooks.state."<hook>"]` and
therefore declares no hook. Cursor and Copilot list their handlers under the event, and Copilot CLI also
reads Claude Code's matcher groups there, so a file of theirs counts with any entry in an
event.

`dir_agent:.md` matches the Markdown files directly in an Antigravity agents directory,
including a plugin's `agents/`, and each `<name>/agent.md` under it, the name in any letter case,
if the file's front matter has a non-empty `name` and `description`. An agent at
`<name>/agent.md` is named for its folder.
The subagents page marks both keys "(Required)". Antigravity CLI 1.3.5, run offline on
2026-10-14, lists no agent without them and logs "Markdown agent at %s is missing name or
description in frontmatter". It also leaves out an agent with a front matter that is not YAML.

Antigravity's `.agents/hooks.json` and a plugin's `hooks.json` match by `json_named_hooks`, the
content test that applies the same decoding as Antigravity CLI 1.3.5. The CLI drops a file that it
cannot decode and logs "failed to parse hooks.json at %s". The file has to be strict JSON with no
comment, trailing comma or byte order mark. It maps each hook's name to the hook, which is an object
or null, and a hook's `enabled` is a boolean. PreToolUse and PostToolUse are lists of groups with a
string `matcher` and a list `hooks` of handlers, while SessionStart, PreInvocation, PostInvocation
and Stop are lists of handlers. A handler's `type` is `command` or absent, its `command` a string
and its `timeout` a whole number that fits in 32 bits. Keys match these fields in any case, and the
CLI ignores other keys. A file in Cursor's format with `"version": 1` fails, because the CLI reads
`version` as a hook. Two elastic repositories keep such a file. The same file without that key
loads. An empty object loads with no hook in it and does not count.

A Codex compatibility manifest can replace the default skills, MCP, apps and hooks paths. The paths
must begin with `./` and stay inside the plugin. Invalid values and empty path lists keep the
defaults. Valid inline MCP or hooks objects replace the default file, including an empty hooks
object, and nonempty inline configuration is attributed to its manifest. A portable plugin keeps
its standard skills and MCP locations. `extensions.com.openai` selects Codex hooks, and a
compatibility manifest is used as an overlay only when that extension is absent.

Where a plugin root contains both manifests, Codex reads the plugin through the portable manifest.
`find_plugin_manifest_path` in `codex-rs/utils/plugins/src/plugin_namespace.rs` takes the root
`plugin.json` before `.codex-plugin/plugin.json`. `parse_agent_plugin_manifest_uri` in
`codex-rs/core-plugins/src/agent_plugin_manifest.rs` then takes skills from `./skills` and MCP
servers from `./mcp.json`, and the compatibility manifest sets only apps, hooks and the interface
(read 2026-10-13). It also lets Codex copy the `env_vars` of a stdio server.
`apply_codex_env_overlay` in `codex-rs/core-plugins/src/agent_plugin_mcp_overlay.rs` of
[d960b366941b](https://github.com/openai/codex/blob/d960b366941bf062adb4d43111e81b64507e8879/codex-rs/core-plugins/src/agent_plugin_mcp_overlay.rs)
(read 2026-11-08) reads the servers that `.codex-plugin/plugin.json` configures inline or in the
MCP file that it names, `.mcp.json` where it names none, and adds the `env_vars` of each stdio
server to the stdio server of `./mcp.json` with the same name. It looks each server up with
`agent_servers.get_mut(&name)` and skips one that `./mcp.json` does not configure, so the MCP file
of `.codex-plugin/plugin.json` adds no server for Codex (pp-0214 of the evaluation of 2026-11-04).
`paths.plugin_components` therefore reads no `<codex-plugin>` place under such a
root, and it returns the portable `skills/` and `mcp.json` as Codex's parts, as in
saadeghi/daisyui's root and EvotecIT/OfficeIMO's `.agents/plugins/officeimo-document-tools`. A
skill of such a plugin is Codex's where Codex's parser accepts its SKILL.md
(`detection._codex_skill_loads`). In the October 5–6 collection, the detection accuracy
evaluation of 2026-10-13 found repositories with a plugin with both manifests in which the MCP
rows came only from the compatibility manifest's places.

A Codex plugin's `.app.json` routes the plugin's MCP servers through the connections that are
registered in ChatGPT. The page keeps the manifest's apps field "only for registered MCP server
mappings in .app.json" ([plugins](https://developers.openai.com/plugins/build/plugins), read
2026-10-21). Codex reads `.app.json` at the plugin root, or the file that the manifest's `apps`
names in its place, and declares an app for each entry of its `apps` object with an `id`
(`plugin_app_config_paths` in `codex-rs/core-plugins/src/loader.rs` and
`codex-rs/connectors/src/plugin_config.rs` of openai/codex 3f31c0a040ca, read 2026-10-21). Two `mcp`
rows of `<codex-plugin>/.app.json` match it: `json_servers:apps`, so that the empty `apps` object of
the scaffold declares no app, and `manifest_field:apps:replaces`. Codex reads the apps only of a
plugin with a legacy manifest, "format == PluginManifestFormat::Legacy" in `plugin_supports_apps`
(`codex-rs/core-plugins/src/app_mcp_routing.rs`), and keeps the apps of a portable plugin inactive.
No row matches a `.app.json` beside a root `plugin.json` with the Agent Plugins schema.
jhumanj/opnform's `plugins/opnform/.app.json`, the only `.app.json` in the file lists of the October
5–6 collection, is beside such a manifest and stays uncollected.

The configuration of the GitHub Copilot app in `.github/github-app.yml`, and in the legacy
`.github/copilot-desktop.yml` that the app still supports, counts as settings. Copilot CLI's
executable extensions under `.github/extensions/`, and the `extensions` entries of a plugin, fall
under none of the mechanisms: They implement extension APIs rather than one of the configuration
mechanisms that the published CSVs represent. Leaving them out of scope does not mean that
Copilot cannot load them. The
[Copilot app reference](https://docs.github.com/en/copilot/reference/github-copilot-app-reference/repository-configuration)
and the [CLI extensions guide](https://docs.github.com/en/copilot/concepts/customizations/extensions)
document the layouts.

Antigravity's project settings use `.gemini/config.json`, and Antigravity no longer supports
the old filename `.agents/settings.json`. The root `.worktreeinclude`, which Claude Code and Codex
read when they create a worktree, is listed under `other` beside the ignore files of the tools,
with the tool `any`. Claude Code can load a settings file with a
custom name through `--settings`, but the path heuristics follow automatic discovery and do not
interpret such command-line references in a project's scripts.

## Planning documents

A planning document is a file that a spec-driven development framework or a planning feature of
one of the six tools writes into the repository, at a location that the framework or the tool
documents. Agents read it as a work product through a command or a reference, not as
configuration. Planning documents are not configuration artifacts. They set no tool flag, do not
make a repository configured, appear in no mechanism CSV and change no count of the
configuration artifacts. The detection step publishes them in a CSV of their own,
`output/planning-documents.csv`, which [detection](steps/detection.md) describes.

The work products count: specs, plans, task lists and the files of single tasks, designs,
proposals, research, requirements and product requirement documents, stories, roadmaps,
constitutions, and the project context that a framework generates for agents to read, such as
BMAD's `project-context.md` or the codebase map of Get Shit Done. An issue tracker that keeps its
items as Markdown task files written by the framework, as in Backlog.md and Beans, counts with
its tasks. A framework counts when its own documentation names at least one of the six tools.
The metadata beside the documents, such as OpenSpec's `.openspec.yaml` or the `spec.json` of
cc-sdd, and the templates, scripts, installations and configuration of the frameworks are no
planning documents.

### The heuristics

`planning_document_patterns.csv` has one row for each documented location of a document, <!--s:heuristics.planning_document_patterns.rows-->523<!--/s-->
rows for <!--s:heuristics.planning_document_patterns.frameworks-->73<!--/s--> frameworks and tools, with the columns `framework`, `document`, `pattern`,
`match_mode`, `anchor`, `example`, `doc_url`, `doc_verified`, `evidence` and `notes`. `framework`
names the framework, such as `spec-kit`, or the tool with the planning feature that writes the
file, such as `cursor`, and `document` is the source's name for the document, such as `spec` or
`plan`. `pattern` is a regular expression that the file's path matches in full, relative to a
base that `match_mode` gives: the repository root, every directory, every directory that
contains one of a framework's markers (`with:`), or the folder that a project's setting names
(`setting:`). `example` is a path that the row matches. The module docstring of `planning.py`
defines each mode and anchor. A new layout needs a row and no code. The rows are read in file
order, and the first row that matches a file gives its framework and document. The rows of the
folders that a setting names come last, because a framework can write into the folder that a
person's setting names. The row of superpowers' plans in `.claude/plans/` also comes before
Claude Code's row of that folder, because superpowers' writing-plans skill lets a person's
preference choose the folder, as jirkapok/GasPlanner's `.claude/CLAUDE.md` does. Spec-Flow's
rows come before Spec Kit's rows of feature folders without a `.specify/`, because Spec-Flow's
documents open with the headings of Spec Kit's templates. The rows of the conductor plugin of
claude-code-workflows come before Conductor's rows, because the plugin writes the paths of
Google's Conductor with templates of its own. The row of the notes of awesome-copilot's Task
Researcher comes before hve-core's rows, because both name the notes
`YYYYMMDD-<task>-research.md` in `.copilot-tracking/research/`.

Each row cites the page that states its location, with the date on which the page was read. `notes`
quotes the text of that page that grounds the row, or the framework's file at a tag or commit that
states the location, linked by its full address. The notes of a framework's rows also quote a
sentence of the framework's documentation that names one of the six tools, from the cited page or
from a page that the note links. Where the page does not state the whole path, `evidence` links
files at that path or the framework's code that writes them, each at a fixed commit, as for the path
patterns. A survey of the frameworks on 2026-10-19 proposed the rows, and every cited page was read
again that day. Double quotes in a note enclose only quotations, except in a code span between
backticks outside a quotation, such as a line of a framework's code, where they enclose strings of
that code. A quotation can contain backticks, such as those of a Markdown source. A quotation
counts for its row only where it is in the row's own pages: its `doc_url`, the pages that its note
links outside its quotations, and the files that its `evidence` links. `docs --quotes` checks this
for every quotation and for no string of a code span. It compares the text that a reader sees, so
that a quotation of a rendered page matches its Markdown source, and `...` in a quotation marks an
omission. With `--update`, it records each quotation found, by a digest, under the page that
contains it, with the page's hash, in `heuristics/doc_quotes.json`. The documentation watch reads
both files of the planning documents with `--quotes`. `tests/test_planning.py` checks the
grounding against that record without the network. It checks that every quotation is in a page of
its row, that every row quotes its page or a vendor's file that its note links, and that a
framework's rows quote a sentence that names a tool. It also checks that the hash under which a
page's quotations were found is the hash that `doc_hashes.json` records for the page. The suite
also builds, for each row, a tree with the row's example and anchors in which the row matches
before any other row. Tests of single frameworks, such as
`test_research_kit_beside_its_research_folder`, also build a layout without its anchors and check
that no row counts its files there. The suite does not do this for every row.

### Anchors

Many frameworks write into folders with common names, such as `docs/plans/`, `specs/001-name/`,
`backlog/` or `conductor/`. A row for such a folder counts a file only where a file or folder of
the framework is there too, the row's anchor:

- a marker beside the folder (`with:`), such as `.specify/` for Spec Kit's feature folders,
  `.bmad-core/` for the documents of BMAD-METHOD 4 in `docs/`, or another file of Conductor's
  project context for `conductor/product.md`
- a file beside the document (`beside:`), such as the `spec.json` that cc-sdd writes into a
  feature folder of `.kiro/specs/`, which Kiro's own specs lack
- a path anywhere in the repository (`repository:`), such as `.lean-spec/config.json` for
  LeanSpec's `specs/NNN-name/README.md`
- the opening of the file (`header:`), read in its first <!--s:parameters.agentcontext.planning.HEAD_BYTES-->4,096<!--/s--> bytes, as for the plans that
  Cursor saves, which open with a front matter with `name` and `overview` or, in older versions,
  with a comment of two UUIDs. The rows of the plans of superpowers in `docs/plans/`, where
  versions before 5 saved them, and in `.claude/plans/` read the line of the opening that names
  the plan's executor. The row of the notes of awesome-copilot's Task Researcher reads the
  heading `# Task Research Notes:`, where hve-core's researcher writes `# Task Research:`.
- the opening of a document of the same folder (`folder_header:`), as for a Spec Kit feature
  folder without a `.specify/`, where `spec.md`, `plan.md` or `tasks.md` has the heading of
  Spec Kit's template and the field below it, `**Feature Branch**:` or `**Input**:`, because a
  project's own workflow can use the heading alone, and for a `conductor/` of the conductor
  plugin of claude-code-workflows, where `index.md` has a line that begins with
  `Navigation hub for` or `tracks.md` the header of the plugin's table,
  `| Status | Track ID | Title | Created | Updated |`. Google's Conductor opens `index.md` with
  `# Project Context`, and agents that run its skills also title `tracks.md`
  `# Tracks Registry`, the heading of the plugin's template, so the heading does not tell the two
  apart

A marker, a file beside or a path elsewhere is no planning document, and the detection step
lists none. The cloning step still copies the anchor with the documents that it anchors, and the
first file in path order where the anchor is a folder. The collected file cleanup keeps the
anchor, so that the cleanup and the detection step find in the copy the documents that the
cloning step found in the clone. The cloning step reads an opening in the clone, and the cleanup
and the detection step read it in the copy, which has the same bytes. A link has no opening,
neither in the clone nor in the copy, which stores the link as its marker. `planning.read_head`
reads no opening from a file with a link's marker as its text (`paths.link_marker`). A linked
`.specify/memory/constitution.md`, with a marker that lacks the template's heading, is therefore
an anchor in the copy as in the clone. A tool directory that is a link is read through the link, in
the clone and through its marker in the copy, as the tool reads it: The files of the directory that
it leads to are also matched at their paths through the link, and a document or an anchor found that
way is listed at its own path (`planning.find`). samanhappy/mcphub's
`.agents/plans/cleartext-logging-fix.md` is a plan of Claude Code through `.claude -> .agents`,
and the `plansDirectory` of the settings file behind such a link names a folder of plans. A
place of the linking tool's local state there, such as `.claude/worktrees/`, is left out at
both paths.

### The frameworks and tools

The five tools with planning features that write into the repository have rows of their own:

| Tool | Location | The source |
|---|---|---|
| Claude Code | `.claude/plans/` and the folder that `plansDirectory` names | The [settings reference](https://code.claude.com/docs/en/settings-reference): "Choose where Claude Code stores the plan files it writes in plan mode." The setting has "Scope: Any file", and a person's own settings can therefore name `.claude/plans/`. The evidence links plans there |
| Cursor | `.cursor/plans/*.md` with Cursor's opening | The [blog](https://cursor.com/blog/agent-best-practices): "Tip: Click "Save to workspace" to store plans in .cursor/plans/ ." |
| Gemini CLI | `.gemini/plans/` and the folder that `general.plan.directory` names | The [Plan Mode page](https://geminicli.com/docs/cli/plan-mode/): "For example, to store plans in a `.gemini/plans` directory within your project" |
| OpenCode | `.opencode/plans/` at the root of a git repository | The [agents page of OpenCode 1](https://docs.opencode.ai/docs/agents/): "With OPENCODE_EXPERIMENTAL_PLAN_MODE enabled, Plan saves its plan to .opencode/plans/ for Git projects", and OpenCode 1.19.3 names a plan `<created>-<slug>.md` there. OpenCode 2 keeps its plans in `~/.opencode/plan/`, outside the repository |
| GitHub Copilot | the folders of app modernization, `.github/upgrades/`, `.github/modernize/` and `.github/modernization/`, `.github/java-upgrade/`, `.github/appmod/` and `.appmod/.migration/` | The [.NET overview](https://learn.microsoft.com/en-us/dotnet/core/porting/github-copilot-upgrade/overview): "The agent stores all upgrade state in `.github/upgrades/{scenarioId}/`.", the pages of the modernize agent and of the Java and Azure migrations, and the agents that Microsoft ships. The assessment's JSON and CSV files are the data that the tools parse, and no row reads them |

Codex keeps its plans in the session and writes none into the repository. Antigravity keeps its
implementation plans, task lists and walkthroughs in the application's data directory, and
Copilot's Plan agent and Copilot CLI keep their plans in the session too.

Of the frameworks, superpowers, OpenSpec and Spec Kit have the most repositories in the
`planning-documents.csv` of the collection that the checkout tracks, <!--s:collection.planning_documents.superpowers_repositories-->246<!--/s-->, <!--s:collection.planning_documents.openspec_repositories-->82<!--/s--> and <!--s:collection.planning_documents.spec_kit_repositories-->45<!--/s-->.
Get Shit Done, Backlog.md, ai-dev-tasks, Compound Engineering, BMAD-METHOD, Beans, HumanLayer's
commands, Ralph, PRP, Task Master, planning-with-files, cc-sdd, Liatrio's spec-driven workflow,
Conductor, Pilot Shell, hve-core, awesome-copilot and Matt Pocock's skills have the others,
<!--s:collection.planning_documents.frameworks_with_matches-->24<!--/s--> frameworks and tools in all. The other
rows describe frameworks that no repository of the collection used, such as Agent OS, ccpm,
Spec Kitty, MoAI-ADK and SpecPulse, so that a later collection finds them. A row for an earlier
version of a framework, such as the alpha releases of BMAD-METHOD 6 or Agent OS 1, cites the live
page of the framework, so that the documentation watch reports a change. Its notes quote the file
at the tag or commit that states the location. Pilot Shell continues as QualityLayer from version 12,
which removed Pilot Shell's pages. The rows of Pilot Shell cite QualityLayer's page for the users
of Pilot Shell and quote the files of version 11.0.9. QualityLayer's task folders under
`docs/plans/` have rows of their own. HumanLayer replaced its commands with the skills of its
Research-Plan-Implement plugin on 2026-10-22, and the skills write into `.humanlayer/tasks/`.
The rows of the commands' `thoughts/` folders cite the skills that replaced them and quote the
commands at
[the last commit that contains them](https://github.com/humanlayer/humanlayer/tree/99abe673498cf8bdcd5f989aebe9406a27185b3b).

### The layouts left out

`not_planning_documents.csv` records <!--s:heuristics.not_planning_documents.rows-->170<!--/s--> layouts that are left out on purpose, with the columns
`framework`, `layout`, `example`, `reason`, `source`, `doc_url`, `doc_verified`, `evidence` and
`notes`. `reason` is one of `out of scope`, `outside the repository`, `not documented`, `not
distinctive`, `configuration`, `state`, `not a planning document` and `not settled`. A `doc` row
cites the page that grounds it, and a `sample` row links files of the layout. `example` is a
path of the layout that no positive row matches, blank where the layout is outside the
repository. The decisions that the rows record include these:

- The tools outside the six, such as Kiro, Windsurf, Cline and Kilo Code, are out of scope with
  their layouts. cc-sdd counts, although it writes into Kiro's `.kiro/specs/`, because its
  documentation names Claude Code and Codex, and the `spec.json` beside its documents separates
  them from Kiro's.
- Codex's ExecPlans have no documented place. OpenAI's archived recipe names `.agent/PLANS.md`,
  the rules for writing ExecPlans, which `config_path_patterns.csv` collects, and none for the
  plans.
- The metadata, the templates and the configuration of the frameworks, such as Spec Kit's
  `.specify/templates/`, OpenSpec's `config.yaml` and `.openspec.yaml`, and BMAD's installation,
  are left out, and some of them anchor rows.
- A `spec.md`, `plan.md`, `tasks.md`, `TODO.md` or `PLAN.md` outside a documented layout, and
  folders such as `docs/plans/`, `plans/` and `specs/` without an anchor, are not distinctive.
- The design documents that superpowers 3.2.2 to 4 wrote into `docs/plans/` have no opening of
  their own, and metaswarm and people write the same names.
- Zenflow's `.zenflow/tasks/` is not settled, because its live page no longer names the folder.
- BMAD-METHOD writes into another folder where `output_folder` names one: "The store is your BMad
  output folder, _bmad-output by default", and "Otherwise set output_folder in
  _bmad/custom/config.toml, which is committed and applies to the whole team"
  ([ticket tree](https://docs.bmad-method.org/plan/set-up-the-ticket-tree/), read 2026-11-01). The
  rows match `_bmad-output/` and do not read the setting. In the file lists of the collection,
  <!--s:collection.planning_documents.bmad_custom_config_repositories:words-->two<!--/s--> repositories have `_bmad/custom/config.toml`. In the run that started on
  2026-10-23, they are CDCgov/cdc-open-viz and redhat-cop/vault-config-operator, and neither sets
  `output_folder` (read 2026-11-01).

`not_configuration.csv` keeps its rows for `.claude/plans/**`, `.cursor/plans/**`,
`.claude/specs/**` and `.claude/context/**`, because no tool loads these files as configuration.
Their notes name the files that the planning rows count, and the rows keep the other files of
these folders, which stay in `other.csv`, out of the ranking of `unmatched paths`.

### Planning documents beside the configuration

A file that `config_path_patterns.csv` classifies stays a configuration artifact wherever it is.
The `openspec/AGENTS.md` that earlier versions of OpenSpec wrote is a context file, and the skills
that Spec Kit installs for Claude Code, such as `.claude/skills/speckit-specify/SKILL.md`, are
skills. A planning folder inside a tool's directory, such as `.cursor/plans/`, `.claude/plans/`,
ccpm's `.claude/prds/` or the folder that `plansDirectory` names, gives planning documents, and
the detection step lists them in `planning-documents.csv` and not in `other.csv`. They therefore
do not set the `other` flag of `repos.csv`, which a row of `other.csv` sets.

The cloning step copies the planning documents of every repository that it collects, inside the
repository and outside `.git` and the vendored directories, and the collected file cleanup keeps
them. A planning document alone does not keep a repository in the dataset. The cleanup drops a
repository without configuration artifacts together with its planning documents, and the CSV
contains the planning documents of the repositories that `repos.csv` lists. In the collection that
the checkout tracks, it lists <!--s:collection.rows.planning_documents-->27,593<!--/s--> planning documents in <!--s:collection.planning_documents.configured_repositories-->417<!--/s--> repositories.

### The cost of the history

The cloning step reads the history of every file that it copies. In the collection, it measured the
history of <!--s:collection.file_history.collected_files-->196,849<!--/s--> collected files, among them the
<!--s:collection.rows.planning_documents-->27,593<!--/s--> planning documents. It copies planning
documents only from a clone from which it copied a configuration artifact, because the cleanup drops
every other repository, and a planning document keeps no repository in the dataset.
In the collection that the checkout tracks, epam/ai-dial-chat has
<!--s:collection.planning_documents.epam_ai_dial_chat-->3,584<!--/s--> planning documents, and the cloning step reads the history of each of them. No
bound limits the reading, because the rows beyond a bound would have no
`created_at` or `#commits`.

## Session logs

A session log records one session of a coding agent: the prompts, the responses, the tool calls and
what the session read. No tool reads it as configuration, and no command reads it as a work
product. It is therefore neither a configuration artifact nor a planning document, and the
dataset does not collect it. The six tools keep their session logs in the user's home directory,
as [Claude Code](https://code.claude.com/docs/en/sessions) states for its transcripts in
`~/.claude/projects/` and [Codex](https://learn.chatgpt.com/docs/reference/troubleshooting) for
`~/.codex/sessions/`. A repository contains a session log only where someone commits an export or
a copy. In the file lists of the October 5–6 collection, idaholab/TMAP8 keeps transcripts of
Claude Code or rollouts of Codex as a practice, and OpenAgentsInc/openagents, runmedev/runme and
che-incubator/che-code keep them as test or benchmark data. A session log contains local paths
with the user's name, everything that the session read, which can include secrets, and text of
third parties that the repository's license need not cover. A release could publish at most
whether a repository has a session log.

A tool directory that the collector copies whole can contain such files of a user's machine:
novasamatech/nova-spektr and helium/helium-program-library commit the notes that Claude Code keeps
under `.claude/projects/-Users-<name>-.../memory/`, with the user's name in the path. The
collection leaves these places out ([The local state of the tools](#the-local-state-of-the-tools)).

## The local state of the tools

A tool keeps files for its own use in its home directory: session logs, prompt histories,
caches, locks, logins and the checkouts of its worktrees. A repository contains them where its
tool directory is the tool's home directory, such as a `CLAUDE_CONFIG_DIR`, a `CODEX_HOME` or a
`GEMINI_CLI_HOME` in the project, or where someone commits a copy of a home directory. Some tools
also write them into the project, as Claude Code does with `.claude/worktrees/` and
`.claude/scheduled_tasks.lock` and Cursor with the logs of its Debug Mode. No tool reads these
files as configuration, and they can contain a user's paths, prompts and credentials.
`local_state_paths.csv` lists their places inside the tool directories that the collector copies
whole, the paths of the `spec_dir` and `spec_dir_with_place` rows. The cloning step does not
copy them. The collected file cleanup deletes them from a tree collected under an earlier
collector, with the reason `local state of a tool (<place>)`.

The file has the columns of `not_configuration.csv` and the same checks. A `doc` row cites the
page that names the place. Where the page names the home directory but not the place, the row
cites that page and quotes the tool's code in `notes`: the source at a fixed commit for Codex,
Gemini CLI and the Go program named opencode, and the strings of the published binary, with its
package version, for Claude Code. Cursor names some of its places only in the posts of its staff
on its forum, which `notes` quotes beside the page of the feature. A `sample` row links the files
that it was derived from: the folder of a Claude Code project on Windows, and the lock of Claude
Code's scheduled tasks that bruin-data/bruin's `.claude`, a link to `.agents`, puts into
`.agents`.

A `path` starts with the tool directory and names the place below it. A trailing `/` makes the
path a directory, and everything below that directory is left out. Without the `/`, the path
names a file. `*` and `?` match within one component of a path, as in rsync's patterns. A place
matches wherever the collector copies its tool directory, at any depth. `paths.local_state`
matches a path against the places, and `paths.is_pruned` leaves them out of every listing of a
clone. `paths.local_state_excludes` gives the rsync patterns of `paths.copy_dir`.
`reuse.path_heuristics` records the places with the path heuristics, so that the cloning step
collects again the repositories with a path in a changed place. The documentation watch reads the
file with `python3 -m agentcontext docs --heuristics
heuristics/local_state_paths.csv --label-columns tool,path`.

Some places are named precisely, because projects keep their own files under the same names.
Claude Code names the folder of a project in `.claude/projects/` for the project's path, such as
`-home-user-work-my-repo`. `.claude/projects/-*/` and `.claude/projects/?--*/` match these names
and leave a project's own folder, such as apache/superset's `js-to-ts`, in the collection. Claude
Code keeps each task list in a directory of its own, and `.claude/tasks/*/` leaves a file
directly in `.claude/tasks/` in the collection.

The file has no row for these places:

- Files that a tool writes and reads as configuration: `.claude/settings.local.json` and
  `.claude/agent-memory-local/`, which path patterns read, Gemini CLI's `policies/auto-saved.toml`,
  Cursor's `cli-config.json`, and Codex's `rules/default.rules`, which its [rules
  page](https://learn.chatgpt.com/docs/agent-configuration/rules) documents as a file that a
  person creates.
- Planning documents, such as `.claude/plans/`, `.cursor/plans/` and `.opencode/plans/`
  ([Planning documents](#planning-documents)).
- The files that OpenCode 1 writes for the dependencies of its plugins, `package.json`,
  `package-lock.json`, `bun.lock` and `.gitignore`, which hold no session or user data and are
  rows of `not_configuration.csv`.
- `.gemini/extensions/`, which Gemini CLI's A2A server reads in a trusted workspace.
- Places that neither a page nor the code of the vendor grounds, which only a user's post,
  another project's documentation or a name in the code without a stated purpose shows: Cursor's
  `extensions/`, `argv.json`, `ide_state.json`, `ai-tracking/`, `agent-cli-state.json` and
  `statsig-cache.json`, Antigravity's `conversations/`, `implicit/`, `knowledge/`,
  `annotations/`, `code_tracker/`, `browser_recordings/` and `playground/`, Codex's
  `.codex-global-state.json`, Claude Code's `downloads/` and Gemini CLI's `user_id`.

GitHub Copilot has no place in the file. The collector copies no `.copilot/`, Copilot CLI's home
directory, and it copies `.github/` only for a Copilot path. The collected file cleanup then
keeps only the paths that the path patterns name.
