# Current development dataset

The development dataset is the run of all five stages that started on the NAS on 2026-11-03. The
image of the run of 2026-11-03 started it, the image that resumes that run resumed the labeling,
and the image with the backup providers finished the labeling on 2026-11-04 and ran the collection
and every later step ([the build records](validation/README.md)). Its handover is
<!--s:collection.handover.archive-->agentcontext-handover-20261106T125741Z-complete.7z<!--/s-->,
<!--s:collection.handover.bytes-->8,481,941,362<!--/s--> bytes, with the SHA-256
<!--s:collection.handover.sha256-->1c0b5c81cb26cd85bbe0b83f1619d3a6cdd439e918c388eb530e875ad7908697<!--/s-->.
Keep the downloaded handover unchanged as evidence, and process a working copy of it, extracted
with `7zz x -snld` inside a Docker volume ([development](development.md#handover-working-copy)).
Extracted that way, the working copy has every path of the archive listing, the symlinks among
them included. Its empty files are files of the data rather than placeholders of refused links.
The tracked outputs are that collection, regenerated with the current code and heuristics.

Use the handover to improve the collection, the validation, the detection heuristics, the tests
and the verification. Derive expected results from the vendor documentation and from repository
evidence that you examined. A corrected heuristic may change which repositories a population
contains, the attribution, the counts and the figures. The counts published in March do not
constrain these results.

## Changes after the collection

The collection copied files under the path heuristics that `collection_heuristics/` of the handover
keeps and under the collector of the image with the backup providers. Both changed afterwards, and
`run --mark-complete repository_cloning` records the cloning step of the working copy under the
current versions. The changes of the path rows reuse places that the collector copies anyway or
copy less, except for the row below. The collector's changes reach repositories by their file
lists (`code_versions.antigravity_root_folder_agent_or_claude_manifest`,
`code_versions.cursor_plugin_manifest`, `code_versions.agent_role_file`,
`code_versions.opencode_agent_configuration`,
`code_versions.codex_or_copilot_marketplace_or_portable_overlay` and
`code_versions.codex_marketplace`). The working copy gains no file from them, because the files
that they accept are in directories that the collection copied whole, except for the marketplace
file below. `code_versions.codex_marketplace` copies the parts of no plugin that Codex does not
install, and over the tree it gives the plugin roots of the earlier collector: Of the Codex
marketplaces of the tree, only UMEP-dev/SUEWS's has no entry that Codex installs, and the earlier
collector already skipped its only entry, which is set to `NOT_AVAILABLE`.

The row of gstack's design documents reads
`docs/designs/notebook-linked-writing-workspace.md` of readest/readest, which the tree predates.
The readers of a plugin that a Copilot CLI marketplace lists copy
`src/msbuild/.github/plugin/marketplace.json` of dotnet/dotnet, which the tree predates as well.
The working copy fetches neither file at the collected commit,
[c46730772847](https://github.com/readest/readest/commit/c46730772847d5d551e73aa1b2adebff339f634c)
of readest/readest and
[fd470c6737f0](https://github.com/dotnet/dotnet/commit/fd470c6737f07cf2794aff59f6bf74121fee7dba)
of dotnet/dotnet. A repository with a per-repository commit record that gains a file after the
collection fails the path check of `commit-reclassification` (`no_path_history`), and its commits
would leave the commit CSVs. Without the marketplace file, `plugins.csv` lacks its row with the
tool `copilot`. The next collection collects both repositories again, because the changes reach
their file lists.

On 2026-11-07, `imports.copilot_at` reads a fence as Copilot CLI 1.0.107 reads it. Over the
working copy, the change takes away imports of accordproject/concerto and garmeeh/next-seo, all with
the status `missing`, and it adds none, so that it copies no other file. A file list cannot show a
fence, and a predicate over it would collect nearly every repository again without a copied file
that differs. The version in `agentcontext/code_versions.py` therefore has the reach `NO_REPOSITORY`,
and the cloning step of the working copy is recorded again without a fetch
(`run --mark-complete repository_cloning`).

On 2026-11-08, a chain of Claude Code or Gemini CLI from a root `claude.md`, `agents.md` or
`gemini.md` in another case opens each import as written and otherwise as the one entry of its
directory that matches it ignoring case, and for the chain of a root `agents.md` in another case,
a root CLAUDE file in any case keeps Claude Code from reading the `agents.md`
(`imports.ignores_case`). The change reaches each repository with such a root file
(`code_versions.root_file_in_another_case`), and the tree predates it. The import `@AGENTS.MD` of
the root `claude.md` of mzmine/mzmine now reaches `AGENTS.md`, which the cloning step copies to
`data/imported/`. The working copy fetched `AGENTS.md` of mzmine/mzmine at the collected commit
[216cf1f737c3](https://github.com/mzmine/mzmine/commit/216cf1f737c3e845a045f1f514e81b58df68c504)
into `data/imported/` (blob 6f48b270230518ede19cf2ba37bdb523e275cdee), and the cloning step is
recorded again after the fetch (`run --mark-complete repository_cloning`). `data/collected/`
already holds that file, so the fetch adds no row to `fetched_files.csv`, whose files the window
of the scan times leaves out.

On 2026-11-08, the planning heuristics also gained the rows of the conductor plugin of
wshobson/agents, a superpowers plan in `.claude/plans/` and the research notes of awesome-copilot.
These rows reach repositories by their file lists, copy no file that the tree lacks, and the
cloning step of the working copy is recorded again without a fetch. Later that day, the notes of
an import row and of four planning rows changed to name the evaluation of each item that they
cite, no pattern changed, and the cloning step is recorded again without a fetch.

The sample changed after the collection. The labels of run `2026-11-03T002808Z` selected the
repositories that the collection collected. Run `2026-11-06T170312Z`, which made no model request,
derived these labels again under the rule of 2026-11-05 that merged pull requests override only the
mirror exclusion ([classification](../2_classification/classify-repos-spec.md)), and it found
repositories gone that GitHub served before. GoogleChromeLabs/chromium-bidi, travisvroman/kohi,
knik0/faac, BookStackApp/BookStack, hank-ai/darknet and utopia-php/messaging are labeled `false`
under that rule. GitHub no longer serves Giveth/impact-graph, ONSdigital/eq-questionnaire-runner,
aukilabs/posemesh, clickfwd/yoyo and odigos-io/odigos. The working copy removed the files of these
repositories with `reuse.remove_left`, as the cloning step does at its end for a repository that
left its input list, and wrote `data/collected/repos.csv` and `clone_failed.csv` without their
rows. GoogleChromeLabs/chromium-bidi and odigos-io/odigos were the only ones of them with
configuration artifacts.

The published labels are those of run `2026-11-06T170312Z`, under the classification parameters
that `repos_annotated.json` records, and a later change of the classification applies from the
next run. The prompt
additions of 2026-11-06 and the reading of the links of a README change the parameters, so the next
run labels every repository again ([classification](../2_classification/classify-repos-spec.md)).
The corrected check of a quotation and the corrected language rule change these stored labels from
`unclear`, and each repository labeled `true` then needs a collection:

| Repository | Label in a run under the current code | Reason |
|---|---|---|
| asciidoctor/asciidoctorj | `true` | the quotation matches the rendered README |
| smallrye/smallrye-health | `true` | the quotation matches the rendered README |
| smallrye/smallrye-jwt | `true` | the quotation matches the rendered README |
| Almamu/linux-wallpaperengine | `true` | the quotation matches the rendered README |
| poldracklab/fitlins | `true` | the quotation matches the rendered README |
| ITISFoundation/osparc-simcore | `true` | the quotation matches the rendered README |
| qor/media | `true` | the quotation matches the rendered README |
| YOURLS/YOURLS | `true` | the quotation matches the rendered README |
| streamshub/console | `true` | the quotation matches the rendered README |
| FusionComputingLab/freegsnke | `true` | the quotation matches the rendered README |
| agglayer/provers | `true` | the quotation matches the rendered README |
| SpriteStudio/SS6PlayerForUnrealEngine4 | `false` | the count of the words names only Japanese |
| EsupPortail/esup-sgc | `false` | the count of the words names only French |

## Sampling and collection dates

The sampling stage of the run took retrieval `2026-11-02T223052Z` of GHS 1.17.1. Its sampling
window ends on 2026-10-22, the crawl date of Elm, the oldest of GHS's languages, and
<!--s:sampling.matched_repositories-->209,702<!--/s--> repositories matched the query. The filter kept <!--s:sampling.frame_repositories-->46,503<!--/s--> of them as the
sampling frame, `sampling/ghs_filtered.json` of the handover, with GitHub's repository id and times in UTC
([refresh strategy](refresh.md)). The classification run `2026-11-03T002808Z` labeled the
sampling frame with DeepSeek V4.1 Flash, and run `2026-11-06T170312Z` derived those labels again
without a model request. It keeps <!--s:classification_2026_11_06.labeled-->45,151<!--/s--> labels, excluded <!--s:classification_2026_11_06.excluded-->1,337<!--/s--> repositories before the model
and found <!--s:classification_2026_11_06.gone-->15<!--/s--> gone, and its `repos_excluded.csv` lists the excluded repositories with the
gone ones and the copies that it left out of the sample. The sample is the
<!--s:collection.sample_repositories-->40,901<!--/s--> repositories that it labels as engineered projects. The collection that
started on 2026-11-04 scanned them from 2026-11-04 20:20 to 2026-11-06 06:51 UTC under git 2.55.0.
The scan times do not show the age of the sampling and classification inputs.

The tracked `data/sampling/ghs_filtered.json.7z` and `data/classification/repos_annotated.7z`
contain the sampling frame and the labels of the run, which the analyses and the subsample run
read once extracted. The labels archive also contains the run's `repos_excluded.csv`, which the
release reads to state why a repository in the sampling frame has no label.

## Current output sizes

These counts describe the collection of the run that started on 2026-11-03, processed with the
current code. A row of a mechanism CSV is one configuration artifact, which can be a directory
with several files.
`output/imports.csv` has a row per tool and import, in <!--s:collection.imports.repositories-->2,170<!--/s--> repositories, and
<!--s:collection.imports.found-->6,048<!--/s--> of its rows reach a file. Adoption analyses state their denominator, because `output/repos.csv` contains only the
configured repositories, while the classified sample also includes repositories without
configuration. `tests/test_stated_facts.py` checks the counts below against the listed files.
They are not expected results of the heuristics.

| File under `data/collection/` | Rows |
|---|---:|
| `data/collected/repos.csv` | <!--s:collection.sample_repositories-->40,901<!--/s--> |
| `data/collected/clone_failed.csv` | <!--s:collection.clone_failures-->2<!--/s--> |
| `data/validated/repos.csv` | <!--s:collection.validated_repositories-->10,944<!--/s--> |
| `output/repos.csv` | <!--s:collection.configured_repositories-->10,944<!--/s--> |
| `output/settings.csv` | <!--s:collection.rows.settings-->3,532<!--/s--> |
| `output/context-files.csv` | <!--s:collection.rows.context_files-->25,353<!--/s--> |
| `output/skills.csv` | <!--s:collection.rows.skills-->23,585<!--/s--> |
| `output/subagents.csv` | <!--s:collection.rows.subagents-->2,713<!--/s--> |
| `output/commands.csv` | <!--s:collection.rows.commands-->1,436<!--/s--> |
| `output/hooks.csv` | <!--s:collection.rows.hooks-->619<!--/s--> |
| `output/rules.csv` | <!--s:collection.rows.rules-->3,483<!--/s--> |
| `output/mcp.csv` | <!--s:collection.rows.mcp-->885<!--/s--> |
| `output/plugins.csv` | <!--s:collection.rows.plugins-->751<!--/s--> |
| `output/lsp.csv` | <!--s:collection.rows.lsp-->17<!--/s--> |
| `output/other.csv` | <!--s:collection.rows.other-->12,911<!--/s--> |
| `output/imports.csv` | <!--s:collection.imports.rows-->10,662<!--/s--> |
| `output/planning-documents.csv` | <!--s:collection.rows.planning_documents-->27,593<!--/s--> |

`data/collected/clone_failed.csv` records the <!--s:collection.clone_failures:words-->two<!--/s--> repositories that git could not clone,
cuthbertLab/music21 as `not_found_or_no_access` and dethrace-labs/dethrace as `clone_failed`.
`output/snapshot.json` counts <!--s:collection.repositories_without_head:words-->two<!--/s--> collected rows without a commit hash, the rows of these repositories. The commit CSV of
the six tools contains <!--s:collection.commits.tools-->1,281,628<!--/s--> commits, and the commit CSV of the other tools contains
<!--s:collection.commits.other_tools-->72,527<!--/s-->.

## Evidence limits

In the collection, every repository with a commit history that was read kept its full commit log.
Under the current heuristics, `commit-reclassification` classified
<!--s:collection.reclassified_repositories-->11,112<!--/s--> of the
<!--s:collection.history_read_repositories-->11,112<!--/s--> repositories with a per-repository
commit record, those that the outputs list, and `complete` is true. `population_complete` is false because of the
<!--s:collection.clone_failures:words-->two<!--/s--> recorded clone failures.

Lodekeeper, the agent account `lodekeeper` of ChainSafe/lodestar, runs on OpenClaw, and its README
says "I'm an AI assistant powered by Claude". Its commits count for OpenClaw as `account`, and
<!--s:collection.openclaw.lodekeeper.claude.commits-->38<!--/s--> of its <!--s:collection.openclaw.lodekeeper.commits-->205<!--/s--> commits credit Claude as well:
<!--s:collection.openclaw.lodekeeper.claude.lines-->21<!--/s--> through a line that names Claude, as in `This PR was authored with AI assistance
(Claude).`, `(Claude Opus 4.6)` or `(Claude/Codex)`, and <!--s:collection.openclaw.lodekeeper.claude.trailer_only-->17<!--/s--> only through the co-author line that
Claude Code writes, which can record a change that OpenClaw delegated to Claude Code, as its skill
coding-agent does. The full-body rows read a line without regard to the author of the commit,
and a line that names Claude alone therefore credits Claude, although Lodekeeper may have run the
model in OpenClaw ([the commit heuristics](commits.md#the-tools-outside-the-six-2026-09-25)).

A line can also name a tool that a repository's instructions name, while another tool made the
change. readthedocs/addons
[5ca20a6af7a5](https://github.com/readthedocs/addons/commit/5ca20a6af7a5c7db0eca5dc4dee1ce667af25f24) ends with
`*Generated by Copilot*` and a link to a session of Claude Code, and its squash merge lists
`Co-authored-by: Claude <noreply@anthropic.com>`, because the repository's AGENTS.md and
`.claude/CLAUDE.md`, both titled "Copilot Instructions for Read the Docs", state "Put a footer note
that this was generated by Copilot". `copilot-full-body-05` and `copilot-full-body-07` cannot tell
that line from the footer of Copilot's own commits in readthedocs/readthedocs.org, and the commit
credits Copilot beside Claude, although only Claude made the change
([The credits of the final review of 2026-11-05](commits.md#the-credits-of-the-final-review-of-2026-11-05)).

A line can also stand for a tool that it does not name. rsyslog/rsyslog's AGENTS.md tells an agent
to write the footer `AI-Agent: Codex`. Its commits with `AI-Agent: ChatGPT`, such as
[ba32958f09cb](https://github.com/rsyslog/rsyslog/commit/ba32958f09cb92bed5bde72052015b86e2db42ac),
and its commit
[621ee6063c1b](https://github.com/rsyslog/rsyslog/commit/621ee6063c1b7c4ecef57c4a75a7a976f2e097be)
with `AI-Assisted: yes` are squash merges of pull requests from `codex/` head branches. These
lines name no coding agent, so that `tool-not-named-trailer-25` and `tool-not-named-trailer-22`
count the commits under `tool not named` and not for Codex
([The key-value lines that no heuristic credited, read 2026-11-08](commits.md#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)).

In its own clone, the cloning step measured the history of
<!--s:collection.file_history.collected_files-->196,849<!--/s--> collected files at the collected
commit. No row of `file_history.csv` lacks a history.

`output/snapshot.json` describes the current collection and the population of the outputs, and
`output/provenance.json` identifies the detection heuristics. The commit reclassification writes
`output/commit-reclassification.json`. Before it records every required commit history as
reclassified, it checks the commit logs, the resolved refs, the revision, the checksums and the
history of the copied paths. [The reclassification specification](steps/commit_reclassification.md)
defines `complete`, `population_complete` and each repository outcome.

A workflow named `claude.yml` or `claude-code-review.yml` counts as Claude's settings by its name
when it has a line that is not a comment ([a workflow that runs an agent](heuristics.md#a-workflow-that-runs-an-agent)).
cloudposse/atmos's `.github/workflows/claude.yml`, "Claude Quality Checks", runs only the
repository's own action `check-claude-md-size` with the token of the workflow and no Claude Code,
and it is a settings row of Claude all the same. The repository has other settings rows, and its
flags do not depend on the file. A test of the workflow's steps for a Claude action, a reusable
workflow or a command would also drop workflows that run Claude Code in another way, such as the
review workflow of evcc-io/evcc and the request to Anthropic's API of dosemu2/dosemu2, and the
detection step keeps the name rule.

The detection step leaves the `language` blank for an item behind a link with a target that the
validated tree lacks. A link can lead out of the repository, as the seven commands of
Thank-you-Linus/Linus-Dashboard's `.sapiens/command/` and aidenlx/zotlit's
`.claude/workflows/implement-tickets.js` do. It can lead into a submodule, whose files belong to
another repository, as two commands of sirius-db/sirius's `.claude/commands/` and
pingidentity/terraform-provider-pingone's `.github/agents/LocalCodeReview.agent.md` do. Or it
can lead nowhere at the collected commit. livekit/rust-sdks's
`.claude/skills/uniffi-swift-debug/SKILL.md` names a `livekit-uniffi` folder that the repository
no longer has, and the linked items of SciSharp/NumSharp, rivet-dev/rivet and likec4/likec4 name
paths that their repositories lack.

The planning documents keep two errors that the detection accuracy evaluation of 2026-11-04
confirmed, because no path tells them apart from the plans at the same place.
Synerise/synerise-design keeps a playbook of context for agent sessions in `.claude/plans/`, where
Claude Code writes the plans of plan mode (planning_precision pd-0086), and ScrollPrize/villa
writes the task plans of its own workflow as `task_plan.md`, the name of the plan of
planning-with-files (pd-0274). Most task plans of planning-with-files in the collection, such as
those of UniClipboard/UniClipboard, open with the title of their task and not with the
`# Task Plan:` of its template, so that the opening of a plan does not tell them apart either.
The planning precision of the evaluation counts both errors.

## Scope and interpretation

The sample covers the repositories that meet the study's selection criteria. Results drawn from it
do not directly represent all software development. A refresh of the sampling inputs keeps this
restriction and updates the population to which the criteria apply.

Committed configuration shows that configuration artifacts are present. It does not show actual
tool use, frequency of use, productivity or causal effects. Personal configuration can be absent
from a repository, and committed configuration can be unused. Commit attribution likewise
measures only the recorded credits that match the heuristics. AI assistance that nobody disclosed
cannot be observed. A credit does not show how much of the change the tool wrote, and a person
can type the line that a tool writes. The commit CSVs contain the commits that the cloning step
read from a single-branch clone of the default branch, with the history that its fetched tags
reach. They lack the commits of an agent on a branch that nobody merged into the default branch
and that no tag reaches.

Trends over time depend on the selected repositories and on the commit evidence that remains.
Deleted configuration and unavailable repositories can be missing from that evidence. A creation
date derived from the commit history of a path that the repository kept does not show when the
repository first used a tool. These limits apply even when the collection and the processing
succeed.

## Storage and regeneration

The tracked output CSVs, collection lists and caches contain the collection of the run that
started on 2026-11-03. The large commit CSV of the six tools is stored in the numbered 7-Zip
volumes `output/all_ai_commits.csv.7z.001` and on, and every volume is required.
`output/other_ai_commits.csv.7z` stores the CSV of the other tools, and
`output/all_ai_commits-unmatched-2026-11-04.csv.7z` the collection's unmatched attributions that
name a bot or a tool and no person.
`dev/setup.sh` extracts each of the two commit CSVs when it is absent or differs from its archive,
and keeps a CSV that matches its archive ([archives](archives.md)). It does not restore the March
inputs.

The complete collected tree, the file lists and the per-repository commit records stay in the
original handover and its working copy. The processing after the collection requires them. A Git
checkout alone is enough for the unit tests and the checks of the tracked outputs.
[Development](development.md) gives the native and container commands and the review procedure.
Each reviewed change needs a grounded regression test, and where appropriate a stronger
verification check, before the outputs are regenerated.

