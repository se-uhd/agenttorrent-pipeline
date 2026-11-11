# The detection accuracy evaluation

The detection accuracy evaluation uses labeled samples of a collection to measure errors and
omissions in configuration detection and commit attribution, and errors in import and planning
document rows.
`python -m agentcontext evaluation sample` draws the samples, and
`python -m agentcontext evaluation report` turns their labels into estimates. Each evaluation has
a directory of its own, `data/collection/evaluation/<day>/`, named by the day on which its
collection started. The same day names the collection's
`output/all_ai_commits-unmatched-<day>.csv.7z`, and `unmatched.collection_started` reads the day
from the list of finished repositories. Separate directories prevent item numbers from colliding, because every draw starts at 1. In the commands
below, `HANDOVER` stands for the directory of the extracted handover of the collection, and `DAY`
for the day on which the collection started.

```shell
python3 -m agentcontext evaluation sample \
    --file-lists HANDOVER/data/collection/data/file_lists \
    --commit-logs HANDOVER/data/collection/collect_commits \
    --mentions HANDOVER/data/collection/output/all_ai_commits-mentions.csv \
    --finished HANDOVER/data/collection/finished_repositories.jsonl
python3 -m agentcontext evaluation report data/collection/evaluation/DAY
```

`sample` reads the output CSVs of the collection under `data/collection/output/` and, from the
collection's handover, the file lists, the per-repository commit records, the mentions CSV and
the list of finished repositories, with the record of the collection in progress,
`finished_repositories.collection.json`, when that record is beside the list. It writes into the
directory of the day on which the collection started, under `--evaluation-dir`, and exits 1 when
that directory already holds a `sample.json`, because the items of a second draw would take the
labels of the first draw's items. Before it draws, `sample` also exits 1 when a commit CSV
differs from the archive that git tracks beside it, or when 7zz cannot list that archive. In a
checkout, such a CSV is not the committed input, and the samples drawn from it would not be the
samples of the commit ([archives](../archives.md)). In a working copy, git
tracks no archive, and `sample` draws from the CSVs that the collection wrote. `report` reads the
samples and the labels of the directory that it is given and writes the report there.

`sample --only` draws the samples of `imports.csv` and `planning-documents.csv` under
`--output-dir` into the directory of an evaluation that holds a draw, so that an evaluation with
labels gains the two samples without a new draw of the others:

```shell
python3 -m agentcontext evaluation sample --only import_precision planning_precision \
    --into data/collection/evaluation/DAY
```

`--only` names one or both samples, and `--into` the directory. Such a draw reads neither the
file lists, the per-repository commit records, the mentions CSV nor the list of finished
repositories. It takes the seed that the `sample.json` of the directory records, so that each
sample has the items that a draw of the whole collection with that seed gives it. It exits 1
when the directory has no `sample.json`, when it holds the CSV of a named sample or its labels in
`labels/`, when `--seed` names another seed than the recorded one, or when the CSV of a named
sample is missing. It writes the samples into the directory and appends a record of the draw to
`sample.json` under `later_draws`, and the samples, the labels and what `sample.json` records for
the earlier draw stay unchanged.

`sample.json` records the seed, the SHA-256 of every input file, and one SHA-256 for each of the
two directories, computed over the path and the SHA-256 of every file in it, so that the samples
can be drawn again. The `sample.json` of the evaluation of 2026-10-05, drawn on 2026-10-13,
predates the digests of the directories and records only the files. Neither the `sample.json` of
the evaluation of 2026-10-05 nor that of the evaluation of 2026-10-26 records the digest of the
list of finished repositories. Beside `sample.json`, `sample` keeps an xz-compressed copy of the
mentions CSV that it read, under the CSV's name with `.xz`, such as
`all_ai_commits-mentions.csv.xz`. `sample` keeps the copy because every commit reclassification
rewrites the mentions CSV, and the CSV that a reclassification in a working copy wrote is in no
handover. Git ignores the copy, because the lines of the CSV can name people. The other inputs need
no copy: Git tracks the mechanism CSVs, `imports.csv`, `planning-documents.csv` and
`ai_commits-repos.csv`, and the archives of the two commit CSVs rather than the CSVs. The
handover keeps the file lists, the per-repository commit records and the list of finished
repositories. A record under `later_draws` names the samples that the draw added, the number of
repositories that the `evidence` of the heuristics links, the sizes, the SHA-256 of each CSV that
it read and of each heuristics file, and the strata drawn from these repositories, which
[the samples](#the-samples) describes.

Each evaluation draws its samples anew and labels every item. Reusing the labels of an earlier
evaluation would save little: A new draw from the same collection would share only the items of
the small cells of `path_precision` (each a pair of a `tool` value and a mechanism) and the most
frequent phrasings of `mentions`, and almost no item of the other samples, which draw a small
share of large populations. A draw that keeps the earlier items would need estimates weighted by
inclusion probability, or permanent random numbers per item. Labeling every item is affordable,
because agents label the items (decided on 2026-10-25).

## The samples

| Sample | Item | Drawn |
|---|---|---|
| `path_precision.csv` | A row of a mechanism CSV | Up to <!--s:parameters.agentcontext.tools.evaluation.PATH_PRECISION_PER_CELL:words-->ten<!--/s--> rows for each pair of a `tool` value and a mechanism, with one row per repository before any repository gets a second |
| `path_recall.csv` | A repository | <!--s:parameters.agentcontext.tools.evaluation.RECALL_UNCONFIGURED-->200<!--/s--> of the repositories without detected configuration, which no output CSV lists, `other.csv` included, and which are not clone failures, and <!--s:parameters.agentcontext.tools.evaluation.RECALL_CONFIGURED-->100<!--/s--> of the repositories that an output CSV lists. Among the repositories without detected configuration are those from which the cloning step copied no file and those from which the collected file cleanup removed every collected file because the detection found no mechanism. `path_recall_candidates.csv` lists up to <!--s:parameters.agentcontext.tools.evaluation.CANDIDATES_PER_REPOSITORY-->200<!--/s--> candidate paths of each file list, and in `detected` whether the CSV of a mechanism lists each path (`yes`), only `other.csv` lists it (`other`), or no CSV does (empty) |
| `commit_precision.csv` | A matched commit and one tool that it credits | <!--s:parameters.agentcontext.tools.evaluation.COMMIT_PRECISION_PER_TOOL-->60<!--/s--> per tool from the commit CSV, one for each commit heuristic that the per-tool samples leave out, and <!--s:parameters.agentcontext.tools.evaluation.COMMIT_PRECISION_PER_TOOL-->60<!--/s--> from the CSV of the tools outside the six |
| `commit_recall.csv` | A commit that the cloning step read | <!--s:parameters.agentcontext.tools.evaluation.COMMIT_RECALL_COMMITS-->1,000<!--/s-->, with every commit equally likely among the repositories with a commit history that was read, and the tools that the current heuristics find |
| `mentions.csv` | A phrasing that names a tool in commits that no heuristic matched | <!--s:parameters.agentcontext.tools.evaluation.MENTIONS-->200<!--/s-->, each drawn with a weight equal to its number of commits |
| `import_precision.csv` | A row of `imports.csv`, an import that a tool reads and the target that it reaches | Up to <!--s:parameters.agentcontext.tools.evaluation.IMPORT_PRECISION_PER_CELL:words-->seven<!--/s--> rows for each pair of an import heuristic (`heuristic`) and a target status (`target_status`), with one row per repository before any repository gets a second |
| `planning_precision.csv` | A row of `planning-documents.csv` | Up to <!--s:parameters.agentcontext.tools.evaluation.PLANNING_PRECISION_PER_CELL:words-->twenty<!--/s--> rows for each framework or tool (`framework`), with one row per repository before any repository gets a second |

A row of `import_patterns.csv` names the tool, the importing files and the syntax, with the
conditions and the limits of the chain ([imports](../heuristics.md#imports)), and the status of
a row of `imports.csv` states what happens to its target, such as `found`, `missing` or `outside`
(`imports.STATUSES`). A pair of an import heuristic and a status therefore gets items wherever
`imports.csv` has a row of it, also a pair with few rows. The planning documents have one stratum
per framework or tool, as `framework` of `planning-documents.csv` records it, and because they
have fewer strata than the imports have pairs, each stratum gets more rows. Both samples write
the stratum of an item into `stratum` and the rows of the stratum into `stratum_rows`, and the
rows include those in the repositories that the samples leave out. Where the `evidence` of a
heuristic links the repository of every row of a stratum, the stratum has no row elsewhere to
draw. The draw then takes its items from these repositories, with the size and the generator of
every other stratum, and sets `from_evidence` to `yes` for each of them (decided on 2026-11-08).
For every other item, `from_evidence` is empty. The heuristics of such a stratum were read off
the repositories that its items come from, and a precision that includes the stratum can be
biased toward the heuristics. [The report](#the-report) gives the precision without these strata
beside the precision over all strata, and the difference shows how far these strata move the
estimate. `sample.json` records the strata drawn from these repositories, with their rows, under
`strata_from_evidence`, in the record of the draw or of a later draw.

An item of `import_precision.csv` names its importing file under `data/collection/` in
`importer_local_path`, in the validated tree where the file starts the chain (`root`) and in the
imported tree where an import reached it, because the cloning step copies every file that an
import reaches to the imported tree. It names a found target in `target_local_path`, in the tree
of its `target_location`: the validated tree for `repos_data.7z` and the imported tree for
`imported_files.7z` ([import detection](../steps/import_detection.md)). An item of
`planning_precision.csv` names its planning document in the validated tree in `local_path`.

A candidate path names one of the six tools or a file that they share, or it is at a place that
a row of `heuristics/config_path_patterns.csv` names, matched as `agentcontext.reuse` matches a
row's path: its components anywhere in the path, in any letter case. The rows add candidate paths
that name no tool, such as `REVIEW.md`, `tui.json`, `AGENTS.override.md`, `.github/lsp.json`, the
Agent Plugins folders `.plugin/` and `.github/plugin/`, Antigravity's `_agents/` and the GitHub
Agentic Workflows in `.github/workflows/*.md`. A row adds no place where its path is a generic name
that the row reads only by content, beside a plugin's manifest or among a project's other files: a
plugin's parts under a plugin placeholder, such as `<claude-plugin>/skills`, the files beside a
manifest (such as `README.md`, `LICENSE` and `package.json`), a manifest or marketplace with a bare
name (i.e., `plugin.json` and `marketplace.json`), the files of `.github` that no other row names,
and the YAML workflows, which are a project's CI unless their content runs an agent. Such names
would fill the candidates of a repository with files that are no configuration. The comment above
`CANDIDATE` in `agentcontext/tools/evaluation.py` names the match modes of the rows that add no
place.

The repository tracks the evaluation of the October 5–6 collection in
`data/collection/evaluation/2026-10-05/` and the evaluation of the collection of the first
complete run in `data/collection/evaluation/2026-10-26/`. The samples of the evaluation of
2026-10-05 were drawn on 2026-10-13, before OpenCode had heuristics. Their candidate paths name
one of the other five tools or a file that these tools share, and OpenCode's commits are in the
sample of the tools outside the six. A candidate that only `other.csv` lists has the `detected`
value `yes` in this evaluation. The samples of the evaluation of 2026-10-26 were drawn on
2026-10-29, before the candidate paths included the places that the
rows of the path heuristics name and before `detected` marked the paths that only `other.csv`
lists. Their candidate paths name one of the six tools or a file that they share, and a
candidate that only `other.csv` lists has the `detected` value `yes`. The resolutions of both
evaluations therefore leave ignore files such as `.cursorignore` and `.aiexclude` out of
`paths`. A draw with the same seed under the current code gives other candidates, and with them
it can draw other repositories for the sample of path recall.

Both evaluations drew the stratum without detected configuration from the repositories from
which the cloning step copied no file, with the outcome `no_files` in `ai_commits-repos.csv`.
That stratum left out the repositories from which the collected file cleanup removed every
collected file because the detection found no mechanism, <!--s:collection_2026_10_05.detection_evaluation.path_recall_left_out-->120<!--/s--> in the evaluation of 2026-10-05
and <!--s:collection_2026_10_26.detection_evaluation.path_recall_left_out-->159<!--/s--> in the evaluation of 2026-10-26, such as CloudLLM-ai/cloudllm with its
candidate paths in `.claude/agents/`. The report of each evaluation therefore states the share of
repositories with missed configuration, and the number of repositories that it represents, for
the repositories from which the cloning step copied no file. The evaluation of 2026-10-26 keeps
its sample of path recall, because a new draw from the current stratum would contain other
repositories, and the reviewed labels of the repositories drawn on 2026-10-29 would be lost
(decided on 2026-10-30).

The mentions CSV of the evaluation of 2026-10-26 is the one that the commit reclassification of
2026-10-29 wrote in the working copy of the handover, with the SHA-256 that its `sample.json`
records, and the mentions CSV that the cloning step wrote into the handover differs. A draw from
that CSV with the recorded seed reproduces the items of `mentions.csv`. The history of the private
repository `se-uhd/agenttorrent-pipeline-internal` holds it. The directory of the evaluation of 2026-10-05 has
no copy of its mentions CSV.

`commit_recall.csv` and `mentions.csv` write `(a person)` for the identity, the GitHub mention and
the link to the profile of a person, by the rules of this section. Before a draw is tracked, a
person masks by hand what these rules do not read, as the end of this section describes.
`commit_recall.csv` keeps the identity of a bot and writes `(a person)` for any other author or
committer.

A bot has a name with `[bot]` or the word `bot`, or a name that a row of `not_ai_commits.csv` reads,
at an address that names no person. The GitHub noreply address of an account names no person when
the login has `[bot]` or ends in `-bot` or `_bot`. Any other address names no person when its domain
is no personal mail service, such as gmail.com, its mailbox has no `+`, and the mailbox is
`noreply`, `no-reply`, `support`, `bot` or `robot`, or ends in `-bot`, `_bot`, `-robot` or `_robot`,
at a domain that fits the name: the label before the top-level domain, or before the second-level
label of a country, as in `example.co.uk`, is in the name without its spaces or holds a word of the
name with at least four letters, as in `Mend Renovate <bot@renovateapp.com>`. A name that a row of
`not_ai_commits.csv` reads also stays when its mailbox is the name, at any domain but a personal
mail service and without a `+`, as in `gemini-cli-robot <gemini-cli-robot@google.com>`, the identity
under which the release workflows of google-gemini/gemini-cli commit. At a domain with that row as a
label, the name stays at a mailbox of the automation itself: the mailbox `noreply`, `no-reply` or
`support`, alone or before `-`, `+`, `_` or `.` and more, or a mailbox that spells the words of the
name before the row, so that the address spells the name.

Weblate writes a machine translation under an internal bot at `noreply-{scope}-{name}@weblate.org`
(`DEFAULT_INTERNAL_BOT_EMAIL_TEMPLATE` in weblate/auth/defaults.py of weblate 06ca66345304), as in
`Weblate <noreply-mt-weblate@weblate.org>`. Crowdin's GitHub action commits as
`Crowdin Bot <support+bot@crowdin.com>` (`github_user_email` in action.yml of crowdin/github-action
d651c77756ac: `default: 'support+bot@crowdin.com'`). "Hosted Weblate has a dedicated push user (with
the username weblate, e-mail hosted@weblate.org, and a name or profile description Weblate push
user)" ([Weblate](https://docs.weblate.org/en/latest/vcs.html), read on 2026-11-08), and it commits
as `Hosted Weblate <hosted@weblate.org>`, as in the committer of WeblateOrg/weblate b870187066f3.
Any other mailbox at the domain of the automation can be a person's, as in
`Weblate Jane <jane@weblate.org>`. The maintainer decided on 2026-11-08 that the identity of an
automation that a row of `not_ai_commits.csv` reads stays, because `(a person)` would credit its
commits to a person in the tracked sample, as it would for the author of `cr-0627` of the evaluation
of 2026-10-26, a machine translation of Weblate.

`commit_recall.csv` also keeps an identity at an address that an author heuristic other than a
convention reads when the name names the tool of that heuristic. At
the GitHub noreply address of an account, such as `198982749+Copilot@users.noreply.github.com` of
the app copilot-swe-agent, the name has the words of the login, without `[bot]` and the word `bot`,
as `Copilot` does. At any other address, the name begins with the tool, as in
`Cursor Agent <cursoragent@cursor.com>` and `Claude <noreply@anthropic.com>`, or with a label of the
domain of the address and then the tool, as in `GitHub Copilot <copilot@github.com>`, where
`copilot-trailer-02` reads `github` before `copilot`. At either, such a heuristic of the same tool
can read the name instead, as `copilot-author-02` reads `copilot-swe-agent[bot]`. A bot at such an
address stays as well, as `Kilo Bot <kilo-bot@kilo.ai>` does at the address that
`kilo-code-author-01` reads. So does an identity that such a heuristic reads only with its name and
address together, such as `Codex <noreply@openai.com>`. At the GitHub address of an app and at the
mailboxes `noreply` and `no-reply`, the name of one of the six tools stays, alone or followed by a
word for the kind of tool such as `cli`, as in `Gemini <noreply@google.com>`, and any other name
there can be a person's. When the last words of any other name form an identity that stays,
`(a person)` replaces the words before them, as in
`(a person) Claude Opus 4.8 <noreply@anthropic.com>`. Otherwise `(a person)` replaces the whole
name, and the address stays after it where an author heuristic other than a convention reads the
address, as in `(a person) <198982749+Copilot@users.noreply.github.com>`. Each parenthesis of such
a name that begins with the name of one of the six tools stays after `(a person)`, as
`(a person) (Claude)` does for `Jarvis (Claude) <jane@example.org>`. `commit_recall.csv` keeps the
message of a commit that a heuristic credits as `mentions.csv` keeps a line, and leaves the message
of any other commit empty, and its `github_link` is the evidence for every item. A commit CSV
publishes the message of a credited commit verbatim, and the maintainer decided on 2026-11-08 that
`commit_recall.csv` masks the people in it nonetheless, so that the tracked sample of commit recall
treats the people of a message as the tracked sample of mentions treats the people of a line.

`mentions.csv` treats each identity in a line, a name before an address in angle brackets, as
`commit_recall.csv` treats an author. After the token of a trailer, such as `Co-authored-by:`, the
name is all that follows the token on its line, parentheses and quotes included, so that
`Co-authored-by: Jane Doe (test merge) <jane@example.org>` becomes `Co-authored-by: (a person)`.
Elsewhere in a line, the name is up to five words before the address, and these words can include
words of the line before the name. All of them stay when an identity of their last words stays, as
in `Assisted with Claude <noreply@anthropic.com>`, because the sample keeps the other words of a
line as well. An address alone stays when `commit_recall.csv` would keep it under the login of the
address or else its mailbox, as `198982749+Copilot@users.noreply.github.com` does, and
`(a person)` replaces any other. An address alone ends in a label that begins with a letter, as a
top-level domain does, so that a package with its version, such as
`@tiptap/extension-collaboration-cursor@0.0.0-beta.0`, stays whole.

Both samples write `(a person)` for a GitHub mention of a login, such as `@janedoe`, and for the
login of a link to a GitHub profile, such as `https://github.com/janedoe`, in the lines and the
patterns of `mentions.csv` and in the authors, the committers and the messages of
`commit_recall.csv`. They keep the login of a bot or a tool: a login that names a bot or that is a
whole row of `not_ai_commits.csv`, such as `dependabot`, a login that an author heuristic other than
a convention reads alone or with `[bot]`, such as `copilot-swe-agent` and `coderabbitai`, and the
name of one of the six tools, alone or followed by a word for the kind of tool, such as `@claude`
and `@gemini-cli`. A login that begins with the name of a tool can be a person's, as the login in
the line of `mn-0153` of the evaluation of 2026-10-05 is, and a login that ends in a hyphen, which
GitHub's API records for accounts of the type User, is never the name of a tool. A login before a
slash is the scope of an npm package, and a login before a dot and a letter names a file, as
`@AGENTS.md` does, and both stay.

Dependabot and Renovate write a zero-width space after the `@` of each mention in the release notes
that they quote, and a commit can quote their text in turn. Dependabot writes the character U+200B
(`insert_zero_width_space_in_mention` in dependabot-core: `mention.sub("@", "@\u200B")`) to keep
the mention from "triggering notifications to users who've been mentioned in changelogs", and
Renovate writes the HTML entity `&#8203;` (`sanitizeMarkdown` in renovate:
`part.replace(regEx(/@/g), '@&#8203;')`) "to prevent unintended hyperlinking", as in
`[@&#8203;janedoe](https://redirect.github.com/janedoe)`. Both samples read a mention after the
character and after the entities `&#8203;` and `&#x200b;`, and in a pattern, where
`commits.mention_pattern` writes `0` for the digits, after `&#0;` and `&#x0b;` (decided on
2026-11-08). A link to a profile has the login as its whole path, with or without `https://`, at
`github.com` and `www.github.com`, where Dependabot links each mention, and at `togithub.com` and
`redirect.github.com`, through which Renovate has redirected the links to GitHub in what it quotes.
The login ends where the link ends, as before `)`, `"`, `>`, `?`, a space or the end of a line, and
a link with more path after the login, such as the link of a repository, stays. The maintainer
decided on 2026-11-08 that a profile link is masked by the rule of a mention, because it holds the
same login.

The maintainer decided on 2026-11-07 that an `@` in a Markdown code span or a fenced code block is
code and no mention of a person. Both samples keep it whole, as the line of `mn-0104` of the
evaluation of 2026-10-26 keeps the route decorator `@generation`. The maintainer decided on
2026-11-08 that an `@` with a zero-width space after it is a mention in code as well, because
Dependabot writes each mention that it quotes as a code span of `@`, U+200B and the login in a link
to the profile, and the zero-width space only keeps the mention from notifying or linking the
login, while a plain `@` in code stays whole. A profile link is masked in code as well. As
CommonMark defines them, a code span is a run of backticks, the code and a run of as many backticks
on the same line, and a fenced code block runs from a line that starts with at least three
backticks or tildes, after up to three spaces, to the next line that starts with at least as many
of the same character, with only whitespace after them, or to the end of the text.

The rules read a person only in an identity with an address, a mention and a link to a profile. A
name without an address stays, and so does a login outside a mention and a profile link, such as
the name in `<strong>Jane Doe</strong>`, the logins in `Thanks to janedoe and Claude`, the owner of
a fork in a merge subject such as `Merge pull request #1 from janedoe/branch`, the login of the
owner of a GitHub Action, as in `use the janedoe PR action`, and the user in a home directory such
as `/Users/janedoe/`. A plain `@` before a login in code stays whole as well, by the decisions of
2026-11-07 and 2026-11-08, even where it credits or links a person, as in ``by `@janedoe` in`` and
in ``<a href="https://github.com/janedoe">`@janedoe`</a>``, where the rules mark the login of the
link and keep the code span. Before a draw is tracked, a person reads its lines, patterns and kept
messages and writes `(a person)` for each such name and login. The full name of a repository stays,
as its link does, and so does its owner in the merge subject of a branch of that repository. In
the tracked evaluations, every cell of `commit_recall.csv` and `mentions.csv` is what the rules
give for the texts as they were drawn, except the cells that a person masked. In the evaluation of
2026-10-26, these are the message of `cr-0448`, with the home directory `/Users/(a person)/`, and
the line and the pattern of `mn-0037`, with the owner of an action in
`phpstan-bot and (a person) PR action`, which the `evidence` of its label quotes in the same form.
In the evaluation of 2026-11-04, they are the line and the pattern of `mn-0086`, with a display name
in `<strong>(a person)</strong> and <strong>Claude Opus 4.8</strong>`, and of `mn-0109`, with two
logins in `Thanks to (a person), (a person) and Claude !`.

`sample` writes the identities and the messages for the labelers into `commit_recall-texts.csv`,
which git ignores. In the labels of the evaluations, `(a person)` stands for the name, the address
or the login of a person in `evidence` and `note`, such as the author of a pull request and the
owner of a fork in a merge subject, and the history of the private repository holds the texts as
they were labeled. The identity of a tool, an app, a bot account or a company account stays, such
as `dependabot[bot]`, `Cursor Agent <cursoragent@cursor.com>` and `MONTREAL.AI <info@montreal.ai>`.
The full name of a repository stays, as its link does, and so does its owner in the merge subject of
a branch of that repository. Where that owner also opened the pull request or wrote a commit of the
branch, the label writes "the owner of the repository" for the author. `commit_precision.csv` keeps
`commit_message`, because a commit CSV publishes it.

The samples leave out every repository that the `evidence` of a heuristic links, because the
heuristics were derived from those repositories. Only a stratum of `import_precision.csv` or
`planning_precision.csv` with every row in such a repository is drawn from them, as
[the samples](#the-samples) describes. When a labeled item leads to a heuristic, link the item's
commit or file in the heuristic's `evidence`, so that later samples leave that repository out too.
Name the item in the heuristic's `notes` with its sample and its evaluation, as
`<sample> <item> of the evaluation of <day>`, such as
`mentions mn-0057 of the evaluation of 2026-10-05`. `tests.test_tools_evaluation` looks the item
up in the directory of that day, and it fails while a reference names no evaluation, names an
item that the evaluation lacks, or the `evidence` does not link the item.
A seeded fifth of every sample, the items with `second_pass` set, is labeled a second time by a
labeler who does not see the first labels. The labels are not blind to the heuristics, because
the labelers see which heuristics matched an item, and the people and agents that label the
samples also write the heuristics. State this limit beside an estimate that you publish.

`sample` also writes measurements of the heuristics that need no labels.
`commit_heuristic_attribution.csv` gives, per commit heuristic, the commits and repositories that
it credits, the commits that no other heuristic of the tool credits, and the repositories that
would lose every commit of the tool without it. `path_heuristic_yield.csv` gives, per path
heuristic, the rows of its mechanism's CSV with a tool that the heuristic names and a
configuration artifact that its path matches anywhere, as `agentcontext.reuse` matches a path. It
also gives the rows that no other heuristic matches. Detection records no heuristic per row, and
the yield is therefore an approximation that counts a row once for every heuristic that could
have produced it. A `collect_only` heuristic copies files that no mechanism CSV lists, and its
yield is always 0.

## The labels

[The record of the evaluation of 2026-11-04](../history/2026-11-detection-evaluation.md) states
who labeled and resolved its samples, the model, the workflows with their prompts, and where the
transcripts are kept.

Write the labels into `labels/<sample>.csv` with the columns `item`, `pass` (1 or 2), `label`,
`tool`, `mechanism`, `attribution_correct`, `paths`, `uncertain` (`yes` or empty), `evidence` and
`note`. Give the evidence of every label in `evidence`: the vendor page or the heuristic row that
decides it, a quote of the line or the file, or what the file contains. Set `uncertain` to `yes`
when the evidence does not settle the label. A person reads `review.csv` and writes the labels
that settle its items into `labels/resolutions.csv`, with `item`, `label`, `tool`,
`attribution_correct`, `paths` and `note`, and these labels replace the passes. Judge the
`attribution_correct` of a resolution under the meaning that the passes used. Label an item of
`import_precision.csv` or `planning_precision.csv` with `from_evidence` set to `yes` like any
other item of its sample.

**`path_precision`.** The configuration artifact is at `local_path` under `data/collection/`.

- `correct`: a product of the named tool reads the configuration artifact as the named mechanism
  at its location, according to the vendor's documentation, which the matching row of
  `heuristics/config_path_patterns.csv` cites, or to the product's own code. For `any`, at least
  one of the six tools reads the shared configuration artifact. For several tools joined with
  `|`, each of them reads it. For `other`, the file is in a configuration directory of the named
  tool, which no named mechanism covers. A configuration artifact in a format that its vendor
  has since retired is correct when the tool read it in that format, such as Antigravity's
  workflows, which Antigravity stopped reading on 2026-10-19.
- `wrong_tool`: configuration of another tool, or the named tools are not the tools that read it.
- `wrong_mechanism`: configuration of the tool, but as another mechanism.
- `not_configuration`: no tool reads it as configuration, because of where it is or what it
  contains, such as a format that the tool refuses.

**`path_recall`.** `paths` lists every candidate path of the repository that a product of the six
tools reads as configuration at its location, as one of the ten mechanisms. A file that the
heuristics keep in `other.csv` by design, such as `.cursorignore`, `.aiexclude` or
`.worktreeinclude`, belongs to no mechanism and is not listed. The label is `missed` when the
detection did not find one of these paths, and `none` otherwise. A path that no CSV lists has
an empty `detected` in `path_recall_candidates.csv`. A path that only `other.csv` lists has
`other` and counts as not found as well, because the detection gave it no mechanism. A
repository with no candidate in its file list needs no label and counts as `none`.
`heuristics/not_configuration.csv` lists paths that no tool reads as configuration, and also files
that a tool reads but that a maintainer decision leaves uncounted, such as the Markdown files with a
description in their front matter directly in `.claude/skills/` and `.agents/skills/`, which
OpenCode 2 reads as skills, under the decision of 2026-11-07
([Configuration artifact detection](../heuristics.md#configuration-artifact-detection)). `paths`
lists such a file, because a product of the six tools reads it. A file there without a description,
such as a `README.md`, is not listed, as the rows of `.opencode/skills` count no such file, because
"skills without one are not advertised" ([skills](https://opencode.ai/docs/skills/)).

**`commit_precision`.** The commit is labeled for the tool of its item.

- `correct`: the commit credits a product of the tool with making the commit or producing its
  change, under the attribution criterion of [the heuristics](../heuristics.md#ai-contribution-detection).
  The product made the commit, including an empty commit, wrote the change, applied the change
  after a person confirmed it, or suggested the change in a review that a person committed.
- `wrong_tool`: the text credits another product or tool.
- `not_contribution`: the text that the heuristics matched credits no tool with the change,
  such as a person whose name resembles a tool's, a mention of the tool, or a branch named after
  it.

A credit that the message only quotes from another commit is `not_contribution`, and a credit that
a squash merge or a cherry-pick copies with the change is `correct`, as [Detection scopes](../commits.md#detection-scopes) states.

`attribution_correct` is `yes` when `attributions` gives the tool the attributions that its
lines have, as the Attributions table in [AI contribution detection](../heuristics.md#ai-contribution-detection) defines them, and `no` otherwise. The sample of
the evaluation of 2026-10-05, drawn on 2026-10-13, recorded the attributions under the names that this pipeline used then: `kind` for the
column and `product` for the value that is `standard` now. `product` then covered any co-author
line that named the tool, whatever its address, and the labels were given under that meaning. The
column and the values were renamed on 2026-10-14 without a change to what was recorded.

**`commit_recall`.** The label is `credits` when the commit's identities or message credit one or
more of the six tools under the same criterion, with the tools in `tool` joined with `|`, and
`none` otherwise. Write a credit to a tool outside the six into `note`. Commit recall does not
cover a branch that the clone did not fetch, because the sample covers the commits that the
cloning step read ([scope and interpretation](../dataset.md#scope-and-interpretation)).

**`mentions`.** The label is `attribution` when the line credits the tool with producing the
change of its commit, as a trailer, an identity or a disclosure does, and `not_attribution` when
the line mentions the tool in another way, such as a link to its account, a dependency or a
person's handle.

**`import_precision`.** The importing file is at `importer_local_path` and a found target at
`target_local_path` under `data/collection/`, and both are on GitHub at `head_sha`. The row of
`heuristic` in `heuristics/import_patterns.csv` gives the tool's syntax, what a path resolves
against, the depth, the limits and the conditions, with the vendor page that states them, and
`heuristics/not_imports.csv` names what a tool does not read as an import
([imports](../heuristics.md#imports)). A row is labeled by what the tool does with the import
as written, whatever its status, also where the collection does not hold the target.

- `correct`: the tool reads `import` on `line` as an import, resolves it to `target`, and
  `target_status` states what happens to the target. A row of any status can be `correct`. A
  `missing` row is `correct` where the repository has no file at `target` at `head_sha`, as for a
  package name that Claude Code reads as a path, and an `outside` row where the path leaves the
  repository as written, as `@~/.claude/notes.md` does. A `not_read` or `over_limit` row is
  `correct` where the tool skips the target for the reason that the status names. The statuses
  describe the repository at the collected commit, as the comment above `imports.STATUSES`
  defines them, and a target in a gitlink or in `.git`, or behind a link that leads out of the
  repository, is therefore `missing`. The statuses `pruned` and `not_copied` state what the
  collection holds and not what the tool does, and a row with one of them is `correct` when the
  tool reads the import and resolves it to `target`.
- `not_loaded`: the tool reads no import in this text, whatever the status of the row. The text
  is in a code span or a fenced code block that the tool skips, it has a form that the tool does
  not read, or the tool reads no import of the importing file, such as an AGENTS.md that Claude
  Code reads only without a CLAUDE.md (`no_claude_md`).
- `wrong_target`: the tool reads the import, and the row misstates what it reaches: The tool
  resolves it to another path than `target`, or `target_status` is wrong, such as a `missing`
  row of a path with a regular file of the repository at `head_sha`. A `found` row is
  `wrong_target`, and not `not_loaded`, where the tool reads the import and loads no file
  through it, as for a target that resolves outside the repository or that the tool skips for
  its extension, its content or the limits of the chain. `paths` names the file that the tool
  loads, and it is empty where the tool loads none.

**`planning_precision`.** The planning document is at `local_path` under `data/collection/` and
on GitHub at `head_sha`. The first row of `heuristics/planning_document_patterns.csv` that
matches the path gives the framework or the tool, cites the page that states the location in
`doc_url` and quotes it in `notes`, and `heuristics/not_planning_documents.csv` names the
layouts that are left out ([planning documents](../heuristics.md#planning-documents)).

- `correct`: a planning document that the framework or the tool of `framework` writes or reads at
  its location, as its documentation states. A planning document is a work product of a
  spec-driven development framework or of a planning feature of one of the six tools, such as a
  spec, a plan, a task or a design. The project context that a framework generates for agents to
  read counts too, such as BMAD's `project-context.md`, the codebase map of Get Shit Done and
  Conductor's `product.md`, `tech-stack.md` and `workflow.md`.
- `wrong_framework`: a planning document of another framework or tool, such as a spec of Kiro in
  `.kiro/specs/` without the `spec.json` of cc-sdd, or a feature folder of Spec-Flow that a row
  of Spec Kit reads. `tool` names that framework or tool, as `framework` of
  `planning_document_patterns.csv` names it where a row names it.
- `not_planning_document`: no planning document, such as a template, metadata, a script, an
  installation or the configuration of a framework, or a file that a project keeps at the
  location for a purpose of its own.

Write in `note` where `document` names another document of the framework than the file is.

## The report

`report` writes `report.json`, `report.md` and `review.csv`. It exits 1 while an item has no label
or a label is not one of the labels above. Each proportion has its Wilson interval at a confidence
of 0.95. Path precision is weighted by the rows of each tool and mechanism, and the commit
precision of the six tools by the commits of each tool. The interval of these two estimates is a
normal interval around the weighted estimate. Its variance uses the finite population correction
and each stratum's proportion with two successes and two failures added, as Agresti and Coull
adjust a proportion. With a stratum's own proportion, the interval would have no width where
every sampled row is correct, which is often the case for the <!--s:parameters.agentcontext.tools.evaluation.PATH_PRECISION_PER_CELL:words-->ten<!--/s--> rows of a cell.

For path recall, `report` gives, per stratum, the share of repositories with configuration that
the detection missed, the number of repositories without detected configuration that this share
represents, and the share of configuration paths that the detection found in the repositories
with configuration. A configuration path with the `detected` value `other` counts as not found
in this share, because the detection gave it no mechanism. For commit recall, it gives the share
of the sampled commits that credit a tool that the heuristics find. For the mentions, it gives
the share of phrasings that are attributions and that no heuristic matches. For the imports and
the planning documents, it gives the precision weighted by the rows of each stratum
(`stratum_rows`), with the interval of path precision, over all strata and without the strata of
the items with `from_evidence` set to `yes`, which the draw took from the repositories that the
`evidence` links. Where the draw took every stratum from them, the second precision has no
estimate. `report` also gives the precision of each stratum with its rows, the number of each
label, and the strata drawn from those repositories with their rows. It reads these two samples
where the directory holds them, because an evaluation can be drawn and labeled without them, and
it exits 1 for the labels of such a sample without the sample. The agreement of the two passes
is the share of items that they label alike, with Cohen's kappa. An item goes to
`review.csv` when a pass is uncertain or the passes disagree, and the estimates use the first
pass until a resolution replaces it.

## Tests

`tests/test_tools_evaluation.py` checks the Wilson interval and the stratified estimate against
values computed by hand, and the sampling against small output CSVs, file lists and
per-repository commit records built in the test. It also checks that:

- the samples leave out the repositories that the `evidence` of a heuristic links, except in a
  stratum of the imports or the planning documents with every row in them, which they draw from
  these rows with the size and the generator of every stratum and mark with `from_evidence`
- `sample` writes into the directory of the day on which the collection started and does not
  draw into it a second time
- `sample` keeps the mentions CSV that it read, with the SHA-256 that `sample.json` records
- beside archives that git tracks, `sample` exits 1 before it draws when a commit CSV differs
  from its archive or 7zz cannot list the archive, and outside a repository it draws from the
  CSVs whatever archive lies beside them
- the stratum without detected configuration contains a repository from which the collected file
  cleanup removed every collected file, and no clone failure
- the candidate paths include the places that the rows of the path heuristics name, except the
  generic names
- `detected` marks a path that only `other.csv` lists, and `report` counts such a path as not
  found
- when the notes of a heuristic name a labeled item, they also name the evaluation of the item,
  and the `evidence` of the heuristic links the item
- `report` exits 1 for missing and unknown labels
- the samples of `imports.csv` and `planning-documents.csv` draw up to their cell sizes per
  stratum, one row per repository first, weight each stratum by all its rows, record the strata
  drawn from the repositories that the `evidence` links and draw with generators of their own, so
  that a later draw with `--only` gives the items of a draw of the whole collection
- `sample --only` adds the samples and a record under `later_draws` to the directory of an
  earlier draw, leaves its samples, its labels and its record unchanged, marks a seeded fifth of
  each sample for the second pass, and exits 1 for a directory without a draw, for a sample or
  labels that the directory holds, for another seed and for a missing CSV
- a draw of the whole collection and a draw with `--only` leave out the repositories that the
  `evidence` links outside the strata with every row in them, record their number and the strata
  drawn from them in `sample.json`, and `report` over the drawn directory names those strata by
  the marks of the items, also for a sample that was drawn again
- `report` reads an evaluation with or without these two samples, weights their precision by the
  rows of each stratum as computed by hand, over all strata and without the strata drawn from the
  repositories that the `evidence` links, gives the second precision no estimate where the draw
  took every stratum from them, and allows only the labels of their criteria
- both tracked samples keep the same identities: the identity of a tool or a bot at an address
  that an author heuristic other than a convention reads, an automation that a row of
  `not_ai_commits.csv` reads at an address of no person or at a mailbox of the automation at its
  own domain, and the name of one of the six tools at an address that reads no mail, and they
  write `(a person)` for any other name at such an address, for a person's identity and for the
  login of a person
- both tracked samples mark a login after the entities of a zero-width space and in a link to a
  profile, and a login that ends in a hyphen
- both tracked samples keep an `@` in a code span or a fenced code block whole, mark a login after
  a zero-width space there, and mark a login after a run of backticks that no run of the same
  length closes on its line
- `commit_recall.csv` keeps the message of a credited commit as `mentions.csv` keeps a line
- `mentions.csv` takes all that follows the token of a trailer on its line as the name before an
  address, and keeps a package with its version whole

Over the tracked evaluations, the test checks that every phrasing labeled `attribution` in a
sample of mentions is matched by a current commit heuristic of the tool that the phrasing names,
except the items of `UNREAD_ATTRIBUTIONS`, each listed with the reason why no heuristic matches it.
It also checks that the masking changes no cell of `commit_recall.csv` and `mentions.csv`, so that
a rule that marks more fails the test until the samples are masked again from their texts.
