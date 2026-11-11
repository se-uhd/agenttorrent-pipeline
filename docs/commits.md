# The AI commit heuristics and reading the commit history

## Context

The names and addresses of people in the examples of this document and in the `notes` of the
heuristics are fictional, in a form that keeps what a row reads, such as `Claude Doe <claude@example.org>`
for a person named Claude with a `claude@` address. The repository or the commit beside an example
identifies the case.

`agentcontext/commits.py` compiles the heuristics that attribute a commit to a tool once, from `ai_commit_patterns.csv`, and contains the code with which the cloning step reads every commit of a clone. `detect_ai_tools_in_fields` and `matched_rules_in_fields` match a commit's author, trailers and body against the heuristics. `read_commit_history` reads every commit of a clone once and writes the matches and the unmatched attributions to the shards. From the shards, `assemble_ai_commits` builds the commit CSV, the CSV of the tools outside the six, the unmatched attributions, the mentions and `ai_commits-repos.csv`.

The module has no command line of its own. The cloning step computes the AI-authorship columns of `context-files.csv` from the clone that it already has, so that no repository is cloned a second time.

The commit CSVs of the cloning step become the release's `commits.csv` and `other_commits.csv`. `data/collection/ai_authorship.csv` records the attribution measured during collection, and the detection step joins it into the context-file rows. `history.read_artifact_history` lists the commits of one path as records, and the cloning step calls it. `ai_authorship_from` computes the four AI columns from these records, and `history.history_from` derives `first_commit_sha` and `last_commit_sha` from the same records.

## Key decisions

- **Language:** Python 3.14, in one module, `agentcontext/commits.py`, with no dependency beyond the standard library and `git`
- **Shared detection logic:** this module defines `detect_ai_tools_in_fields` and the compiled patterns, and the cloning step and `checks/commits.py` import them. The patterns therefore exist in one place only
- **Three detection scopes:** each commit is checked against three scopes in order: (1) the author and committer identity fields, (2) git trailer lines only, for trailer-anchored patterns such as `assisted-by:` and `co-authored-by:`, and (3) the full commit message body. The trailer scope contains only lines in the form of a trailer, and prose descriptions therefore do not match there
- **`check_author` flag:** `detect_ai_tools_in_fields` accepts `check_author: bool = True`. The default checks all three scopes, which maximizes recall and catches the commits that only the author or committer identity marks as AI, such as the commits of a bot account that commits as the author (`claude-code`, `copilot-swe-agent[bot]`) or of an author at Claude Code's `noreply@anthropic.com` address. `False` skips the author and email scope, which is more precise but misses those commits. `commit-attribution-validation` classifies each commit by the detection scope that matched it
- **Attribution limits recall:** detection requires a recognizable identity, trailer or
  disclosure. What a commit contains depends on the product and its settings, and the table in
  [Attribution conventions (per tool)](#attribution-conventions-per-tool) gives what each product writes and which setting changes it.
- **`matched_heuristics` names heuristics by id:** `matched_rules_in_fields` tries every heuristic rather than stopping at the first match per tool, and it returns each match by the row's `id` in `ai_commit_patterns.csv`, `<tool>-<scope>-<nn>`, which the cloning step publishes in its `matched_heuristics` column. An id is never given to another row, and a published id therefore names the same heuristic in every release. A row's position, in contrast, would change for every row after a row that is added or removed
- **`attributions` records how the commit credits each tool:** `account`, `reviewer`, `standard` or `convention`, as [AI contribution detection](heuristics.md#ai-contribution-detection) defines them, and the `attribution` column of the heuristics files gives one per heuristic. Several heuristics can match one line. The heuristic of the code review app and the heuristics of Copilot's name and address all match `Co-authored-by: Copilot` with the code review app's address. `attributions_in_fields` therefore gives each line the first of reviewer, standard and convention with a heuristic that matches the line, and gives the author and the committer `account` when an author heuristic with `account` matches, and otherwise `convention` when an author heuristic with `convention` matches. The function maps a match back to the lines that the match spans through `_texts_by_line`, which builds the two texts that the heuristics match piece by piece and records the line of each piece

## `agentcontext/commits.py`

### Architecture

#### Detection scopes

`detect_ai_tools_in_fields` builds three texts from each commit and matches each scope's patterns against its text. `classify` passes it the subject and the body as `message_parts` splits them off the message that the commit CSV stores, and the check of the commit CSV uses the same function. `message_parts` treats a lone carriage return as a line break and strips the whitespace that a NUL-separated log keeps around the subject and the body. Read from the fields of the log, `Assisted with Claude\rMore` would miss `claude-full-body-13`, and the check would find a difference in the row. `ai_authorship_from` reads each commit of a path the same way (`stored_message`), and the AI columns and the commit CSV therefore agree on such a message.

A history can have hundreds of thousands of commits, and most of them contain no literal of most heuristics. `_any_match` therefore reads the literals that a match of a pattern has to contain (`heuristics.required_literals`), and a pattern with a set of required literals of which the text contains none does not run. A lookaround, a class and an optional part contribute no literal, so this check changes no result, and `TestTheRequiredLiterals` compares it with the patterns alone.

- **Author text**: `author_name + author_email + committer_name + committer_email`, matched against the author patterns, such as bare tool names, email domains and bot account names
- **Trailer text**: `subject + _extract_trailers(body)`, matched against trailer-anchored patterns such as `co-authored-by:` and `generated-by:`, and restricted to lines in the form of a trailer, so that prose such as "Used claude.ai to draft this" does not match
- **Full-body text**: `subject + body`, the subject and the body on lines of their own, with every quoted line that is indented below another line and every unticked box of a task list (`- [ ] ...`) left empty and a ticked box (`- [x] ...`) read as its bullet, matched against free-form patterns for attribution sentences such as "this commit was generated by X". [The detection accuracy evaluation, read 2026-10-13](#the-detection-accuracy-evaluation-read-2026-10-13), gives the reason for leaving out the indented quotes. An escaped line break in a line of the body is a line break in both texts, except in a list item that names another commit by its hash, and an escaped line break in the subject is a line break in the full-body text ([Trailer extraction](#trailer-extraction))

`scope_texts` builds the trailer and full-body texts for the classification and for the attribution check alike. It leaves out the subject of a revert, which git writes as `Revert "<the reverted commit's subject>"` (`sequencer.c` at v2.47.0, lines 2356-2358), because that credit belongs to the reverted commit. Read as a subject, `Revert "Co-authored-by: Copilot"` would credit Copilot with a person's revert. In GitHub Flavored Markdown, an unticked box is a task not done ("If the character between the brackets is a whitespace character, the checkbox is unchecked"), and `- [ ] created with the help of Claude` in a pull request template would credit Claude with a commit of chainsafe/lodestar.

A credit that a message copies from other commits counts when the commit records their change, as a squash merge and `git cherry-pick -x` do ([Trailer extraction](#trailer-extraction)). GitHub's squash list names every commit of the pull request, including one already on the base or undone later on the branch, so such a credit stays counted as a known limit ([The prefix `[codex]`](#the-prefix-codex)). A credit that the message only quotes belongs to the other commit and does not count, such as the subject of the reverted commit in a revert, the description of a pull request that release notes quote below a list item ([The detection accuracy evaluation, read 2026-10-13](#the-detection-accuracy-evaluation-read-2026-10-13)), the release notes of a dependency that an update of Renovate quotes ([The credits of 2026-11-05](#the-credits-read-2026-11-05)), the release notes, changelog and commits of a dependency that an update of Dependabot quotes, or the message of the commit before that a version bump of kube-HPC/hkube quotes ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)). A credit in a commit of Chromium that brave/brave-core quotes below a line `Chromium changes:`, or below `Chromium change:` above a single quoted commit, in the form of `git log`, does not count either, as `gemini-cli was used to quickly prototype` in brave/brave-core 6e74e53c4e22, which changes only Brave's own files. Each such block ends at the first unindented line that is no header, and a trailer below it stays read. Any other `git log` block stays read, because `git merge --squash` writes the same form for a change that the commit records. The block `<details>` with `<summary>Original prompt</summary>`, in which Copilot's cloud agent quotes the issue and the comments on it, credits nothing either, because a statement there names the issue and no change, as `Wow - this is an issue I asked copilot to create.` of microsoft/onnxruntime 616a4dfc1434 does. The block ends at a line `</details>`, and a block without such a line stays, because a trailer can follow it directly. Both scopes leave the quotes of Renovate, Dependabot, kube-HPC/hkube and brave/brave-core and that block empty (`commits._own_lines`), and a revert and a version bump of kube-HPC/hkube have no subject that the heuristics read. The detection accuracy evaluation labels its commits by this rule ([the labels](tools/evaluation.md#the-labels)).

#### Trailer extraction

`_extract_trailers` returns every line of the commit body that matches `^[A-Za-z][A-Za-z0-9-]*:\s*\S` (git trailer format), wherever the line is, and nothing else.

The space after the colon is optional, as it is for `git interpret-trailers`, which reads `Co-authored-by:Claude <noreply@anthropic.com>` as a trailer. Requiring the space would leave such a commit with no trailer, and the commit would count as a person's.

Collecting only the trailing block, up to the first line that is neither blank nor a trailer, would miss every trailer that is not last. A squash merge concatenates the messages of the commits that it replaces and leaves another commit's text below the trailer. A blank line can split two co-authors into two blocks, of which only the second would be read. `git cherry-pick -x` appends a `(cherry picked from commit ...)` line below the trailers. In each case, the tool's trailer would not be read, and the commit would be recorded as a person's.

The extraction reads a line without the color codes of a terminal (`ESC [ <numbers> m`), which a
message pasted from a colored terminal keeps around every line, and without one emoji before its
token, as in `🤖 Co-Authored-By: Claude Code`. It also reads a line without the bullet `* ` at
its start when the rest has the form of a trailer (`commits._SQUASH_BULLET`). GitHub's squash
merge lists each commit of the pull request as `* ` and its message, so a commit whose whole
message is a trailer appears as `* Co-authored-by: ...`, as comet-ml/opik
[60258b3c235b](https://github.com/comet-ml/opik/commit/60258b3c235bbc78483bc43867e3300278605b6a)
lists c66ae12cac19 of pull request 5179, whose message is
`Co-authored-by: Cursor <cursoragent@cursor.com>`. The squash merge records the change of the
listed commit, so the credit counts. The extraction reads every top-level `* ` item of trailer
form this way, not only the items of a squash list, and reads the rest of the line like any line
of trailer form, with every token in it, so that `* Tool: Codex` reaches the trailer rows of the
key `Tool:`. Of a top-level `* ` item that ends in a co-author line with an address, it reads that
co-author line alone (`commits._JOINED_CO_AUTHOR`). A listed commit whose subject line ends in its
co-author line appears in the squash list with that line after its subject, as allenai/open-instruct
[97590011918f](https://github.com/allenai/open-instruct/commit/97590011918f562621a994eeebdc4a83a1cbb9d4)
lists d5fc8655cf38, whose message is the one line `Restore 🤡 to resample warnings; use
self.training_step in DataPreparationActor.run. Co-Authored-By: Claude Opus 4.7
<noreply@anthropic.com>`. A bullet indented below another line is a nested list item and stays as
it is, and so does a hyphen bullet, as in the list item `- Assisted-By: GPT 5.6 Sol, OpenCode` of
electron/electron d84976dd6420. It leaves out a line
that closes an inline code span that the line before it opened, which it detects as an odd number
of backticks in both lines, because such a line quotes a trailer rather than writing it
([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).

A trailer heuristic matches where its token begins a token (`heuristics.compile_commit_pattern`),
that is, after a character that cannot continue a token or after an escaped line break. An escaped
line break is the two characters `\n` that some tools write into a message in place of a line
break, which puts the trailers on the subject line or at the end of one line of the body. In the
body, both scopes read an escaped line break as a line break (`commits._own_lines`), except in a
list item that names another commit by its hash, `- <hash>: <message>`, because a release commit
can list the messages of other commits on one line each, as meziantou/Meziantou.Framework
[0700bc2ff449](https://github.com/meziantou/Meziantou.Framework/commit/0700bc2ff4495f0ac4f4dcb777ba7a8a814f2a64)
does. Any other list item reads the escape, as the squash merges of xtaci/smux
[148c01bcb4ad](https://github.com/xtaci/smux/commit/148c01bcb4ad) and SonicJs-Org/sonicjs
[6df3b91cc86b](https://github.com/SonicJs-Org/sonicjs/commit/6df3b91cc86b) have their trailers in
one, and the token follows the two characters `\n`, which this section reads as a line break, so
that it is no credit token in the middle of a line under the decision of 2026-11-02. The full-body
text also reads an escaped line break in the subject as a line break (`commits._full_body`), where a
tool that writes the whole message on one line leaves it, as Capsize-Games/airunner
[2e41c054d021](https://github.com/Capsize-Games/airunner/commit/2e41c054d021) does with
`\n\nChanges made via copilot.` at its end. A token that only ends in the heuristic's
token credits what its first part names. `Commit-Message-Assisted-by: Claude (via Claude Code)`
therefore credits the commit message and no change, and the commits of openshift/hypershift that
carry it as their only credit count for no tool. The prefixes `AI-`, `Co-` and `Tests-` name who
or what assisted with the change, and `AI-Assisted-By:`, `Co-Assisted-By:` and `Tests-Assisted-By:`
count as `Assisted-by:`. The prefix `Almost-Exclusively-` states how much of the change the
credited author wrote, and `Almost-Exclusively-Authored-By:` of containers/kubernetes-mcp-server
counts as `Authored-by:`. `Co-authored-by:` is a token of its own, the co-author line, and does not
count as `Authored-by:`, so that a row of the token `Authored-by:` does not read every co-author
line ([The Authored-by, Created-by, Written-by and Made-with lines, read 2026-11-03](#the-authored-by-created-by-written-by-and-made-with-lines-read-2026-11-03)).
`unmatched commits` reads the tokens in the same way. A token of `commits.ATTRIBUTION_TOKENS`
after one of these prefixes is an attribution under its whole token, such as
`ai-assisted-by: jcode (openai)` of caddyserver/caddy
[401582cccfb3](https://github.com/caddyserver/caddy/commit/401582cccfb3), which no heuristic reads.
A token of `commits.ATTRIBUTION_TOKENS` itself, such as `Co-developed-by:`, stays a token of its
own. The cloning step lists no such credit among the unmatched attributions, and the check reads
them from the kept commit logs (`checks.unmatched.prefixed_credits`,
[the unmatched checks](checks/unmatched.md)). Recorded by the cloning step, they would change the
collector's code and `classified_by`.

The form of a line still excludes prose, wherever the line is. A sentence such as "I was inspired by the co-authored-by claude convention" has no `Token: value` and never matches. A line with a space in its token, such as `"Link to Devin run:"`, is excluded for the same reason.

#### Pattern classification

`_COMPILED_PATTERNS`, built at import by `heuristics.compiled_ai_commit_patterns`, contains one entry per tool: the tool's name and its heuristics grouped by the heuristics' `scope` column into three compiled lists, `author`, `trailer` and `full_body`. The patterns are compiled once at startup and not per commit. The scope is a column rather than a form read off the pattern, because a heuristic that matches anywhere in the message and a heuristic that matches a trailer line can be spelled the same way.

Every pattern is compiled with `re.IGNORECASE`, and `Co-Authored-By:`, `co-authored-by:` and `CO-AUTHORED-BY:` therefore match alike.

### Attribution conventions (per tool)

The patterns find only what the tools write into a commit, and the products document what
they write:

| Product | Commit attribution | Configuration | Source |
|------|----------------------------|---------------|--------|
| Claude Code | `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`, where the model name is the session's model. Cloud and Remote Control sessions also add a `Claude-Session` trailer, on by default. The pull request footer is `🤖 Generated with [Claude Code](https://claude.com/claude-code)` | Yes, and the `attribution` setting sets `commit`, `pr` and `sessionUrl` independently. Empty strings hide the attribution | [Claude Code settings](https://code.claude.com/docs/en/settings) |
| Copilot cloud agent | The coding agent authors the commit itself and records the human who assigned the task as co-author. Commits have an `Agent-Logs-Url` trailer that links to the session log. They are signed and therefore show as Verified | No user setting changes it, because the agent's own account authors the commit | [About Copilot coding agent](https://docs.github.com/en/copilot/concepts/coding-agent/about-copilot-coding-agent), [changelog 2026-03-20](https://github.blog/changelog/2026-03-20-trace-any-copilot-coding-agent-commit-to-its-session-logs/) |
| Cursor | A `Made-with: Cursor` trailer, and `Co-authored-by: Cursor <cursoragent@cursor.com>` on agent commits. The documentation does not give the trailer verbatim, and real commits found through GitHub's commit search API confirm its form | Yes, on by default, toggled per user, and enterprise admins can disable it for a whole organization | [Cursor git integration](https://cursor.com/help/integrations/git) |
| Codex | When enabled, `Co-authored-by: Codex <noreply@openai.com>` | A session authenticated with the backend reads the account's `commit_attribution_enabled` setting, and the source does not show the setting's server-side default. Every other authentication path turns the attribution off | [Attribution policy](https://github.com/openai/codex/blob/main/codex-rs/ext/git-attribution/src/policy.rs), [trailer instructions](https://github.com/openai/codex/blob/main/codex-rs/ext/git-attribution/src/world_state.rs) |
| Gemini CLI | None. No official convention exists. Discussion #11447, which requests one, had no reply on 2026-10-13, and issue #25721, about the project's CLA blocking `gemini@google.com` as a co-author, was closed on 2026-05-10 without a change | n/a | [discussion #11447](https://github.com/google-gemini/gemini-cli/discussions/11447), [issue #25721](https://github.com/google-gemini/gemini-cli/issues/25721) |

Copilot CLI and VS Code have separate attribution controls. The shipped CLI
1.0.63 reads `includeCoAuthoredBy`, with `include_coauthor` as a compatibility
alias. A user can set VS Code's `git.addAICoAuthor` to `off`, and its maintainer describes
the settings in [issue 314311](https://github.com/microsoft/vscode/issues/314311).
The cloud agent's commit identity does not show the recall for these products.

Detection records only the AI contributions that a commit acknowledges. A tool can contribute
without leaving a credit that identifies it, and users can turn attribution off where the
product permits it. The measured counts are therefore lower bounds. Liu et al.
describe the same limitation in their [method for detecting AI commits](https://arxiv.org/abs/2603.28592).
State a count of the commit CSV as the commits that credit a tool, such as the commits that
credit Copilot, and not as the commits that the tool wrote, because a person can write the credit
and a tool can contribute without one.

Recall differs across products and settings, among them the products grouped under
Copilot, and a comparison of commit counts cannot separate how much a tool is used from how
it attributes its commits. `commit-attribution-validation` writes `detection_source` and
`matched_scope`, which identify the evidence that matched each recorded contribution.

#### Conventions considered and not adopted

The GitClear help page [Documenting (designating) lines written by Claude and
other AI](https://www.gitclear.com/help/documenting_llm_claude_code_repo_contributions)
catalogs the conventions that a commercial analysis tool recognizes. Each convention on it
that marks a commit was compared with these patterns, and the table gives the decision on each
with its reason:

| Convention | Adopted | Reason |
|------------|---------|--------|
| `Assisted-by: <model>` | Yes | Tool-agnostic, and it therefore applies to every tool alike. The Linux kernel also requires it, in `Documentation/process/coding-assistants.rst` |
| `Co-authored-by: <model>` | Already present | The heuristics already match it |
| `Generated-by: <model>` | Already present | The heuristics already match it |
| `Generated with [Claude Code]` | Yes, 2026-09-22 | Rejected until the vendor documented it. `attribution.pr` is "unset, so Claude Code adds 🤖 Generated with [Claude Code](https://claude.com/claude-code)". A squash merge copies the pull request description into the commit message, which makes the line the tool's own default rather than a template suggestion. It is Claude-only, which the symmetry argument below still counts against. It was adopted anyway, because the vendor documents it and because <!--s:collection.generated_with_claude_code_no_trailer.commits-->9,175<!--/s--> commits of the commit CSV, in <!--s:collection.generated_with_claude_code_no_trailer.repositories-->544<!--/s--> repositories, have it without a `Co-Authored-By` trailer |
| `Generated by Claude (<model>)` | No | Claude-only |
| `[Claude]` subject-line tag | No | Claude-only, and a template suggestion rather than a tool default |
| `[Mixed]` subject-line tag | No | Names no tool, and it cannot be attributed to one |
| Subject line beginning with a model name | No | Bare model names such as `Opus` or `Sonnet` are ordinary English words and short identifiers, and matching them would give up much precision for little recall |
| `<model>@<provider>` as author email | No | The documented examples name tools that this dataset does not cover |
| Per-method comments, file headers, test annotations | No | This step classifies commits, not lines within a file |
| `AI_CONTRIBUTIONS.md` log file | No | A repository file rather than a credit in a commit, which would belong in `agentcontext/steps/detection.py` if it were adopted |
| Agent trace metadata block, a JSON block in the commit message | No | GitClear's page shows no example of the block, and the heuristics therefore have no form to match. A line of the block that names one of the six tools, in a commit that no heuristic matches, appears in `output/all_ai_commits-mentions.csv` |
| `from <owner>/gemini/<branch>` in a merge commit | No | No Gemini product documents a branch prefix. The issue fixer of google-github-actions/run-gemini-cli names its branch `gemini-fix-<number>` (`.github/workflows/gemini-issue-fixer.yml` at 387c8ddb7a72), and the merge commits of `gemini/` branches in the commit logs of the October 5–6 collection include ccxt/ccxt's branches for the Gemini exchange, such as `gemini/staking` |
| `from <owner>/jules/<branch>` in a merge commit | No, 2026-10-21 | No page of Jules names a branch prefix, and `jules/` is not the prefix of Jules's branches. The name of a branch of Jules can end in a number of 19 or 20 digits, as `jules/fix-e2e-tests-and-bugs-8689437116413832293` does, from which `google-labs-jules[bot]` opened rush86999/atom#482. In the commit logs of the collection that the checkout tracks, in the repositories with a commit of `google-labs-jules[bot]`, <!--s:collection.jules_branches.no_prefix-->461<!--/s--> of the <!--s:collection.jules_branches.long_id_merges-->548<!--/s--> merge commits of a branch with a name that ends in such a number have no prefix, and <!--s:collection.jules_branches.jules_prefix:words-->three<!--/s--> have `jules/`. Jules is also a given name |
| `Agent-Logs-Url:` trailer | No | The trailer links to an agent session on github.com and is also in the commits of other agents that run on GitHub, such as the commits of `anthropic-code-agent[bot]` in gofiber/fiber, and it therefore does not name the tool. The account of Copilot's cloud agent, which authors its commits, is an author heuristic |
| `Written by [Cursor Bugbot]` and `Reviewed by [Cursor Bugbot]` in a commit body | No, 2026-09-26 | The commit is still in the commit CSV and counts for any tool that another line credits, but this line is not read as evidence that Cursor produced the change. Bugbot "writes a summary of each pull request it reviews" and by default "adds the summary to the pull request description and updates it on each review" ([Bugbot](https://cursor.com/docs/bugbot)). A squash merge therefore copies the note into the commit of every pull request that Bugbot reviewed, whoever wrote the code. Under the criterion in `docs/heuristics.md`, reviewing a change is not producing it. Other lines record what Bugbot does contribute: a suggestion committed with one click names the reviewer as co-author, and an Autofix commit is a Cloud Agent's, with Cursor's agent identity. In the subsample's mirrors, commits of streamlit/streamlit and jdx/mise have the note and no other line that credits a tool. Reading the note would have credited Cursor with those commits |
| `(copilot-swe-agent)` at the end of a squash subject | No, 2026-09-26 | A merge tool's note of the pull request's author, found in the mirrors. The coding agent's own account is already an identity heuristic |
| `[codex]` in lowercase at the start of a subject | Yes, 2026-11-06 | The prefix that a skill of OpenAI told Codex to write ([The prefix `[codex]`](#the-prefix-codex)) |
| `(COPILOT)` at the start of a subject | No, 2026-11-02 | One developer's marker, and a tool's name in that place also names the area of a change ([The labeled attributions decided on 2026-11-02](#the-labeled-attributions-decided-on-2026-11-02)) |
| `by @Copilot in #<n>` in release notes that a commit contains | No, 2026-09-26 | GitHub's generated release notes crediting a pull request of another repository, copied into a dependency update |

Symmetry is the reason common to the rejections. Several of the conventions
above exist only for Claude, because the page's template is written for
`CLAUDE.md` and gives no Copilot, Cursor, Codex, Gemini or OpenCode equivalent. Adopting
them would raise the detection rate for one tool while leaving the other five
untouched, and any comparison across the tools would show the difference as a property of
the tools rather than of the heuristics. This pipeline adopts a convention when it
is tool-agnostic or when the vendor of the tool that it names documents it.

A tool-agnostic convention is matched for a tool once the collection or GitHub's commit search
finds a commit that contains it for that tool, and the row links that commit. A row for a form
that nobody writes changes no count and cannot be checked, and leaving it out therefore makes no
tool look less assisted. If a later commit contains such a form, `unmatched commits` and the
mentions CSV of the cloning step list it.

#### The other two outside lists, read 2026-09-22

The published lists below were compared with these heuristics, and neither is a source to copy
from. A list records third-party observations, which may be out of date, wrong, or produced by
a model that nobody checked against a vendor page. Each entry was therefore a candidate,
adopted only where a vendor page documents it, or where the commits that this pipeline read
show it and the row states how many.

**The MSR 2026 mining challenge, AIDev, and PRarena, from which AIDev adapted its queries.** Both
publish GitHub search queries rather than commit patterns. A query such as `head:codex/` uses a
qualifier that the search API supports and that no commit message contains. Their queries name branch prefixes that a clone does not contain at all, the
`Co-Authored-By: Claude` trailer that the heuristics already match, and the accounts of Devin and
Jules, tools that this dataset does not cover. Carried as `full_body` heuristics, such qualifiers
matched no commit. The heuristics match the merge commit that GitHub writes instead, `from
<owner>/copilot/<branch>`, which a clone contains.

**The author patterns of `labri-progress/agent-impact`.** Its patterns for Claude Code, Copilot, Codex, Cursor and Gemini that match
no commit heuristic of this dataset are mostly bare display names or model names, such as `Claude`, `cursor`, `Claude Opus
4.5` and `Codex (gpt-5.2-codex)`, and none was adopted. `Claude` is a common given name, and the
September 2026 collection shows that those spellings belong as often to the tool that ran the model as to
the tool that they name, as in `assisted-by: opencode (claude opus 4)`. `generated-by: gemini 3.1 pro
(high) on antigravity` is Gemini's, because Antigravity counts as Gemini. The comparison also found places where the heuristics
disagreed with each other rather than with the outside list. The Claude `assisted-by:` and the
Gemini `co-authored-by:` trailers lacked the vendor prefix that `(?:github\s+)?copilot` and
`(?:openai\s+)?codex` already allowed, and so did the Copilot `generated-by:` trailer. Codex had
no `generated-by:` row at all, where the other three tools had one. Of these rows,
`claude-trailer-01`, `copilot-trailer-03` and `codex-trailer-03` remain, and the note of each
gives the form that the September 2026 collection showed. A trailer that begins with Gemini or Google Gemini names no Gemini product and
counts as `Gemini (unspecified)` ([Gemini naming no product](#gemini-naming-no-product-2026-09-27)).

`labri-progress/agent-impact` also reads the branch names and the labels of pull requests through
GitHub's GraphQL API, and credits every commit of the pull request's author in a pull request that
a branch name or a label marks. The cloning step reads a clone and no pull request, and a commit
counts only through an identity, a trailer or a disclosure that it records. No heuristic reads a
label or credits the other commits of a pull request, and a branch name counts only in the merge
commit that GitHub writes, as `copilot-full-body-01` reads `from <owner>/copilot/<branch>`.

#### The attributions that no heuristic matched in the September collection, read 2026-09-25

The collection started on 2026-09-24 ran over the whole sample and wrote every attribution that no heuristic
explained to `output/all_ai_commits-unmatched.csv`, one row per token with its commit count and
up to <!--s:parameters.agentcontext.commits.UNMATCHED_SAMPLE:words-->twenty<!--/s--> of its lines. <!--s:heuristics.ai_commit_patterns.rows_read_from_unmatched:Words-->Thirteen<!--/s--> rows were derived from that file, each a `sample` row with a
note that names that file as its source.

<!--s:heuristics.ai_commit_patterns.rows_read_tool_name:Words-->Seven<!--/s--> match the tool's name in places where the older rows do not. The older rows require the name right
after the colon, while people put it anywhere in the value: after `via` (`Assisted-by: GPT-5.6 via
Codex`), in parentheses after a model (`Assisted-by: GPT-5.3 (Cursor)`), after an emoji or an @
(`Co-authored-by: @Copilot`), joined to its vendor (`Assisted-by: GitHubCopilot:claude-sonnet-4.6`)
or written out in full (`Generated-by: Google Gemini 2.5 Pro + Gemini CLI`). Others match the
account of OpenAI's app for Codex, `openai-code-agent[bot]`, as author and as co-author, and the
account of OpenCode's app, `opencode-agent[bot]`, as author.

<!--s:heuristics.ai_commit_patterns.rows_read_address:Words-->Three<!--/s--> match an address that belongs to a tool, under whatever name stands before it: the noreply
address of the GitHub account with the login Copilot, which GitHub's API gives to the apps
`copilot-swe-agent`, `github-copilot-cli` and `copilot-pull-request-reviewer` and which no person
can hold; `cursoragent@cursor.com`, the address of Cursor's agent trailer; and
`codex@openai.com`, which the Codex identity heuristic already matches in the author field.

Each row has limits beyond which it matches nothing, and `tests.test_commits.TestTheAttributionsTheSeptemberCollectionLeftUnexplained`
contains a real line of the September collection at each limit, beside the lines from which the rows were derived:

- **A slash after the tool's name.** OpenCode and pi write a model as `provider/model`, and the
  provider is the account to which the requests went. OpenCode's `github-copilot` provider is a
  Copilot subscription used as a model API, and pi's `openai-codex` provider signs in with a
  ChatGPT plan and calls `chatgpt.com/backend-api`. The agent that made the commit is therefore
  OpenCode or pi. The commits of josh-project/josh contain the form for every model that the
  project uses: their `Assisted-By:` lines name `anthropic/claude-opus-4-8`, `moonshotai/kimi-k2`,
  `openrouter/openai/gpt-5.6-sol` and `openai-codex/gpt-5.6-sol`.
- **A model's name alone.** Several tools offer `GPT-5.3-Codex` and `Gemini 3.1 Pro`.
- **A vendor's noreply address under a name that Claude Code does not write.** Claude Code's names
  are `Claude <model>`, `Claude` and `Claude Code`, which `claude-trailer-02`, `claude-trailer-06`
  and `claude-trailer-09` match. Other agents write
  Anthropic's address under their own names: `goose <noreply@anthropic.com>` comes from goose,
  and `MechaCoder <noreply@anthropic.com>` from OpenAgents, with commits that contain `Generated
  with [OpenAgents]`. Such a line shows whose model or trailer format was used, not which tool
  ran, and the same applies to `noreply@openai.com` and `noreply@cursor.com` under a model's name.
  The line of OpenAgents names its own agent, and `mechacoder-trailer-01` credits MechaCoder at any
  address ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
  A persona name before Claude's model in parentheses at Claude Code's address does count for Claude,
  as a convention, as in `Kofi Bingo (Claude Opus 5.5, 1M context) <noreply@anthropic.com>`, which
  `claude-trailer-15` reads, because the line keeps Claude Code's address and the model of its
  trailer. A name that contains another tool or a model family stays out, such as `Fable
  (claude-fable-5)`, and so does a persona at another address.
- **A person at a vendor.** No row matches a vendor's domain in a trailer, because Anthropic's
  and OpenAI's staff co-author and commit under their own addresses, and the identity rows match
  `noreply@anthropic.com` and addresses that name Claude rather than the whole domain. An
  identity row that matched `anthropic\.com` would credit Claude with the commits of <!--s:collection_2026_10_02.identity_review.anthropic_com_people-->64<!--/s--> people at
  their own addresses in the October 2–3, 2026 collection. Cursor's identity row matches the
  addresses of Cursor's agent, `cursoragent@cursor.com` and `assistant@cursor.com`, rather than
  `@cursor\.com`.
- **A person with a tool's name.** A heuristic on a bare name also matches a person who chose
  that name for an account. pi's row leaves out a GitHub user's own address, so that it does not
  credit pi with the 4 commits of valory-xyz/mech by a co-author named Pi at the GitHub address
  of a person.
- **A tool's name inside another word.** `capgemini.com`, `copilotkit.ai` and `codex-soft.com`
  are companies, and `doegemini` and `claudidoe` are people. The same applies to identities. The
  Copilot identity rows match a name that begins with Copilot and the addresses of GitHub's
  Copilot apps, because `copilot.money` is a company, `OneUptime Copilot` and
  `erigon-copilot[bot]` are other projects' automations, and `+copilot` is a person's mail alias.

In the current commit CSV, <!--s:collection.rows_read_from_unmatched.alone_commits-->5,148<!--/s--> commits are matched by these rows and by no other
heuristic. A line such as `Assisted-by: Claude Opus 4.6 via GitHub Copilot` or `Assisted-by: Claude
Sonnet 4.5 (via Cursor)` credits the tool that it names, and since [the narrowing of the Claude
trailers](#the-claude-trailers-narrowed-2026-09-25) it does not credit Claude.

#### The tools outside the six, 2026-09-25

In the collection started on 2026-09-24, no heuristic matched thousands of attributions that name
tools that this dataset does not study, such as Devin's and Jules's GitHub apps as the
author, CodeRabbit's as a co-author, Amp, Qwen Code and aider. In the commit CSV of the other tools,
which the rows of `other_ai_commit_patterns.csv` fill, Devin's app is the author of <!--s:collection.devin_author_commits-->7,380<!--/s--> commits and Jules's of
<!--s:collection.jules_author_commits-->2,422<!--/s-->, and a trailer credits CodeRabbit's app on <!--s:collection.coderabbit_coauthor_commits-->4,430<!--/s-->.
Without heuristics for these tools, their commits would count as people's in any question about
AI use, and their attributions would fill the unmatched attributions that `unmatched commits`
ranks for the six.

`heuristics/other_ai_commit_patterns.csv` contains the heuristics for those tools, in the columns and under the
checks of `ai_commit_patterns.csv`, and its loader raises `ValueError` for a row that names one of the six. The
cloning step matches every commit that it reads against both files. A commit that a heuristic of
the second file matches goes to a shard of its own and from there to `output/other_ai_commits.csv`.
Its attributions are not in the unmatched attributions, as the attributions of a commit of the six
are not. A commit that credits one of the six and a tool outside them is in both CSVs.
The detection step restricts the second CSV to the published repositories as it does the first,
and the release publishes it as `other_commits.csv` beside `commits.csv`, with both heuristics
files, because `matched_heuristics` and `attributions` in each CSV refer to the heuristics of its
own file.

Each heuristic matches what the tool writes, or a line or a name that names the tool, never a
name that a person can have:

- **The tool's GitHub app account**, confirmed with its owner through GitHub's API:
  `devin-ai-integration[bot]` (Cognition), `google-labs-jules[bot]` (Google Labs),
  `coderabbitai[bot]`, `factory-droid[bot]`, `mentatbot[bot]`,
  `continue[bot]`, the Qodo and Windsurf review apps, and `sweep-ai[bot]` and JetBrains Junie's
  `jetbrains-junie[bot]`, which the commits name although their app pages no longer load. A
  second reading of the same list added the apps with a page that calls them AI: the review apps of
  Greptile, Sourcery and cubic, Sentry's Seer, LangChain's Open SWE, Promptless, Dosu and
  Codeflash. Qodo's `codiumai-pr-agent-pro[bot]` commits under the bot user id of the app that the
  rows already match and is that app under a new name. `kilo-code-trailer-02` reads Kilo Code's
  `kiloconnect[bot]` and `kilo-code-bot[bot]` in co-author lines, and `kilo-code-author-01` reads both
  as the author.
  The reading of the commit logs of the run that started on 2026-10-23 added `ellipsis-dev[bot]`,
  whose site calls Ellipsis a "Cloud agent platform built for developers", and `pullfrog[bot]`,
  "The agent-powered GitHub bot that does it all". A co-author line of Ellipsis is a suggestion of
  the reviewer that a person committed, as a co-author line of Copilot code review is, and a
  commit that `ellipsis-dev[bot]` authored is the app's own, such as the merge of a pull request
  that Ellipsis opened. A co-author line of CodeRabbit is `standard`, because CodeRabbit also
  commits changes of its own. In a suggestion that a person committed with the subject that
  GitHub gives it, the line is `reviewer` ([The detection accuracy evaluation of 2026-10-26, read 2026-10-29](#the-detection-accuracy-evaluation-of-2026-10-26-read-2026-10-29)).
- **A trailer with the tool's own address**, such as `Amp <amp@ampcode.com>`,
  `Mistral Vibe <vibe@mistral.ai>`, which Mistral Vibe's system prompt has it write,
  `Qwen-Coder <qwen-coder@alibabacloud.com>`, `Junie <junie@jetbrains.com>`,
  `openhands@all-hands.dev`, `crush@charm.land`, `CommandCodeBot <noreply@commandcode.ai>`,
  `multica-agent <github@multica.ai>` or Pullfrog's `team@pullfrog.com`. Devin writes
  `Co-Authored-By: Devin <noreply@cognition.ai>` below "Generated with [Devin](https://devin.ai)",
  and another address at that domain can be a person's. Letta Code's `<noreply@letta.com>` has
  <!--s:collection.commits.other_tools.letta_code-->6<!--/s--> published commits, and the description of Letta Code's Bash tool instructed the agent to write
  the trailer. Another address at the same domain is a person's. No row matches
  `person102@ampcode.com` or `person213@coderabbit.ai`, and `unmatched commits` therefore lists them.
- **The tool's default git identity**, `openhands <openhands@all-hands.dev>` as the author or the
  committer. OpenHands' settings page names this identity: "By default, OpenHands uses the
  following Git author information for all commits and pull requests". GitHub's API links the
  address to the account `openhands-agent`, and the trailer row reads the same address.
- **The merge commit of a branch with the tool's prefix**, as for the six tools ([The branch merges and the apps of 2026-10-21](#the-branch-merges-and-the-apps-of-2026-10-21)): Devin's `devin/`, and `sweep/`, which the code of the app
  `sweep-ai[bot]` gives its branches. A person can give a branch the prefix too.
- **The tool's name first in an attribution trailer**, where the name is not a given name:
  `Assisted-by: Kiro:claude-opus-4.8`,
  `Co-authored-by: aider (gemini/gemini-2.5-pro) <aider@aider.chat>`. Where the name could be a
  given name, the heuristic requires more. It matches TRAE only alone or as TRAE CLI, Trae Agent or
  Trae AI, Zed only before a colon or AI, and goose only first, and Trae Stiles, Zed Poe and Mary
  Goose therefore stay people.
- **A name of the tool that users gave an author or a committer**, as a `convention`:
  `hermes-agent-author-02` reads the name Hermes Agent, as in `Hermes Agent <hermes@nousresearch.com>`,
  and `openclaw-author-03` reads OpenClaw in such a name, as in `Edgar (OpenClaw) <edgar@openclaw.com>`
  ([Names that users gave an identity, read 2026-10-31](#the-tools-outside-the-six-2026-09-25)). An
  agent account or an app that its own page or its project's own configuration grounds as running
  the tool is `account`, as Lodekeeper is for OpenClaw (`openclaw-author-02`).
- **IBM Bob and Ona, read 2026-10-30** over the commit logs of the run that started on
  2026-10-23. IBM's agentic development environment Bob is named in `Assisted-by: IBM Bob`,
  `Co-authored-by: IBM Bob 2.2.1`, `Generated-By: IBM Bob`, `Co-authored-by: Bob <bob@ibm.com>`,
  OpenLiberty/open-liberty's `Co-authored-by-AI: IBM Bob 2.2.3`, under a trailer key of its own,
  and the checkbox of Qiskit/qiskit-ibm-runtime's pull request template, and the rows of IBM Bob
  credit <!--s:collection.ibm_bob.commits-->736<!--/s--> commits in <!--s:collection.ibm_bob.repositories-->22<!--/s--> repositories. Ona writes `Co-authored-by: Ona <no-reply@ona.com>` at its
  own address. Both are conventions, because neither vendor's page states the line. No row reads
  ceph/ceph-csi's `Assisted-by: AskBob <askbob@ibm.com>`, a name that no source ties to IBM Bob.
  `unmatched commits` ranks the key `Co-authored-by-AI` as an attribution
  (`commits.ATTRIBUTION_TOKENS`).
- **Codg, read 2026-10-31.** Codg is the agent harness of vcaesar/codg, which runs the models of
  several vendors. Its desktop app ends a commit message with `Generated with Codg` and, when the
  message names a model, with `Assisted-by: <model> via Codg <codg@atomai.cc>`
  (`ui/desktop/pub/frontend/dist/assets/index-D8rfZ2-g.js` at 05af1049d229). `codg-trailer-01`
  reads the address and `codg-full-body-01` the footer, both as `standard`, on <!--s:collection.codg.commits-->96<!--/s--> commits in
  <!--s:collection.codg.repositories:words-->two<!--/s--> repositories, go-vgo/robotgo and go-ego/gse. The line names a Claude model, as
  `Assisted-by: claude-opus-5-5 via Codg` of robotgo 53b2b7f79f44 does, or a model of OpenAI, and
  the Claude rows skip it ([The Claude trailers, narrowed 2026-09-25](#the-claude-trailers-narrowed-2026-09-25)).
- **The tools of the commit logs, read 2026-10-30.** The second reading of the commit logs of the
  run that started on 2026-10-23 found tools outside the six that no row credited. Graphite Agent
  comments on pull requests with suggestions
  that a person can commit, and its co-author line is `reviewer`, as Greptile's is. The
  orchestrator Paperclip has the agents that it runs write
  `Co-Authored-By: Paperclip <noreply@paperclip.ing>`, as its skill
  `skills/paperclip/SKILL.md` requires. Datadog's Bits Code commits at the address that its setup
  page names, `no-reply@dtdg.co`, and under the account `datadog-bits`, and the descriptions of
  its pull requests end with `PR by Bits - [View session in Datadog](...)` or
  `[View Dev Agent Session](...)`. PostHog's coding agent writes `Generated-By: PostHog Code`,
  later `Generated-By: PostHog Desktop`, in its own code, and commits through PostHog's apps
  `posthog[bot]` and `posthog-eu[bot]`. Detail's app, which removes dead code and fixes the bugs
  that Detail's scans find, authors commits as `detail-app[bot]`. The rows read the accounts of
  the apps of Capy, Fullsend's coder agent and Detail as authors and in the co-author lines of the
  squash merges of their pull requests, and Charlie Labs' agent at its address
  `charlie@charlielabs.ai`. A commit of Fullsend that names Claude as well is in both CSVs, such
  as quay/quay 6f8b78304ecb with `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>` and
  konflux-ci/multi-platform-controller 18360adc6a4b with
  `Authored-By: Claude Code (fullsend-ai-coder)`
  ([The Authored-by, Created-by, Written-by and Made-with lines, read 2026-11-03](#the-authored-by-created-by-written-by-and-made-with-lines-read-2026-11-03)).
  The app of Nx Cloud's Self-Healing CI counts as the author, also of the empty commits with which
  it reruns CI, as the empty `Initial plan` commits of Copilot's coding agent do. No row reads its
  co-author line, because a squash merge copies the line for an empty rerun as for a fix. In most
  pull requests of a sample of such squash merges, read through GitHub's API, the app's only
  commits were empty reruns. No row reads Fullsend's
  orchestrator app `fullsend-ai-fullsend[bot]` either, whose commits update Fullsend's
  installation templates. Cloudflare's Bonk runs OpenCode and counts as OpenCode, as the section
  [OpenCode, 2026-10-20](#opencode-2026-10-20) describes. Qoder, also in its footer
  `Generated with [Qoder][https://qoder.com]`, and Snowflake's Cortex Code, also under the name
  Snowflake CoCo, are conventions, because no page of theirs states the line, and so is Hermes
  Agent's address `hermes@nousresearch.com`, which its release script lists among AI addresses.
  No row reads Mysten Labs' `Capy Bot <capy-bot@mystenlabs.com>`.
- **The tools in three or more repositories, read 2026-10-30.** The same reading found tools
  outside the six in three or more repositories each that no row credited. Gitar, which its
  documentation calls an AI code review agent that pushes
  fixes, commits at `noreply@gitar.ai`, the committer address that the documentation names, and
  its co-author line in the squash merge of a pull request is `standard`. Gitar's first product
  removes stale feature flags, and Gitar describes it as built on Polyglot Piranha, "a powerful
  code rewrite engine", and not as AI
  ([Gitar](https://www.sonarsource.com/blog/more-feature-flags-less-tech-debt/)). In the commit
  logs, its app `gitar-bot[bot]` authors only the squash merges of the pull requests that it opened
  to remove a stale flag in Unleash/unleash: those under Gitar's title for a cleanup,
  `Cleaning up stale flag: <flag> with value <true or false>`, and `chore: improve addon metrics
  usage`, the squash merge of pull request 8381, in which a person extended Gitar's cleanup of the
  flag `addonUsageMetrics`. The address `noreply@gitar.co` occurs only in such cleanups. The rows
  read neither of the two, nor a co-author line below Gitar's title for a cleanup, and they
  therefore credit none of the commits of these pull requests, as the lines of Pixee
  and Grit are left out below.
  CodSpeed's AI agents set up benchmarks and open pull requests with optimizations through the app
  `codspeed[bot]`, earlier `codspeed-hq[bot]`, and commit as `CodSpeed Bot <no-reply@codspeed.io>`.
  In oxc-project/oxc, they also commit at the GitHub noreply address of the
  app's bot user without `[bot]`, `codspeed <117304815+codspeed@users.noreply.github.com>`, which
  GitHub links to the same account. The app of Tuft, Expo's agent service, `expo-tuft[bot]`,
  commits to the pull requests that it works on. The rows read each of these accounts as the
  author and its line in the squash merge of a pull request as `standard`. Blacksmith's coding
  agent [code]smith ends its commits with
  `Co-authored-by: Codesmith <codesmith-bot@users.noreply.github.com>`. The open-sourced code of
  Terragon, which ran Claude Code, Codex and other agents in the cloud until 2026-02-09, appends
  `Co-authored-by: terragon-labs[bot]`. The sandbox of Paradigm's Centaur set the agent's identity
  `Centaur AI <ai@centaur.local>`. These lines are `standard` as well. A co-author line of
  CodeAnt AI's review app `codeant-ai[bot]` or of its app for open-source repositories,
  `codeant-ai-for-open-source[bot]`, is `reviewer`, as Greptile's is. The second app credits
  CodeAnt in squash merges of apache/superset, some of which credit no other tool.
  Macroscope suggests fixes in its reviews and also commits changes of its own, and its co-author
  line is therefore `standard`, as CodeRabbit's is, except in a suggestion that a person committed
  with GitHub's subject for it, which is `reviewer`. OpenClaw, which delegates coding work to
  agents, takes the name and the address of an agent's commits from its configuration
  (`src/agents/github-tool-identity-env.ts`). Its users name OpenClaw in the names of authors and
  committers ([Names that users gave an identity, read
  2026-10-31](#the-tools-outside-the-six-2026-09-25)) and in trailers, including `Co-developed-by`
  and `Signed-off-by` lines. A trailer names OpenClaw first in its value, as in
  `Signed-off-by: OpenClaw Agent <agent@openclaw.dev>` of SeaQL/sea-orm and
  `Co-authored-by: OpenClaw Bot <bot@openclaw.local>`, or as a word after a space or a
  parenthesis, as in
  `Co-authored-by: Joshua (OpenClaw) <person241@users.noreply.github.com>`. OpenClaw is also named in
  the footer `🤖 Generated by LLM (Claude, via OpenClaw)` of compiler-explorer/compiler-explorer and
  mattgodbolt/jsbeeb, also with an article before LLM, and in the line
  `Authored by OpenClaw (model: gpt-5.5)` that jdoe-bot writes in deepmodeling's repositories and
  that OpenClaw's code does not write. oxc's disclosure, as in
  `Generated with AI assistance (Claude/OpenClaw).`, names OpenClaw in commits of
  ChainSafe/lodestar, and Claude in it is the model that OpenClaw ran, so the line credits OpenClaw
  alone ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
  The names, the trailers and the lines
  are conventions. The rows of OpenClaw credit <!--s:collection.openclaw.commits-->600<!--/s--> commits in <!--s:collection.openclaw.repositories-->54<!--/s--> repositories. The line of jdoe-bot alone
  credits <!--s:collection.openclaw.footer_only.commits-->61<!--/s--> of them, in <!--s:collection.openclaw.footer_only.repositories-->3<!--/s--> repositories, and the trailers alone <!--s:collection.openclaw.trailers_only.commits-->119<!--/s-->, in <!--s:collection.openclaw.trailers_only.repositories-->35<!--/s-->
  repositories.

  The commits that an agent account authors or commits count as `account` when the account's own
  page grounds it as running OpenClaw, as the README of Cloudflare's Bonk grounds Bonk for OpenCode,
  and not when only a name that its owner configured ties it to OpenClaw. Lodekeeper, the account
  `lodekeeper` of ChainSafe/lodestar, is such an account. Its profile calls it an "AI contributor to
  Lodestar", and its README says "I'm an AI assistant powered by Claude" and names OpenClaw "the
  platform that powers me"
  ([lodekeeper/lodekeeper](https://github.com/lodekeeper/lodekeeper/blob/2714edb3bcae25bca480de5712d32b868c87ecea/README.md),
  read 2026-10-30). Its co-author line in a squash merge,
  `Co-authored-by: lodekeeper <lodekeeper@users.noreply.github.com>`, is `standard`, as Bonk's line
  is. The account gives OpenClaw `account` in <!--s:collection.openclaw.account.commits-->187<!--/s--> commits, and it is in <!--s:collection.openclaw.lodekeeper.commits-->205<!--/s--> commits, <!--s:collection.openclaw.lodekeeper.alone-->168<!--/s--> of which no other
  row of OpenClaw credits. Most commits of Lodekeeper contain none of the terms by which the
  commit logs were read for this section, such as `with AI assistance`.
  oxc's disclosure that names Lodekeeper counts for OpenClaw as well, also in the forms
  `This PR was authored by Lodekeeper 🌟 with AI assistance.` and
  `AI assistance: Prepared with OpenClaw (Lodekeeper).` (`openclaw-full-body-04` and
  `openclaw-full-body-05`), and a line that names a Claude model that Lodekeeper used, as in
  `(lodekeeper using Claude Opus 4)`, is Lodekeeper's, so that such a commit credits OpenClaw and
  not Claude. These
  rows add no commit beside the account. A line of the account that names Claude alone, as in
  `This PR was authored with AI assistance (Claude).`, still credits Claude
  ([Evidence limits](dataset.md#evidence-limits)). A line that names OpenClaw at
  the address of another GitHub app's bot account is that app's, as
  `Co-authored-by: OpenClaw <230910855+capy-ai[bot]@users.noreply.github.com>` is Capy's in
  OpenCloudGaming/OpenNOW. A line of Tuft, Terragon, Centaur or OpenClaw does not
  name the agent that the tool ran, and a commit that also names one of the six is in both CSVs.
- **The identities left out on 2026-10-30.** breken's page describes a service that finds bugs
  and opens pull requests with fixes but does not call it AI. `vercel[bot]` commits mix the
  deterministic updates of `fix-react2shell-next`, the setup of Vercel Web Analytics and committed
  review suggestions, which its line does not tell apart. `sentry[bot]` and
  `hf-security-analysis[bot]` have no page that describes their commits as AI. `PR Bot
  <pr-bot@minimaxi.com>` updates the model defaults of MiniMax's repositories, and
  `agent <agent@local>` names no tool. Databricks' `Isaac <no-reply@databricks.com>` has no public
  page: Databricks' unity-gateway names Isaac as an internal launcher of agent harnesses, and
  repositories' own instructions, such as the `AGENTS.md` of apache/spark-connect-rust, require the
  line. Slash, `rzp-slash[bot]`, is Razorpay's internal agent platform, used in Razorpay's
  repositories. `Co-developed-by: Aone Copilot <noreply@alibaba-inc.com>` is the line that
  Alibaba's internal assistant appends to a commit message that it generated
  (`aoneCopilot.autoGitCommit.appendCoDevelopedBy`), and a generated commit message credits no
  change. Pixee's `pixeebot[bot]` and Grit's `grit-app[bot]` apply deterministic codemods as well
  as AI, and their lines do not tell the two apart. No row reads an agent that a user named at an
  `openclaw.ai` address, such as `Storm Knight <storm-knight@openclaw.ai>`, or with OpenClaw only
  inside a longer name or a plus address, such as `person80+openclaw@example.com`, nor Gitar's
  `gitar@collate.io` and `gitar@gitar.ai`, which no Gitar page names, nor `Mira
  <mira@hermes-agent.local>` of Divkix/Alita_Robot, which names Hermes Agent only inside the address
  that its user chose. `unmatched commits` lists all of them.
- **Names that users gave an identity, read 2026-10-31.** Under the definitions of `account` and
  `convention` in [AI contribution detection](heuristics.md#ai-contribution-detection), Lodekeeper
  is `account` for OpenClaw, and a name or an address that users chose for an author or a
  committer and that names the tool is a `convention`, as a co-author line with such a name is.
  `openclaw-author-03` reads OpenClaw in such a name as `openclaw-trailer-01` reads it in a
  co-author line, as in `OpenClaw <openclaw@dimagi.com>`, `OpenClaw Subagent <subagent@openclaw.ai>`,
  `OpenClaw Bot <first248.last@example.com>`, `Edgar (OpenClaw) <edgar@openclaw.com>`,
  `Max Q. Stiles (OpenClaw)` and `jdoe-bot (driven by OpenClaw (model: gpt-5.5))[bot]`. Each of
  these names is a `convention`, because OpenClaw takes the name of an agent's commits from its
  users' configuration, and a person's bot account says on its own page only "A bot of
  @jdoe". These names credit OpenClaw in
  <!--s:collection.openclaw.names.commits-->165<!--/s--> commits in <!--s:collection.openclaw.names.repositories-->19<!--/s--> repositories, <!--s:collection.openclaw.names.only-->161<!--/s--> of which no other row of OpenClaw credits.
  `hermes-agent-author-02` reads the name Hermes Agent, as in `Hermes Agent <hermes@nousresearch.com>`
  of BasedHardware/omi, which GitHub links to a person's account, `Hermes Agent <agent@hermes.dev>`
  of the same repository, `Hermes Agent` at a placeholder address in NYRDS/remixed-dungeon and
  `Hermes Agent <hermes@sprocket.bot>` of sprocketbot/sprocket. The code of Hermes Agent writes
  only `Hermes <hermes@localhost>`, `Hermes worktree archive <hermes-archive@localhost>` and
  `Hermes Checkpoint <hermes@local>`, and Nous Research's scripts list `hermes@nousresearch.com`
  among "AI/bot emails to ignore in co-author trailers" and beside users' own agent addresses such
  as `hermes@habibilabs.dev` (NousResearch/hermes-agent at 6bb377a51d5c, read 2026-10-31). In the
  commit CSV of the tools outside the six, the name credits Hermes Agent in
  <!--s:collection.hermes_agent_names.commits-->1,254<!--/s--> commits in <!--s:collection.hermes_agent_names.repositories:words-->six<!--/s--> repositories, and the commits of Mira credit
  no tool. Among the author rows of the six, `cursor-author-03` reads the name Cursor Agent, as in
  `Cursor Agent <cursor-agent@local>` of HKUDS/LightRAG and `Cursor Agent <agent@cursor.sh>` of
  ryokun6/ryos, `codex-author-01` the name OpenAI Codex, as in `AI-Edgar (OpenAI Codex)` of
  MolSSI/QCFractal, `codex-author-02` the name Codex Agent, as in `Codex Agent <codex-agent@local>`
  of firecrawl/firecrawl-mcp-server, and `gemini-author-05` the name Gemini CLI at another address
  than `gemini-cli@google.com`, as conventions. Cursor's agent at `cursoragent@cursor.com`, Codex at
  `codex@openai.com` and `Gemini CLI <gemini-cli@google.com>` stay `account`.
- **More names, and the noreply addresses of the apps anthropic-code-agent and openai-code-agent, read 2026-11-01.** `opencode-author-03` and
  `codex-author-11` read OpenCode and Codex in the name of an author or a committer as
  `openclaw-author-03` reads OpenClaw, as in `opencode <person231@example.org>` of apache/tomcat, `codex
  <codex@local>` of kuberhealthy/kuberhealthy and `Codex (vfs-s3 bot)` of abashev/vfs-s3, but not as
  the first part of a hyphenated name, such as `codex-release` of Vexa-ai/vexa, the author of
  commits that credit Claude in a co-author line, not right after `user.name` in an address that
  holds the text of a git config command, as in BasedHardware/omi, and not in the name
  `Codex (Mateus)` of AAEmu/AAEmu, which a person used before OpenAI created openai/codex.
  `claude-author-07`, which reads the spelling
  claude-code, also reads the name Claude Code with a space, as in `Claude Code (John Roe)` of
  stefanuebe/vaadin-fullcalendar. These names are conventions. `claude-author-05` and
  `codex-author-06` read as `account` the noreply addresses of the apps anthropic-code-agent and
  openai-code-agent, `242468646+Claude@users.noreply.github.com` and
  `242516109+Codex@users.noreply.github.com`, which GitHub writes as the author of a squash merge of
  the app's pull request. `claude-trailer-10` and `codex-trailer-06` read a co-author line at these
  addresses as `standard`. In the commit CSV, `opencode-author-03` alone credits OpenCode in
  <!--s:collection.names.opencode.commits-->1,513<!--/s--> commits in
  <!--s:collection.names.opencode.repositories-->21<!--/s--> repositories,
  <!--s:collection.names.opencode.tomcat-->697<!--/s--> of them in apache/tomcat, `codex-author-11` alone
  credits Codex in <!--s:collection.names.codex.commits-->281<!--/s--> commits in
  <!--s:collection.names.codex.repositories-->28<!--/s--> repositories, and `claude-author-07` alone credits
  Claude in <!--s:collection.names.claude.commits-->90<!--/s--> commits in
  <!--s:collection.names.claude.repositories:words-->nine<!--/s--> repositories. The unmatched attributions record an
  identity that names one of the six tools in its name or its address, so that `unmatched commits`
  ranks such an identity until a row reads it.
- **The identities of the six that count as account or convention, decided 2026-10-31.** GitHub
  links `claude@anthropic.com` to Anthropic's account `claude`, but no code or environment of
  Anthropic writes the address: Claude Code writes its trailer, and an Anthropic-hosted session its committer, at
  `noreply@anthropic.com` (`claude-author-04`), and Claude Code's GitHub Action writes the noreply
  address of its bot (`src/github/operations/git-config.ts` of anthropics/claude-code-action at
  d535f5172dec). `claude-author-06` therefore reads the address, and the other addresses at
  anthropic.com that name Claude, as a convention, as `claude-trailer-02` reads a co-author line at
  the address. `codex@openai.com` stays `account` (`codex-author-05`), because the environment of
  Codex cloud writes it as the git identity: A user reports its defaults in openai/codex issue
  1281, and drl990114/MarkFlowy applies the patch of a Codex cloud task "preserving its author
  attribution" under it. A co-author line at the address is `standard` for the same reason. `codex-author-09` reads the other local parts at openai.com
  that begin with codex as a convention, such as `codex-worker@openai.com` of
  graphistry/pygraphistry, which is no account's. `copilot-author-01` reads a name that begins
  with Copilot as a convention, such as `🤖 Copilot <no-reply@github.com>`, which a skill of
  AArnott's repositories requires, `Copilot CI <copilot-ci@microsoft.com>` of dotnet/maui's
  pipeline and `Copilot <Copilot@users.noreply.github.com>`, an address without an account id.
  `copilot-author-03` and `copilot-author-04` read the spellings copilot-agent and github-copilot,
  as in `GitHub Copilot Agent <copilot-agent@github.com>` of ZSWatch/ZSWatch and
  `GitHub Copilot <github-copilot[bot]@users.noreply.github.com>` of SubtitleEdit/subtitleedit,
  which nothing ties to GitHub, and `claude-author-02` and `cursor-author-01` read the spellings
  claude-ai and cursor-ai, all as conventions. The identities that GitHub and Microsoft write stay
  `account`. `copilot-author-06` reads the squash merge or the merge commit that GitHub writes when
  a person merges a pull request of the coding agent, the name Copilot at the merger's address with
  GitHub as the committer, and leaves out an address at the reserved domain `.invalid`, such as
  the unsigned `Copilot <copilot@example.invalid>` of pyrevitlabs/pyRevit. `copilot-author-07` reads
  `GitHub Copilot <GitHub Copilot>`, the author that Azure DevOps writes for the squash merge of a
  pull request that Copilot created in Azure Repos. `claude-author-01` reads as `account` only the
  apps under which their projects' workflows run Claude Code's GitHub Action,
  `claude-code-aisi[bot]` of UKGovernmentBEIS/inspect_evals, `liam-claude-code-action-bot[bot]`
  of liam-hq/liam and `vortex-claude[bot]` of vortex-data/vortex, and `claude-author-07` reads the other identities that spell claude-code as a
  convention, such as `Claude Code Bot <claude-code-bot@github.com>`, which a workflow of
  DavidWells/analytics configures. `codex-author-03` reads as `account` only
  `elsa-workflows-codex[bot]`, which the AGENTS.md of elsa-workflows/.github names as the identity
  of Codex sessions, and `codex-author-10` reads the other logins that end in `codex[bot]` as a
  convention, such as `dagger-codex[bot]` of dagger/dagger, which nothing but its name ties to
  Codex. A `convention` row credits the tool as an `account` row does, and only the attribution
  differs.
  `claude-author-03` and `cursor-author-02` read only the vendors' apps `claude[bot]` and
  `cursor[bot]`. `claude-author-08` reads the other logins that end in `claude[bot]` as a
  convention, such as `futo-claude[bot]` of immich-app/discord-bot, which GitHub's API and the
  project's workflows do not tie to Claude Code. `gemini-author-01` to `gemini-author-03` read the spellings gemini-code, Gemini Code
  Assist and google-gemini as conventions. Their evidence commits are unsigned commits that
  people made under these identities, such as `Gemini Code Assist
  <gemini-code-assist-comitter@example.com>` of TakamiChie/audio2movie_old, and the page of Gemini
  Code Assist documents its app as a pull request reviewer.

The heuristics leave some names out on purpose. The commits of Cline's employees have their
addresses at cline.bot and no signature of the tool, and the rows of Cline read only its app, the
identity of its cloud agent and Cline named first in a line
([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)). DeepSource's app page does not state that its reviews are AI, and
`hyperlint-ai[bot]`, `langsmith-fleet[bot]` and `eon-pulumi-agent[bot]` have no page that
describes them. The pages of the apps `deepsource-autofix`, `aikido-autofix` and `mintlify`
describe pull requests with fixes and the sync of documentation but do not call the apps AI.
`linear-code[bot]` has an app page without a description, and `sentry-junior[bot]` has no app
page. Sourcegraph's co-author is Batch Changes, which is not a coding agent. A provider and a model, such as `kimi-code/k3`, is the form that OpenCode and pi write, and it shows
which account served the model, not which agent ran. The rows of the `tool not named` label of
the next section match it. `tests.test_commits.TestTheOtherToolsOfTheSeptemberCollection`
contains a real line of the September collection for every row, and the people and bots above as
lines that no heuristic may match.

#### OpenCode, 2026-10-20

The rows of `ai_commit_patterns.csv` with the tool OpenCode credit OpenCode, one of the six since
the count of [the tools](heuristics.md#the-tools). The first four were read as rows of
`other_ai_commit_patterns.csv`:

- **OpenCode's GitHub app** as the author, `opencode-agent[bot]`, which anomalyco owns.
- **Cloudflare's app Bonk**, `ask-bonk[bot]`, read 2026-10-30. Bonk "is configured via your workflow
  file and OpenCode's config", hands the commits and pull requests to `opencode github run`, and
  keeps the repository's instructions and OpenCode's (README.md of Cloudflare-Studio/ask-bonk).
  Its commits are therefore OpenCode's: the app as the author is `account`, its co-author line in
  the squash merge of its pull request `standard`, and a suggestion of the app that a person
  committed `reviewer`, as a committed suggestion of CodeRabbit is. The rows of Bonk credit
  OpenCode in <!--s:collection.bonk.commits-->51<!--/s--> commits in <!--s:collection.bonk.repositories-->5<!--/s--> repositories, <!--s:collection.bonk.alone.commits-->47<!--/s--> of which no other row of OpenCode credits.
- **A trailer with OpenCode's name first or with an `opencode.ai` address**, such as
  `Assisted-by: OpenCode:gpt-5.4` and `Co-authored-by: opencode <noreply@opencode.ai>`.
  OpenCode's code writes `noreply@opencode.ai` and no other `opencode.ai` address ([The detection accuracy evaluation of 2026-10-26, read 2026-10-29](#the-detection-accuracy-evaluation-of-2026-10-26-read-2026-10-29)).
- **Oh My OpenCode's Sisyphus**, at `clio-agent@sisyphuslabs.ai` or an `ohmyopencode.dev`
  address. Oh My OpenCode runs inside OpenCode, and the agent that made the commit is therefore
  OpenCode's. Its git-master skill instructed the agent to write the trailer from 2026-01-09 to
  2026-06-09.
- **A model that OpenCode ran**, with OpenCode after `via` or in parentheses, as in
  `Assisted-by: Claude Opus 4.8 (opencode)` ([A trailer that names no tool](#a-trailer-that-names-no-tool-2026-09-26)).
- **`Co-developed-by` and `Signed-off-by`** with OpenCode first, as antgroup/vsag signs off its
  commits.
- **The labeled disclosure, the line that states that a change was made with the tool,
  `Assisted by` with a space and `This PR was written by`**, in the forms that the rows of the
  other tools read. The description of OpenCode's Bash tool had the agent end a commit with
  `🤖 Generated with [opencode](https://opencode.ai)` at v0.3.0 (2025-07-13), and getsentry/craft,
  vercel/vercel and anomalyco/sst have the line. The description no longer contains it at v0.10.0
  (2025-09-18). The footer alone on its line is OpenCode's own marker, which
  `opencode-full-body-06` reads as `standard`.

A provider and a model, such as `github-copilot/gpt-5.4` in `Assisted-by:
OpenCode:github-copilot/gpt-5.4`, names the account that served the model and does not credit
Copilot, as [The attributions that no heuristic matched in the September collection](#the-attributions-that-no-heuristic-matched-in-the-september-collection-read-2026-09-25) states for a
slash after the tool's name.

#### A trailer that names no tool, 2026-09-26

Some trailers credit AI and name no tool: a model, `Assisted-by: GPT 5.6`, `Co-Authored-By:
grok <noreply@x.ai>`, `Co-authored-by: Opus 4.8 <noreply@anthropic.com>`; a vendor,
`Co-authored-by: OpenAI <noreply@openai.com>`; a provider and a model; or AI itself,
`Co-authored-by: AI <ai@example.com>`, `Assisted-by: LLM`. The Linux kernel's
`Documentation/process/coding-assistants.rst` has `Assisted-by:` name the agent and the model,
and many lines name only the model. The September collection left these trailers unmatched. <!--s:heuristics.other_ai_commit_patterns.tool_not_named_trailer_rows:Words-->Twenty-eight<!--/s--> rows of `other_ai_commit_patterns.csv` match them under one label,
`tool not named`, in place of a tool: AI itself, the models of OpenAI, xAI, DeepSeek,
Moonshot AI, Z.ai, Anthropic, MiniMax, Alibaba's Qwen and ByteDance's Doubao, a provider and a
model joined by a slash, a list of models, a persona with a model, a name that calls itself an AI
agent, and services that name no coding agent
([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)). In the commit CSV
of the other tools, these rows credit <!--s:collection.tool_not_named.commits-->7,561<!--/s--> commits under that label. ChatGPT is
OpenAI's chat app, and the dataset paper chose Codex in its place ([the
tools](heuristics.md#the-tools)). A trailer that names ChatGPT or an OpenAI model alone, such as
`Assisted-by: ChatGPT` or `Assisted-by: GPT-5.3-Codex`, therefore counts under `tool not named`
and not for Codex, also where every pull request with the line comes from a `codex/` head branch,
as for `AI-Agent: ChatGPT` of rsyslog/rsyslog ([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)).

A model's row matches the value only when it is the model alone, with a vendor, a version, a
parenthesized vendor, model id, reasoning effort, context size or purpose, tags in brackets and an
address at most. A value that names a tool after `via`, `with` or `and`, or that ends in `CLI`,
`Code`, `Harness` or `agent`, before its address, names a tool, and
`Grok Build (grok-4.6)`, which `grok-build-trailer-01` reads as Grok Build, and
`ChatGPT (OpenAI) and GitHub Copilot`, which `copilot-trailer-04` credits to Copilot, therefore do
not take the label. Moonshot AI's model Kimi K2.7 Code ends in `Code` and names no tool, and
`tool-not-named-trailer-05` reads it
([The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)),
as it reads `Kimi Code 2.7` at an address of Moonshot AI, and `tool-not-named-trailer-03` reads
`Grok Build 0.1` at an address of xAI ([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)).
`gpt-5.5 via Codg <codg@atomai.cc>` is Codg's (`codg-trailer-01`). Any tool also takes precedence over the label.
`commits.detect_other_ai_tools_in_fields` drops the label when a row of either file names a tool
for the same commit. A commit with a Claude Code trailer and a GPT-5 trailer is Claude
Code's, and `Co-authored-by: AI <noreply@pi.dev>` is pi's. The rows leave out people that they
could match. No row matches OpenAI's staff at `openai.com`, `Mark Ai`, `Anna Grok` or
`doe-xai`, and `unmatched commits` lists them, as it lists
`OpenAI <noreply-mt-openai@weblate.org>`, the machine translation of Weblate, which is not a
coding assistant.

#### The Claude trailers, narrowed 2026-09-25

The Claude heuristics for attribution trailers took the name after the colon for the tool, and the September collection shows lines
in which the name is not the tool.

A co-author can be a person named Claude. `co-authored-by:\s*claude` matched Claude Stiles on
django/django, Claude Mustermann on apache/creadur-rat and claudedoe on apache/beam, in commits that
nothing else attributed. Claude Code's settings reference states what the tool
writes: `Co-Authored-By: <name> <noreply@anthropic.com>`, where the name is the session's model
(`Claude Sonnet 5`), `Claude` alone when the version cannot be confirmed, or `Claude Code` for a
model that is not Claude's. The Claude co-author heuristic requires Claude to be followed by what those names put there
(i.e., nothing, punctuation, Code, Opus, Sonnet, Haiku, Fable or a version number) or Anthropic's
domain in the line, and a surname is none of these. An account name built on Claude is none of
them either. `claudespice <claude@spice.ai>`, Spice AI's account, is spelled like `claudedoe` and
uses a `claude@` address as Claude Stiles does. No row therefore matches its commits or theirs,
nor those of `Claude Autofix` and of other such names. `unmatched commits` lists all of them. The
squash merges of `claudedoe` on apache/beam name the person as `Claude` alone, at an address of
google.com. The co-author rows therefore leave out a bare Claude at google.com, and they test the
domain rather than the person's address, so that the heuristics name no person's address. Over the
commit logs of the collection of the handover 20261106T125741Z, every such line is that person's,
in apache/beam and googlecloudplatform/dataflowtemplates.

A line can name Claude as the model and another tool as the tool that ran it: `Assisted-by: Claude
Opus 4.6 via GitHub Copilot`, `Assisted-by: Claude Sonnet 4.6 (via Cursor)`. `claude-trailer-01`
to `claude-trailer-03` skip a line that names Copilot, Codex, Cursor or OpenCode after `via` or in
parentheses, in the forms after `via` and in parentheses that `copilot-trailer-04`,
`codex-trailer-04`, `cursor-trailer-05` and `opencode-trailer-04` match. These four rows read every
key of the three Claude rows, also `Generated-with:` and `Generated-with-ai:`, which
`claude-trailer-03` reads beside `Generated-by:`. A line that names Copilot
after `and`, which `copilot-trailer-04` also reads, names two assistants and stays Claude's as
well. Every line that these Claude rows skip is
therefore credited to the tool that it names. `copilot-trailer-04` matches `(GitHub Copilot CLI)`
for that reason. The Claude rows
of the other forms skip the same lines, and the rows of each of those tools for the same form
match a line that names Claude and then the tool, as osbuild/image-builder-frontend writes
`Assisted by claude-4.5-sonnet via Cursor`.
`tests.test_commits.TestALineThatAClaudeRowSkipsCreditsTheToolThatRanClaude` checks a line of
each of these forms, and of each key of `claude-trailer-01` to `claude-trailer-03`, for every tool.
The Claude rows of the forms that read the plain name of a tool, such as `claude-trailer-33` with
`Agent: Claude Opus 4.8`, skip such a line too, but no row of the other tool reads it, so that the
line credits no tool. The openers of `claude-full-body-08`, such as `AI-Tool:`, `X-AI-Tool:`,
`Coding agent:` and `Tools used:`, and the key `Author:` have such rows of the other tools, so that
`X-AI-Tool: Claude Code via Cursor` credits Cursor
([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)).
`Claude (via Conductor)` stays Claude, because Conductor runs Claude Code. Since
2026-10-30, the Claude rows also skip a line that names an agent outside the six after `via` or in
parentheses, such as IBM Bob, Kiro, Cline or Windsurf, because the line names a Claude model and
not Claude Code, as `Assisted-by: Claude 3.5 Sonnet (via IBM Bob)` of apache/artemis does. Such a
commit keeps the agent that it names through a row of that agent, such as IBM Bob through
`ibm-bob-trailer-01` and Crush through its address or through `crush-trailer-03`, which reads
`via Crush`. An agent without such
a row leaves its line unmatched, and `unmatched commits` lists it. The Claude rows skip such a line
only when the agent that it names is on a list in their patterns. They match a line that names
an agent not on the list, and `unmatched commits` cannot show it, because it ranks only the
attributions of the commits that no row matched. Codg was such an agent until it joined the list
on 2026-10-31 ([Codg, read 2026-10-31](#the-tools-outside-the-six-2026-09-25)). So was pi under the
name pi coding agent, the name of its package @earendil-works/pi-coding-agent on pi.dev, read
2026-10-31. `pi-trailer-01` now reads the name in parentheses after a model, as in
`Assisted-by: claude-opus (pi coding agent)`, and google/gvisor 4868950ce9f7 therefore credits
pi and not Claude. The Claude rows also skip a line that ends with
`via pi`, or with `via pi` before an address, and `pi-trailer-01` reads such a trailer as a
convention, because pi's own code writes no commit trailer (earendil-works/pi at f0646a00fb4c,
read 2026-10-31). The commits of adobe/da-live with `Generated-by: claude-opus-5.5 via pi`
therefore credit pi and not Claude, and the commits of glideapps/quicktype with
`Co-Authored-By: gpt-5.6-sol via pi <noreply@openai.com>` credit pi. Prose that names PI or pi after
`via` and goes on, as in `via PI serialization` of giselles-ai/giselle or `via Pi's own documented
escape hatch` of coleam00/Archon, is no line of pi. A line at pi's address is `standard` only in the
form that the pi extension pi-co-authored-by of bruno-garcia writes, `Co-Authored-By: <model>
<noreply@pi.dev>` followed by `Generated-By: pi <version>` (`pi-trailer-03`), because the
extension's code writes it while pi makes the commit, as OmX's line counts for Codex. A line at
`noreply@pi.dev` or `pi@earendil.works` alone is a convention (`pi-trailer-04`): no code that writes
it alone was found, and the AGENTS.md of d3ara1n/Polymerium tells agents to "use `noreply@pi.dev`
when no matching address exists". In the commit CSV of the other tools, commits of
brechtm/rinohtype have the extension's lines. The Claude row of oxc's
disclosure skips such a tool inside the parenthesis of the disclosure, also after `through`, as in
`(Claude through OpenCode)` ([The forms of the six tools in the commit logs, read 2026-10-30](#the-forms-of-the-six-tools-in-the-commit-logs-read-2026-10-30)).

#### Disclosures written into a message body, read 2026-09-26

The cloning step keeps, in `output/all_ai_commits-mentions.csv`, the lines that name one of the
six tools in commits that no heuristic matched. Over the mirrors of the subsample, that file showed attributions
written as prose rather than as a trailer or an identity, and GitHub's commit search measured
each across GitHub before a row was written for it. The <!--s:heuristics.ai_commit_patterns.this_pr_was_written_by_rows:words-->six<!--/s--> rows that match `This PR was written by <tool>` also match `written with the assistance
of`. The rows of `ai_commit_patterns.csv` that each item names match the following forms:

- **Cursor's pull request footer**, `Made with [Cursor](https://cursor.com)`, which Cursor
  documents: "When Agent creates commits or pull requests, Cursor can add a Made with Cursor
  trailer automatically. This applies to both git commits and pull requests created via gh pr
  create." A squash merge copies it into the commit, and its row, `cursor-full-body-02`, matches <!--s:collection.cursor_pr_footer.commits-->1,565<!--/s-->
  commits of the commit CSV. The same page documents the commit trailer `Made-with: Cursor`, and the row for that
  trailer, `cursor-trailer-04`, is therefore a `doc` row as well.
- **An AI-assisted line that names the tool**, `AI-assisted by OpenAI Codex.`, `AI-assisted with
  Codex.`, `AI-Assisted: Claude Code`, and the form that `jdx/mise`'s `AGENTS.md` requires, one
  italic span, `*AI-assisted — Tool: <tool>; model: <provider>/<model>; version: <version-or-unavailable>.*`.
  mise requires it on any GitHub content to which an AI contributed, including a pull request
  description, and after a squash merge the line can therefore record that the tool wrote the
  description. The heuristics treat it as they treat an `Assisted-by:` trailer, as a credit for
  the change. The rows `claude-full-body-03`, `claude-full-body-11`, `codex-full-body-02`,
  `copilot-full-body-04` and `cursor-full-body-04` match the line.
- **rsyslog's footer**, `With the help of AI-Agents: <agent-name>`, which its commit skill,
  `.agent/skills/rsyslog_commit/SKILL.md`, has an agent write. A name counts outside parentheses
  and not inside a model's name, which makes `GitHub Copilot (claude-sonnet-4.6)` Copilot's and gives
  `gpt-5.2-codex` and `GPT-5 Codex` the label `tool not named` (`tool-not-named-full-body-02`), because
  a credit that names only a model is tool not named. A model alone as the whole value, ChatGPT, a GPT
  model or Composer, takes the same label (`tool-not-named-full-body-09`). The rows
  `claude-full-body-04`, `codex-full-body-03`, `copilot-full-body-03`, `cursor-full-body-03` and
  `gemini-full-body-02` match the footer, `gemini-full-body-02` with Gemini CLI, Gemini Code Assist or
  Antigravity. In the
  commit CSV, the footer alone credits <!--s:collection.rsyslog_footer.codex_commits-->896<!--/s--> commits of
  rsyslog/rsyslog to Codex.
- **A line that states that a change was made with the tool**: authored, co-authored, implemented,
  written, generated, built, created, made, developed or drafted with it, by it, using it or via
  it, also with assistance from it or with help from it, as in `Authored with Claude Code; reviewed by a human
  before merge.`, `🤖 Generated with [Codex CLI](https://openai.com/codex)` and
  `*✍️ Drafted by Claude Code*`. Claude counts in the forms that the trailer rows match, as in
  `Created with assistance from Claude` and `Drafted with Claude Opus 4.8`, except a Claude model
  in the parentheses of another tool's name, which is that tool's. `by`, `drafted`, the two
  `from` forms and Claude without Code were derived from the October 2–3, 2026 collection, and
  `performed`, and `Code`, `Changes` or `Code changes` before the verb, as in
  `Code changes implemented by OpenAI Codex.`, from the detection accuracy evaluation of
  2026-10-26. The rows `claude-full-body-05`, `codex-full-body-05`, `copilot-full-body-05`,
  `cursor-full-body-05`, `gemini-full-body-03` and `opencode-full-body-02` match the line, and
  `claude-full-body-05` also reads `Initially` before the verb
  ([The labeled attributions of the evaluation of 2026-11-04](#the-labeled-attributions-of-the-evaluation-of-2026-11-04-decided-on-2026-11-08)).
- **A labeled disclosure**: a line that begins with `AI disclosure:`, `Generative AI
  disclosure:`, `AI assistance:`, `AI usage:`, `AI use:`, `AI-assisted:`, `Disclosure:` or
  `Disclaimer:` and
  names a tool anywhere after the label, as in `AI disclosure: Partially written with Codex and
  GPT 5.5.`, for each of the six. The rows `claude-full-body-08`, `codex-full-body-07`,
  `copilot-full-body-10`, `cursor-full-body-07` and `gemini-full-body-04` were derived from the
  October 2–3, 2026 collection, and `opencode-full-body-01` matches the same form for OpenCode.
  A line that states that the tool was not used does not count,
  and a bare `AI:` does not either, because it is also a conventional-commit type, except
  `AI: Codex` with Codex as the whole value (`codex-trailer-13`). Neither does a
  statement after the label that gives the tool only activities that make nothing or that make only
  the description, nor the tool followed by `found` unless the text goes on to a part of the change,
  as in `AI usage: Claude helped investigate the root cause and draft this` above
  `description from the checked diff and CMS responses.` of cowprotocol/cowswap 1fd4d76fa47c, with
  the forms of the first line below a disclosure heading. The labels
  `Coding agent:`, `Tool used:`, `Tools used:`, also after AI or `Model/`, and openemr's question
  `Was an AI assistant used? Yes`, with the tool on the same line, were derived from the
  detection accuracy evaluation of 2026-10-26, and the keys `AI-Tool:`, `AI-Agent:` and
  `AI-assistant:`, with the tool at the start of the value, from the commit logs of the run that
  started on 2026-10-23.
- **oxc's disclosure**, `Implemented with AI assistance (<tool>).`, the form that its contributors
  write for the contribution guide's "Please disclose AI usage", and the same disclosure with
  developed, written, generated, created, built, made, prepared, drafted, authored, produced or
  completed, as in `This contribution was developed with AI assistance (Codex).`, also with the
  tool on the next line of a wrapped parenthesis or after from, by, via or using in place of the
  parenthesis. The rows `claude-full-body-06`, `codex-full-body-04`, `copilot-full-body-06`,
  `cursor-full-body-10`, `gemini-full-body-06` and `opencode-full-body-09` match the disclosure.
  Rows of `other_ai_commit_patterns.csv` match the disclosure under `tool not named`
  when it names no tool, under `Gemini (unspecified)` when it names Gemini and no product, and
  under Augment when it names Augment.

Each of the last five forms names whatever tool was used, and each is therefore matched for every
tool for which a commit contains it, as [Conventions considered and not adopted](#conventions-considered-and-not-adopted) states. The
AI-assisted line is matched for <!--s:heuristics.ai_commit_patterns.ai_assisted_line_tools:words-->one<!--/s--> of the six tools, rsyslog's footer for all but OpenCode,
and the line that states that a change was made with the tool, the labeled disclosure and oxc's
disclosure for all six. Gemini counts only as Gemini CLI, Gemini Code
Assist or Antigravity, the names that the Gemini rows match ([Gemini naming no product](#gemini-naming-no-product-2026-09-27)). <!--s:heuristics.ai_commit_patterns.line_start_form_rows:Words-->114<!--/s--> rows match their form
only at the start of a line, and a subject such as `feat: AI-assisted search with Codex` is
not an attribution. For those rows, `commits._full_body` puts the subject and the body
on lines of their own.

#### Gemini naming no product, 2026-09-27

Trailer rows matched `Assisted-by: Gemini` and `Co-authored-by: Gemini` as Gemini whatever
followed the name, and a prose row matched `This PR was generated by Gemini`. The Gemini of this
dataset is Gemini CLI, Gemini Code Assist and, since 2026-09-29, Antigravity. Google documents no
commit trailer for Gemini CLI or Gemini Code Assist ([Attribution conventions (per tool)](#attribution-conventions-per-tool)). Gemini is also Google's chat app: apache/impala writes `Assisted-by: Gemini
<https://gemini.google.com>`, and the collection started on 2026-09-24 has `Generated-by: Google Gemini
2.5 Pro (Preview) Deep Research`. A bare `Claude` or `Copilot` is different, because Claude Code
writes `Co-Authored-By: Claude <noreply@anthropic.com>` and GitHub's coding agent co-authors as
`Copilot`.

Rows of `other_ai_commit_patterns.csv` match such a credit under `Gemini (unspecified)`: a
trailer with a value that starts with Gemini or Google Gemini, the sentence that names Gemini alone,
and oxc's disclosure that names Gemini alone, as in `This change was developed with AI assistance
from Gemini.` of envoyproxy/envoy. A
line that names Gemini CLI, Gemini Code Assist or Antigravity anywhere is Gemini's, and a line that names Copilot,
Codex, Cursor or OpenCode as the tool that ran the model is that tool's alone, as for Claude. In the
commit CSV of the other tools, the trailer row matches <!--s:collection.gemini_unspecified_trailer.commits-->758<!--/s--> commits in <!--s:collection.gemini_unspecified_trailer.repositories-->88<!--/s--> repositories, and <!--s:collection.gemini_unspecified_trailer.outside_six_tools-->733<!--/s--> of
them credit none of the six tools and are not in the commit CSV.

#### The forms that no heuristic matched in the collection started on 2026-09-27, read 2026-09-28

The per-repository commit records of that run showed credits that no row matched, on commits that no row matched. The
rows that each item names match them, in `ai_commit_patterns.csv` for the six tools and in
`other_ai_commit_patterns.csv` for a tool outside the six or for Gemini alone:

- **`Co-developed-by` and `Signed-off-by`** that name the tool first, as the corresponding co-author heuristics match
  `Co-authored-by`, such as `Co-developed-by: Claude Opus 4.8 <noreply@anthropic.com>`, which systemd
  writes, and `Signed-off-by: Codex <codex@openai.com>`. The rows `claude-trailer-07`,
  `claude-trailer-08`, `codex-trailer-07`, `codex-trailer-09`, `copilot-trailer-06`,
  `copilot-trailer-09`, `cursor-trailer-07`, `cursor-trailer-08` and `opencode-trailer-05` match
  such a line, and `gemini-unspecified-trailer-02` matches one that names Gemini alone. The Linux
  kernel's `submitting-patches.rst`
  defines `Co-developed-by` as the trailer for a co-author, and its `coding-assistants.rst` instructs an
  assistant to write `Assisted-by` and never `Signed-off-by`, which some agents write anyway. In the development dataset, these lines alone credit <!--s:collection.codev_signoff.claude-->465<!--/s-->
  Claude commits, <!--s:collection.codev_signoff.codex-->33<!--/s--> Codex, <!--s:collection.codev_signoff.cursor-->44<!--/s--> Cursor and <!--s:collection.codev_signoff.copilot-->7<!--/s--> Copilot. `unmatched commits` ranks
  `co-developed-by` attributions as well. `Signed-off-by` stays out of that ranking, because every commit under
  the Developer Certificate of Origin has a person's.
- **`Assisted by:` and `Generated by:` with a space** where the trailer has a hyphen, at the start
  of a line, for all six tools, with or without the colon, as in `🤖 Assisted by Claude Code`.
  The rows `claude-full-body-07`, `codex-full-body-06`, `copilot-full-body-07`,
  `cursor-full-body-06`, `gemini-full-body-05` and `opencode-full-body-03` match such a line.
  Git reads no key with a space as a trailer, and the rows therefore match the message. They also
  match an underscore before the line, as in Claude Code's web footer
  `_Generated by [Claude Code](https://claude.ai/code/session_...)_`, a link around the tool's
  name, as in `Assisted by [GitHub Copilot](https://github.com/features/copilot)`, and assisted
  by inside a sentence or a subject, as in `This PR was assisted by Claude Code.`, unless a
  quotation mark, not, n't or never comes right before it or a colon after it. These rows credit
  <!--s:collection.assisted_by_rows.claude-->900<!--/s--> Claude commits, <!--s:collection.assisted_by_rows.copilot-->1,238<!--/s--> Copilot, <!--s:collection.assisted_by_rows.cursor-->87<!--/s--> Cursor, <!--s:collection.assisted_by_rows.codex-->66<!--/s--> Codex, <!--s:collection.assisted_by_rows.gemini-->2<!--/s--> Gemini and
  <!--s:collection.assisted_by_rows.opencode-->5<!--/s--> OpenCode that no other heuristic of the tool credits. <!--s:collection.assisted_by_rows.easycla.copilot-->1,222<!--/s--> of these Copilot commits
  and <!--s:collection.assisted_by_rows.easycla.claude-->298<!--/s--> of these Claude commits are linuxfoundation/easycla's, where one author's commit template ends every commit, merge
  commits included, with both lines. Those commits count as any other written disclosure does,
  because the dataset records what a commit credits. A line that names Gemini alone counts as
  `Gemini (unspecified)` through `gemini-unspecified-full-body-02`. `Generated by Copilot at <commit>` is the summary that Copilot for
  pull requests writes of a pull request, not a credit of its code, and no row matches it. The same
  applies to `This pull request description was generated with GitHub Copilot.` and to Cursor
  Bugbot's `Written by` and `Reviewed by` footers, because a description or a review that a tool
  wrote does not show who wrote the change. The rows also read an `Assisted-by:`, `Generated-by:` or `Co-authored-by:` line in
  italics or bold, as in gitea's `_Assisted-by: Codex:GPT-5_`, a form that the detection accuracy
  evaluation of 2026-10-26 found, but not one after a backtick, which quotes a trailer.
- **Copilot's coding-agent markers**, the HTML comments `<!-- START COPILOT CODING AGENT TIPS -->`
  and `<!-- START COPILOT CODING AGENT SUFFIX -->` that the agent writes into the description of a
  pull request that it opens, which a squash merge copies into a commit under the name of whoever
  merged it. The row `copilot-full-body-08` matches them. In the development dataset, the markers
  alone credit <!--s:collection.copilot_agent_marker.commits-->249<!--/s--> commits in <!--s:collection.copilot_agent_marker.repositories-->32<!--/s--> repositories.
- **Copilot Workspace's session link**, `copilot-workspace.githubnext.com`, which GitHub Next's
  agent wrote into the description of the pull requests that it opened until it ended on 2025-05-30.
  The row `copilot-full-body-09` matches it. In the development dataset, the link alone credits <!--s:collection.copilot_workspace.commits-->177<!--/s--> commits in <!--s:collection.copilot_workspace.repositories-->73<!--/s--> repositories.
- **Codebuff**, a command-line agent, in `Co-authored-by: Codebuff <noreply@codebuff.com>`. In the development
  dataset, its rows, `codebuff-trailer-01` and `codebuff-trailer-02`, alone credit <!--s:collection.codebuff.commits-->865<!--/s--> commits in <!--s:collection.codebuff.repositories-->29<!--/s--> repositories.
- **pi with a model after a space**, `Assisted-By: pi gpt-5.5`, which the commits of openstack
  contain and which the earlier pi attribution heuristic did not match. The row `pi-trailer-02`
  matches it.

No row matches Cursor Bugbot's `Written by Cursor Bugbot` and `Reviewed by Cursor Bugbot` lines,
which are a pull request's summary and a review rather than the change, nor a `Reviewed-by`
trailer or a subject such as `address copilot comments`. The counts are the commits of the
development dataset that these rows alone credit. The `commit_reclassification` step applies
every row to a collection from the commit logs that its run kept, as every run has since
2026-09-28.

#### The detection accuracy evaluation, read 2026-10-13

The labeled samples of [the detection accuracy evaluation](tools/evaluation.md) found one commit
credited for a quote, phrasings that no row matched and lines with the wrong attribution:

- **A quote nested in a list.** ratatui's release commits list each pull request with its
  description quoted below it, `  > This PR was generated by Codex`, and `codex-full-body-01`
  credited Codex with the release. `_full_body` leaves a quoted line indented below another line
  empty. A quote at the start of a line stays, because a squash merge copies the pull request's
  own description there, as `> [!NOTE]` above `> This PR was created with the assistance of GitHub
  Copilot.` In the commit CSV of the October 5–6 collection, the commits credited only by an
  indented quote are ratatui's releases, and each commit credited only by a quote at the start of
  a line contains the disclosure of its own pull request. The reclassification of 2026-10-13 no
  longer credits these release commits.
- **Phrasings that no row matched**, from the sample of mentions: goodboy/tractor's
  ``(this patch was generated in some part by [`claude-code`][claude-code-gh])``, which
  `claude-full-body-01` reads ([The forms that the mentions of 2026-10-20 left unmatched](#the-forms-that-the-mentions-of-2026-10-20-left-unmatched));
  freenet/river's `[AI-assisted - Claude]`, `claude-full-body-11`;
  `agent-tool: Codex CLI` and `agent-tool: copilot-cli`, `codex-trailer-10` and
  `copilot-trailer-11`; and the `Copilot-Session:` trailer with a session's UUID,
  `copilot-trailer-10`. No GitHub page names that trailer, but Copilot CLI writes it beside its
  co-author trailer: <!--s:collection.copilot_session.with_cli_trailer-->10,291<!--/s--> of the <!--s:collection.copilot_session.matched-->11,747<!--/s--> commits of the commit CSV that have it, in
  <!--s:collection.copilot_session.repositories-->320<!--/s--> repositories, also have the trailer of the `github-copilot-cli` app.
- **Attributions.** The claude.ai session link that Claude Code puts into a pull request
  description, `claude-full-body-09`, is `standard`, and it is also the only credit of commits that
  no other row matches. A `Co-developed-by` or `Signed-off-by` line with the address of the trailer
  that the tool writes, `claude-trailer-08`, `copilot-trailer-09`, `cursor-trailer-08` and
  `codex-trailer-09`, is `convention` rather than the tool's own line, because no tool writes such
  a line.

In the commit CSV, `claude-full-body-09`, `claude-full-body-11`, `codex-trailer-10`,
`copilot-trailer-10`, `copilot-trailer-11` and the rows of the `Co-developed-by` and
`Signed-off-by` lines alone credit a tool in <!--s:collection.detection_evaluation_rows.alone.commits-->1,991<!--/s--> commits: <!--s:collection.detection_evaluation_rows.alone.copilot-->1,342<!--/s--> to Copilot in
<!--s:collection.detection_evaluation_rows.alone.copilot_repositories-->105<!--/s--> repositories, <!--s:collection.detection_evaluation_rows.alone.claude-->578<!--/s--> to Claude in <!--s:collection.detection_evaluation_rows.alone.claude_repositories-->117<!--/s--> and <!--s:collection.detection_evaluation_rows.alone.codex-->71<!--/s--> to Codex. The reclassification
of 2026-10-13 also changed the attributions of other commits and kept their tools. A suggestion of Devin's
review that a person committed has the trailer of Devin's own commits and stays `standard`
(commit_precision cp-0345 of the evaluation of 2026-10-05).

#### The attributions that no heuristic matched in the evaluation's sample, read 2026-10-14

The detection accuracy evaluation labeled phrasings of the sample of
mentions as attributions that no heuristic of 2026-10-13 matched, each in one or two commits.
No row matches mn-0171,
[a commit of raycast/extensions](https://github.com/raycast/extensions/commit/be792fa72ac5b747bf3f3628d27d2f19923fb78e)
in which a list of commit subjects joined Cursor's trailer `Made-with: Cursor` to the end of a
listed line. The line does not have the form of a trailer, and the trailer scope therefore does
not contain it. [The labeled attributions decided on 2026-11-02](#the-labeled-attributions-decided-on-2026-11-02)
gives the reasons why no row reads it. These rows match the other phrasings and the forms beside
them in the commit logs:

- `claude-trailer-11`: an `Assiste-By:` trailer
- `claude-full-body-12`: "Claude did most of"
- `claude-full-body-13`: a verb of making followed by "with Claude" at the end of a line
- `claude-full-body-14`: "with the help of Claude"
- `claude-full-body-15`: "have claude fix"
- `claude-full-body-16`: "written by Claude"
- `claude-full-body-17`: rivet-dev's `[SLOP(claude-…)]` prefix
- `codex-full-body-05`: "Written by an agent (Codex", an agent named in parentheses in the line
  that states that a change was made with the tool ([The forms that the mentions of 2026-10-20 left unmatched](#the-forms-that-the-mentions-of-2026-10-20-left-unmatched))
- `copilot-full-body-11`: a `-Copilot` signature after a sentence

No row matches a line where a word that could be a surname follows Claude, as in Claude Stiles.
The rows also leave out a feature that works with Claude, a change to what Claude Code does, and
a commit message, an issue description or tag blurbs that Claude wrote. In the commit CSV, the
rows listed above alone credit a tool in <!--s:collection.unmatched_phrasing_rows.alone.commits-->502<!--/s--> commits in <!--s:collection.unmatched_phrasing_rows.alone.repositories-->97<!--/s--> repositories:
<!--s:collection.unmatched_phrasing_rows.alone.claude-->191<!--/s--> to Claude, <!--s:collection.unmatched_phrasing_rows.alone.codex-->310<!--/s--> to Codex and <!--s:collection.unmatched_phrasing_rows.alone.copilot:words-->one<!--/s--> to Copilot.

#### The forms that the mentions of 2026-10-20 left unmatched

The reclassification of 2026-10-20, with OpenCode among the tools, wrote the lines of unmatched
commits that name OpenCode into `output/all_ai_commits-mentions.csv`, and with them forms that
no row of any tool read. Each form is a variant of a form that the rows of the tools already
read, and the rows of that form therefore read it for every tool:

- **The line that states that a change was made with the tool** also reads co authored written
  with a space or as one word, authored using, a colon after the preposition, an underscore
  before the line and an agent named in parentheses after an article: `Co authored with
  OpenCode.` of apache/tomcat, `Co Authored with: Claude Opus` of numba/numba and `Written by an
  agent (opencode, big-pickle).` of pnpm/pnpm, which writes the form for every agent. The Gemini
  row reads Antigravity there too, as its `Assisted by` row does.
- **`This PR was written by`** also reads a patch, the verbs produced, implemented, authored and
  prepared, with alone, a patch generated in some, large, great or small part, and an agent in
  parentheses: goodboy/tractor's ``(this patch was generated in some part by `opencode` …)`` and
  coder/coder's `*This PR was produced by opencode (agent) …*`. A commit message that a tool
  wrote, as in ``(this commit msg was generated in some part by …)``, credits no change and is
  not read.
- **`Assisted by`** also reads an `Assisted-by:` line after an emoji shortcode, as in
  `:robot: Assisted-by: Copilot:GPT-5.4` of scikit-build/scikit-build-core, which git does not
  read as a trailer.
- **The labeled disclosure** also reads a hyphen after AI, as in ggml-org/llama.cpp's
  `AI-usage: full (Bob, OpenCode + Qwen3.6-35b)`.
- **OpenCode first in an attribution trailer** also reads punctuation before the name, as in
  libredwg/libredwg's `Co-authored-by: "opencode + DeepSeek V4 Pro /high"`.

The rows of these forms read what `claude-full-body-10` and `codex-full-body-09` read, and those
two rows were removed. `tests.test_commits.TestTheFormsTheMentionsOf1020LeftUnmatched` contains a
line of each form, and lines of the same mentions that credit no change.

#### The branch merges and the apps of 2026-10-21

The merge commit that GitHub writes for a branch with an agent's prefix credits the agent, as
`copilot-full-body-01` reads `from <owner>/copilot/<branch>`. `codex-full-body-10` reads `codex/`,
`claude-full-body-18` reads `claude/`, `cursor-full-body-08` reads `cursor/` and
`opencode-full-body-05` reads `opencode/`. Claude Code's cloud sessions and its GitHub Action,
Cursor's cloud agents and OpenCode's GitHub agent name their branches with these prefixes, and a
person can change the prefix or give a branch the prefix by hand. `copilot-full-body-01` leaves out
two such branches of people and their automation: `copilot/` of the fork of ChavdaSachin in
Expensify/App, from which he opened pull requests about Expensify's own feature Copilot, and
`copilot/v1.30.2` of backstage/community-plugins, from which backstage-service opened a version
update of the repository's workspace copilot
([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
No OpenAI page names the prefix
of Codex's cloud tasks. Its row rests on the merge commits themselves, the earliest of which is
dated the day on which OpenAI introduced Codex, on the posts of its users in OpenAI's forum and on
PRarena's search query, as its notes state. Most of the merge commits that these rows read in the
commit logs of the October 5–6 collection are of `codex/` branches, and no other heuristic
credited them. `codex-full-body-12` reads the branches with the id that Codex puts before `codex`
([The labeled attributions decided on 2026-11-02](#the-labeled-attributions-decided-on-2026-11-02)).

`codex-author-07` reads `Codex <noreply@openai.com>` as the author or the committer. Codex's code
commits under this identity only in the git repository of its memories directory under
`CODEX_HOME`, which is no user's repository. The identity reaches a project's commit log when the
agent makes a commit in the project under it. Among the tools outside the
six, the GitHub apps of Codegen and Augment, and the footer that Augment writes into a pull
request that it opens, are rows of `other_ai_commit_patterns.csv`. The app of PR Arena, which
adds its workflow file when a person installs it, is a row of `not_ai_commits.csv`.

Among the tools outside the six, `devin-full-body-01` reads Devin's `devin/` and
`sweep-full-body-01` reads `sweep/`. No page of Devin names the prefix, and GitHub's records
give `devin-ai-integration[bot]` as the author of pull requests from such branches, with a Unix
time in the branch's name or a description alone. `sweepai/sweep`, the code of the app
`sweep-ai[bot]`, names its branches `"sweep/" + to_branch_name(title)`. The notes of each row
give the pull requests and the dates, and those of `sweep-full-body-01` also the
merge of a person's `sweep/` branch that the row reads. No row reads `jules/` ([Conventions considered and not adopted](#conventions-considered-and-not-adopted)). `openhands-author-01` reads OpenHands' default git identity as the
author or the committer, where `openhands-trailer-01` reads the address only in a trailer.

`codex@openai.com` is the identity that the environment of Codex cloud writes for its commits
(`codex-author-05`, grounded by openai/codex issue 1281), and GitHub's API links the address to
OpenAI's account `codex`. `codex-trailer-06`, which reads the address under any name, therefore
has the attribution `standard`, as do the rows that read the addresses of the accounts of
Copilot, Cursor and Gemini. GitHub writes such a line into a squash merge when a squashed commit
was authored at the address, as in ScrollPrize/villa 49c98db65e08, and it also carries the line
over from the message of a squashed commit, in which a person or an agent wrote it, as in
0xPolygon/polygon-cli 15eaa215d174. People and agents also write the line into commits that they
commit themselves. `noreply@openai.com` and the
account's GitHub noreply addresses belong to the same account ([The detection accuracy evaluation of 2026-10-26, read 2026-10-29](#the-detection-accuracy-evaluation-of-2026-10-26-read-2026-10-29)). A commit that the account authors at one of its
noreply addresses, as `codex <267193182+codex@users.noreply.github.com>` in i365dev/free4chat, is
the account's (`codex-author-08`). GitHub's API links
`claude@anthropic.com` to Anthropic's account `claude` as well, but no row reads that address
in a co-author line under any name, because it appears under a model's name beside another
tool's line, as `Co-authored-by: Anthropic Claude Opus 4.6 <claude@anthropic.com>` beside
`Co-authored-by: Cursor AI <ai@cursor.sh>` in pypa/setuptools-scm. As an author or a committer,
`claude-author-06` reads the address as a convention, because no code or environment of
Anthropic writes it. GitHub installs the apps
`anthropic-code-agent` and `openai-code-agent` for its partner agents for Claude and Codex. Its
API returns no record of either app, and no page names their owners.

#### The detection accuracy evaluation of 2026-10-26, read 2026-10-29

The labeled samples of [the detection accuracy evaluation](tools/evaluation.md) of the collection
of the run that started on 2026-10-23 found lines with the wrong attribution and attributions
that no row matched:

- **OpenCode's own lines.** The description of OpenCode's Bash tool told the agent to end a commit
  with `🤖 Generated with [opencode](https://opencode.ai)` and
  `Co-Authored-By: opencode <noreply@opencode.ai>` (`packages/opencode/src/tool/bash.txt` at
  v0.4.0), and `opencode-full-body-06` reads the footer alone on its line as `standard`, as
  `claude-full-body-02` reads Claude Code's. OpenCode's GitHub agent sets the git identity
  `opencode-agent[bot] <opencode-agent[bot]@users.noreply.github.com>`
  (`packages/opencode/src/cli/cmd/github.handler.ts` at v1.19.3), and `opencode-trailer-06`
  reads that address, bare and with the number of the app's bot user, as `standard`.
  `opencode-trailer-02` reads `noreply@opencode.ai` and no other `opencode.ai` address, because
  OpenCode's code writes no other. Another `opencode.ai` address is a convention of
  `opencode-trailer-07` under any name, as `Kimi-2.5 <assistant@opencode.ai>` in antgroup/vsag and
  `Sisyphus <sisyphus@opencode.ai>` in DataDog/dd-trace-java, because the domain is the domain of
  one product, while `noreply@openai.com` under a model's name credits no tool, because OpenAI makes
  several products.
- **The addresses of OpenAI's account `codex`.** `noreply@openai.com` is the address of the
  account, as `codex@openai.com` is (decided on 2026-10-29), and `codex-trailer-11` reads every
  `Assisted-by`, `Generated-by` and `Co-authored-by` line that names Codex there, in the forms in
  which the other Codex rows read the name, as `standard`, such as
  `Assisted-by: OpenAI Codex <noreply@openai.com>` and
  `Co-Authored-By: GPT-6.1-Sol via Codex <noreply@openai.com>`. A model's name alone at the
  address, as in `GPT-5 Codex <noreply@openai.com>`, names no tool. `codex-trailer-06` also reads
  the account's GitHub noreply addresses under any name.
- **CodeRabbit.** CodeRabbit commits the changes that its chat makes, such as
  `📝 CodeRabbit Chat: Rename TransformedGetSlotsQuery types to InternalGetSlotsQuery in slot
  services` in calcom/cal.diy, and the docstrings that it generates. Its co-author line is
  therefore `standard` (`coderabbit-trailer-01`), as for the other apps that review and make
  changes ([AI contribution detection](heuristics.md#ai-contribution-detection)), except in a
  suggestion that a person committed through GitHub's web interface. Such a commit has the subject
  that GitHub gives it, as `Update src/renderer/components/_atoms/accordion/index.tsx` of
  Autonomy-Logic/openplc-editor, and `coderabbit-full-body-01` and, for Devin's suggestions,
  `devin-full-body-02` read it as `reviewer`. A squash merge lists the commits that it replaces
  as bullets, each with its co-author lines, and repeats those lines below a line of nine
  hyphens. `coderabbit-full-body-02` reads a suggestion in the list, a bullet with GitHub's
  subject and the CodeRabbit line below it, as in AllStarLink/app_rpt's
  `* Update configs/rpt/rpt.conf`, as `reviewer`, and `devin-full-body-03` reads Devin's, as in
  statelyai/xstate's `* Update .changeset/vue-selector-error-snapshot.md`. The line of a change
  that CodeRabbit made, listed in the same squash merge, stays `standard`. A co-author line
  repeated below the separator credits nothing of its own (`commits._repeated_co_author_lines`).
  A squash merge can list an earlier squash merge, as skim-rs/skim's `7d4502201f89` lists
  `tests: improve coverage to 90% (#1099)` with that merge's separator and its repeated
  CodeRabbit line. The lines that the listed squash merge repeats below its own separator, up to
  the first line that is neither blank nor a trailer, credit nothing of their own either, so
  that CodeRabbit gets no `standard` from them in a squash merge that lists such a squash merge.
  The list of the outer squash merge
  continues after that block, and a co-author line of a commit listed there is that commit's
  own, as CodeRabbit's line of `perf: upt contracts/reputation/src/lib.rs` in kindfi-org/kindfi's
  `872d5d5f0021`. Reading every line up to the next separator as the block would take `standard`
  from CodeRabbit for such a line.
  Below the separator, GitHub writes one line for each co-author, including the author of a
  listed commit, so one CodeRabbit line there can stand both for a suggestion in the list and for
  a commit that CodeRabbit made without a co-author line, such as its docstrings. The squash merge then credits
  CodeRabbit only as `reviewer`, also when it lists commits of CodeRabbit's own, as IJHack/QtPass's
  [`f1a5fea87ce53fb6128317730ef9e1fb2c591e6e`](https://github.com/IJHack/QtPass/commit/f1a5fea87ce53fb6128317730ef9e1fb2c591e6e) lists `📝 Add docstrings to` and
  `📝 CodeRabbit Chat:` beside a suggestion. Since the message does not name the author of a
  listed commit, reading the repeated line as a credit would give `standard` to every squash
  merge that lists only suggestions. `devin-full-body-03` gives
  `reviewer` to <!--s:collection.devin_listed_suggestions.commits-->44<!--/s--> commits in <!--s:collection.devin_listed_suggestions.repositories-->15<!--/s--> repositories of the commit CSV of the tools outside
  the six, and Devin's line below the separator has the same limit, because Devin also commits
  changes of its own. fastrepl/anarlog's
  [`e757d740eefe21cd46d20649cdc0323d7b64dfda`](https://github.com/fastrepl/anarlog/commit/e757d740eefe21cd46d20649cdc0323d7b64dfda) credits Devin without `standard` although its
  pull request has a commit that `devin-ai-integration[bot]` authored, and it lists Devin's
  `fix: resolve review comments on sidebar and TOC` beside a suggestion. When GitHub's API was
  read on 2026-10-30, the pull requests of the other squash merges that list a suggestion of
  Devin and repeat its line below the separator had no commit of Devin. The commit logs contain
  no committed suggestion of the Codex and Cursor apps in this form.
- **The `claude` app.** The app of Claude Code GitHub Actions commits the changes that a mention
  of `@claude` requests, and its reviews contain suggestions. The action gives Claude a tool for
  inline review comments, whose description of the comment text reads "The comment text
  (supports markdown and GitHub code suggestion blocks). For code suggestions, use:
  ```` ```suggestion\nreplacement code\n``` ````."
  ([`src/mcp/github-inline-comment-server.ts`](https://github.com/anthropics/claude-code-action/blob/d70510b174a1244958df0b03166a54912811f3e6/src/mcp/github-inline-comment-server.ts#L47-L48)).
  The app's co-author line is therefore `standard` (`claude-trailer-10`), except in a suggestion
  that a person committed through GitHub's web interface. `claude-full-body-19` and
  `claude-full-body-20` read such a suggestion as `reviewer` in the two forms of
  `coderabbit-full-body-01` and `coderabbit-full-body-02`. One is a commit of its own, as
  `Update gridded_etl_tools/utils/store.py` of Arbol-Project/gridded-etl-tools, which applies the
  suggestion of claude[bot]'s review comment on its pull request 274. The other is a bullet in the
  list of a squash merge, as Automattic/wp-calypso's
  `* Update client/my-sites/plugins/marketplace-ai-experience/agent-provider.ts` in a pull
  request with only a person's commits. Both rows read only the address of the app's bot user,
  209825114, because GitHub writes the address of the account that suggested the change. In the
  commit CSV, the two rows credit Claude as `reviewer` in <!--s:collection.claude_app_suggestions.commits-->168<!--/s--> commits in <!--s:collection.claude_app_suggestions.repositories-->20<!--/s--> repositories. A
  squash merge of the second form repeats the app's line below the separator, where the line has
  the limit that CodeRabbit's line has there. When GitHub's API was read on 2026-10-30, none of
  the pull requests of these squash merges that it returned had a commit that `claude[bot]`
  authored, and it did not return the repository of oxy-hq/oxygen's `64360c081c6e`. Reading the
  repeated line as a credit would give `standard` to the squash merges with only suggestions of
  the app.
- **Keys that name the agent.** `Committed-By-Agent:` names the agent that made a commit in
  Stripe's repositories and in the commits of Stripe's staff elsewhere. `codex-trailer-12`,
  `claude-trailer-12` and `cursor-trailer-10`, and `goose-trailer-02` of
  `other_ai_commit_patterns.csv`, read it as a convention. `Agent:`, `AI-Agent:` and `AI:`
  (`codex-trailer-13`) and `Tool:`, `AI-Tool:`, `Harness:`, `Client:` and `AI-Client:`
  (`codex-trailer-14`) count for Codex when Codex is the whole value, so that a subject with the
  scope `agent:` or the type `ai:` is not read, `Agent:` also with the name of an actor after a
  slash, as `Agent: codex/root`
  ([The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)), and
  `Codex-Agent:` and `Codex-Model:` (`codex-trailer-15`) name Codex in their key. `cursor-trailer-11` reads
  `Impl-Model: cursor-grok-4.5-high-fast` of DemonDamon/AgenticX, a model that ran in Cursor.
- **Variants of the forms of every tool.** The labeled disclosure also reads the labels
  `Coding agent:`, `Tool used:` and `Model/Tool used:`, with the tool at the start of the value,
  so that a subject of Azure/azure-dev with the scope `coding-agent:`, which names GitHub Copilot
  later, is not read, and openemr's question `Was an AI assistant used? Yes`. Claude's row reads
  `claude.ai` there only as `claude.ai/code`, because consulting the chat, as in
  `Yes, consulted with claude.ai`, is advice to a person. The line that states that a change was
  made with the tool also reads `performed`, and `Code`, `Changes` or `Code changes` before the
  verb, as in `Code changes implemented by OpenAI Codex.`, and the `Assisted by` rows read
  `_Assisted-by: Codex:GPT-5_` in italics. A backtick or a quotation mark before the key quotes
  a trailer, as in `` `Assisted-by: Claude Code` trailer ``, and is not read.
- **A plugin of Copilot CLI.** `copilot-full-body-12` reads
  `Generated by the ai-ready Copilot CLI plugin.` as a convention.

No row reads `exp: creating new Avatar e2e tests with Copilot (#1571)`. A Copilot row of the
form of `claude-full-body-13` would not read this subject but would read
`This pull request description was generated with GitHub Copilot.`, a description that Copilot
wrote. A form loose enough to read the subject, a verb of making anywhere before
`with Copilot` at the end of a line, also reads features, as in
`Update workflows to auto-generate changelogs with GitHub Copilot`.

`tests.test_commits.TestTheFormsTheEvaluationOf1026Found` contains the lines of the items, the
forms for every tool and lines beside them that credit no tool.

#### The pull requests of the cloud agents of Codex and Cursor, read 2026-10-30

A Codex cloud task and a Cursor cloud agent open a pull request from a branch under the account
of the person who started them, and the commits of the branch can carry that person's identity
alone. The merge commit of such a pull request names the branch, which `codex-full-body-10` and
`cursor-full-body-08` read. A squash merge keeps only the description of the pull request, under
the name of the person who merged it. These rows read what the two agents write into the
description:

- **The link to the Codex cloud task.** Codex ends the description with a line of six hyphens and
  `[Codex Task](https://chatgpt.com/codex/tasks/task_e_<32 hex digits>)` or the bare link, also
  under `codex/cloud/tasks/` and with `task_i_` or `task_b_`. The pull requests of
  obi1kenobi/cargo-semver-checks's `ecec9b8ba32a` (#1578), openai/openai-agents-python's
  `a776d809d3f4` (#1996) and DataDog/datadog-agent's `79ed5a962576` (#49471) have a `codex/` head
  branch and only commits of people in GitHub's API. No OpenAI page names the footer, and Codex's
  command line builds the same link to a cloud task (`codex-rs/cloud-tasks/src/util.rs`).
  `codex-full-body-11` reads the link as `standard` wherever it stands, bare or as the target of
  a whole Markdown link, as `claude-full-body-09` reads Claude Code's session link.
  In the commit CSV, the row credits Codex in <!--s:collection.codex_task_links.commits-->318<!--/s--> commits in <!--s:collection.codex_task_links.repositories-->46<!--/s--> repositories, <!--s:collection.codex_task_links.alone.commits-->304<!--/s--> of which
  no other row of Codex credits. ratatui/ratatui's release
  `1c3dbd1ff4fa` quotes the description of pull request 2487 below its item, and the message
  breaks a quoted line, so that `Task](https://chatgpt.com/codex/cloud/tasks/...)` stands on a line
  outside the quote that `_full_body` leaves empty. The row does not read the rest of a link that
  begins in the quote, and the release credits no tool. A link to Codex's site, such as
  `https://chatgpt.com/codex`, names no task.
- **The description of a Cursor cloud agent.** Cursor's cloud agents write the description between
  `<!-- CURSOR_AGENT_PR_BODY_BEGIN -->` and `<!-- CURSOR_AGENT_PR_BODY_END -->` and end it with
  the buttons Open in Web and Open in Cursor, which link to the agent as
  `https://cursor.com/agents/bc-<id>` and `https://cursor.com/background-agent?bcId=bc-<id>`, in
  earlier descriptions also as `https://cursor.com/agents?id=bc-<id>`. A user writes in Cursor's
  forum that the buttons "are injected outside `<!-- CURSOR_AGENT_PR_BODY_* -->` as
  system-managed metadata", and Cursor's staff replied that it is "not possible to disable these
  today" ([the thread](https://forum.cursor.com/t/allow-disabling-cloud-agent-open-in-web-open-in-cursor-pr-footer-buttons/165430)).
  `cursor-full-body-09` reads the opening marker and the links as `standard`, but not the images
  of the buttons alone, because documents and features name Open in Cursor, as
  better-auth/better-auth's ``docs: add `Open in Cursor` option (#6992)`` does.
  In the commit CSV, the row credits Cursor in <!--s:collection.cursor_agent_descriptions.commits-->916<!--/s--> commits in <!--s:collection.cursor_agent_descriptions.repositories-->59<!--/s--> repositories, and no other row
  of Cursor credits <!--s:collection.cursor_agent_descriptions.alone.commits-->104<!--/s--> of them, in <!--s:collection.cursor_agent_descriptions.alone.repositories-->19<!--/s--> repositories. The row also reads a squash merge whose only
  co-author is the person, such as port-labs/ocean's `0b30d6bbcfd5`.
- **The address of OpenAI's app `chatgpt-codex-connector[bot]`.** `codex-trailer-08` reads the
  app's name. `codex-trailer-16` reads the address of its bot user, 199175422, with or without the
  number, under any name, as in
  `Co-authored-by: Codex <199175422+chatgpt-codex-connector[bot]@users.noreply.github.com>` of
  openinterpreter/openinterpreter, because GitHub's API gives the app's account as the user of
  both addresses (the paragraph Attributions in [AI contribution detection](heuristics.md#ai-contribution-detection)).
  A line that names the app only by this address therefore counts as `standard`.

`tests.test_commits.TestThePullRequestsOfTheCloudAgentsOfCodexAndCursor` contains a line of each
form and lines beside them that credit no tool.

#### The forms of the six tools in the commit logs, read 2026-10-30

The second reading of the commit logs of the run that started on 2026-10-23 found the line that a
layer on Codex CLI has the agent write, and variants of forms that the rows of the six read, which
the rows of each form read for every tool, as they read the form:

- **The line that states that a change was made with the tool** also reads `using` and `via`, as
  in `Generated using Claude Code automation to enhance AI-assisted development capabilities.` of
  openshift's operators, `[Generated using codex]` of DataDog/libdatadog and
  `Created using gemini-cli.` of signalapp/webrtc.
- **The labeled disclosure** also reads the keys `AI-Tool:`, `AI-Agent:` and `AI-assistant:`, with
  the tool at the start of the value, as in `AI-Tool: Antigravity` of ahochsteger/gmail-processor
  and `AI-assistant: Claude Code v2.1.138 (Claude Sonnet 4.6)` of Nextcloud's apps. It reads them
  only with the hyphen, because `- **AI Agent**: claude-code, aider, goose, amazon-q` of
  coder/coder lists the agents of a template and `# => AI agent: claude` of vercel/turborepo is
  the output of a command.
- **oxc's disclosure** also reads `prepared`, as in
  `This change was prepared with AI assistance (Claude Code).` of ray-project/ray, and a tool on
  the next line of a wrapped parenthesis, as dotnet/runtime's
  `This PR was prepared with AI assistance (Anthropic Claude and OpenAI` names Codex on the next
  line. A tool that only checked the change, as in `(Anthropic Claude, checked by` above
  `OpenAI Codex)` of dotnet/runtime, does not count, as a `Reviewed-by` trailer does not. Only the
  rows of Claude, Codex and Copilot read the disclosure before, and `cursor-full-body-10`,
  `opencode-full-body-09` and `gemini-full-body-06` read it for the other tools, as in
  `This change was implemented with AI assistance (a Cursor agent),` of agentgateway/agentgateway
  and `_Prepared with AI assistance (OpenCode).` of NVIDIA/NeMo-Agent-Toolkit. For Gemini, the
  commit logs contain the disclosure only with a preposition, as in `created with AI assistance via`
  above `Gemini/Antigravity.` of open-telemetry/opentelemetry-collector, and the Gemini row also
  links `Developed with AI assistance (Antigravity by Google DeepMind).` of TimoSavi/bbe, which
  GitHub's commit search found. Each row of the disclosure also reads a second verb or an object
  before `with`, as in `implemented and benchmarked with AI assistance (Claude Code)` of
  oxc-project/oxc and `I prepared this change with AI assistance (Claude through OpenCode).` of
  theagentrouter/agent-router. It also reads `drafted`, `authored`, `produced` and `completed`, as
  in `This PR was drafted with AI assistance (Claude)` of beetbox/beets and
  `Note: This work was completed with AI assistance (Claude Code).` of crossbario/autobahn-js. A
  list of verbs and an adverb before `with` count as well, as in
  `Investigated, implemented, and verified with AI assistance (Claude Code)` of oxc-project/oxc and
  `authored and validated locally with AI assistance (OpenAI Codex).` of projen/projen. `from`,
  `by`, `via` or `using` can stand in place of the parenthesis, with the tool before the end of
  the sentence, as in `Created with AI assistance from OpenAI Codex.` of ChainSafe/lodestar and
  `_This PR was generated with AI assistance by Codex._` of jdx/usage. Each row also reads `done` and
  `performed`, as in `were performed with AI assistance (Anthropic Claude)` of apache/cassandra, a
  comma before the preposition, as in `developed with AI assistance, using Claude Code` of
  apache/iceberg-rust, and a colon in place of the parenthesis, as in
  `Prepared with AI assistance: Claude Code (Claude Opus 5.5) and Codex` of apache/doris. A tool
  that is the object of the preposition or of the colon takes the line from Claude when a model of
  Claude follows it, either in parentheses, also after `model:` or as `anthropic/claude-...`, or
  after `with`, `using` or `running`, also followed by `the`, as in
  `developed with AI assistance via Cursor (Claude Opus 4.8)`. GitHub's commit search finds no
  commit with this form, and the Claude row treats it as the Claude rows treat a tool after `via`.
  Claude Code listed beside the tool is a tool of its own and keeps the line, as in
  `from Codex CLI and Claude Code` and `(Codex CLI + Claude Code)` of meilisearch/meilisearch-js,
  and so does Claude listed with `and`, as in `from Copilot and Claude`.
  A tool that uses a model of Claude inside the parenthesis takes the line as well, as in
  `(GitHub Copilot, using various Claude models)` of openQSE/QFw, which the search found, but not
  one that uses Claude Code, as in `(OpenClaw using Claude Code)`. The disclosure that follows a
  comma after what a person did, as in `I made this PR and directed its development, with AI
  assistance from Claude and GPT-6 Astra.` of pytorch/torchtitan, is not read, because the rows read
  the disclosure only when a verb of the list or its object stands right before `with`, and a
  clause about the person's own work does not state that the tool made the change. A disclosure of the
  description of a pull request alone, as in `PR description drafted with AI assistance (Cursor)`
  of PennyLaneAI/pennylane, credits no change, as a generated commit message credits none, and no
  row reads it. When the parenthesis names Claude and another tool after `via` or
  `through`, or in a parenthesis of its own, the Claude row leaves the line to the row of that tool,
  as the other Claude rows leave such a line ([The Claude trailers, narrowed 2026-09-25](#the-claude-trailers-narrowed-2026-09-25)).
  envoyproxy/envoy's `(Claude via` above `Cursor)` is therefore Cursor's alone, and a parenthesis
  that lists two tools, such as `(Claude Code and Cursor agent)` of envoyproxy/envoy, credits both.
  `(Augment Code and Claude code)` of apache/arrow-java credits Claude in the commit CSV and
  Augment in the CSV of the tools outside the six, through `augment-full-body-02`, which reads the
  disclosure for Augment as the rows of the six read it for their tools. The disclosure that names
  Gemini and no product counts as Gemini (unspecified)
  ([Gemini naming no product](#gemini-naming-no-product-2026-09-27)).
- **OmX's line for Codex.** OmX (oh-my-codex) calls itself "a workflow layer for
  [OpenAI Codex CLI](https://github.com/openai/codex)" that "keeps Codex as the execution engine".
  The hook that it installs in Codex, `src/scripts/codex-native-pre-post.ts` of
  Yeachan-Heo/oh-my-codex, hard-codes `Co-authored-by: OmX <omx@oh-my-codex.dev>` and blocks the
  agent's inline `git commit` until the message contains the line. The hook required the line from
  2026-04-13, and since 2026-05-19 it does so only when `OMX_LORE_COMMIT_GUARD` is set.
  `codex-trailer-17` reads the address as `standard`, as in
  `Co-authored-by: OmX <omx@oh-my-codex.dev>` of apache/airflow's `15561a3f11a3`, just as
  `opencode-trailer-03` reads the address of Oh My OpenCode for OpenCode
  ([OpenCode, 2026-10-20](#opencode-2026-10-20)).

`FAMILY_FORMS` in `tests/test_commits.py` contains the variants of the first two items and oxc's
disclosure for every tool, with its verbs and
prepositions, `tests.test_commits.TestOxcsDisclosureCreditsEveryTool` the lines of the disclosure
that name Cursor, OpenCode, Gemini or Augment, and
`tests.test_commits.TestOxcsDisclosureWithMoreVerbsAndAPreposition` the lines of the verbs and
the prepositions and the disclosures of the description alone.

#### The prefix `[codex]`

`codex-full-body-33` reads `[codex]` in lowercase at the start of a subject or of a listed subject,
a `* ` line of a squash merge, as a convention of Codex. OpenAI's skill `yeet` told Codex to title a pull request this way:
"PR title: `[codex] {description}` summarizing the full diff." (openai/skills@8766dfebfe2e,
removed by 50617dbacca4 on 2026-04-09, and in openai/plugins until 33bd9529725f on 2026-08-26), and
MahjongPantheon/pantheon's `AGENTS.md` states "Prefix every commit created by Codex with `[codex]`".
Because a tool's name in that place also names the area of a change, the row is an exception to the
decision on `(COPILOT)`. openai/codex writes the same prefix among tags such as `[app-server]`,
`[mcp]` and `[tui]`, and a row cannot leave out one repository, so that the row credits those
commits too. The maintainer decided on 2026-11-06 that the row stands, because in the other
repositories the prefix is the marker that the vendor shipped. Other spellings stay unread, such as
`[Codex] Opt Mistral` and `[Platform][Codex] Align model catalog`. A `- ` list item stays unread too,
such as `- [codex] Fix Spotify player lyrics command (#29198)` of raycast/extensions 2c65c45fdbe7 and
77ffdc4ffbc6 and `- [codex] Delete project files from R2 (#28)` of bad036d13797, because these lists
quote commits that the change does not contain. No form of a `- ` line separates them from a list
whose change the commit records, so jito-foundation/jito-solana a3f2fe92c116, `Jito Patch`, loses the
credit of `- [codex] Run cargo audit at the end of CI (#1483)`, a documented lost credit, although its
patch holds the change of #1483.

The skill gives the prefix to any agent that follows it, and the row credits Codex for a pull request
that another agent made with the skill too. In moq-dev/moq, Claude signed some of the pull requests
that have the prefix, with `(Written by Claude)` or `(Written by Claude Opus 4.8)` in the
description. Most of the others are signed `(Written by GPT-5)` or `(Written by Codex)`, #2017 is signed
`(Written by GPT-5; simplification commit and this update written by Claude Opus 4.8)`, and #2032
is not signed. The squash merges of the pull requests that Claude signed are documented wrong
credits of `codex-full-body-33`, which no form of the subject can separate from the pull requests
of Codex. 309e3cccf251 of #1921, 3ce0331de8d0 of #1924 and 4c60c52becd6 of #1939 also carry
`Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>`, which credits Claude as `standard`
beside the convention of Codex. 3f355a1b3c60 of #1930, 920616871195 of #1931 and
[404ec17e1a9a](https://github.com/moq-dev/moq/commit/404ec17e1a9ad36d0169fc3466392add783cac7a)
of #1938, `[codex] upgrade TypeScript 7 RC (#1938)`, keep only the title and credit Codex alone
(commit_precision cp-0098 of the evaluation of 2026-11-04; GitHub's API and its search of the pull
requests, read 2026-11-08).

A `* ` line names a commit of the pull request and does not show that the commit records its change, as
[Detection scopes](#detection-scopes) states for GitHub's squash list. The credits of such lines for a
change that the commit does not hold are documented wrong credits, which no form of the line can
separate from right ones such as cloudposse/atmos 6259051fef0b: coleam00/Archon c71f7f5243af, 1213b8082dfe and 671e2ee77ea8 list #1994, which Release 0.6.0
e67940a3f577 already recorded; rivet-dev/agentos 04596984b40b lists #1521, already on main as
930fb26d5bc6; PrimeIntellect-ai/prime-rl 1e0fe9637d32 lists #2523, which 7076bb1d37e0 removed from the
branch; and zgsm-ai/costrict
[3ccc049eca50](https://github.com/zgsm-ai/costrict/commit/3ccc049eca50858ca489cf72c65881743b934a30)
lists below `* Roo to main (#477) (#478)` the commits of #477 with their co-author lines of Roo
Code and Ellipsis, although #477 had reached main as 2e8c30460a1f, an ancestor of its parent
80e66bedb0ab (commit_precision cp-0563 of the evaluation of 2026-11-04; GitHub's API, read
2026-11-08).

#### The labeled attributions decided on 2026-11-02

The detection accuracy evaluations labeled attributions that no row read and left open whether a
row should read them. The vendors' pages and code, GitHub's API, and the commit CSVs, the mentions
CSV and the unmatched attributions of the run that started on 2026-10-23 decide each of them:

- **The id before the branch of a Codex cloud task.** `codex-full-body-12` reads the merge commit
  of a branch with six letters or digits and a hyphen before `codex`, as
  `Merge pull request #75 from jeonghoonkang/4zbtia-codex` of jeonghoonkang/BerePi (mentions
  mn-0127 of the evaluation of 2026-10-26) and
  `Merge pull request #3989 from MontrealAI/lm21ul-codex/execute-and-validate-ci-workflow-for-codex`
  of MontrealAI/AGI-Alpha-Agent-v0. In both repositories, nearly every branch with the id repeats
  the branch of an earlier pull request with the id in front, as
  `bqww7c-codex/reverse-proxy-server` repeats `codex/reverse-proxy-server` in BerePi, and every
  pull request of BerePi from a branch with the id ends its description with the link to its
  Codex task. GitHub's search returns such branches for pull requests with the link in other
  repositories as well, also before a branch format that a user set, as
  `garja9-feature/codex/implement-timeout-for-windows-and-posix` of U22-2025/WIP. After
  2025-10-13, Codex's id stands after the name in the branches of BerePi's pull requests, as
  `codex/create-run.py-for-ev_field_check-t00rtq`, which `codex-full-body-10` reads. No OpenAI
  page and none of openai/codex's code names either form. The row reads the six characters in
  any case, as in a person's branch `Update-codex`, because every heuristic is compiled
  case-insensitively ([Pattern classification](#pattern-classification)). The row requires the
  branch to end after `codex` or to continue with a slash, so that a person's branch such as
  `crytic/remove-codex-detector` of crytic/slither is not read. In the commit CSV, the row
  credits Codex in <!--s:collection.codex_id_branches.commits-->654<!--/s--> commits in <!--s:collection.codex_id_branches.repositories-->32<!--/s--> repositories, <!--s:collection.codex_id_branches.alone.commits-->654<!--/s--> of
  which no other row of Codex credits. The mentions CSV lists the merge of a branch
  `<id>-codex/<branch>` only when `codex` also stands as a word elsewhere in the line, because a
  name followed by a slash is not a mention. The merge of a branch that ends after `codex`, such
  as the line of mentions mn-0127 of the evaluation of 2026-10-26, is a mention by itself.
- **The prefix `(COPILOT)`.** One developer of NextCapital starts the subjects of his commits
  with `(COPILOT)`, as in `(COPILOT) Upgrade license-checker-rseidelsohn to v4 [ENG-17684]` of
  NextCapital/copy-service (mentions mn-0064 of the evaluation of 2026-10-26), and the merge
  commits of his pull requests repeat the prefix in the title below the subject. In the
  collection, only commits of NextCapital/copy-service start with the prefix in capitals, and
  GitHub's commit search finds it in other repositories of NextCapital, always in his commits. No
  vendor page and no file of the repository explains it. No row reads it, because a tool's name
  in parentheses or brackets at the start of a subject also names the area of a change. The
  exception is [the prefix `[codex]`](#the-prefix-codex), which a skill of OpenAI prescribed.
  `(Copilot) add prep invocation to listTopics (#2126)` of confluentinc/vscode changes the
  extension's chat tool `src/chat/tools/listTopics.ts`,
  ``[copilot] allow `copilot-setup-steps.yml` to fail (#10261)`` of dotnet/android changes the
  workflow that sets up Copilot's cloud agent, and
  `[CLAUDE] Added document theme/ as a separate frontend repo in CLAUDE.md` of
  informed-governance-project/SERIMA changes a context file. Only the capitals separate the
  developer's prefix from the prefix of confluentinc/vscode, in which no commit of the commit CSV
  credits Copilot, and every heuristic is compiled case-insensitively
  ([Pattern classification](#pattern-classification)). The pipeline also
  declined the `[Claude]` subject-line tag of GitClear's page
  ([Conventions considered and not adopted](#conventions-considered-and-not-adopted)).
- **Cursor's trailer at the end of a listed subject.** The squash merge be792fa72ac5 of
  raycast/extensions (mentions mn-0171 of the evaluation of 2026-10-05) contains a message that
  lists the subjects of the commits of the extension's own repository, one per line, as its
  lines of merge commits show. Its line
  `- Enhance log-weight initialization: ... percentage symbol. Made-with: Cursor` ends with
  Cursor's trailer. git takes the subject from the first paragraph: "The text up to the first
  blank line in a commit message is treated as the commit title"
  ([git-commit](https://git-scm.com/docs/git-commit)). The listed commit therefore had the
  trailer in its first paragraph, and no commit of the commit CSV has the trailer there.
  `cursor-full-body-08` already credits Cursor in be792fa72ac5, through the merges of `cursor/`
  branches that the same list contains. A row that read the trailer anywhere in a line would
  also read prose that quotes it, such as
  `Add Made-with (e.g. "Made-with: Cursor") as an accepted AI attribution` in
  flightctl/flightctl 6119c6b08824, which credits only Claude. No row therefore reads the trailer
  outside the trailer scope.

`tests.test_commits.TestTheLabeledAttributionsDecidedOn1102` contains the merges that
`codex-full-body-12` reads, the commits that its evidence links, and lines beside them that credit
no tool, among them the lines of mn-0064 and mn-0171.

#### The Authored-by, Created-by, Written-by and Made-with lines, read 2026-11-03

`commits.ATTRIBUTION_TOKENS` counts `Authored-by`, `Created-by`, `Written-by` and `Made-with` as
tokens of a credit, and the commit logs of the run that started on 2026-10-23 credit the six tools
in such lines, which no row read. Apart from Cursor's own `Made-with: Cursor`, which
`cursor-trailer-04` reads as `standard`, none of the six tools writes such a line
([Attribution conventions](#attribution-conventions-per-tool)). A person writes it, or an agent
that the instructions of a repository tell to. The AGENTS.md of redhat-appstudio/infra-deployments
at 9fc165fef906 states "Interactive sessions: Use the -s flag and `Assisted-by:` trailer. Agentic
workflow: `Authored-by:` trailer. Include agent name and tool.", and the AGENTS.md of
konflux-ci/multi-platform-controller at bbd438b1b90a states "Agentic workflow: `Authored-By:`
trailer. Include agent name and tool." Each row that reads such a line in another form is
therefore `convention`, also at the address of a tool, as for a `Co-developed-by` or
`Signed-off-by` line. The rows read the tokens in the forms that the commit logs show, also after
a prefix of a token ([Trailer extraction](#trailer-extraction)):

- **A tool named in the value.** `claude-trailer-06` also reads `Authored-by`, `Made-with` and
  `Written-by`, as in `Authored-by: Claude Code (claude-sonnet-4-6)` of redhat-appstudio/o11y,
  `Made-with: Claude Code` of x402-foundation/x402 and
  `Co-written-by: Claude Code following [the agentic coding guidelines](...)` of meltano/meltano.
  `copilot-trailer-04`, `codex-trailer-04` and `cursor-trailer-05` also read `Authored-by`, as in
  `authored-by: CoPilot / GPT-5.6` of apache/tomcat, `Authored-by: Codex (GPT-6) via Codex app` of
  redhat-appstudio/infra-deployments, `AI-authored-by: OpenAI Codex` of fedixyz/fedi and
  `Authored-By: Claude Sonnet 5 (Cursor)` of konflux-ci/multi-platform-controller.
  `opencode-trailer-01` also reads `Authored-by`,
  `Written-by` and `Made-with`, as in `Authored-by: Opencode.ai <claude-opus-4-6@default>` of
  osbuild/image-builder, `Written-By: Opencode.ai<Opus 4.6>` of coreos/coreos-assembler and
  `Made-with: OpenCode` of complytime/complyctl.
- **A tool named first.** `claude-trailer-13` reads Claude first in an `Authored-by`, `Created-by`
  or `Made-with` line, in the forms that `claude-trailer-02` reads in a co-author line, as in
  `Authored-By: Claude Opus 4.6 <noreply@anthropic.com>` and
  `Created-by: Claude (a couple of minor edits / corrections from me)` of subsurface/subsurface,
  `Tests-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>` of oamg/leapp-repository,
  `Almost-Exclusively-Authored-By: claude` of containers/kubernetes-mcp-server and
  `Made-with: Claude` of keybase/client. `codex-trailer-18` reads Codex first in an `Authored-by`
  or `Made-with` line, as in `Authored-by: Codex` of phantomcyber/splunk-soar-sdk and
  `Made-with: Codex` of flightctl/flightctl, and `cursor-trailer-12` reads Cursor first in an
  `Authored-by` line, as in `Authored-by: Cursor + Opus4.6` of redhat-appstudio/infra-deployments.
- **A Claude model that another tool ran.** `claude-trailer-13` skips a line that names Copilot,
  Codex, Cursor, OpenCode or an agent outside the six after `via` or in parentheses, as the other
  Claude rows do ([The Claude trailers, narrowed 2026-09-25](#the-claude-trailers-narrowed-2026-09-25)).
  `copilot-trailer-04`, `codex-trailer-04`, `cursor-trailer-05` and `opencode-trailer-04`
  therefore read every token of `claude-trailer-13` as well, so that such a line credits the tool
  that it names. The commit logs contain no `Created-by` or `Made-with` line in their forms, and
  no line of these tokens in the form of `opencode-trailer-04`.

A co-author line stays the line of the rows that read `Co-authored-by`, because `Co-` before
`authored-by` is no prefix of a token ([Trailer extraction](#trailer-extraction)). Fullsend's coder
app authored konflux-ci/multi-platform-controller 18360adc6a4b, which names Claude Code in its own
`Authored-By:` lines, and the commit is in both CSVs
([The tools outside the six](#the-tools-outside-the-six-2026-09-25)). flightctl/flightctl
f52fb6d31a31 credits both tools: Cursor in its co-author lines and Claude Code in its
`Made-with: Claude Code` lines. complytime/complyctl c0bbe315f488 credits OpenCode in
`Made-with: OpenCode` and Cursor in `Assisted-by: Cursor (claude-opus-4-0526)`.
`Authored-by: Composer <noreply@cursor.com>` of redhat-appstudio/infra-deployments and
konflux-ci/multi-platform-controller names Cursor's model Composer at Cursor's address, and
`cursor-trailer-17` credits it to Cursor by the decision of 2026-11-06
([The credits of the review of 2026-11-06](#the-credits-of-the-review-of-2026-11-06)). Every commit
of the commit logs that has the line credits Cursor in its co-author line as well.
`Written-by: <anthropic/claude-opus-4.6>` of coreos/coreos-assembler names a model with its
provider, as OpenCode and pi name a model, and names no tool
([The attributions that no heuristic matched in the September collection, read 2026-09-25](#the-attributions-that-no-heuristic-matched-in-the-september-collection-read-2026-09-25)).
`tool-not-named-trailer-08` reads it under the label `tool not named` since 2026-11-05
([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
No `Authored-by`, `Created-by`, `Written-by` or `Made-with` line of the commit logs names Gemini
CLI, Gemini Code Assist or Antigravity, and the rows of Gemini read none of these tokens. Of the
rows of the tools outside the six, only `pi-trailer-01` reads one of them. `Made-With: pi` is the
only credit of OpenAPITools/openapi-generator
[7fb0f556191c](https://github.com/OpenAPITools/openapi-generator/commit/7fb0f556191c0d8292f655e65660928d481db9fa)
and
[502c943a3450](https://github.com/OpenAPITools/openapi-generator/commit/502c943a34507660926817f0889ad95d7206e93b),
and `pi-trailer-01` reads pi after `Made-with:` in the forms in which it reads pi after
`Assisted-by:`, `Co-authored-by:` and `Generated-by:` (read 2026-11-05). The line is a convention,
as the other lines of the row are, because pi's code writes no commit trailer, and GitHub's code
search for `made-with` in earendil-works/pi found nothing at `d57c2d21916e`.

`tests.test_commits.TestTheLinesOfAuthoredByCreatedByWrittenByAndMadeWith` contains the lines of
these commits, the lines that the evidence of each row links, and the attributions of the tracked
unmatched attributions that name a tool in such a line. `tests.test_commits.TestMadeWithPiCreditsPi`
contains the two commits of pi.

#### pi's own names, read 2026-11-05

pi names itself "Pi coding agent" in the description of its
[flake.nix](https://github.com/earendil-works/pi/blob/d57c2d21916eaad1158031526ac6ec860eae5f14/flake.nix),
its package is `@earendil-works/pi-coding-agent`, and its site, [pi.dev](https://pi.dev), gives the
command `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`. Credit lines name pi
first in these forms, as elastic/elasticsearch-js
[f1736cbb014f](https://github.com/elastic/elasticsearch-js/commit/f1736cbb014f4e81430485f185866659c2297a0e)
does in `Assisted-by: Pi Coding Agent (claude-opus-5-5)`. Before `pi-trailer-01` read them, the
unmatched attributions of the collection of 2026-10-26 listed `assisted-by: pi-coding-agent`,
`assisted-by: pi.dev` and `co-authored-by: pi coding agent`. Credit lines also name pi first and
then how it ran or the model that it ran, as OpenVidu/openvidu
[ba72e6b9693b](https://github.com/OpenVidu/openvidu/commit/ba72e6b9693b6ce2224e10d17d3c680cefbc0b26)
does in `Assisted-by: pi - Hosted locally - Qwen 3.8 Flash Next` and as
`co-authored-by: pi with gpt-5.3-codex` of the same unmatched attributions did. `pi-trailer-01`
reads pi first in the value with each of these continuations, after every token that it reads and
with or without a provider after a colon or a model in parentheses, as a convention, because pi's
code writes no commit trailer. It also reads pi's own names later in the value, as in
`Assisted-by: Claude Opus 4.6 and pi.dev` and `Assisted-by: Gemini 3.1 Pro Preview and pi.dev` of
openshift/kube-compare
[fac46301af7c](https://github.com/openshift/kube-compare/commit/fac46301af7cd50bbe71064a2f0dd2a1b2f8bfc5)
and [687c3dc6ef72](https://github.com/openshift/kube-compare/commit/687c3dc6ef72ef76eba441cf5b9fe18770c4ffd9).
The Claude rows skip a line with `pi.dev` or `pi-coding-agent`, so that the first commit credits
pi and not Claude. The second credits Gemini (unspecified) beside pi, as `Gemini 3.1 Pro via pi`
does. `pi-trailer-01` does not read `pi.dev` as the domain of an address, such as
`noreply@pi.dev`, which `pi-trailer-04` reads. No Claude row reads `Assisted-by: Pi Coding Agent (Claude Opus 5.5)` or
`Assisted-by: pi-coding-agent (Claude Sonnet 4.6)`, so such a line credits pi and not Claude.
`pi-trailer-01` reads no other word after pi, such as the names in `co-authored-by: pi doe`
and `co-authored-by: pi-dal` of the unmatched attributions, while `pi-trailer-02` reads a model
family after pi, as in `Assisted-By: pi gpt-5.5` of LINBIT/openstack-cinder
[06dcc7923c3b](https://github.com/LINBIT/openstack-cinder/commit/06dcc7923c3bed0b7a8ca8083eee3178ef083fd5).
`tests.test_commits.TestPisOwnNamesCreditPi` contains the commits, the lines that the row reads and
those that it does not read.

#### The other credits of pi, read 2026-11-05

The unmatched attributions of the collection of 2026-10-26 and the mentions of its
[evaluation](tools/evaluation.md) list further credits of pi. `pi-trailer-01` reads the token
`Authored-by`, as `claude-trailer-13` does, in `Authored-by: DeepSeek V4 Flash 0731 (Rusty) via pi`
of redhat-appstudio/infra-deployments
[342482087d75](https://github.com/redhat-appstudio/infra-deployments/commit/342482087d75be98adec80299f7b6924f5bc59cc),
whose AGENTS.md prescribes "Agentic workflow: `Authored-by:` trailer". It reads `AI agent pi`, as
in `Co-authored-by: AI agent pi <noreply@openai.com>` of lnreader/lnreader-plugins
[b5f89dc25903](https://github.com/lnreader/lnreader-plugins/commit/b5f89dc259034b3884873fa002496e7991073958),
and a provider and a model joined by a colon before pi, as in `Assisted-by: llama.cpp:local pi` of
ggml-org/llama.cpp
[cd963fee6a86](https://github.com/ggml-org/llama.cpp/commit/cd963fee6a86387d598ebe3888017376d6e9e8f6). It does
not read a name that ends with pi, such as `co-authored-by: jane pi`. `pi-trailer-05` reads
`Sim Pi Agent <pi@sim.ai>`, which the squash merges of simstudioai/sim list as a co-author, as in
[c05168a61bb2](https://github.com/simstudioai/sim/commit/c05168a61bb23b3188e12a6e6c58afcd96992843). The pi block
of Sim runs the agent on pi's SDK, and its
[shared.ts](https://github.com/simstudioai/sim/blob/61544827f92e9802517b5f2c4f55a6ab1ef4070c/apps/sim/executor/handlers/pi/cloud/shared.ts)
commits with `-c user.email="pi@sim.ai" -c user.name="Sim Pi Agent"`. The row is a convention,
because the code of Sim and not the code of pi writes the identity.

Two body rows read disclosures that name pi, most of them as the agent that ran Claude.
`pi-full-body-01` reads the labeled disclosure that the rows of the six tools read, with pi right after the label, as in
`AI-assisted: pi (Claude)` of Blumlaut/EasyAdmin
[5174b2a64c95](https://github.com/Blumlaut/EasyAdmin/commit/5174b2a64c956b99e1b377460346eca4dd34c3a3), or with one
of pi's own names later in the line, as in
`AI disclosure: Claude (Anthropic) in the Pi coding agent, via a Roadie` of Automattic/studio
[e4f567f20243](https://github.com/Automattic/studio/commit/e4f567f202433700b6ff85ba90f703dbfc80676c). A later pi
alone does not count, because such a line can name Raspberry Pi. `pi-full-body-02` reads a line
that begins with Claude, or with Written by Claude or a like verb, and names the Pi coding agent after `via`, `in`, `through` or `and` and `the`, also with a line break in the name, `Pi backend` or `via Roadie, Pi`, as
in `Claude (via the Pi coding agent in a Roadie session) read the failing` of Automattic/studio
[2894315b4d16](https://github.com/Automattic/studio/commit/2894315b4d1629ac55d1d126b3cabe79ae003b27), the form that
the mentions list for the repository. The Claude rows skip these lines, as they skip
`via the Pi coding agent`, `in the Pi coding agent`, `(via pi)`, `via pi (` and pi before a Claude
model in parentheses. `Claude (Claude Code/Pi, via Roadie)` of Automattic/studio a1e293898a50 names
Claude Code and stays Claude.
The commits of Blumlaut/EasyAdmin, Automattic/studio and WordPress/sqlite-database-integration
with such a line therefore credit pi and not Claude.
`tests.test_commits.TestTheOtherCreditsOfPi` contains the commits.

#### The credits, read 2026-11-05

The review of the pipeline on 2026-11-05 read the commit CSVs and the unmatched attributions of the
collection of 2026-10-26 again. A squash merge of an update of Renovate copies the release notes of
the dependency into the commit, with the credits of the dependency's commits, such as
`Co-authored-by: Claude Opus 4.6 (1M context) <noreply@anthropic.com>` of
conventional-changelog/commitlint in sbb-design-systems/lyne-components
[0d5b1a0295f4](https://github.com/sbb-design-systems/lyne-components/commit/0d5b1a0295f4c705126e7decaf305db80efaa437)
and the disclosure of jdx/mise in prometheus/client_java
[9a4d600cf5f2](https://github.com/prometheus/client_java/commit/9a4d600cf5f282a2eb480ffbf62bcd1a6511148c). In a
message that names Renovate, both scopes therefore leave the `### Release Notes` section up to the
`---` line before `### Configuration` empty (`commits._own_lines`), and a credit after the section
still counts. The section also ends at the separator of a squash merge
(`commits.SQUASH_SEPARATOR`) when only trailers follow it to the end of the message, as apify/crawlee
[36375cea18b2](https://github.com/apify/crawlee/commit/36375cea18b2) and apify/apify-cli
[8756c2295095](https://github.com/apify/apify-cli/commit/8756c2295095) end with the co-author line of
Copilot's coding agent, because a separator with other text below it can be one that the release
notes quote. `claude-trailer-14` reads `roachdev-claude`, the account of CockroachDB's Claude Code
sessions, as in cockroachdb/pebble
[abab24a50ec8](https://github.com/cockroachdb/pebble/commit/abab24a50ec83672322ca925a212eb33634615e6), as a
convention, because roachdev and not Claude Code writes the line. `claude-author-01` also reads
`claude-nightshift[bot]`, the app under which the workflows of marin-community/marin run Claude
Code. `deepseek-harness-trailer-01` reads DeepSeek Harness, as in
`Assisted-by: DeepSeek Harness:deepseek-flash` of apache/nuttx
[00a379c3f7b6](https://github.com/apache/nuttx/commit/00a379c3f7b6f0aff852dc1a572ca74a186ef91c), and
`oh-my-pi-trailer-01` reads oh-my-pi, a fork of pi with the command `omp`, as in
`Assisted-by: oh-my-pi:claude-opus-5.5` of bluesky/blop
[7824e3f61ba2](https://github.com/bluesky/blop/commit/7824e3f61ba27ac106322c8a45c6b4080287bc44). The notes of each
row give its grounding.

#### The credits of the final review of 2026-11-05

The final review of the commit attributions on 2026-11-05 read the commit CSVs, the unmatched
attributions and the mentions of the collection of 2026-10-26 again. The scopes leave out the
credits below that a commit only quotes, and the trailer scope reads the trailers below that a tool
wrote into a line of another form (`commits._own_lines`, `commits._extract_trailers`).

A dependency update of Dependabot quotes the dependency in `<details>` blocks, which
dependabot-core writes as `<details>\n<summary>#{summary}</summary>\n\n` ... `</details>\n` with
the summaries `Vulnerabilities fixed`, `Release notes`, `Changelog`, `Upgrade guide` and `Commits`
([metadata_presenter.rb](https://github.com/dependabot/dependabot-core/blob/667a0ec84e4a256293015b02c4e27b3d69c4f356/common/lib/dependabot/pull_request_creator/message_builder/metadata_presenter.rb),
`build_details_tag`, read 2026-11-06), and a squash merge copies the blocks into the commit.
Automattic/studio
[fd755e0eee7d](https://github.com/Automattic/studio/commit/fd755e0eee7d1984030697a2fe1a2ea6d24f3f65)
quotes the `Claude-Session:` line and the session link of electron-playwright-helpers, and
maplibre/maplibre-tile-spec
[3b9d0a479bbc](https://github.com/maplibre/maplibre-tile-spec/commit/3b9d0a479bbc903126f489b8ab27f72b334109f5)
quotes `(co-authored with Claude Code)` of rust-diplomat/diplomat. Neither commit credits Claude
anywhere else. In a message that names Dependabot, both scopes leave these blocks empty, as they
leave the release notes that Renovate quotes empty, and a block of Dependabot's own, such as
`Maintainer changes`, and the trailers below the blocks still count.

A version bump of kube-HPC/hkube quotes the whole message of the commit before it: its
[scripts/createVersion.sh](https://github.com/kube-HPC/hkube/blob/4a57bc616f612c499196b3929eb99b09762e12c9/scripts/createVersion.sh)
commits with `MESSAGE="$(git log -1 --pretty=%B) .... bump version [skip ci]"` (read 2026-11-06).
GitHub Action's
[17cc2d34134a](https://github.com/kube-HPC/hkube/commit/17cc2d34134a6850515ff587de02822fea11fccd)
changes only `package.json` and `package-lock.json` and ends with
`Co-authored-by: Copilot <copilot@github.com> .... bump version [skip ci]`, the co-author line of
the squash merge before it. The heuristics read neither the subject nor the body of a message that
ends in the suffix, and the squash merges, such as
[fb89350f0172](https://github.com/kube-HPC/hkube/commit/fb89350f017277b69fb8ad8e443e8a5b6f9f6134),
keep their credit of Copilot.

A tool that writes the two characters `\n` in place of a line break leaves its trailers at the end
of one line of the body, as tikv/pd
[34c56d780657](https://github.com/tikv/pd/commit/34c56d780657073633bf3866015edb0ea9e57a8e) does
with `\n\nCo-authored-by: Claude Sonnet 5 <noreply@anthropic.com>` and mkrueger/icy_tools
[dc45955601d9](https://github.com/mkrueger/icy_tools/commit/dc45955601d90b1e4b68170335c5af828e036332)
with Copilot CLI's `\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`. A
squash merge copies such lines from the commits that it lists, as microsoft/vscode
[12a7c522737b](https://github.com/microsoft/vscode/commit/12a7c522737b0db738b50c2677d9d4fed0409972)
does. Both scopes read the escape as a line break, except in a list item that names another commit
by its hash ([Trailer extraction](#trailer-extraction)), and the rows read these trailers as they
read them on lines of their own. In the commit CSVs, every line that the escapes separate and a row reads is an
agent's own trailer or disclosure, such as `(Written by Copilot)` in the squash merges of
microsoft/vscode. The escape is also read where it is part of a Windows account name in a
co-author line, such as `co-authored-by: ccr\ntyukaev` among the unmatched attributions of the
collection of 2026-10-26, which then names the domain `ccr` alone and still names a person.

An emoji before a trailer, as in `🤖 Co-Authored-By: Claude Code` of DataDog/dd-trace-dotnet
[141636c73d66](https://github.com/DataDog/dd-trace-dotnet/commit/141636c73d668b960e6296150b80094380f03c20)
and `🤖 Assisted-by: Claude Code` of Project-HAMi/HAMi
[d7980599053c](https://github.com/Project-HAMi/HAMi/commit/d7980599053c7c57df946fb17e5fd14edbc8f1f9),
and the color codes of a terminal around every line of a message pasted from it, as in
SatelliteQE/broker
[77d12182a7eb](https://github.com/SatelliteQE/broker/commit/77d12182a7eb565c2120667fda6733da73712d01),
leave a line without the form of a trailer. The trailer scope reads such a line without them. The
line of SatelliteQE/broker is Claude Code's own trailer at `noreply@anthropic.com` and `standard`,
and the lines without an address are `convention`. An emoji before prose, such as Claude Code's
footer of a pull request, does not make the line a trailer.

Comfy-Org/ComfyUI_frontend
[4a4b1e487635](https://github.com/Comfy-Org/ComfyUI_frontend/commit/4a4b1e48763586a3509a055014eb1c2960f4e47c)
adds a check that fails a pull request with an agent's co-author trailer, and its message quotes
the trailer of commit 4afc68a7c3 of pull request 14073 in an inline code span that a line break
splits. The line that ends with ``reports `4afc68a7c3:`` opens the span, and the next line,
``Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` ``, closes it. The trailer scope leaves
out a line that closes a span that the line before it opened, and the commit credits no tool.

`commits.ATTRIBUTION_TOKENS` also counts `Co-authored-by-agent`, `Authored-by-agent` and
`Assisted-by-AI` as tokens of a credit, the keys that moeru-ai/airi and zephyrproject-rtos/zephyr
f4e9ea557a14 use for an agent, as in `Co-authored-by-agent: Cursor` and
`Assisted-by-AI: GitHub Copilot (GPT-5.4)`, so that `unmatched commits` lists a line under them
that no row reads.

`tests.test_commits.TestTheReleaseNotesThatDependabotQuotesCreditNoTool`,
`TestAVersionBumpOfHkubeQuotesThePreviousMessage`, `TestATrailerAfterAnEscapedLineBreakInABodyLine`,
`TestAnEmojiOrTheColorsOfATerminalBeforeATrailer`,
`TestATrailerThatAWrappedCodeSpanQuotesCreditsNoTool` and `TestTheTokensOfAnAgentAreAttributions`
contain the lines of these commits.

The maintainer decided on 2026-11-06 to add each tool outside the six that its own repository, code
or page grounds. A row is a `convention` unless it reads the tool's own account, and its notes quote
its grounding.

`aider-author-01` reads the name that aider writes for the author of its commits, as in
`Jane Doe (aider) <person219@aider.chat>`. [aider's page on git](https://aider.chat/docs/git.html)
states: "If aider authored the changes in a commit, they will have “(aider)” appended to the git
author and git committer name metadata." and "If aider simply committed changes (found in dirty
files), the commit will have “(aider)” appended to the git committer name metadata." The row
therefore reads the author's name alone, and a person's commit that aider only committed credits no
tool. `aider-trailer-03` reads the name in the co-author line of a squash merge, as in
`Co-authored-by: person-30 (aider) <person94@users.noreply.github.com>`. Both rows are `convention`,
because the name stands at the person's own address.

Oh My OpenAgent has a label of its own. `oh-my-openagent-trailer-01` reads the co-author line at its
GitHub account, and it is `standard`, because the tool writes the line:
[docs/reference/omo-json.md](https://github.com/code-yeongyu/oh-my-openagent/blob/5df8d1caca5bf0b083875a796d47ba8cddad1e2d/docs/reference/omo-json.md)
states "By default git-master also adds `Co-authored-by: sisyphus-dev-ai
<sisyphus-dev-ai@users.noreply.github.com>` once per commit". `oh-my-openagent-trailer-02` reads
OmO as the agent, as in `Assisted-by: OmO:Kimi-K3`, and `oh-my-openagent-trailer-03` reads Sisyphus
first in the value, as in `Assisted-by: Sisyphus:kimi-k2.7-code`. These lines do not record whether
the plugin for OpenCode or the `omo` command of v5.0.0 of 2026-09-26 ran, and the label covers both.
A Sisyphus line that names `oh-my-opencode` or `OhMyOpenCode`, or that stands at an address of
Sisyphus Labs or of opencode.ai, stays OpenCode's.

[Zephyr's guidelines](https://github.com/zephyrproject-rtos/zephyr/blob/f3fc249035296b87c75fa808b3abf78829fe9378/doc/contribute/guidelines.rst)
write the form of the next rows as "Assisted-by: [Agent Name]:[Model Version] [Tool1] [Tool2]", and
each row reads the agent field. `cody-trailer-01` reads
`Assisted-by: Cody:Opus_4.6`, and the maintainer decided to credit Sourcegraph's Cody although the
commits have authors at nxp.com, which also has internal agents. `vero-coder-trailer-01` reads Vero
Coder, Vero-Coder, VeroCoder and Vero before a colon in commits by authors at nxp.com. No public page
of this agent was found, and the maintainer decided on a label of its own. `kimi-code-trailer-04`
reads `Assisted-by: Kimi:kimi-k2.7` under the label of Kimi Code, because the maintainer decided that
this field names the CLI and not Moonshot's chat assistant. `codefuse-trailer-01` reads
`Assisted-by: CodeFuse:claude-sonnet-4.5` of antgroup/vsag, whose AGENTS.md prescribes
`Assisted-by: <AgentName>:<ModelVersion>`. The
[README of CodeFuse-Agent](https://github.com/codefuse-ai/CodeFuse-Agent/blob/cd4ad4fbe1c08c1ace202286d10c67d38509a8f4/README.md)
describes an agent framework with "a complete yet minimal toolset for code-related tasks".

`atomcode-trailer-01` reads AtomCode first in the value, as in
`Co-authored-by: AtomCode (GLM-5.2) <noreply@atomgit.com>`. Its
[README](https://cnb.cool/gitcodecom/atomgit_atomcode/atomcode) calls it an "Open-source terminal AI
coding agent written in Rust". The row does not read the address alone, because nothing shows that
only AtomCode writes it. `agent-tars-trailer-01` reads
`Co-authored-by: Agent TARS <agenttars@truenas.local>` of truenas/webui by the maintainer's decision.
The name is all that links this line to ByteDance's
[Agent TARS](https://github.com/bytedance/UI-TARS-desktop/blob/3ac2cb8d946d78d5ff92ad17b318eaafa18c178c/README.md),
because the addresses are TrueNAS's and neither the commits nor the repository name that stack.

`penify-author-01` and `penify-trailer-01` read the app `penify-dev[bot]`, which authors the squash
merge of its documentation pull request, as in
`[Penify]: Documentation for commit - 3a5c34e (#94)` of mikey0000/PyMammotion. `locadex-author-01`
reads `locadex-agent[bot]`, whose [app record](https://github.com/apps/locadex-agent) states
"Locadex is an AI agent which automates localization workflows." The negatives for Weblate, Crowdin
and Transifex name platforms that commit the work of translators, and this page states that an agent
does the work. `netlify-agent-runners-author-01` reads `netlify-coding[bot]`, whose
[app record](https://github.com/apps/netlify-coding) states "Kick off code changes directly from
Netlify with agents of your choice." A commit of this app with the marker `🤖 **Agent:**` also
credits the agent that the marker names, and a commit without the marker credits Agent Runners alone.

`tool-not-named-trailer-09` reads `Assisted-by: Composer`, also with a version, as in
`Assisted-by: composer-2.5-fast`, under the decision of 2026-11-03 that Composer names a model. It
does not read `Assisted-by: Composer <cursor@cursor.com>`, which `cursor-trailer-17` credits to
Cursor, as it credits Composer at Cursor's address in a co-author line, or a line that names Cursor
in parentheses or after `via`. `tool-not-named-full-body-04` reads the disclosure that
[AGENTS.md of marimo-team/marimo](https://github.com/marimo-team/marimo/blob/23fe0808a6c56d2eff1da9d45f0842ca18c1f691/AGENTS.md)
requires, "**This pull request was authored by a coding agent.**", also with `co-authored` or
`primarily authored`, and a commit that also names a tool credits that tool.

Jetski and Codecov AI stay out until a vendor page grounds them. Overseer of
GoogleCloudPlatform/k8s-config-connector gets no row, because the label resolution mn-0114 reads its
credit as the repository's own agent, and `tool-not-named-full-body-04` does not read its footer
`This pull request was generated by an AI coding agent.`
([The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)).
Polygraph and Trevize, which run other agents, stay out by the
maintainer's decision, and so do WChat, Teammate Indigo and Codet, for which the review found no page
of the tool. An agent account that names no tool stays out by the decision of 2026-10-30.
`Assisted-by: Notion AI` of sosreport/sos stays out, because
[Notion's page](https://www.notion.com/product/ai) describes "your built-in teammate inside Notion"
and does not state that it writes code, so the page does not ground a contribution to the repository.

`tests.test_commits.TestTheToolsOutsideTheSixThatTheFinalReviewFound` contains the commits of these
rows and the lines that stay out, and
`tests.test_commits.TestTheCreditsThatNameNoToolThatTheFinalReviewFound` the lines of the label
`tool not named`.

The review of the commit attributions on 2026-11-05 read the unmatched attributions of the
collection of 2026-10-26, the mentions of its [evaluation](tools/evaluation.md) and the commit
CSVs again, and checked each finding against the commits, the vendors' pages and the projects'
own configuration. The notes of each row quote its grounding.

Projects run Claude Code under apps and accounts of their own. `claude-author-01` also reads the
apps of MervinPraison/PraisonAI, openshift/hypershift, opendatadiscovery/odd-platform and
yorkie-team/yorkie-js-sdk, whose workflows or playbooks pass the app's token to Claude Code, and
`claude-trailer-16` reads the co-author line of the first and the third app in the squash merges
of their pull requests, as `opencode-trailer-08` reads Bonk's. PraisonAI's release workflow also
commits under the app's identity, so `claude-author-01` credits such a commit, as
[84b0fb12b6a2](https://github.com/MervinPraison/PraisonAI/commit/84b0fb12b6a2559ab91b5749d4b7c6a11253c511),
which records no change of Claude Code. `claude-author-09` and `claude-trailer-18` read Devsy's
app, which its page calls "Powered by Claude Code". `claude-trailer-19` reads Synadia's review
app, which runs Claude Code's GitHub Action with read-only access to the contents, as `reviewer`.
`claude-author-13` and `claude-trailer-25` read the Claude agents of neomjs/neo at their addresses
at neomjs.com, because `identityRoots.mjs` of neomjs/neo-agent-brain assigns them to the Code tab
of Claude's desktop app, and they do not read the project's GPT, Gemini and Kimi agents. `claude-trailer-21` reads the co-author line
of an app with a login that ends in `claude[bot]`, the counterpart of `claude-author-08`, which
now also reads `snowplow-claude-review[bot]`, and `claude-author-02` reads the spelling `claudeai`,
as in the account `bm-claudeai` that basicmachines-co/basic-memory names as Claude's.
`claude-author-16` reads the identity of Expensify's account MelvinBot and `claude-full-body-57`
its review suggestions that people committed
([The confirmed errors of the evaluation of 2026-11-04, checked 2026-11-08](#the-confirmed-errors-of-the-evaluation-of-2026-11-04-checked-2026-11-08)).

Further rows read Claude in forms that the rows missed. `claude-trailer-01` and
`claude-trailer-03` read a punctuation mark or an emoji before Claude, as in
`Assisted-by: 🤖 Claude Opus/Sonnet 4.6` and ``Generated-by: `Claude Fable 5` ``, but not the
`<` of `Assisted-by: <claude/opus 5>`, which names a provider and a model. `claude-trailer-02`
reads `Claude AI` and `Claude w/ Model`. `claude-trailer-22` reads Claude after a word that names
no person, as in `Assisted-by: AI (Claude)`, `Assisted-By: The Claude (claude-sonnet-4-5@20250929)`
and `Co-Authored-By: Clanker Claude <person117@example.com>`, `claude-trailer-23` reads Claude beside a
person, as in `Co-Authored-By: John Roe and Claude`, and `claude-trailer-27` reads the
misspellings `Cloude`, `Code Claude <noreply@anthropic.com>` and `Claude noreply@anthropic.co`.
`claude-trailer-26` reads the co-author line that the commit-changes skill of epam/cloud-pipeline
prescribes with a Claude model, `Cloud Pipeline Agent (<model>) <noreply@epam.com>`, and
`claude-full-body-25` a Claude model in parentheses at the end of a subject, as strawlab/strand-braid's
AGENTS.md requires. `claude-full-body-08` reads the ticked box of the disclosure of
Qiskit/qiskit-ibm-runtime, `- [x] I used the following tool to generate or modify code: Claude`,
`claude-full-body-13` reads `Fixed with Claude.` and the share of the person after Claude,
`claude-full-body-05` and `claude-full-body-07` read `Claude AI`, and `claude-full-body-05` reads
`_Developed with Claude Code_`. `claude-full-body-23` reads the footer of turso's Autofixer, which
names Claude Code as the agent that wrote the change, and `claude-full-body-24` the footer of
Automattic's Travbot.

Some credits named no change of Claude's or a model that another agent ran. `claude-trailer-02`
no longer reads `claude-configs-distributor[bot]`, which copies configuration files.
`claude-full-body-05` does not read a line that begins with a quotation mark, which quotes a
disclosure instead of making one, nor `generated by Claude, for convenience.`, which credits a
walkthrough of the change on claude.ai, and neither does `claude-full-body-07`.
`claude-full-body-16` does not read a section of a description, as in
`The "Behaviour changes" section was written by Claude`, or the reason for moving files that a
commit deletes. `claude-trailer-05` reads only the claude.ai session link that Claude Code writes,
because GaijinEntertainment/daScript writes the thread id of Codex, a UUIDv7, under the same token
beside Codex's co-author line. The Claude rows skip a line that names `omp`, the command of
oh-my-pi, after `via`, or Eclipse Theia's AI in parentheses, as in
`Assisted-by: Claude Opus 4 via omp` of TecharoHQ/anubis and
`Assisted-by: Claude Opus 4.6 (Eclipse Theia IDE AI)` of systemd/systemd, and
`oh-my-pi-trailer-01` and `theia-ai-trailer-01` credit those agents. `claude-full-body-06` skips
Claude beside Lodekeeper or another agent in the parenthesis, as in `(Lodekeeper/Claude)`,
`(Claude/OpenClaw)` and `(Cursor + Claude)`, but not Claude Code there, so that
`(Claude Code and Cursor agent)` still credits both tools.

`tests.test_commits.TestTheIdentitiesThatRunClaudeCodeForAProject`,
`tests.test_commits.TestTheFormsOfClaudesLinesThatTheReviewOf1105Found` and
`tests.test_commits.TestTheCreditsOfClaudeThatTheReviewOf1105Corrected` contain the commits and the
lines beside them that the rows do not read.

The final review of the pipeline on 2026-11-05 read the unmatched attributions and the mentions of
the collection of 2026-10-26 and the commit CSVs of the run that started on 2026-10-23 for the
forms of Codex, Copilot, Cursor and OpenCode that no row read or that a row read wrongly. The notes
of each row give its grounding, read 2026-11-06.

`cursor-author-05` reads Cursor as the first word of the name of the author or the committer, as
`codex-author-11` and `opencode-author-03` read Codex and OpenCode, as in
`Cursor Assistant <cursor@noreply.local>` and `cursor <cursor@local>` of scszcoder/eCan.ai
[843cb90e0f16](https://github.com/scszcoder/eCan.ai/commit/843cb90e0f165230d195f9fe5934d5627b577535).
It reads only the first word, because `jane cursor` of the unmatched attributions is a person with
the surname Cursor. `cursor-trailer-13` reads the misspelling `Cursour`, as in
`Generated-by: Cursour/Claude`, the only credit of operator-framework/operator-controller
[627ce439459a](https://github.com/operator-framework/operator-controller/commit/627ce439459a6cd7ab7ca6cbd63af96cdc72af42).
`cursor-trailer-14` reads Cursor after `by` in a Generated-by line that states who made the change,
as in `Generated-by: The changes to sql-parser.cup were by Cursor AI` of apache/impala
[b38852802b49](https://github.com/apache/impala/commit/b38852802b493b69159b1c0230e992f3e7237b28),
and not a line that states that Cursor reviewed the change.

The command line of Commonly adds `Agent-Model: <adapter>/<model>/<effort>` to every commit of an
agent seat, and `codex-trailer-19` reads the adapter `codex`, as in
`Agent-Model: codex/gpt-6-sol/medium` of Team-Commonly/commonly
[edd02425a3ff](https://github.com/Team-Commonly/commonly/commit/edd02425a3fffe6d9e98639d3d26e9f32bd8c492),
as a convention, because the code of Commonly and not the code of Codex writes the line.
`codex-trailer-20` reads a co-author app with a login that ends in `codex[bot]`, the counterpart of
`codex-author-10`, as in microsoft/garnet
[67c6376b28e0](https://github.com/microsoft/garnet/commit/67c6376b28e0d879f1185848737c0606466dc877).
`codex-trailer-21` reads an address at the domain `codex-app`, which cyberchitta/llm-context.py
writes for the Codex app as it writes `@claude-cli` for Claude Code. `codex-trailer-22` reads Codex
as a later word of the name in a co-author line, as `codex-author-11` reads it in the name of the
author, as in `Co-authored-by: Doe Codex <doe-codex@users.noreply.github.com>` of
sgl-project/sglang
[078f84d80df3](https://github.com/sgl-project/sglang/commit/078f84d80df31e21200e88386b00e21998bc5a25),
and not a name with GPT or OpenAI in it, such as `GPT-5 Codex <noreply@openai.com>`, which names a
model and no tool ([A trailer that names no tool, 2026-09-26](#a-trailer-that-names-no-tool-2026-09-26)).
`codex-author-12` reads `subtensor-ai-review[bot]`, the identity under which the AI review
workflow of RaoFoundation/subtensor commits the fixes that Codex makes, as `account`, and
`codex-full-body-13` reads the line `Executor model: codex · <model>` of plasmicapp/plasmic.

Several rows read what a tool or a workflow writes into a pull request, which a squash merge copies
into the commit. `codex-full-body-14` reads the title `Codex-generated pull request` and the body
that Codex's cloud agent writes when it fails after generating the change, as `standard`, as in the
merge of pull request 5083 of swarmauri/swarmauri-sdk,
[2cbe2b90bfe8](https://github.com/swarmauri/swarmauri-sdk/commit/2cbe2b90bfe80db1dd769ecba3d4b0b7f109917d).
`codex-full-body-15` and `cursor-full-body-12` read a footer that states that a model wrote the
change on Codex or Cursor, as in `> Written by gpt-5.6-sol on Codex` of marimo-team/marimo
[48f32bc780d3](https://github.com/marimo-team/marimo/commit/48f32bc780d3b19fb5e81518a12b238341377398),
with the name of a model as the first word, so that a person who writes about the tool is not
read. `codex-full-body-16` and `copilot-full-body-17` read a merge that Codex or Copilot CLI made,
as in `Merged by Copilot CLI` of microsoft/vscode-js-debug
[06ce1aa29692](https://github.com/microsoft/vscode-js-debug/commit/06ce1aa29692a9147fbca4a82aed68c43a13655e),
because a commit that records no change of the tool shows that the repository uses it
([AI contribution detection](heuristics.md#ai-contribution-detection)). GitHub Agentic Workflows
writes the marker `<!-- gh-aw-agentic-workflow: <name>, ..., engine: <engine>` into the pull
requests of a workflow (`actions/setup/js/generate_footer.cjs` of github/gh-aw).
`copilot-full-body-16` reads the engine `copilot`, GitHub's own Copilot CLI, as `standard`, and
`codex-full-body-17` reads the engine `codex` as a convention, as in fedixyz/fedi
[a23801034156](https://github.com/fedixyz/fedi/commit/a238010341564905fe0b527f9ab31723f3219ac6).
The marker in the release notes of gh-aw that an update of Renovate quotes credits no tool
([The credits, read 2026-11-05](#the-credits-read-2026-11-05)). `copilot-full-body-14` reads the
footer `_Suggested fixes powered by Copilot Autofix. Review carefully before merging._` of a pull
request that a person creates from a suggestion of Copilot Autofix, as `reviewer`, as in
gaseous-project/gaseous-server
[92cdd8b6cf9d](https://github.com/gaseous-project/gaseous-server/commit/92cdd8b6cf9d3e239fd96eaf8f3c3cdc2d80df00).
`copilot-full-body-15` reads the footer `Impacted by AI (Agent mode: copilot | ...)` that the VS Code
extension AI Contribution Tracker writes for a session of Copilot, as in Azure/Azure-Sentinel
[fcc66b55c81c](https://github.com/Azure/Azure-Sentinel/commit/fcc66b55c81cb026b694424d11eeca9c6fe9b092),
and `cursor-full-body-11` reads the description that the workflow `ai-fix-flaky-e2e-tests` of
streamlit/streamlit writes, which names only the model that Cursor CLI ran, as in
[93e4420a2e2d](https://github.com/streamlit/streamlit/commit/93e4420a2e2d49facb0612f1e78df09cb512e34a).

`opencode-trailer-04` reads OpenCode as the harness in the form
`Assisted-by: AGENT_NAME:MODEL_VERSION:HARNESS` that the AGENTS.md of icsharpcode/ILSpy requires, as
in `Assisted-by: OpenAI:gpt-5.5:OpenCode` of
[193e493ccc08](https://github.com/icsharpcode/ILSpy/commit/193e493ccc08bc12fe80c027e9fce5735430db32).
`opencode-trailer-10` reads an address at opencode.ai in a Co-developed-by or Signed-off-by line, as
in `Co-Developed-by: qwen3.6:27b via llama.cpp <assistant@opencode.ai>` of
zephyrproject-rtos/zephyr
[640cddef0678](https://github.com/zephyrproject-rtos/zephyr/commit/640cddef067875c4052d06f6f310965084e71d8f).
`opencode-trailer-11` reads `Co-Authored-By: multicode <multicode@yawk.at>`, which the skill of
multicode, a program that runs OpenCode, has the agent write, as a convention, as for the line of
Sim's pi block (`pi-trailer-05`). `opencode-trailer-04` reads OpenCode in the list of tools of the
form `Assisted-by: AGENT_NAME:MODEL_VERSION [TOOL1] [TOOL2]` of mudler/LocalAI, as in
`Assisted-by: Hephaestus:openai/gpt-5.5 [opencode]` of
[165936505942](https://github.com/mudler/LocalAI/commit/165936505942b484bb96439bb0f336f797a72018).

Other rows leave lines out. `codex-trailer-01`, `codex-full-body-02` and `codex-full-body-07` skip a
model of Codex that Cursor, Copilot or OpenCode ran, as in `Assisted-by: Codex 5.3 (Cursor)` of
apache/impala 2c011ceba3b1 and `AI-Assisted: Codex (Cursor)` of podman-desktop/podman-desktop
cd6b5f515c98, as the Claude rows skip a model of Claude that another tool ran
([The Claude trailers, narrowed 2026-09-25](#the-claude-trailers-narrowed-2026-09-25)).
`codex-trailer-02` leaves out the people `codex-doe` and `codex-reset.com`, and `copilot-trailer-02`
and `copilot-trailer-04` leave out the person behind
`Github Copilot Fallback <person-c@example.com>` of vercel/turborepo d8e82af75ad5 by the display
name, not by the person's address.
`cursor-trailer-01` skips `assisted-by: Cursor for code-review`, because a tool that only checked
the change does not count, as a Reviewed-by trailer does not. `copilot-full-body-01` leaves out two
branches that name something other than the agent
([The branch merges and the apps of 2026-10-21](#the-branch-merges-and-the-apps-of-2026-10-21)).
`copilot-full-body-05` and `copilot-full-body-07` skip a line that a quotation mark closes, the
summary of a pull request after a parenthesis, the attribution line of Copilot for pull requests
alone on a quoted line and another pull request that Copilot created, as in
hashicorp/terraform-provider-aws 818baaff8e66, DataDog/dd-trace-dotnet 302187659b9a,
near/nearcore ca8693667889 and tenstorrent/tt-metal 8273e9067dbd. readthedocs/addons 5ca20a6af7a5
still credits Copilot through `*Generated by Copilot*`, which the repository's instructions have a
session of Claude Code write ([Evidence limits](dataset.md#evidence-limits)).

`tests.test_commits.TestCursorNamedInFormsThatNoRowOfCursorRead`,
`tests.test_commits.TestCodexNamedInFormsThatNoRowOfCodexRead`,
`tests.test_commits.TestTheMarkersThatAToolOrAWorkflowWritesIntoAPullRequest` and
`tests.test_commits.TestOpenCodeNamedInFormsThatNoRowOfOpenCodeRead` contain the commits that the
evidence of the new rows links and lines beside them that the rows do not read.
`tests.test_commits.TestLinesThatCreditAnotherToolOrNoChangeOfCodexCursorOrCopilot` contains the
commits that the other rows leave out and lines that these rows still read.

The review of 2026-11-05 read the mentions, the unmatched attributions and the commit CSVs of the
collection of 2026-10-26 again and found disclosures that name a tool in forms that no row read.
A disclosure names whatever tool was used, so each form below is a row of every tool that a
commit of the collection names in it, as for the made-with and the labeled-disclosure rows
([Disclosures written into a message body, read 2026-09-26](#disclosures-written-into-a-message-body-read-2026-09-26)).
Gemini is Gemini CLI, Gemini Code Assist or Antigravity in every form, and Gemini alone stays out
of them. The Claude row of each form skips a line in which another tool ran a Claude model, after
`via`, in parentheses after the tool or after the tool on the same line, and reads Claude before a
capitalized word only when the word is Code, AI or a model, so that a person such as Claude Stiles
is not read. The row of Copilot, Codex, Cursor, OpenCode and Gemini reads the Claude form of its
line with that tool after `via` or in parentheses, as `codex-full-body-05` does, so that a line
that the Claude row skips credits the tool that ran Claude.
`tests.test_commits.TestALineThatAClaudeRowSkipsCreditsTheToolThatRanClaude` has a line of each
form.

- **A label of a pull request template.** `claude-full-body-32`, `codex-full-body-31`,
  `copilot-full-body-19`, `cursor-full-body-13`, `opencode-full-body-11` and
  `gemini-full-body-08` read a labeled disclosure that names the tool after the label, with the
  labels of the labeled-disclosure rows except `Disclaimer:` and two labels of templates:
  `AI tools and models used:`
  of jupyterlab/jupyterlab's
  [template](https://github.com/jupyterlab/jupyterlab/blob/7cc43c0627f4d29f36bed788d213773bd76cc1ff/.github/pull_request_template.md)
  (`- AI tools and models used: <!-- FILL IN -->`), as in `- AI tools and models used: Claude Opus 5.5`
  of [f58f15c43a60](https://github.com/jupyterlab/jupyterlab/commit/f58f15c43a60b9329d7d4b3bea82f54a63013c42),
  and `AI assistance used:` of openvinotoolkit/openvino, as in
  `- AI assistance used: yes. Codex implemented the changes` of
  [1e133546ae13](https://github.com/openvinotoolkit/openvino/commit/1e133546ae13c66f2fb493db8d76f7a634f0afe8).
  A tool that only verified, checked or reviewed, as in
  `Disclosure: Used claude to verify and check.`, and a disclosure of the description of the pull
  request are not read, and neither is another statement of activities that make nothing or that
  make only the description, or the tool followed by `found` unless the text goes on to a part of
  the change, as below a disclosure heading.
- **AI-assisted with the tool in parentheses.** `claude-full-body-26` and the rows of the same
  form, up to `gemini-full-body-09`, read `AI-assisted (<tool>)` as the whole line,
  `AI assistance (<tool>) was used for this change` and `AI-generated by`, `with` or `using
  <tool>`, as in `AI-assisted (Claude).` of cuthbertLab/music21 and `AI-generated by Codex on
  behalf of Eve Public.` of quarkiverse/quarkus-cxf. `claude-full-body-03`,
  `codex-full-body-02`, `copilot-full-body-04` and `cursor-full-body-04` also read AI-generated
  and AI-assisted contribution or implementation, and `opencode-full-body-23` and
  `gemini-full-body-20` read the same form for OpenCode and Gemini, as in `AI-assisted with
  OpenCode; reviewed by John Roe.` of podman-desktop/podman-desktop.
- **This pull request was made with the tool.** `claude-full-body-27` and the rows of the same
  form read a sentence that states that this pull request, change, commit, patch, contribution or
  fix was made with the tool, with an adverb such as `primarily` and a phrase of help before the
  tool, as in `**This PR was primarily authored with Codex using GPT-5-Codex` of
  lancedb/lancedb. The verb and the preposition stand next to each other, so that a sentence such
  as `this test was failing sometimes when using Claude` is not read.
- **Prepared, Done and written by the tool.** `claude-full-body-28` and the rows of the same form
  read Prepared, Done or Contributed by or with the tool at the start of a line, as in `Prepared
  with the assistance of Claude Code.` of chipsalliance/Surelog, `(written by <tool>)` at the end
  of a line or before `at a`, and an automated update created by the tool, the line of the
  OpenShift operators. A parenthesis that continues the sentence, as in `MEMORY.md files are always
  valid UTF-8 in practice (written by Claude Code),`, is not read.
- **The tool as the subject.** `claude-full-body-29` and the rows of the same form read a line
  that begins with the tool as the subject of wrote, implemented, generated, authored or drafted,
  as in `Claude Code wrote the tests and drafted this description and PR.` of oxia-db/oxia. An
  object that is only the description, also after an adjective, as in `Claude drafted the initial
  PR description text`, is not read.
- **I used the tool.** `claude-full-body-30` and the rows of the same form read `I used <tool> to`
  and a line that begins with `Used <tool> to`, with a verb of making, as in `I used Claude Code to
  help write this change and its tests.` of envoyproxy/envoy, and not a description of the pull
  request or a cause that the tool only found.
- **Opened by the tool.** `claude-full-body-39` and the rows of the same form read the footers of
  rerun-io/rerun and vortex-data/vortex, as in `🤖 This PR was opened by a coding agent (Claude
  Code).`, because the agent that opened the pull request made its commits.
- **With the help of the tool.** `claude-full-body-40` and the rows of the same form read `with
  help of` or `from <tool>` and `with <tool>'s help`, the forms of `claude-full-body-14` for every
  tool, as in `Use gha cache with help of copilot` of nandyalu/trailarr, also with `lots of` and with
  a line break in the phrase, as in `with lots of help from claude`. An analysis with the
  tool's help (`diagnosing with`, `Found this with`, `Checked again with`, `audited with`,
  `benchmarks with`, `Diagnosed with`, `to confirm`, and `the help of claude identifies` of duckdb/duckdb
  73834e6b8b7b), a description or release notes that the tool drafted, a description with an audit
  (`> This PR description and the underlying audit were drafted with the` above `help of GitHub
  Copilot.` of dotnet/runtime 73a9e0a51ed8), a review and earlier
  code are not read, and `copilot-full-body-26` refuses `Copilot backend` after the name, because the
  line break would otherwise credit a product's backend. `claude-full-body-14` no longer reads `Reviewed with the help of Claude
  Code.` of eclipse-platform/eclipse.platform, `The original code was written with the help of
  Claude` of subsurface/subsurface and `This section was generated with the help of Claude.` of
  talmolab/sleap-nn.
- **The tool that ran a model.** `claude-full-body-41` and the rows of the same form read the
  exact forms of the commit logs, `created using <tool> in agent mode`, `written with assistance
  using <tool> agent mode`, `implemented with <model> in <tool>` and `created by Oz (running
  <tool>)`, as in `_This PR was created by [Oz](https://warp.dev/oz) (running Codex)._` of
  warpdotdev/warp. The looser forms also read features, such as `Fix bug with assertion in
  cursor`.
- **Had the tool fix it.** `claude-full-body-15` also reads had, let and I had, the verbs write,
  implement, add and refactor and a model in parentheses after Claude, and `codex-full-body-30`,
  `copilot-full-body-28`, `cursor-full-body-22`, `opencode-full-body-21` and
  `gemini-full-body-18` read the same form, as in `Had copilot write its own directions.` of
  idaholab/MontePy.
- **A disclosure heading and the line below it.** `claude-full-body-35` and the rows of the same
  form read an AI-disclosure heading or a question of a template, such as openvino's
  `### AI Assistance:` with `- AI assistance used: yes`, openemr/openemr's `Was an AI assistant
  used?` and the ASF question `Was this patch authored or co-authored using generative AI
  tooling?`, and the first line below it that names the tool. They also read a line of the first
  paragraph below the heading that states that the comments, the tests, the code or another part of
  the change are by the tool, as in `- Comments are by Claude` of PowerNukkitX/PowerNukkitX
  f2cdff7eed3a, below a first line that credits no change. The first line does not count when it
  opens an HTML comment, states that no AI was used, gives the tool as an example after `e.g.` or
  `such as`, or names the tool only as the one that reviewed, checked or verified, as in
  `Written by Claude, reviewed by me and Codex` of apache/arrow, which credits Claude alone.
  The rows also read the statement that follows one of these compound labels on the same line:
  `AI usage disclosure:`, `AI assistance disclosure:`, `AI use disclosure:`, `AI tool disclosure:`,
  `AI agent disclosure:`, `AI assistance disclosed:`, `AI assistance was used:` and
  `AI disclaimer:`, also after Generative, as in
  `AI assistance was used: Codex helped implement the change and draft this` of ray-project/ray
  ed8ddca1b690 and `AI usage disclosure: Claude Code helped write the tests.` of Eventual-Inc/Daft
  8cba057ea37c. A compound label states what a heading such as `## AI usage disclosure` states, so
  the rows read the text after it with the limits of the first line below a heading, which are
  narrower than those of the labeled disclosure, which reads a tool anywhere after a label such as
  `AI usage:` or `AI disclosure:`. The first line below a heading and the text after a compound
  label also do not count when the tool is followed by `found` and the text does not go on to a
  part of the change, or when the tool is followed by `helped` and neither `, so the comments are`,
  help with the change or a part of it, nor a verb of making with an object other than the
  description, a summary or the commit message follows in the sentence. The rows now read
  `helped with` the boilerplate or the comments and `helped, so the comments are` too, as the rows
  of the tool as the subject of another verb do, and comments on a pull request, a review or an
  issue and a result other than the comments after `helped, so` stay out.
  `AI usage disclosure: Codex helped find the bug.`,
  `AI usage disclosure: Codex helped me write the PR description.` and
  `AI usage disclosure: Claude wrote the PR description.` therefore stay unread, as the same
  statements do below a heading. After a compound label, the rows also refuse names, says, adds and
  mentions,
  the verbs with which microprediction/precise 77774f36dd97 lists its edits to the sections of a
  paper: `- AI usage disclosure: names the models (Claude Opus 4.8, Opus 5, Fable 5, Opus 5.5, via
  Claude` describes the edit to the section `# AI usage disclosure` of `papers/joss/paper.md` and
  not how the commit was made, and the trailers of the commit credit Claude. The other compound
  labels of the collection credit a part of the change that the commit records, as in `AI use
  disclosure: fix authored with assistance from Claude Code.` of vortex-data/vortex a6513a0a28a0.
  ChainSafe/lodestar 2e84801d698e, a commit of Lodekeeper, states `AI assistance disclosed:
  implemented with OpenAI Codex.` and credits Codex beside OpenClaw, as Lodekeeper's `AI
  assistance: implemented with Lodekeeper/Codex.` of 4631f9fe5aa0 already does.
  The maintainer decided on 2026-11-07 that a disclosure of the description of a pull request alone,
  a review, an analysis, a search for a bug, brainstorming or a consultation credits no change and
  that no row reads it, for every verb and not only for `helped`, in the rows of a disclosure
  heading and after a compound or a plain label. The rows do not read a first line or the text after
  a label that gives the tool only activities that make nothing or that make only the description, a
  summary or the commit message. They read the activities after `was used to`, `was used for`,
  `used <tool> to`, `used <tool> for`, `used <tool> while`, `asked <tool> to`, `assisted with`,
  `helped`, a verb with `this PR by` and `was consulted`, also with `only` after `used` and with a
  role between `used <tool>` or `was used` and `to` or `for`, as in
  `used Claude as a coding assistant to`, and after the tool as the subject of a verb, to the end of
  the sentence or to a clause of the person. A clause of the person follows a semicolon, a comma,
  `and`, `but`, a parenthesis or a dash, and its subject is I, we, the author, the human, the
  submitter, the maintainer, the contributor, the developer or the code of the person, as in
  `the code is my own` and `all code is mine`, so that
  `AI-assisted: I used Claude Code while investigating; I reviewed, ran and` above
  `verified every change myself.` of line/armeria faa918d9b9d1 stays out. A clause in which the
  person used the tool or requested a part from it, as in `and I asked it to write the fix`, does
  not end the activities. The line counts when one of the activities belongs to none of these kinds:

  - a review, a check, a verification, a validation or a confirmation
  - an analysis, an investigation, an inspection, research, a search, a finding, a discovery,
    locating, an identification, mapping out, a diagnosis, debugging, tracing, an exploration, the
    navigation of the code, a comparison, an explanation or understanding
  - an audit, triage, a reproduction, a measurement, a benchmark, profiling or an evaluation
  - planning, a discussion, brainstorming, a consultation or an experiment
  - a run of checks, tests, a validation, a reviewer, the resolution of links, a repro, a
    reproduction or an experiment
  - the making of the description, a summary, the commit message or the text, the metadata, the
    body or the title of the pull request, also `the commit and PR text`
  - comments on the pull request: a number of comments, review or inline comments, and comments to
    or on the pull request, as in `generated 8 comments` of the summary of a review by Copilot

  A noun pair at the end of an activity belongs to the activity only when its last word names an
  activity that makes nothing and its first word is no verb, as in
  `verifying the diff and reference check,` of pomerium/pomerium ad119608be99. A pair that ends in
  another noun or begins with a verb of change starts an activity of its own, so that
  `OpenAI Codex assisted with code exploration and implementation drafting.` of apache/arrow-rs
  237aed5c8e40 stays read below a heading and after a label, and so do
  `Codex was used for analysis and code generation.` and
  `Codex was used to debug and update tests.` In the sources of the description, the pair may end
  in any noun, as in `from the checked diff and CMS responses.` of cowprotocol/cowswap
  1fd4d76fa47c. `- [x] AI tools (Claude) were used by the maintainer to draft this` above
  `description and review the change. Code authored by @amerberg.` of Netflix/metaflow 3e99c6433004,
  `Codex assisted with source inspection and preparation of the commit` above
  `message and PR description.` of verl-project/verl 68c9ac35d9f3,
  `AI assistance (Claude Code) was used for the collision analysis, the` above
  `control experiment, and drafting this description.` of ray-project/ray f606a9fa4e18 and
  `Codex finalized this PR by verifying the diff and reference check,` above
  `running the shared Claude reviewer, amending the branch commit message,` of pomerium/pomerium
  ad119608be99, which does not credit the deletion of `.fossa.yml` to Codex, stay out. A
  consultation counts only when a verb of making of the tool follows that the sentence does not
  negate, after no determiner and no person and with an object other than the description or
  comments, or when the next sentence begins with It and wrote, made, implemented, fixed or
  removed, so that `Claude was consulted but did not write code.` and
  `Codex was consulted and suggested the fix, which I implemented.` stay out and
  `Codex was consulted. It wrote the tests.` stays read. `Claude Code was used to review` above
  `reuse/simplification/efficiency issues and apply the fixes.` of openvinotoolkit/openvino
  c13c670d775e and `Used Claude Code for review, planning, implementation, and test writing.` of
  ChainSafe/lodestar 2fba242f5d53 stay read, because the list goes on to a part of the change, and
  so does `Used ChatGPT (Codex) for analysis and implementation guidance on` of tursodatabase/turso
  f51e31cefd2b, whose later lines state that the AI implemented the fixes. Found counts when the
  sentence goes on to wrote, made, implemented, fixed or removed or, in a list after a comma or
  `and`, to another verb of making, after `found a way to` or `found a solution to` and a verb of
  making, as in `AI disclosure: Claude found a way to create a mypy plugin` of pantsbuild/pants
  0a2a96137298, and when the next sentence begins with It and wrote, made, implemented, fixed or
  removed, as in `Claude Code found the root cause from the CI artifact's network journal.` above
  `It wrote the change, the tests and this description` of oxia-db/oxia 859c246f8109. Each of these
  verbs counts only with an object other than the description, a summary or the commit message. The
  refusals of reviewed, checked and verified and of a tool used to review do not apply when a list
  goes on to a verb of making or to the implementation, the development, a refactoring or the
  coding, as in `Claude reviewed PR #35680 (original DynamicQuantizeLSTM` above
  `implementation), identified correctness gaps and duplication,` and
  `implemented fixes and refactoring.` of openvinotoolkit/openvino 963bbc991fa0. A verb of making
  in a clause of the person or after a determiner, or with the description, the commit and PR text
  or comments as its object, does not count there, so that
  `OpenAI Codex was used to review the human-authored change, run checks,` above
  `prepare the commit and PR text, and submit the PR.` of verl-project/verl 45cbfcfa7767, whose
  trailer credits Codex, and the summary of a review
  `Copilot reviewed 24 out of 24 changed files in this pull request and generated 8 comments.` of
  the mention 6fece110ca0e of the collection of the run that started on 2026-11-03 stay out. A
  person in a relative clause does not end the list, as in
  `I used Claude Code to review the fix I applied and to add additional` above `tests,` of
  oxc-project/oxc 7ffb710a76c1. Claude Code counts as Claude in these refusals, in those of found,
  reviewed, checked and verified and in that of a tool used to review, so that
  `AI usage disclosure: Claude Code reviewed the code.` and `Claude Code was used to review this PR.`
  stay out. Without this allowance, the slot of the tool falls back to Claude alone and reads the
  line. The sentence after one that only names the tool counts with `The tool` as its subject, so
  that `AI-assisted (Claude Code). The tool was used to run the link-resolution` above
  `sweep described above and to draft this description.` of dotnet/runtime 97dd992a04cd stays out.
  The maintainer decided on 2026-11-08 that mapping out, mapping, navigating and the navigation of
  the code are activities that make nothing, and that the activities after
  `used <tool> as a <role> to` or `for` are read as after `used <tool> to` or `for`, because neither
  form says that the tool wrote a part of the change.
  `AI disclosure: I used Claude as a coding assistant to help map out` above
  `affected methods. I reviewed every modification, ran the full test suite` of delta-io/delta-rs
  c85075e4277a and `Used Claude (Anthropic) to navigate the codebase and identify the gap` below
  `## AI Usage Disclosure` of rolldown/rolldown d69e0716a709 credit no tool. The rows read
  `was used as a <role> to` and `for` in the same way. The maintainer also decided on 2026-11-08
  that a first sentence that only names the tool or only says that it was used, also for this PR,
  the change or the commit, takes its activities from the following sentences of the paragraph, up
  to the next blank line, that have It, the tool or the name of the tool as their subject, and that
  the line does not count when these sentences give the tool only activities that make nothing,
  because they narrow the general statement of the first sentence, as the next sentence of
  dotnet/runtime 97dd992a04cd does. The rows read each of these sentences as they read the text
  after the tool as the subject of a verb, and a request to the tool, as in `I asked it to` or
  `had it`, as they read the text after `used <tool> to`. They refuse the line when one of these
  sentences gives the tool an activity and these sentences and the requests give the tool only
  activities that make nothing, unless a sentence says that the tool made a part, as in
  `The fix was written by Claude Code.` A sentence that states what It is, as in
  `It is a small change.`, gives the tool no activity. After `I used Claude Code for this PR.`,
  `It found the bug and helped me write the fix.`,
  `It investigated the failure and restored the old behavior.` and
  `It searched the docs and set up the CI workflow.` therefore keep the tool, and so does the
  request `I asked it to` above `> write the regression test.` In these sentences, a line break
  followed by the quote markers of a blockquote counts as a space, because the squash merges of
  uber/NullAway wrap its quoted disclosures at any word. The word It counts as the subject at the
  start of a sentence or a line, after a comma, a semicolon or a colon and after a conjunction or a
  subordinator such as that or which, and not as the object of a verb, as in `made it obvious`. A
  clause after that or so belongs to the activity before it, a code span or a number after and
  belongs to its object, to before an activity joins that activity to the one before, and a
  semicolon, or a comma or a conjunction before It, ends the sentence. Installing, guarding out and
  demonstrating also make nothing in these sentences, because the decision gives the disclosure of
  daa58285bf43 no credit although it says `installed a JDK 25 so the pre-fix commit would build,`,
  `guarded out the` call on master `to confirm the new cases fail without it` and
  `to demonstrate that the new assertions fail without that` fix, none of which is a part of the
  change. In such a first sentence, the tool or its vendor follows used, an opening
  parenthesis, a colon, bold, a bullet or a quote, or begins the line, so that
  `I fixed the parser with Claude Code. It also found a bug.` stays read. The disclosure of
  uber/NullAway daa58285bf43 begins with
  `> I used Claude Code for this PR. I asked it to help me find a good next` and gives Claude Code
  only a search, runs of the repro, a check, an identification, the installation of a JDK and an
  experiment, in sentences such as `It located #1371 via` and `it also guarded out the`, and credits
  no tool. The disclosures of 6bba0e525329 (`and drafted the regression test.`), 98d7a76a12e1
  (`and drafted the fix.`), f3febf6efc15 and de9b4d409f63 (`Claude then implemented the fix`) of
  uber/NullAway keep Claude.
  `codex-full-body-26` and `claude-full-body-31` read the section `## How AI was used in this PR`
  of Automattic/studio's
  [template](https://github.com/Automattic/studio/blob/trunk/.github/PULL_REQUEST_TEMPLATE.md),
  with a first line that begins with Codex, as in `Codex wrote the whole thing based on a detailed
  upfront plan` of [0b89e74871f7](https://github.com/Automattic/studio/commit/0b89e74871f756d2d70a662eae52c496bab5beaf),
  or with Claude and a verb of making. In that commit, `claude-full-body-05` also reads the
  wrapped line `drafted with Claude.`, which names the tool that drafted the plan and not the
  change, and no form of a line separates the two.
- **The agent of Netlify's Agent Runners.** `claude-full-body-38`, `codex-full-body-25`,
  `gemini-full-body-14` and `opencode-full-body-17` read the marker `🤖 **Agent:** <agent>
  (<model>)` that the runners write into the description of their pull request, as in
  `🤖 **Agent:** Opencode (deepseek/deepseek-v4-pro)` of netlify/cli
  [a271357d13f6](https://github.com/netlify/cli/commit/a271357d13f640f3a2c7ac20434e1ab38316d98d).
  The runners run only Claude Code, Codex, Gemini or Antigravity and OpenCode.
- **The sessions of OpenAgents and zauberzeug's colophon.** `codex-full-body-23` and
  `claude-full-body-34` read the session that made a change that OpenAgentsInc/openagents' queue
  applies, `from session codex:<uuid>` and `from session claude-code:<uuid>`, and
  `claude-full-body-34` also reads `Worked by Coder from an OpenAgents chat (claude ...)` and the
  colophon link of zauberzeug/rosys, `zauberzeug.github.io/colophon/#m=claude-...`.
- **The keys of moeru-ai/airi.** `claude-trailer-17`, `codex-trailer-23`, `copilot-trailer-13`,
  `cursor-trailer-15` and `gemini-trailer-06` read `Co-authored-by-agent:` and
  `Authored-by-agent:`, which the maintainers of moeru-ai/airi write by hand when they squash a pull
  request, as in `Co-authored-by-agent: Claude Opus 4.7 <noreply@anthropic.com>` of
  [70d1c317df30](https://github.com/moeru-ai/airi/commit/70d1c317df30ae80ce9eefd0512e7e5e0518e50b).
  The commits of the pull requests carry no such line, so each line is a convention.
- **The identities of neomjs/neo's agents.** The project's code maps each identity to the harness
  that runs it
  ([identityRoots.mjs](https://github.com/neomjs/neo-agent-brain/blob/09f058b61e4d1acdd4884725d064a6e9f83c3d64/ai/graph/identityRoots.mjs)
  and resumeHarness.mjs). `claude-author-13` reads the Opus and Fable identities, which run in
  Claude Code, `gemini-author-06` the Gemini identities, which run in Antigravity, and
  `codex-author-13` the GPT identities, which run in Codex, as the tool's account, and
  `claude-trailer-25`, `gemini-trailer-07` and `codex-trailer-24` read a co-author line at their
  addresses as a convention. `opencode-trailer-12` reads Phoebe, which runs in OpenCode, and
  `kimi-code-trailer-02` Iris, which runs in Kimi Code.
- **epam/cloud-pipeline's commit skill.** `claude-trailer-26`, `cursor-trailer-16` and
  `codex-trailer-25` read `Co-authored-by: Cloud Pipeline Agent (<model>) <noreply@epam.com>`,
  the line that the repository's
  [skill](https://github.com/epam/cloud-pipeline/blob/2f010c4e68cc6799bc67a51e6aefb061522aed7e/.agents/skills/commit-changes/SKILL.md)
  prescribes with the tool that wrote the change in the parentheses.
- **An address that a user chose.** `claude-trailer-29`, `codex-trailer-27` and
  `opencode-trailer-18` read a plus address that names the tool, as in `<person85+claude@example.org>`
  of nobl9/nobl9-go, and `copilot-trailer-16` reads `copilot@users.noreply.github.com`, which
  belongs to no GitHub account and which Cacti/plugin_thold uses, in a co-author or Signed-off-by
  line, as conventions. A plus address with `+copilot` names a person, as in
  `Signed-off-by: person-53 <first184.last+copilot@example.com>` of backstage/backstage c4b7528ee196. `claude-trailer-29` also reads a persona with Claude in parentheses, as in
  `Jarvis (Claude) <jarvis@risingwave-labs.com>`. A local part `claude@` at another domain names a
  person called Claude and is not read, and neither is a numeric noreply address, which is an
  account. A person's plus address as the author still names no tool, as
  `tests.test_commits.TestDetectAiToolsInFields` states for grpc/grpc.

Earlier rows changed beside the new ones. The made-with, the labeled-disclosure
and the Assisted by rows read a line after a `<sub>` tag, as in `<sub>🤖 Generated with Codex</sub>`
of UniClipboard/UniClipboard, and not after `<sup>`, which Cursor Bugbot's footer uses, and no
longer read a line that begins with a quotation mark, which quotes a footer in prose, as in
marin-community/marin d1fef7a20923. `codex-full-body-05`, `codex-full-body-06` and
`claude-full-body-05` read the second of Claude and Codex after a slash, `&` or `and`, as in
`- created with the help of Claude/Codex` of ChainSafe/lodestar. `codex-full-body-02`,
`codex-full-body-07` and the tool slot of Codex in every form above do not read a GPT Codex model
that Cursor, Copilot or OpenCode ran, as in `AI-Assisted: Codex (Cursor)` of
podman-desktop/podman-desktop, which credits Cursor, and `codex-full-body-07` does not read Codex
named only as the reviewer or a disclosure of the description of the pull request, as in
`AI-assisted: Claude Code (Fable 5), reviewed by Codex (gpt-5.6-sol)` of MolSSI/QCFractal. The
rows of rsyslog's footer read its other words, `With AI support:`, `With support from AI agents:`,
`With AI Agent Help:` and `With help from AI coding Agents:`, with the list of names continued on
an indented line, and `gemini-full-body-02` reads `Gemini (CLI)` and Antigravity.
`gemini-unspecified-full-body-04`
reads Gemini alone in the footer, and `jules-full-body-01` Google Jules. The labeled-disclosure
rows of every tool read the ticked box `I used the following tool to generate or modify code:` of
Qiskit/qiskit-ibm-runtime's template, with the tool on the same or the next line.

No row reads the residual phrasings of the review, each of which a single commit writes in its own
words, such as `This work was developed through human-led AI collaboration using` above the list
of the tools in The-PR-Agent/pr-agent 55d34eb7ebc0, the second lines of disclosures that wrap
after the verb, and lines that name a model only, such as `Code written by gpt-5.3-codex-spark`. The
review of 2026-11-06 adds the lines of commonfabric/labs 10c9e7b48a5f, dashpay/dash dc45ca6b6239,
ReproNim/containers 1034b65ca230, Mu-L/vello 8183484db97d, ScorpiusDraconis83/buck2 a8c7c38bc8ea,
rizalgowandy/supabase 5dfd86d35cb9, apache/hadoop 2fd406b5bae0, darktable-org/darktable 69c98c95aab1,
symfony/ux 54110926057a and machinewrapped/llm-subtrans 0c44c19380c6. Grok Build and pi are grounded
tools, and their two sentences stay residual as single commits.
`Code improvements suggested by Cursor` does not state that the product applied the change. A
form loose enough to read it would read prose about features as well, as decided for mentions
mn-0086 ([The confirmed errors of the evaluation of 2026-10-26](#the-confirmed-errors-of-the-evaluation-of-2026-10-26-checked-2026-11-05)).

The classes of `tests.test_commits` from `TestADisclosureLabelOfATemplateNamesTheTool` to
`TestRsyslogsFooterInItsOtherWords` contain the lines of the commits that the evidence of each row
links, shortened, the forms with the tools that no commit of the collection names in them, and
the lines beside them that the rows must not read.

The final review of the pipeline on 2026-11-05 read the unmatched attributions of the collection
of 2026-10-26 and the commit CSVs again for the tools outside the six from Agent Kanban to Mastra
Code, and the rows of this section read the credits that it confirmed. The notes of each row quote
its grounding, read on 2026-11-06.

GitHub apps that their own pages or their vendors' documentation call AI agents author commits
and the squash merges of their pull requests: Agent Kanban, Amazon Q Developer, askmanu, Bosun, Cline's
`cline-cloud[bot]`, Corridor, Fixit, Flashduty, FennoAI, Forkline, Grafana Assistant, Gru, Holon,
Labor0, Lia by LibreChat, LogicStar AI, Lovable, OpenBot and devlo. The rows read each app as the
author, as `account`, and its co-author line as `standard`, as for Tuft. The app of cubic, whose
co-author line is `reviewer`, also commits fixes of its own, which count as `account`, as for
Ellipsis. Lovable's documentation names its app `lovable-dev[bot]` and its earlier login
`gpt-engineer-app[bot]`, which `tool-not-named-trailer-02` read as a GPT model. A commit that
names Lovable is Lovable's, because the label `tool not named` is for a commit that names no tool.
The rows also read identities that a tool's code writes: GnOuGo's default `GnOuGo Agent
<gnougo-agent@localhost>`, Holon's former default `Holon Bot <bot@holon.run>`, Mastra Code's
`noreply@mastra.ai` and `mastra-platform[bot]`, Code Factory's
`333128126+code-factory-bot@users.noreply.github.com`, which its code has the agents append, and
ECA's `eca-agent <git@eca.dev>` with its footer. They read agent accounts that their projects
ground as running a tool, as Lodekeeper is for OpenClaw: Freepilot, which runs goose, devlo's
`devloai`, Sourcery's `bot@sourcery.ai` and the machine accounts of neomjs/neo.
`jules-author-01` and `jules-trailer-01` read the earlier login `labs-code-app[bot]` of Jules's bot
user 161369871 only with that id, because GitHub gave the login to another app.
`junie-author-01` and `junie-trailer-01` read the app Junie EAP.

The co-author lines of the review apps of Almanax, Bito and Sentry's Warden are `reviewer`, as
Greptile's are. Amazon Q Developer and devlo also commit changes of their own, and their co-author
line is `reviewer` only below GitHub's subject of a committed suggestion
(`amazon-q-developer-full-body-01` and `devlo-full-body-01`), as for CodeRabbit.

Lines that name a tool outside the six are conventions where no page of the vendor states them.
The rows read the tool first in the value, as in `Assisted-by: Amp:claude-opus-5.5`,
`Assisted-by: Cline`, `Co-authored-by: CodeBuddy`, `Co-authored-by: codewhale + DeepSeek V4 Pro`,
`Assisted-by: Devin`, `Co-authored-by: ForgeCode`, `Co-Authored-By: Grok Build (grok-4.6)`,
`Assisted-by: Grok CLI:grok-4.7`, `Co-authored-by: Mastra Code (anthropic/claude-opus-4-7)`,
`Co-authored-by: Capy Agent` and `Co-authored-by: Azure SRE Agent`, and DeepSeek Harness under its
command, as in `Assisted-by: dsh:llama.cpp/...`. They read the tool later in the value, as in
`Assisted-by: herwin (Hermes Agent, OpenAI gpt-6.1-sol)`, `Assisted-by: Kimi K3 via Kilo` and
`Assisted-by: Bob (IBM AI Assistant)`, and a tool's address under any name, as for Atlarix,
MonkeyCode, JetBrains Air's `junie@air.dev` and the addresses of Hermes Agent at
nousresearch.com. Azure SRE Agent also authors and commits at Microsoft's noreply address.
Gemini named without a product takes the label Gemini (unspecified) as the name of an author, as
in `Gemini 2.0 Flash Thinking <noreply@google.com>` and `Gemini <gemini@google.com>`, and with
Google before a colon, as in `Assisted-by: google:gemini-1.5-pro`, while
`tool-not-named-trailer-08` keeps the provider and the model joined by a slash, the form of
OpenCode's and pi's configuration. The addresses of Cline's employees at cline.bot, Cline's test
identities `cline-test` and `cline-test-bot` and names that only begin with Cline, such as
`Cline SR` and `Cline Evaluation`, stay unread.

neomjs/neo runs its AI maintainers under machine accounts, and the project's list of agent
identities (`ai/graph/identityRoots.mjs` of neomjs/neo-agent-brain) and its README name the
harness of each: Antigravity for the Gemini identities and Codex for the GPT identities.
`gemini-author-06` and `codex-author-13` read these accounts as `account`, and
`gemini-trailer-07` and `codex-trailer-24` their co-author lines as `standard`. The list records
only the harness of the first session of the Kimi identities, Kimi Code CLI for Iris and OpenCode
for Phoebe, and `kimi-code-trailer-02` and `opencode-trailer-12` read their addresses as
conventions. A line such as `Co-Authored-By: GPT 5.5 <neo-gpt@neomjs.com>` therefore credits Codex
and not the label `tool not named`. `gemini-author-08` reads `Gemini Bot <gemini-bot@github.com>`,
the identity under which a workflow of MervinPraison/PraisonAI runs Gemini CLI.

No row reads `Co-authored-by: graphite-app` at the noreply address of the bot user of Graphite's
app, whose only commits in the data are oxc-project/oxc's updates of its submodules by a workflow,
nor FennoAI's co-author line, which the data does not contain.
`tests.test_commits.TestTheAppsThatTheReviewOf1105Read`,
`tests.test_commits.TestTheCommittedSuggestionsThatTheReviewOf1105Read`,
`tests.test_commits.TestTheLinesThatNameAToolThatTheReviewOf1105Read` and
`tests.test_commits.TestTheAgentsOfNeoMjsCreditTheirHarness` contain the commits and the lines
beside them that no row reads.

The final review of the commit attributions on 2026-11-05 read the unmatched attributions of the
collection of 2026-10-26, its commit CSVs and the mentions of its evaluation again. The rows below
read what it confirmed, and the notes of each row quote the page, the code or the repository that
grounds it.

Agents that open pull requests through a GitHub app of their own count as `account` for the app as
the author and as `standard` for its co-author line in the squash merge, as Detail and Tuft do:
Mendral, Nedi of Netdata, Nero, the apps of nvt-agent, Polylane, Replicas, SonarQube Agent,
StarSling, Stilla, Tembo, Tusk and Vorflux, and Warp's apps through which Oz commits,
`warp-agent-staging[bot]`, later `warp-dev-github-integration[bot]`, `warp-factories[bot]` and
`oz-for-oss[bot]`, later `warp-for-oss[bot]`. `warp-full-body-01` reads Oz's footer
`_This PR was generated with [Oz](https://warp.dev/oz)._`, which warpdotdev/warp 28116630b028
carries without a co-author line. Nedi's app page returns 404, and Netdata's page on Nedi and the
app's pull requests ground the row. Replicas, Tembo, nvt-agent and Vibe Kanban run other agents and
do not name them, as Terragon does, and a commit that also names one of the six is in both CSVs.

The identities that a tool's code writes are `standard`: SWE-agent's `noemail@swe-agent.com`,
Vibe Kanban's `noreply@vibekanban.com`, OpenDevin's `opendevin@all-hands.dev`, which the rows of
OpenHands read beside `openhands@all-hands.dev`, and the line `Assisted-by: code-to-docs AI` of the
GitHub Action Code-to-Docs. Mergestorm's Cyclone commits its fixes to the branch of the pull
request, and its co-author line is `standard`. The co-author lines of Propel, What The Diff and
QodoAI-Agent, earlier CodiumAI-Agent, are suggestions of reviewers that a person committed and
count as `reviewer`, as Greptile's do. `sweep-ai-local[bot]`, a self-hosted Sweep, counts for
Sweep.

The names of a tool in a trailer are conventions: MechaCoder, OpenAgents' agent, which the
repository's AGENTS.md prescribes in its commit signature, Mistral Vibe first at an address other
than its own, nib in the `Assisted-by` form that LocalAI prescribes, Reasonix first or after
`via`, no-mistakes, WorkBuddy, ZCode, Volobox, watsonx Code Assistant, the login
`pax-openclaw[bot]` that ends in OpenClaw's name, OpenClaw's earlier names Clawdbot and Moltbot,
and oh-my-pi's command `omp` before a model and in the parenthesis of an AI disclosure. A hyphen
between Google and Gemini counts for Gemini (unspecified). The Claude rows skip a line that names
oh-my-pi or `omp` after `via` or in parentheses, as they skip the other agents outside the six
([The Claude trailers, narrowed 2026-09-25](#the-claude-trailers-narrowed-2026-09-25)), so that
`**AI disclosure:** implemented with an AI agent (omp, Claude).` of supabase/mcp 4a619c13f6dd
credits oh-my-pi, while its co-author line still credits Claude, and
`Assisted-by: Claude Opus 4 via omp` of TecharoHQ/anubis 3a6f57333d28 credits oh-my-pi and no longer
Claude, because `oh-my-pi-trailer-01` reads oh-my-pi and `omp` in the forms that the Claude rows
skip. `opencode-trailer-04` also reads
OpenCode in brackets or last after a colon in the form `AGENT:MODEL`, and
`Assisted-by: OpenAI:gpt-5.5:OpenCode` of icsharpcode/ILSpy 193e493ccc08 therefore credits OpenCode
and no longer the label `tool not named`. `claude-trailer-03` reads the misspelling Cloude before
a Claude model family, as in `Generated-by: Cloude Sonnet 4.6` of apache/impala cf03d93762e4.

The rows of the label `tool not named` read further credits that name a model, a vendor or AI and
no tool ([A trailer that names no tool](#a-trailer-that-names-no-tool-2026-09-26)): more forms of
the models of the existing rows, such as `Assisted-by: Grok 4.5 (low)` and
`Co-authored-by: GPT-6 Sol (OpenAI) <agent@openai.com>`, whose address no longer counts as naming an
agent, the models of MiniMax, Qwen and Doubao, a list of models, the placeholder `AGENT` of
LocalAI's form, a persona with a model, as in `Co-Authored-By: Lykos (Opus 5.5) <noreply@lykos.ai>`,
a name that calls itself an AI agent, bot or assistant, the agents that work on behalf of a person
in commonfabric/labs, Perplexity, Microsoft Copilot before a model, MariaDB's AI policy, a vendor
before `unknown` or `undisclosed`, and, in a body, a GPT-x Codex model, a vendor-first model and a
Claude model as the value of `Model used`. A line that names a tool keeps that tool, and the rows
no longer read the login `gpt-engineer-app[bot]` of Lovable as a GPT model.

No row reads these lines: agents that the data does not identify, such as Teammate Indigo and
docs-surfaces-luna, Jetski, and agent accounts that name no tool, such as `Eos
<neo-preview@neomjs.com>` of neomjs/neo, whose engine the project does not disclose, Groene AI and
`totally-not-ai[bot]`.

The maintainer decided the following lines on 2026-11-06, and the rows named here read them.

`Assisted-by: OpenAI:Codex` of PX4/PX4-Autopilot 570af388f6ff names the product, and
`codex-trailer-01` credits Codex. With a version, as in `Assisted-by: OpenAI:codex-5.5` of
zephyrproject-rtos/zephyr-testing a14e3675b92e, the line names a model, and
`tool-not-named-trailer-02` reads it. `codex-trailer-01` does not read a line that credits a review
or an investigation only, as in `Assisted-by: Codex via GPT-5.5 as first reviewer and enhanced
global search` of munich-quantum-toolkit/qecc 1685b8741e79.

An adjective built from a tool's name credits the tool when it stands before what the change adds.
`claude-full-body-43`, `codex-full-body-32`, `copilot-full-body-30` and `cursor-full-body-24` read
`<tool>-generated`, `<tool> generated`, `<tool>-written` and `<tool>-authored` in parentheses at the
end of a line or first in a line, as in `(claude generated) - moving logic into a composable` of
learningequality/kolibri 90e9fee62ffb, and after `add`, `added`, `adds`, `adding`, `with`, `includes`,
`are` or `is` before a noun of the change, as in `* add claude-generated tests` of TanStack/query
a0b96db9375d. `codex-full-body-32` also reads `🤖 **Codex-generated PR**` of
softwareone-platform/swo-marketplace-cli 45593eedc613. The rows do not read a heading, such as
`## Summary (Claude-generated)`, a summary, a description, a commit message, a file, a worktree, a
comment or a pull request as the noun, or the adjective without one of these words before it,
because it then also describes what a change fixes or removes, as in `Fix Windows compiler error in
Claude-generated new unit test.` No line of Gemini in the data has one of these forms, so Gemini has
no such row: `Initially Gemini-authored, extensively revised.` of golang/go cff61723f360 and
`* chore: Additional unit tests. Gemini generated, not yet robust but gives some basic coverage` of
google/clasp b30e69460e78 stay unread.

The full-body text reads a ticked box of a task list as its bullet, and the rows that read the start
of a line see the text after the box, as in `- [x] AI-assisted: Claude Code helped diagnose the
install failure` of ChainSafe/lodestar 59c06807b63c. GitHub Flavored Markdown defines the marker as
"a left bracket (`[`), either a whitespace character or the letter `x` in either lowercase or
uppercase, and then a right bracket (`]`)", and states: "If the character between the brackets is a
whitespace character, the checkbox is unchecked. Otherwise, the checkbox is checked."
([task list items](https://github.github.com/gfm/#task-list-items-extension-), read 2026-11-06). A
box that names tools as options credits none of them. The box `- [x] Check if this PR was entirely
AI-generated (by Copilot, Claude Code, Cursor, etc.)` of the template of agno-agi/agno, as in
0d3200f16641, credits AI and names no tool, and `tool-not-named-full-body-03` reads it under the label
`tool not named`.

A persona name before Claude's model in parentheses at Claude Code's address credits Claude, as a
convention, through `claude-trailer-15`: Kofi Bingo of woogles-io/liwords, coder-cedar and coder-colt
of commonfabric/labs, and Mira Volkov of gmtsar/gmtsar. For the same reason, `cursor-trailer-17`
credits Cursor, as a convention, for `Assisted-by: Composer <cursor@cursor.com>` of
kata-containers/kata-containers 7a77673885d2, which names Cursor's model at Cursor's address. The
row reads the same line under the keys `Co-authored-by` and `Authored-by` too
([The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)).
`opencode-trailer-19` reads oh-my-opencode named in a credit, as in `Assisted-by: Sisyphus
(OhMyOpenCode) / GLM (zai-coding-plan/glm-5.3)` of AcademySoftwareFoundation/OpenImageIO 7e2582b5a091
and `Co-authored-by: Sisyphus <sisyphus@oh-my-opencode.dev>` of dragonflyoss/dragonfly ba405327278e.

`claude-author-01` reads `marin-ops-agent[bot]`, the renamed `claude-nightshift[bot]` of
marin-community/marin, `claude-trailer-16` its co-author line, and `claude-trailer-30` the git
identities of its nightshift sessions, as in `Co-authored-by: Nightshift Scout
<nightshift@marin.local>` of 16de369ff7a2. The decision covers the commits before 71968a4ad9b3 of
2026-09-25 18:38 moved the workflows to Loom. A row cannot read a date, and GitHub's commit search
returns no commit of the app and no such line after that commit, read 2026-11-06. The same app makes
the squash merges of `config/update-external.py`, which `.github/workflows/ops-external-dependencies.yaml`
runs with the app's token. These commits are known wrong credits of Claude, because an author row
does not read the subject, and `heuristics/not_ai_commits.csv` only leaves a line out of `unmatched
commits`: 2ebc65b091ea, 70298ed57f7d, e0c43a4e60dd, f5a931e717f5, 9ac45e4de02f, d1d081a33cc2,
31d98f6d7c23, 207b4c9e9704, 309c8f25fa96, 6ee1dc202c0a and 9a109388d188.

`claude-author-11` reads names that users chose for Claude, as in `Claude Agent <agent@bitgo.com>` of
BitGo/BitGoJS 9d90a6dd260e, `Ida Sixpack (Claude)` of cri-o/cri-o 727d18bfee01, `Claude (vfs-s3 bot)`
and `Claude Opus 4.7 <claude-opus-4.7@ai.assistant>`, as `cursor-author-03` reads Cursor Agent.
`claude-author-07` reads `claude.code` and `Claude (Code)`, and `claude-trailer-02` reads
`Co-authored-by: Claude Agent <agent@local>` of W3DevTech/rustfs 1747ed029236. A person named Claude
stays unread, such as Claude Stiles, Claude Mustermann and Jean-Claude Stiles, and so does `agent
<agent@local>`, which names no tool.

`copilot-full-body-13` reads `Checkpoint from Copilot CLI for coding agent session`, the subject that
the code of Copilot CLI writes when it delegates a session to the coding agent, as `standard`. GitHub's
page states that Copilot offers to "commit any of your unstaged changes as a checkpoint in a new
branch it creates."
([delegating tasks](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli-agents/delegate-tasks-to-cca),
read 2026-11-06), and `index.js` of `@github/copilot` 0.0.423 returns that subject.
`claude-full-body-42` reads the marker of gh-aw with the engine `claude`, as in
pulumi/pulumi-aws-apigateway 2078d3ab72ef, as a convention, as `codex-full-body-17` reads the engine
`codex`.

No row reads the following lines, by decision. A thank-you credits no tool, because it states whom
the writer thanks and not what the tool made, as in `A visual overhaul for chat courtesy of Claude
Code.` of jkomoros/card-web 3afcb5e9cd00, and neither `thanks to` nor `courtesy of` has a row. A
listed subject that ends in Cursor's `Made-with: Cursor`, as in the hyphen list of
raycast/extensions be792fa72ac5, stays outside the trailer scope, as
[The labeled attributions decided on 2026-11-02](#the-labeled-attributions-decided-on-2026-11-02)
decided, because no commit of the commit CSV has that trailer in its first paragraph. The trailer
scope reads the subject whole and every token of a line that has the form of a trailer. git's `%s`
joins a trailer in the first paragraph onto the subject, as in Kyoril/mmo
[b7d26a217b07](https://github.com/Kyoril/mmo/commit/b7d26a217b071ee13014242312e8535fd64a82be),
which credits Claude through its subject. GitHub's squash list keeps the line breaks of each listed
message, so a co-author line in the first paragraph stays on a line of its own below the `* ` item,
as artsy/metaphysics
[aad63ec4a6d1](https://github.com/artsy/metaphysics/commit/aad63ec4a6d1ab60e6a267f42f714b59e73bc200)
lists 69904373f55f, and the trailer scope reads it there. A `* ` item that carries a trailer on the
line of its subject repeats a listed commit with a one-line message that holds the trailer, and the
trailer scope reads the subject of that listed commit whole. A `* ` item of trailer form, as
`* fix: add Y Co-authored-by: Claude <noreply@anthropic.com>`, is therefore read whole, and a
top-level `* ` item that ends in a co-author line with an address has that line read
([Trailer extraction](#trailer-extraction)). allenai/open-instruct
[97590011918f](https://github.com/allenai/open-instruct/commit/97590011918f562621a994eeebdc4a83a1cbb9d4)
lists `* Restore 🤡 to resample warnings; use self.training_step in DataPreparationActor.run.
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`, the one-line message of the listed commit
d5fc8655cf38, and GitHub does not repeat that co-author line below the separator. The item then
credits what the listed commit's own subject credits. A co-author line without an address at the
end of an item, as in mercurjs/mercur 4d5f342b27be, another token at the end of an item, a hyphen
item and an indented item stay unread, so that an item that names a token in a sentence is not
read.
`claudespice <claude@spice.ai>` stays unread (resolution mn-0166), because it is Spice AI's own
identity and nothing shows that Claude Code ran. The commits and the merges of Claude Code's
`/install-github-app`, with the branch `add-claude-github-actions-<digits>` and the subjects
`"Claude PR Assistant workflow"` and `"Claude Code Review workflow"`, have no row, because these
commits add the workflow files and record no change of Claude's, and the same holds for Gemini CLI's
`/setup-github`. `copilot-full-body-05` and `copilot-full-body-07` keep reading the footer
`- Generated by Copilot` that microsoft/hve-core's commit message instructions prescribe.

`tests.test_commits.TestTheToolsOutsideTheSixThatTheFinalReviewFound` contains the commits of the
new rows and the people and apps that they leave out,
`tests.test_commits.TestTheCreditsThatNameNoToolThatTheFinalReviewFound` the lines of the label
`tool not named` and the lines that it leaves to a tool or to a decision, and
`tests.test_commits.TestTheLinesOfTheFinalReviewThatTheSixToolsTake` the commits of supabase/mcp,
mudler/LocalAI, icsharpcode/ILSpy and apache/impala, `tests.test_commits.TestTheDecisionsOnTheFinalReview`
the lines of the maintainer's decisions and those beside them that stay unread, and
`tests.test_commits.TestTheTickedBoxOfATaskList` the ticked and the unticked box.

#### The credits of the review of 2026-11-06

The review of the commit attributions on 2026-11-06 read the unmatched attributions, the commit CSVs
and the mentions of the collection of 2026-10-26 again. The rows below read what it confirmed, and
the notes of each row quote the page, the code or the repository that grounds it.

A disclosure names whatever tool was used, so each form below is a row of each of the six tools, as
in [The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05).
The Claude row of each form skips a line in which another tool ran a Claude model. The rows of the
other tools read the Claude form of the line with that tool after `via` or in parentheses, except in
the forms that read the plain name of a tool: What a change adds, The sentences that state the
tool's work and The bare form at the end of a line. There the Claude row of such a line reads
nothing, and no wrong credit follows.

- **The phrase of apache/hadoop's template.** `claude-full-body-44`, `codex-full-body-34`,
  `copilot-full-body-31`, `cursor-full-body-25`, `gemini-full-body-22` and `opencode-full-body-24`
  read `Contains content generated by OpenAI Codex.` of apache/hadoop 7d8dc187523c at the start of a
  line, with `code` for `content` and `contributed` or `written` for `generated`. The
  [template](https://github.com/apache/hadoop/blob/3bf3610bebcc3d6ade818c8891829ddaa2f8a961/.github/pull_request_template.md)
  states
  `The PR includes the phrase "Contains content generated by <tool>" where <tool> is the name of the AI tool used.`
  (read 2026-11-06). `NOTE: libnetwork contains code generated by Claude Code`
  and `Contains content generated by Claude.ai` stay unread.
- **The tool's name in brackets at the end of a line.** `claude-full-body-45`, `codex-full-body-35`,
  `copilot-full-body-32`, `cursor-full-body-26`, `gemini-full-body-23` and `opencode-full-body-25`
  read `fix(databricks): support IFF as a synonym for IF [CLAUDE] (#7488)` of tobymao/sqlglot
  ecd325b4e3e0 and `only send slack message if token is set [copilot]` of bbc/bug 18de62d9e56e, and
  `gemini-unspecified-full-body-08` reads the tag that names Gemini without a product. The AGENTS.md
  of tobymao/sqlglot states "If you are an agent, you must add your model [CLAUDE, CODEX, etc...] to
  the end of PR titles and commit messages." (read 2026-11-06), and that of bbc/bug prescribed
  `If Copilot was used to generate code changes, append [copilot] at the end.` until c7c6c1bb292d
  removed the section. The file says model, and the tag names the tool's name, so commits of sqlglot
  credit Claude beside another tool's credit, such as 2216a6ed43ce with `Co-authored-by: OpenCode
  google-vertex/claude-opus-4-8@default <noreply@opencode.ai>` and d289db33be43 with
  `Made-with: Cursor`. These are documented wrong credits. `Branch name: prefix/[cursor]`, a `- ` list
  item, `Remove leading [cursor]` and `Assisted-by: Claude Code:claude-sonnet-4-6 [Copilot]` of
  apache/tinkerpop stay unread.
- **The tool as the subject of `was used`.** `claude-full-body-46`, `codex-full-body-36`,
  `copilot-full-body-33`, `cursor-full-body-27`, `gemini-full-body-24` and `opencode-full-body-26`
  read a line that begins with the tool, as in `Claude Opus 4.6 was used to assist in the` of
  aanm-org/cilium be7a8ec0fa62, and `assistance (Claude) was used` of ray-project/ray 4d12b23a5701,
  because such a line states the use: a contribution of a product is what "wrote the change itself"
  ([AI contribution detection](heuristics.md#ai-contribution-detection)). The verb of making has to
  stand in the same sentence, except in `claude-full-body-46`, because the line of
  chaos-polymtl/lethe 8c18d75e93be credits the LLM in a later sentence. `Claude was used to find
  this bug`, `Claude was used to review the code.`, `Claude was used to write the PR description.`,
  `AI assistance (Claude) was used to investigate the sglang` and `Codex was used to explain the
  relevant code and review the test. I personally wrote and verified the implementation.` of
  fedify-dev/fedify 1af83de38d01 stay unread. A verb of making in a clause of I or we after a
  comma, a semicolon, `and` or `but` is the person's and does not count, so that
  `Codex was used for brainstorming; I wrote the code.` stays unread, as below a disclosure heading.
- **I used the tool, with a model, a first clause or for.** `claude-full-body-47`,
  `codex-full-body-37`, `copilot-full-body-34`, `cursor-full-body-28`, `gemini-full-body-25` and
  `opencode-full-body-27` read `I used Claude Opus 4.5 to help fix the errors, then reviewed each
  fix. I` of jupyterlab/jupyterlab f8803c829c4b, the same statement as the rows of I used the tool.
  `I used Claude to find the bug, but the fix is my own.`, `I used Codex to review the result.`,
  `Used Claude for code review, and translations.` of mod-playerbots and `I used Claude to help
  generate a high level overview description of the` of oxc-project/oxc 585864103e32 stay unread.
- **An AI agent with the tool in parentheses.** `claude-full-body-48`, `codex-full-body-38`,
  `copilot-full-body-35`, `cursor-full-body-29`, `gemini-full-body-26` and `opencode-full-body-28`
  read the sentence of the rows of the disclosures with the tool in parentheses, as in `> AI-agent
  disclosure: this PR was authored by an automated agent (Codex` of dubzzz/fast-check 24b5456aace1,
  whose template prescribes "> AI-agent disclosure: this PR was authored by an automated > agent
  (<name/version of the agent>)". `This summary was generated by an AI assistant (Claude).` and
  `driven by an AI coding agent (claude-code, codex, cursor, gemini,` stay unread.
- **What a change adds, with the noun first.** `claude-full-body-49`, `codex-full-body-39`,
  `copilot-full-body-36`, `cursor-full-body-30`, `gemini-full-body-27` and `opencode-full-body-29`
  read `Changes were generated with assistance from Claude Code. The` of JChartier001/openemr
  9e12d789a27b and `Mostly Claude generated README.md` of commonfabric/labs 6db71eff00a3. These lines
  state with the noun first what the maintainer's decision on an adjective before what a change adds
  reads. `Improve test written by copilot`, `Clean up code generated by Claude` and `Refactor code written by Copilot` stay unread.
- **The sentences that state the tool's work.** `claude-full-body-50`, `codex-full-body-40`,
  `copilot-full-body-37`, `cursor-full-body-31`, `gemini-full-body-28` and `opencode-full-body-30`
  read sentences such as `This was done using Claude Code.`, `I worked on this with Claude Code.` and
  `This implementation was primarily authored by Codex.`, and the labels of a disclosure. Each row
  also reads the sentences that a project writes for its tool, with the commits that its evidence
  links. The alternative of `I fixed it with <tool>` refuses a further word for Cursor, as in
  `I fixed this with cursor position`. `<p><em>This was written by Codex on behalf of`, `this was
  built with Cursor in mind` and `Prepared with RepoMerger, which was co-authored by Copilot.` stay
  unread. The form does not reach `exp: creating new Avatar e2e tests with Copilot (#1571)` of
  mentions mn-0086. `Validation and this summary were prepared with Codex assistance.` of
  Project-MONAI/MONAI 57e825b25b0e stays unread.
- **The tool as the subject of another verb.** `claude-full-body-51`, `codex-full-body-41`,
  `copilot-full-body-38`, `cursor-full-body-32`, `gemini-full-body-29` and `opencode-full-body-31`
  read `Claude Code found this and reproduced it on main, then wrote the fix` of tursodatabase/turso
  171713847c0c and `Claude Code assisted.` of vortex-data/vortex 56c550cff25c. After a first clause, a
  row refuses a description, a summary or a commit message only up to the first comma or `and` after
  the verb, because a list can go on from the change to the description, as in `Claude Code (Claude
  Opus) did the root-cause analysis and wrote the fix, the tests and this description, under my
  direction.` of apache/arrow 43d04c2f6fad and `OpenAI Codex researched the issue and related PRs, wrote
  the reproducer, regression test, fix, and this description,` of ccfcdd24fcba. A sentence after a
  period is no credit token, so the decision of 2026-11-02 stays untouched. `Codex helped find a bug`,
  `Fix errors spotted by Gemini`, `Some text. Claude wrote the PR description.`, `Claude analyzed the
  diff and wrote the pull request description.`, `Cursor did not AddCell with the cursor cell after
  new effect system implemented.` of Thraka/SadConsole f91c8c6c521c, which reports a bug in the
  library's own cursor, and `Claude wrote the original bad code, but it's my fault for letting it
  through.` of votingworks/vxsuite aadc759f91fe, which names code that the commit removes, stay unread.
  `claude-full-body-51` refuses two objects of `fixed`, each for one commit, because no general form
  of the line separates them from `Claude fixed two bugs`: `- Claude fixed header redefinition errors by
  adjusting include paths` of tenstorrent/tt-xla 805103c92e59 reports what a script's agent did in a
  trial, and `- Claude fixed a lint error during the check step (before asking` of
  opendatahub-io/odh-dashboard e26ae1b64662 reports a defect of the skill that the commit changes.
  The tool followed by `helped` counts when a verb of making follows, also after `me`, `us` or
  `to`, or when a list of activities in the same sentence or its wrapped continuation goes on after
  a comma or `and` to a verb of making, as below a disclosure heading. tursodatabase/turso
  d5536c9e2437 adds a tool and states `Codex helped me create the tool`, WordPress/php-toolkit
  3623d0192a22 adds a fix and states `Codex helped me diagnose and fix this issue.`, and
  tursodatabase/turso c0c8388d9390 states `Codex helped investigate the issue, trace the relevant
  Rust and` above `JavaScript paths, write the implementation and regression tests`. A list whose
  activities make nothing, as in `Codex helped me run the benchmarks and compare the results`, a
  verb of making after `I` or `we`, and a verb of making with the description as its object stay
  unread. The rows also read `helped with` the boilerplate or with comments and `helped, so the
  comments are`, because each names a part of the change that the tool made. tursodatabase/turso
  d21f250493c8 adds the code of an allocator API and states `Codex helped me with the boilerplate`,
  cockroachdb/pebble 34b72958050e adds iterators and their comments and states `Claude helped` above
  `with comments and with limited bits and pieces`, and DataDog/datadog-agent 2f7d898d0840 adds a
  Bazel file with blocks of comments and states `Claude helped, so the comments are verbose, but
  they will help it stay on track in the next phases.` Comments on a pull request, a review or an
  issue are not part of the change and stay unread, and so does a result other than the comments
  after `helped, so`, as in `Claude helped, so the tests are passing now.`, which names no part
  that the tool made. The forms of `helped` other than `helped, so the comments are` also count in
  the sentence after a period or a semicolon, as `wrote` and `implemented` do, because the line of
  cockroachdb/pebble begins with `Most of the code inside the iterators was written by me.`
- **The bare form at the end of a line.** `claude-full-body-52`, `codex-full-body-42`,
  `copilot-full-body-39`, `cursor-full-body-33`, `gemini-full-body-30` and `opencode-full-body-32`
  read `[Translations] updated ar.po (by Claude AI)` of BlackHole/enigma2 bb2ae42417a2 and `(by
  copilot)` of Azure/azure-sdk-for-js 5c685b199ec4. `claude-full-body-28` and `copilot-full-body-22`
  read `(written by <tool>)` at the end of a line, and the bare form says the same. The credit ends a
  line, so the decision on a token in the middle of a line stays untouched. `(found by Claude)`,
  `(flagged by claude)` and `Thanks to Claude` stay unread.
- **A mistyped or spaced key.** `claude-full-body-53`, `codex-full-body-43`, `copilot-full-body-40`,
  `cursor-full-body-34`, `gemini-full-body-31` and `opencode-full-body-33` read
  `Asssited-By: Claude:claude-opus-5.5` of zephyrproject-rtos/zephyr 574cb3d2daa7, `Partially
  generated-by: Claude Sonnet 4.6` of apache/impala b739bf17cacc and `Assistant-By Claude Opus 5` of
  apache/kvrocks 16a59bba61df. Each is a row of the scope `full_body`, because the forms with a space
  before the colon are no trailers in `_extract_trailers`.
- **The tool as the object of a request.** `claude-full-body-54`, `codex-full-body-44`,
  `copilot-full-body-41`, `cursor-full-body-35`, `gemini-full-body-32` and `opencode-full-body-34`
  read `I asked Claude Code to fix it` of cloudflare/workerd 870499694c3a and
  `Asked Cursor to add docs. Adjusted a bit.` of expo/expo 0948ef690090, the statement that
  `claude-full-body-15` reads with had, have and let. `Mea culpa: I asked Claude to write the first iteration of these tests,` of
  medplum/medplum ad4e435058f6, a person's correction of tests from before the change, stays unread.
  `claude-full-body-54` also refuses `I asked Claude to create some ascii art` of neo4j/neo4j
  50c52b65d6b0, with art that the change does not contain,
  `I was facing this issue when I asked Claude to fix issues in a batch` of apify/apify-mcp-server
  c3db1e9af8f5, which states how the problem arose,
  and `Asked claude to:` of tursodatabase/turso a78ff5bb34e7, in a paragraph that goes on to `I tradcoded
  this`. near/nearcore fd40b820e8f5 and oxc-project/oxc 55d99db303f0 are documented wrong credits, which
  no form of a line can separate.
- **The line of a maintainer of jupyterlab/jupyterlab.** `claude-full-body-56` reads
  `> Claude Code (Claude Opus 5.5, max effort) for the maintainer changes.` of 7a15ce07cf8d and
  61fbc4605613 and `Claude Code (Opus 5.5, max effort) for the maintainer changes` of a98efbf8f55e,
  lines that name the product at the start and state what it was used for. A line with only
  `Opus 5.5 (max effort)` names a model and stays unread.
- **An OpenCode session below a disclosure.** `opencode-full-body-36` reads the label `Opencode session:`
  above a link to `https://opncd.ai/share/`, as in tursodatabase/turso 871357281bde, whose section names
  Claude three lines above. `claude-full-body-35` skips a line with the label on one of the next three
  lines, because OpenCode ran Claude's model there, so that a label without a link credits no tool.
  `infra/stage.ts` of anomalyco/opencode names `opncd.ai` as the domain of its production stage.
- **The closing line of stickerdaniel/linkedin-mcp-server.** `claude-full-body-55`,
  `codex-full-body-45`, `copilot-full-body-42`, `cursor-full-body-36`, `gemini-full-body-33` and
  `opencode-full-body-35` read `Generated with GPT-6 Sol for implementation in Claude Code via T3
  Code.` of db55010ba365, with the job at the start of the line. The AGENTS.md of the repository
  states "End PR bodies with `<Job> with <model> (<effort>) in <tool> via <host>.`" (read 2026-11-06),
  and the host after `via` is not the tool. `grok-build-full-body-02` reads the same line with Grok
  CLI as Grok Build.

Earlier rows read more of the same forms. `claude-full-body-31` reads a first line of the section of
Automattic/studio with Claude and a verb of making later in the line, or with the verb first, as in
`Implementation drafted with Claude Code` of 9681d0ed5a86. The rows of a disclosure heading read
`## Description of AI Usage` of tursodatabase/turso, except the Cursor row, because it would read
`implement the cursor` of 83a7e759aefa, the question
`Does your code include anything generated by an AI Engine?` of openemr/openemr,
`Please list the AI tool(s) used` of pgmpy/pgmpy and `Was AI used for this PR?` of apache/arrow. The rows of the AI-assisted label read a middle dot before `Tool:`, as in
`*AI-assisted · Tool: Claude Code; model: anthropic/claude-opus-5-5[1m];` of jdx/mise 6cf33218b101,
and `code`, `changes produced` and `implementation and tests` or `review`. `claude-trailer-17` and
`copilot-trailer-13` read the key `Assisted-by-AI`, as in `Assisted-by-AI: Claude Code` of
containers/ramalama b51bc34f73dc. `copilot-trailer-04` reads the Copilot SDK in parentheses, because
its [README](https://github.com/github/copilot-sdk) states "The GitHub Copilot SDK exposes the same
engine behind Copilot CLI". `opencode-trailer-20` reads the addresses of Sisyphus Labs and of
oh-my-opencode under the keys of moeru-ai/airi, `opencode-trailer-18` an address with the model before
the tool, and `gemini-trailer-09` an address with Antigravity's command, as in
`<first73.last+agent-gemini-3.8-flash-agy@example.com>` of canonical/charmcraft 512458868b2a.
`claude-author-14` reads `claude <claude@claude.local>` of Xtraball/Siberian 82cad8d36149, because the
domain names the tool too, and `claude-author-15` the agent names of lgazo/drawio-mcp-server.
`claude-author-01` and `claude-trailer-16` read `opsmill-bug-pipeline[bot]`, the app of the GitHub
Agentic Workflows with the engine `claude` of opsmill/infrahub. `claude-trailer-27` reads the
misspelling `Claue`.

`pi-full-body-02` reads `Claude Opus 5.5, driven by Homeboy's agent-task runner (Pi), wrote the` of
Automattic/studio e37b1760bdc6, and the guards of `claude-full-body-31` read `(Pi)`, so that the commit
credits pi. `(Claude, via BrAInClaw)` of tenstorrent/tt-metal d69fab9610d7 stays a credit of Claude,
because BrAInClaw runs Claude Code through NanoClaw, as `Claude (via Conductor)` does.

The rows below read tools outside the six, under the decision of 2026-11-06 to add each tool that its
own repository, code or page grounds.

- **Rupic.** `rupic-author-01` and `rupic-trailer-01` read `rupic-app[bot]` and `<agent@rupic.dev>`.
  [Its page](https://rupic.dev) states "Hand over an issue and Rupic opens a draft pull request."
- **Kilo Code.** `kilo-code-author-01` reads the author `kiloconnect[bot]` and `kilo-code-bot[bot]`, and
  `agent@kilocode.ai`, the identity that `session-bootstrap.ts` of Kilo-Org/cloud gives its cloud agent.
  The record of the app states "Tag it on an issue to diagnose the cause or ship a fix as a PR".
  `kilo-code-author-02` reads the names Kilo Agent and Kilo Code at another address, as in `Kilo Agent
  <agent@localhost>` of Kilo-Org/kilocode 2bba083a0810, as a convention.
- **Coder Agents.** `coder-agents-trailer-01` and `coder-agents-full-body-01` read the co-author lines
  and the disclosures of coder/coder, as in `> Generated by Coder Agents (session:
  plat-321-update-release-docs)` of ace1a5a91005. [The page](https://coder.com/docs/ai-coder/agents)
  states "It is not a wrapper around third-party agent tools like Claude Code or Codex." and "There is
  no shared bot identity". `coder-agents-full-body-02` reads the further forms of these disclosures,
  such as `> This PR was created with AI assistance (Coder Agents).` of 5b97d99a4867 and
  `Opened via Coder Agents on behalf of @person.` of 4d4d2575e4b2, with a capitalized Coder. The singular
  counts only at `agent@coder.com` or with a capitalized Coder, and then only after a, after the at the
  end of a line, as a link, before `generated` at the start of a line, or after the verb at the end of
  a line or before `on behalf`, a comma, a punctuation mark or an emoji, as in
  `Co-authored by Coder Agent 🤖` of 9ba822628a96 and
  `> 🤖 Generated by Coder agent, will be reviewed by @person.` of ed4311b2cbca, because Coder Agent is
  a generic persona name. GateWeave names its own agent Coder, and
  `Written by the Coder agent on staging for WEB-7, ...` of gateweavehq/agents 72ef234d49b6 stays
  unread.
- **Harlan GitHub Agent.** `harlan-github-agent-author-01` and `harlan-github-agent-trailer-01` read
  `harlan-github-agent[bot]`, a label of its own, because the lines do not record which of its "two
  agent providers" ran.
- **Blink.** `blink-author-01` and `blink-trailer-01` read `blink-so[bot]` and `blinkagent[bot]`. The
  README of coder/blink states "Blink is a self-hosted platform for running AI agents that your team
  can talk to in Slack, GitHub, and a web UI."
- **CodeAlta.** `codealta-trailer-01` reads the line that its code writes,
  `Co-authored-by: CodeAlta <noreply@codealta.io>`, as `standard`, and `codealta-trailer-02` reads
  `Assisted-by: CodeAlta:gpt-5.5:CodeAlta`, a line that a person or a repository chose, as a
  convention.
- **Maki.** `maki-trailer-01` reads `Assisted-by: AGENT:regolo/glm5.2 [maki]` of mudler/LocalAI
  3d63736c56be and `Assisted-by: MAKI:regolo/glm5.2` of 4d5ea12c3b35. `tool-not-named-trailer-13`
  still reads a line with `[maki]`, and the label drops out because a tool is named.
- **Perfloop.** `perfloop-author-01` and `perfloop-trailer-01` read `perfloop[bot]` and
  `<agent@perfloop.ai>`. [Its page](https://perfloop.ai/docs/work) states "Perfloop commits as Perfloop
  Agent <agent@perfloop.ai>, with a Signed-off-by line for that identity."
- **Entelligence.** `entelligence-trailer-01` reads the co-author line of its review app, as
  `reviewer`.
- **DeepDocs.** `deepdocs-author-01` reads `deepdocsai[bot]` and `deepdocsai-test[bot]`. The record of
  the app states "DeepDocs is a GitHub AI agent".
- **iii harness.** `iii-harness-trailer-01` reads the line that its code writes, `Co-authored-by: iii
  harness ({model}) <noreply@iii.dev>`, as `standard`, and `iii-harness-trailer-02` reads
  `Assisted-by: iii harness / codex-gpt-6-astra` of libkrun/libkrun 72b65678540d, as a convention.
  Codex stays uncredited, because the parenthesis holds the model.
- **Gecko Security.** `gecko-security-author-01` and `gecko-security-trailer-01` read
  `gecko-security[bot]`. [Its page](https://gecko.security) states "AI Code Security That Fixes the
  Root Cause".
- **Twill.** `twill-author-01` and `twill-trailer-01` read `twill-hq[bot]` and `<agent@twill.ai>`.
  [Its page](https://twill.ai) states "Agents open pull requests and post updates where your team
  already works."
- **Specula.** `specula-trailer-01` reads `Generated-by: Specula (Claude Opus 5.5)` of apache/ozone
  0e1d067e3a97. Its README states "It uses coding agents to write TLA+ specs of the target system",
  and Claude stays out, because the lines have no address.
- **Xum, formerly Mux.** `xum-full-body-01` and `xum-trailer-01` read the footers and the co-author
  lines of coder/coder, as in `This change was written by Claude Sonnet 4.5 Thinking using
  [mux](https://github.com/coder/mux) and reviewed by a human` of 37fc6646adf5. The README of coder/xum
  states "Xum has a custom agent loop but much of the core UX is inspired by Claude Code." and "This
  project was renamed from Mux to Xum". `claude-full-body-01`, `claude-full-body-16` and
  `claude-full-body-27` skip such a line, so that the commit credits Xum and not Claude.
  `xum-full-body-01` also reads the tool as the subject at the start of a line, capitalized, with a
  statement of its work on the pull request, as in `> Xum opened this PR on behalf of @person.` of
  e7c2fd5d2e9a and `> Xum acted on Jane's behalf in this pull request.` of 9c42b1485733, because the agent
  that opened the pull request made its commits. `> Mux updated this PR description on behalf of Jane.`
  and `Mux opens the cloned repo by default.` stay unread.
- **Hermes Agent.** `hermes-agent-full-body-01` reads `run through the Hermes agent`, as in
  jupyterlab/jupyterlab 2f19586175c2, where `claude-full-body-32` skips the line above it.
- **Omi GitHub app.** `omi-github-app-full-body-01` reads `Generated by Claude AI via Omi` of
  BasedHardware/omi d7d3bdba38bd, as `standard`, because the code of the app writes the line and calls
  Anthropic's API itself (`client.messages.create(` in claude_coder.py), and no Claude Code runs on this
  path. The Claude rows skip the line, and `Generated by Claude Code via Omi` stays Claude's.
- **Kōan.** `koan-full-body-01` reads `_Generated by [Kōan](https://koan.anantys.com)_`, the footer
  that the code of Anantys-oss/koan writes into the description of each pull request that Kōan opens
  and at the end of the quality report that it adds there, as `standard`. A squash merge copies the
  description, as in aio-libs/aiohttp d60a88e283e0 and aio-libs/multidict 7194baa3206c. The label is
  Koan, as Kōan's README also writes it, because the id of a heuristic spells its tool in the letters
  a to z and digits. Kōan runs Claude Code by default, and `Drafted with Claude Opus 5 via koan` above
  the footer stays Claude's (`claude-full-body-05`), as `Claude (via Conductor)` does, so that such a
  commit is in both commit CSVs. The row does not read `_Automated review by [Kōan](...)_`, the footer
  of a review comment, and no row credits Kōan for `Drafted with Claude Opus 5 via Koan` without the
  footer, as in aio-libs/yarl b3adeb98f538. `tests.test_commits.TestTheFooterOfKoan` contains these
  lines. Kōan's commits land under the git identity of its operator, and its code names their
  author Kōan when `KOAN_EMAIL` is set (`setup_git_identity()` in `koan/app/startup_manager.py`).
  `koan-trailer-01` reads Koan or Kōan alone at the start of a co-author line, the line that GitHub
  writes for the author of the commits of a pull request in its squash merge, as
  `Co-authored-by: Koan <person300@example.org>` of esphome/aioesphomeapi 828c30aa1a24,
  8a520b82ff4e and 7647f3f3d86e and `Co-authored-by: Kōan <person301@example.org>` of
  Bluetooth-Devices/bleak-esphome 355cac657837. The pull requests came from branches named
  `koan/...`. The row is a convention, because the line stands at a person's address, as
  `aider-trailer-03` is, and Kōan's system prompt forbids a co-author line of its own ("Do NOT add a
  `Co-Authored-By:` trailer"). Koan followed by a surname is a person, and a longer name that
  begins with koan is not read. `tests.test_commits.TestTheCoauthorLineOfKoan` contains these lines.
- **Junie.** `junie-full-body-01` reads `Adding tests generated by Junie` of se2p/pynguin 495cf0168397.
- **Crush.** `crush-trailer-04` reads `Co-Authored-By: termai <noreply@termai.io>`, which the code of
  Crush wrote under its earlier name, as in Kilo-Org/kilocode 1d467dfda339. The README of
  opencode-ai/opencode states "The project has continued under the name [Crush][crush], developed by
  the original author and the Charm team."
- **Gemini naming no product.** `gemini-unspecified-full-body-05`, `gemini-unspecified-full-body-06`,
  `gemini-unspecified-full-body-07` and `gemini-unspecified-full-body-08` read Gemini alone in a
  disclosure, as in `This PR was created with help from generative AI (Gemini). I fully` of
  envoyproxy/envoy 413bae5f5c2b.
- **Jules and pi.** `jules-trailer-02` reads Jules's bot user under the keys of moeru-ai/airi, and
  `pi-trailer-06` reads `Generated-by: Pi 1.4.0 (openai-codex/gpt-6-luna)` of apache/thrift
  807973405747.

The rows of the label `tool not named` read further credits that name no tool.
`tool-not-named-trailer-17` reads Xiaomi's MiMo models, as in `Assisted-By: mimo-v2.6-pro` of
apache/nuttx 038720c350c2, `tool-not-named-trailer-18` reads `Co-authored-by-agent: Unknown` of
moeru-ai/airi, and `tool-not-named-trailer-19` reads Anthropic's model family with the vendor's name,
also misspelled, as in `Assisted-by: Anthopic Sonnet 5` of openssl/openssl b758149ceacb. That commit
takes Claude, because `claude-full-body-13` reads the sentence above the trailer,
`which I have fixed with the assistance` above `of Claude`, and the label drops out.
`tool-not-named-full-body-05` reads the ticked box AI of apache/arrow's template, as in `- [x] AI` under
`**PR code and description written by:**` of 2f89cf5bb369. `tool-not-named-trailer-16` also reads the
link of TraceHub, as in `Assisted-by: https://tracehub.aui.dev/t/assistant-ui/assistant-ui/01ce521bfa9b6325`,
`model-unspecified/`, and GLM before `via agentctl`, as in
`Co-Authored-By: GLM via agentctl (cl2-infracost)` of BasedHardware/omi 0914c024cf3b. TraceHub's line marks a session that is attached
to the commit, and it does not show that the agent wrote the change, because its package states
`This fallback preserves sessions for commits made manually.` `tool-not-named-full-body-02` reads a
GPT-x Codex model after `With the help of AI-Agents:`, as the subject of `drafted` and without GPT as
the value of the label, as in `- AI tools and models used: 5.3 Codex` of jupyterlab/jupyterlab
e0d7508f5138.

Some rows read less than they did:

- A digit and a space before Codex name a model. `codex-full-body-03`, `codex-full-body-07`,
  `codex-full-body-24`, `codex-full-body-31` and the Codex slot of the forms above do not read
  `GPT-5 Codex` or `GPT 5.4 codex`, and `Codex 5.3` and `Assisted-by: Codex 5.6 Sol` stay read. The
  head branches of the pull requests of rsyslog/rsyslog with such a footer are `codex/...`, so such a
  credit is lost by the reading rule and not in fact, and no other row can recover it.
- `Assisted-by: OpenAI Codex (via pi)` of tektoncd/pipelines-as-code 26b549e9027c credits pi and not
  Codex (`codex-trailer-01`, `codex-trailer-04`, `pi-trailer-01`).
- `copilot-full-body-01` no longer reads a merge after a commit hash, because a credit that a message
  only quotes from another commit does not count, as in `- f4727f41684c1d727ad0401501bec3a37215089d:
  Merge pull request #1176 from meziantou/copilot/sub-pr-1172 Fix CI`, or Apache's `Closes #5208 from
  jongyoul/copilot/...` of apache/zeppelin 8092916424b9, whose heads carry a co-author line of Claude
  and which Copilot only reviewed.
- `copilot-full-body-19` and `copilot-full-body-29` do not read a line that credits Copilot with code
  review, log or diff analysis or the root cause only, as in `GitHub Copilot was used briefly for code
  review and suggesting C++ idioms.` of openvinotoolkit/openvino 64a330242e29. Lines that go on to a
  change stay read.
- `claude-trailer-01`, `claude-trailer-06` and `gemini-trailer-02` do not read a line that ends in
  `(review)`, as in `Assisted-By: Claude Opus, Claude Code via ATD (review)` of electron/electron
  dc4ed21c9dd9. `gemini-trailer-01` does not read
  `Co-authored-by: Review feedback person, gemini-code-assist` of spiriMirror/libuipc 5b3c281c4ea3, with
  the name of its reviewer replaced, because a tool that only checked the change does not count.
- `claude-full-body-08` and `claude-full-body-32` do not read a line with a statement that all code is
  the author's own or that Claude Code only confirmed. `claude-full-body-32` also does not read Claude
  in a chat, as in `Claude Opus 5 (chat)` of jupyterlab/jupyterlab 86818bb432cd, and
  `claude-full-body-07` does not read an analysis, a review or debugging that Claude assisted, as in
  `After some analysis (assisted by Claude) it appears the issue is due to` of
  keinsaasforever/better-chatbot 09b938f17ca7.
- The rows of a disclosure heading do not read the tool followed by `found` unless the line goes on to
  a making verb, so that `Claude found the issue` stays out and `Claude Code found the cause, wrote the
  fix,` of oxia-db/oxia stays read. They do not read the tool followed by `helped` unless the sentence
  goes on to a making verb, so that `Codex helped inspect the optimizer, measure query preparation, and`
  above `review the change.` of tursodatabase/turso a9cf5d8a8056 stays out and `Claude Code helped review
  the code that found these bugs, and drafted` above `the fixes and the regression tests.` of
  openvinotoolkit/openvino 4918a657126a stays read. A verb of making with the description, a summary
  or the commit message as its object does not count, as in the rows of a line that begins with the
  tool, so that `Claude Code helped find the broken links in the linkcheck report, verify` above
  `the replacement URLs, and draft this description.` of ray-project/ray 1a14a739d61d stays out.
  The tool followed by `helped with` the change, this, these changes, some of this, the
  implementation or a part of the change stays read, as in
  `Claude helped with these changes` of pomerium/pomerium cfdbc90c5bd6, because pomerium's AI policy
  requires a disclosure to state "the extent that the work was AI-assisted". `helped with` a review,
  a description, an investigation or another activity that makes nothing stays out, and so does
  `helped with` the change followed by another noun, as in `helped with the implementation plan`.
  Curate counts as a verb of making, so
  that `Codex helped compare the release and main branches, curate the document` above `split,` of
  munich-quantum-toolkit/core cbeea241b231 stays read, and `Codex helped compare the configuration
  defaults and trace the backup` above `purge logic.` of apache/kvrocks 1139e0624ff7 stays out. Below
  the question of pgmpy/pgmpy,
  `claude-full-body-35` reads the response and the next response up to `- What steps` and refuses a statement
  that the author wrote the code, such as `written by me manually` and `performed manually`. `claude-full-body-35` also does not read a line that states that a
  person wrote the code, that the tool only wrote the description or that it only found, explained,
  reviewed or helped debug, as in aws/jsii f73a0804f33d, or `claude.ai`, as this document decides for
  the chat.
- The rows of a disclosure heading, the rows of the labeled disclosure, `claude-full-body-08`,
  `codex-full-body-07`, `copilot-full-body-10`, `cursor-full-body-07`, `gemini-full-body-04` and
  `opencode-full-body-01`, and the rows of a label of a pull request template,
  `claude-full-body-32`, `codex-full-body-31`, `copilot-full-body-19`, `cursor-full-body-13`,
  `gemini-full-body-08` and `opencode-full-body-11`, do not read a statement that gives the tool
  only activities that make nothing or that make only the description, a summary or the commit
  message, by the decision of 2026-11-07 that the bullet on a disclosure heading states. The rows of
  a label also do not read the tool followed by `found` unless the text goes on to a part of the
  change. The commits that lose their tool, because the tool only reviewed, analyzed, investigated,
  debugged, searched, ran checks or wrote the description, are Netflix/metaflow 3e99c6433004,
  ray-project/ray f606a9fa4e18, verl-project/verl 3e0d79ea8c38 and 68c9ac35d9f3,
  openvinotoolkit/openvino 3d9292d0c3d1, pomerium/pomerium ad119608be99, eic/EICrecon b6a7fbca7ed6,
  cowprotocol/cowswap 1fd4d76fa47c, oxc-project/oxc 8166f80b4197, apache/arrow-rs 1d4437018c14 and
  dotnet/runtime 97dd992a04cd, and the message of none of them states that the tool wrote a part of
  the change. `claude-full-body-08` and `claude-full-body-32` no longer read
  `AI-assisted: I used Claude Code while investigating; I reviewed, ran and` of line/armeria
  faa918d9b9d1, cbb5329421a3 and 109861945f76 and oxc-project/oxc 80045f5f13e2, 42dfbb5010dd and
  2ba7e33f2c45, whose trailers credit Claude. By the decisions of 2026-11-08 that the bullet on a
  disclosure heading states, delta-io/delta-rs c85075e4277a, rolldown/rolldown d69e0716a709 and
  uber/NullAway daa58285bf43 lose Claude, because the tool only mapped out the affected methods,
  navigated the codebase, or searched, ran the repro and ran an experiment in the paragraph after
  `I used Claude Code for this PR.`
- The rows of a line that begins with the tool and the rows of a disclosure heading do not read
  write, build, port, patch, draft, create or generate in a noun pair after a determiner and before
  failures, paths, sides, numbers or halves, as in
  `Codex helped me investigate the test and build failures.`,
  `Codex helped me trace the read and write paths.` and
  `Codex helped analyze the import and port numbers.`, in which build, write and port name a kind of
  failure, path and number. A verb of making before another plural noun is read with its object,
  as in `Codex helped investigate the bug and write tests.` The commit CSVs of the collection of the
  run that started on 2026-11-03 have the noun pair, as in `the read and write paths` of
  ClickHouse/ClickHouse a0cc2ec5b2e4, and a verb with its object in the same form, as in
  `the support and add tests` of Loop3D/LoopStructural 722f50a74be8, and the narrower noun pair
  changes the reading of no commit in them.
- `cursor-full-body-07` and `cursor-full-body-13` do not read the lowercase word cursor after `the`,
  as in `OpenAI Codex prepared the cursor fix` of libsemigroups/libsemigroups 2cde91744d69, and
  `cursor-full-body-22` does not read `have cursor convert to resource` of icanbwell/fhir-server
  92d5ee1add78. `cursor-full-body-06` reads `Assisted by Claude AI and Cursor AI` of
  RedHatQE/openshift-virtualization-tests 32f4b8f366b7 at the end of a line.
- `gemini-author-05` and `gemini-trailer-01` do not read `gemini-cli-robot`, the identity of the release
  workflows of google-gemini/gemini-cli, in which no Gemini CLI runs, and `gemini-full-body-21` reads
  its changelogs, which `.github/workflows/release-notes.yml` generates with Gemini CLI, by their
  subject above the co-author line. A changelog that the robot authored without that line loses Gemini,
  because an author row cannot read the subject. `gemini-author-04` and `gemini-author-05` credit
  f8678f9e9def, the automated version bump of pull request 8667, because
  `.github/actions/create-pull-request` opened that pull request with the token of the app gemini-cli and
  GitHub wrote the app as the author of the squash merge. No Gemini CLI runs in that step, and an author
  row does not read the subject, so this commit is a known wrong credit of Gemini.
- `kilo-code-trailer-01` does not read `kilo-maintainer[bot]`, the CI app of Kilo-Org/kilocode.
  `heuristics/not_ai_commits.csv` names this app and `gemini-cli-robot`.
- `cline-trailer-03` does not read an address at ant.amazon.com, as in `Co-authored-by: Cline
  <person@147dda767eb3.ant.amazon.com>`, which names a person, and `copilot-trailer-16` no longer reads a
  person's plus address.

`tests.test_commits.TestTheLinesOfTheReviewOf1106` contains the lines of these rows, the subjects and
identities that they read, and the lines beside them that they must not read.
`tests.test_commits.TestADisclosureHeadingAndTheLineBelowIt` contains the lines of the templates and of
a disclosure that credits a cause only.
`tests.test_commits.TestAToolThatHelpedMakeAPartOfTheChange` contains the lines in which a tool
helped make a part of the change, the lines of the compound labels and the lines beside them that
make nothing, credit the description alone, describe the section of a document or name a noun
pair. `tests.test_commits.TestADisclosureOfActivitiesThatMakeNothing` contains the lines below a
heading and after a label that give the tool only activities that make nothing, only the
description or only comments on the pull request, also before a clause of the person or in the
paragraph after a first sentence that only says that the tool was used, also in a blockquote that a
line break divides, and the lines beside them that go on to a part of the change that the tool made.

#### The confirmed errors of the evaluation of 2026-10-26, checked 2026-11-05

On the commit side, [the detection accuracy evaluation](tools/evaluation.md) of the collection of
the run that started on 2026-10-23 confirmed commits of its sample of commit precision with wrong
`attributions`, which its labels and resolutions mark with `attribution_correct` `no`, and
phrasings of its sample of mentions that are attributions and that no row read. When the current
rows classify these commits again from the commit logs of that run, each commit has the
attributions that its label names, through the rows of
[The detection accuracy evaluation of 2026-10-26](#the-detection-accuracy-evaluation-of-2026-10-26-read-2026-10-29).
The current rows also read every such phrasing except two, which no row reads by decision: the
prefix `(COPILOT)` of mentions mn-0064
([The labeled attributions decided on 2026-11-02](#the-labeled-attributions-decided-on-2026-11-02))
and the subject `exp: creating new Avatar e2e tests with Copilot (#1571)` of mentions mn-0086,
which a form loose enough to read it would read in subjects about features as well
([The detection accuracy evaluation of 2026-10-26](#the-detection-accuracy-evaluation-of-2026-10-26-read-2026-10-29)).

`claude-full-body-21` reads one of these phrasings, `claude having a go`, the whole message of
damonsk/onlinewardleymaps
[ca3abcc2eb22](https://github.com/damonsk/onlinewardleymaps/commit/ca3abcc2eb2206c3f0ae681a1ba757706c56b313)
(mentions mn-0188 of the evaluation of 2026-10-26). The commit adds an analysis report, a
migration guide and the source and test files of the change. The wording states that Claude made
an attempt at the change, and people write it in other repositories as well, as
`Let claude have a go at refactoring parameter handling (#2)` of NyanCAD/Cadnip.jl,
`I had claude have a go at this.` of duso-org/duso and
`Had Claude have a go since I was out of Gemini tokens` of b0tt0mturn/WindDash, which GitHub's
commit search returned on 2026-11-05. In the commit logs of the run that started on 2026-10-23,
the wording is also the bold line `**let claude have a go at it**` in the message of
DataDog/datadog-agent 9d7f70453c17, which also states "This was mostly AI driven.", and no other
row credits Claude in either commit. The verb is `have`, `having`, `had` or `has`, as in
`Claude had a go`, the whole message of GStaines199/T179-Capstone-Project
[6ac9bad201ee](https://github.com/GStaines199/T179-Capstone-Project/commit/6ac9bad201eedf0031cda1e87c97f344a12b5316),
the line `* Claude had a go` of the squash merge YuKitsune/vMaestro
[3684346d9b05](https://github.com/YuKitsune/vMaestro/commit/3684346d9b05cd1378709fac9e150bbc4925b4c8)
and `claude has a go` of micromouseonline/line-follower-simulation aef64785bb71, read through
GitHub's API on 2026-11-05. The row reads the wording at the start of a line, also after
`let`, `had` or `I had`, because `Will let claude have a go at it from here`, in the middle of a
line of Community-NEMS/cnems-models 7cc70b15d9c0, announces a change that Claude is to make later.

`tests.test_commits.TestTheFormsTheEvaluationOf1026Found` contains the lines of the commits and
phrasings that the rows of that section read, `tests.test_commits.TestTheLabeledAttributionsDecidedOn1102`
the merge of mentions mn-0127, which `codex-full-body-12` reads, and
`tests.test_commits.TestALineThatClaudeHadAGoAtTheChange` the lines of `claude-full-body-21`.
`tests.test_tools_evaluation.TestTheLabeledMentions` recounts the phrasings of the tracked samples
that are attributions and that no row reads.

#### The confirmed errors of the evaluation of 2026-11-04, checked 2026-11-08

[The detection accuracy evaluation](tools/evaluation.md) of the collection of the run that started
on 2026-11-03 confirmed commits that no row credited and credits that the rows give wrongly. The
maintainer decided on 2026-11-08 how the rows read them, and the items below are those of that
evaluation.

Expensify runs Claude Code under its account MelvinBot. The commits that the account makes carry
the identity `infra+melvinbot@expensify.com`, with the name of the person who requested the change
followed by `(via MelvinBot)`, as `Jane Doe (via MelvinBot)` in the merge
[bf904457ffdd](https://github.com/Expensify/App/commit/bf904457ffddc9eb2382f12402d1f6fd6c2877c2)
of Expensify/App (commit_recall cr-0044). The design document in
[Expensify/App#87030](https://github.com/Expensify/App/issues/87030#issuecomment-4225657528) draws
the "MelvinBot App Workflow" with the step "Claude Code Action runs", and `.claude/README.md` of
Expensify/App has the title "Agent Framework (Claude Code + GitHub Actions)", read through GitHub's
API on 2026-11-08. `claude-author-16` reads the identity as Claude's `account`. It does not read the
account's noreply address, `MelvinBot@users.noreply.github.com`, because the account also merged
version bumps under it in 2024, and the merges and branch updates that GitHub makes for the account
at that address therefore stay unread. The account's workflow writes a co-author line for the
person who requested the change, so that `Co-authored-by: MelvinBot
<MelvinBot@users.noreply.github.com>` names the account only in a commit that the account made at
its own request, which `claude-author-16` credits. GitHub writes the same line when a person commits
a review suggestion of the account, and `claude-full-body-57` reads such a commit, with GitHub's
subject for it, as `reviewer`, as `claude-full-body-19` reads the suggestions of the claude app.
The line is not the account's standard line, and no row reads it outside a committed suggestion.

Roomote, the cloud agent of Roo Code, commits as `Roo Code <roomote@roocode.com>`, at an address
that GitHub links to the user `roomote` with the bio "Your own cloud coding agent.", and as
`roomote[bot]`, the bot of the GitHub App `roomote` of RooCodeInc. `roo-code-author-01` reads both
identities as `account`, and `roo-code-trailer-02` reads their co-author lines as `standard`. The
line `Co-authored-by: Roo Code <roomote@roocode.com>`, which `roo-code-trailer-01` reads as a
convention, therefore credits Roo Code as `standard`, as in zgsm-ai/costrict 3ccc049eca50
(commit_precision cp-0563). A review suggestion of `roomote[bot]` that a person committed, with the
subject `Apply suggestion from @roomote[bot]` or as such a commit in the list of a squash merge,
credits Roo Code as `reviewer` (`roo-code-full-body-01` and `roo-code-full-body-02`), as the rows of
CodeRabbit do for its suggestions.

DemonDamon/AgenticX records the model that implemented a change in an `Impl-Model:` trailer and
the model that planned it in `Plan-Model:` (mentions mn-0067). Its
[AGENTS.md](https://github.com/DemonDamon/AgenticX/blob/c6f4400a2bc742282f3eff92d857896d2fa5276a/AGENTS.md)
calls both lines a record of the toolchain and no AI signature, with values that the person
supplies. `tool-not-named-trailer-02`, `tool-not-named-trailer-03`, `tool-not-named-trailer-05`,
`tool-not-named-trailer-06`, `tool-not-named-trailer-07`, `tool-not-named-trailer-09` and
`gemini-unspecified-trailer-01` read the key `Impl-Model` beside the keys that they read otherwise,
so that a model of Anthropic, OpenAI, xAI, Moonshot AI or Z.ai and Cursor's Composer credit
`tool not named` there, as in `Impl-Model: Kimi K3 Max` of
[b507ae39accc](https://github.com/DemonDamon/AgenticX/commit/b507ae39acccb04235c8c811a4ea40ef06069b04)
and `Impl-Model: Sonnet 5` of
[d459553fe995](https://github.com/DemonDamon/AgenticX/commit/d459553fe995142bf29d38fdb9c985a1789126ff).
`Impl-Model: gpt-5.3-codex` of
[124ec522869a](https://github.com/DemonDamon/AgenticX/commit/124ec522869a6cc2bdba15e3d3f155ad9713da60)
therefore credits `tool not named`, as `Assisted-by: GPT-5.3-Codex` does
([A trailer that names no tool](#a-trailer-that-names-no-tool-2026-09-26)), and
`Impl-Model: gemini-3.1-pro` credits Gemini (unspecified). `tool-not-named-trailer-09` also reads
`Fast` after the version of Composer and a version joined to the name, as in
`Impl-Model: Composer 2.5 Fast` of
[b59b657d4ad7](https://github.com/DemonDamon/AgenticX/commit/b59b657d4ad7fd1b14a4e463229cc5952eb73c7b)
and `Impl-Model: composer2.5` of
[cb8a7166a3e5](https://github.com/DemonDamon/AgenticX/commit/cb8a7166a3e5d000ac9e23c9d6cdb9030004c83f).

`tool-not-named-trailer-20` reads the values that the rows of single models leave out under the
key, and they credit `tool not named` as well. A Claude model with Claude first, as in
`Impl-Model: claude-opus-5.5` of
[05fbafd4fe2e](https://github.com/DemonDamon/AgenticX/commit/05fbafd4fe2e96d8ef0537f28954e790119b1368),
credits no Claude product, because the key names the model and not the product that ran it, and
the repository keeps its plans in Cursor's `.cursor/plans/` (mentions mn-0067). Under their own
keys, `claude-trailer-01` to `claude-trailer-03` still read such a value as Claude. A Codex model
with a version names the model GPT-5.3-Codex, because the plan that
[b872201ec225](https://github.com/DemonDamon/AgenticX/commit/b872201ec225605065433fd75e02b5292d14508c)
commits suggests `gpt-5.3-codex` and the commit records `Impl-Model: codex 5.3 Medium`, while
`Assisted-by: Codex 5.3` stays Codex's. A list of models joined by commas or plus signs names
several models and no tool, as `Impl-Model: grok 4.7, claude opus 5.5` of
[4bd78d05240f](https://github.com/DemonDamon/AgenticX/commit/4bd78d05240f9e81c82e10f2372d7c09e1fbf717)
and `Impl-Model: grok-4.5, kimi-k3` of
[763aec98345f](https://github.com/DemonDamon/AgenticX/commit/763aec98345f66bc9ce76a29e0904e504a6a9d24)
do. `tool-not-named-trailer-21` reads `Impl-Model: auto` of
[2c4a83379ea3](https://github.com/DemonDamon/AgenticX/commit/2c4a83379ea30d04cb61ac27b9fb71251f513161).
Auto is the entry of the model pickers of Cursor and GitHub Copilot with which the tool chooses the
model, so that the line states that a model made the change and names neither the model nor the
tool. `Plan-Model:` credits nothing, and neither does `Suggested-Impl-Model:`, the model that a
plan recommends.

The other confirmed credits stay wrong by decision, because no form of their lines separates them
from right credits. [The prefix `[codex]`](#the-prefix-codex) lists the squash merges of the pull
requests of moq-dev/moq that Claude signed (commit_precision cp-0098) and the list of zgsm-ai/costrict
3ccc049eca50, which repeats commits of its base. A git identity that a checkout keeps credits the
wrong agent as well: Fannon/search-bookmarks-history-and-tabs commits as
`Codex Agent <codex.agent@example.com>` while Claude Code works in it. The only commit of its pull
request #417 has that identity and Claude Code's
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, and the description of the pull request
ends with Claude Code's footer, so that the `Signed-off-by:` and `Co-authored-by:` lines of the
identity in the squash merge
[7b984fd6589a](https://github.com/Fannon/search-bookmarks-history-and-tabs/commit/7b984fd6589ab02869042672c452a567c5d614ce)
name the environment and not the agent that made the change (commit_precision cp-0439; GitHub's
API, read 2026-11-08). The commits of 2026-10-30 and 2026-10-31 that carry Claude Code's line beside
the identity, fc265bd6bbde, 1061f5b28f56, dedace6a989d, 6c2aeeba4adc, 4efab983ab8c, 398d85e8d562,
7b984fd6589a, aafd53fba8b4, 7b93365a0e6f, 8f0980644842 and d63c8b8ef4b6, are documented wrong
credits of `codex-trailer-02` and `codex-trailer-07`. In each of them, Codex is only a convention
beside Claude's `standard` line, and an analysis can leave them out by their attributions.

`tests.test_commits.TestTheConfirmedErrorsOfTheEvaluationOf1104` contains the commits that these
rows read and the lines that they leave unread.

#### The labeled attributions of the evaluation of 2026-11-04, decided on 2026-11-08

[The detection accuracy evaluation](tools/evaluation.md) of the collection of 2026-11-04 labeled
phrasings of its sample of mentions as attributions that no row read. The maintainer decided on
2026-11-08 which of them a row reads:

- **The harness trailer of Start9Labs' agent.** `claude-trailer-31` reads
  `Helix-Harness: claude-code`, the trailer with which Helix, the agent of Start9Labs, names the
  harness that ran it, as in Start9Labs/start-technologies
  [3343ce2ad8c8](https://github.com/Start9Labs/start-technologies/commit/3343ce2ad8c8716023bfc9a00f4eaf043c1daa40)
  (mentions mn-0106 of the evaluation of 2026-11-04) with `Helix-Model: claude-opus-5-5` below it.
  The squash merges that list the agent's commits repeat the trailer below each listed commit, as
  [79ed681e770d](https://github.com/Start9Labs/start-technologies/commit/79ed681e770d855c7bc6b697b87bc9bab4029cd6)
  does. GitHub's API resolves the author address of 3343ce2ad8c8 to the account
  [helix-a](https://github.com/helix-a), named Helix-A, with the company `@Start9Labs`, and GitHub's
  commit search for `Helix-Harness` in the repository returns the values `claude-code` and `pi`
  only, both read 2026-11-08. The line names Claude Code as the harness that ran the agent and not
  only as the provider of a model, as `codex` is in
  `Worked by Coder from an OpenAgents chat (codex gpt-6.1-sol)` (mentions mn-0036 of the
  evaluation of 2026-10-05), which credits OpenAgents' own agent. The row credits Claude as a
  convention. By the same reason, `pi-trailer-07` reads `Helix-Harness: pi`, which the agent writes
  with a model of pi's own catalog, such as `Helix-Model: openai-codex/gpt-6.1-sol` in
  [77e71cf60e2f](https://github.com/Start9Labs/start-technologies/commit/77e71cf60e2f90a73f7bc360b2bc64f47c54ac2c),
  and credits pi as a convention (the review of 2026-11-08 of the fixes of the evaluation).
- **`Initially` before the verb.** `claude-full-body-05` also reads `Initially` before the verb of
  a line that states that a change was made with Claude, and no other word there, as in
  `> Initially generated by Claude Opus 4.7, modified and reviewed by (a person)` of coder/coder
  [2adc8f52720d](https://github.com/coder/coder/commit/2adc8f52720daf974f10f43d688a7b959fb3163f)
  (mentions mn-0159 of the evaluation of 2026-11-04). One developer of coder/coder ends the
  descriptions of pull requests with this note, also with Claude Opus 5, and with `Coder Agents`
  where Coder Agents made the change, which `coder-agents-full-body-01` reads. coder/coder has
  `.claude/settings.json` and `.claude/skills` in `data/validated`. Another word in that place can
  name a part other than the change, as in `> Description written by claude code` of
  geoarrow/geoarrow-rs 31921f502d6a.
- **`AI-generated (cursor)` at the end of a line.** `cursor-full-body-37` reads
  `Note: this is almost entirely AI-generated (cursor)` of doged-io/doged
  [01e3ce6ca9af](https://github.com/doged-io/doged/commit/01e3ce6ca9affb5676470d3db1b756f30a84a94c)
  (mentions mn-0037 of the evaluation of 2026-11-04), a commit that converts the tests of Cashtab
  to TypeScript and goes on with
  `imo this is one of the areas it is best at, large scale refactor that is mostly type checking across lots of mocks`.
  doged-io/doged has `.cursorrules` in `data/validated`. The row reads the form for Cursor, the
  tool that the commit names, and not after `not` or `n't`.

No row reads the other phrasings, by decision:

- `Initial pass with Codex than me just reviewing everything line by line`, the description of a
  pull request that the merge tursodatabase/turso 82d48e71fb64 copies (mentions mn-0098). A pass
  with a tool also names a pass that makes no part of the change, as `we did a pass with Claude`
  above `over public Slack channels to find conversations, threads, comments that` of
  Recidiviz/pulse-data e16f5b7c92ad, in which Claude searched the channels.
- `Thanks to (a person), (a person) and Claude !` at the end of a fix of wekan/wekan 68e0032c6d3b
  (mentions mn-0109). A thank-you credits no tool
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)),
  and the notes of `claude-full-body-52` leave `Thanks to Claude` unread.
- The tag in `ElevationPool performance updates (codex).` of pelicanmapping/osgearth 6d7e5e3b93b9
  (mentions mn-0187), which no vendor page names. A tool's name in parentheses at the end of a
  subject also names the area of a change or the reviewer whose finding it addresses, as
  `feat(ai): OpenAI models via a ChatGPT subscription (Codex)` of frappe/builder e145d48c5672,
  which adds Codex's backend, and `bot settings: redact stored API keys in safeKeySummary (Codex)`
  of tloncorp/tlon-apps c214aaee2f2d, which addresses Codex's review in a commit that Claude
  co-authored. [The prefix `(COPILOT)`](#the-labeled-attributions-decided-on-2026-11-02) stays
  unread for the same reason at the start of a subject.

`tests.test_commits.TestTheLabeledAttributionsDecidedOn1108` contains the commits that these rows
read and the lines beside them that credit none of the six tools, among them the lines of mn-0098,
mn-0109 and mn-0187, and `tests.test_tools_evaluation.TestTheLabeledMentions` lists these
phrasings with their reasons.

#### The key-value lines that no heuristic credited, read 2026-11-08

The commit logs of the collection of the run that started on 2026-11-03 have commits that no
heuristic credited and that have a key-value line under a key that can credit a tool,
such as `Impl-Model:`, `AI-Assisted:`, `Agent:`, `Client:` or `Harness:`. This section decides the
lines of a list of such keys, and
[The keys left undecided on 2026-11-08, decided on 2026-11-09](#the-keys-left-undecided-on-2026-11-08-decided-on-2026-11-09)
decides further keys. GitHub's API supplied the
full messages and the files that ground each line, and GitHub's commit search the commits of the
same forms in other repositories. A trailer heuristic begins with its token and has no alternative
at the top level (`tests.test_heuristics`), so a key with a value of another form is a row of its
own, as `kiro-trailer-03` is beside `kiro-trailer-01`. The rows read these lines:

- **The models after `Impl-Model:`.** `tool-not-named-trailer-20` and `tool-not-named-trailer-21`
  read the values of the key `Impl-Model` of DemonDamon/AgenticX that the rows of single models
  leave out, and `tool-not-named-trailer-07` and `tool-not-named-trailer-09` read more of them
  ([The confirmed errors of the evaluation of 2026-11-04](#the-confirmed-errors-of-the-evaluation-of-2026-11-04-checked-2026-11-08)).
- **CC at Claude Code's address.** CC, the initials of Claude Code, at Claude Code's address counts
  for Claude as a convention, because the line names Claude Code itself and no other agent.
  Examples are `Co-authored-by: [CC] <noreply@anthropic.com>` of fifthsegment/Gatesentry
  [edf2823b3226](https://github.com/fifthsegment/Gatesentry/commit/edf2823b322680d4a4b29904818951641277b34e),
  whose squash merges b9e3a50455a7 and f75a39bbe612 carry it beside
  `Co-Authored-By: Claude <noreply@anthropic.com>` and
  `Co-Authored-By: Claude Code <noreply@anthropic.com>` from the same pull request, and
  `Co-authored-by: MF,CC <noreply@anthropic.com>` of TA-Lib/ta-lib
  [0525feb80489](https://github.com/TA-Lib/ta-lib/commit/0525feb80489cc149388ee615c32132e132e1b75),
  which the project also writes without the address, as in
  [edf281cb0cf0](https://github.com/TA-Lib/ta-lib/commit/edf281cb0cf07c534f609cb90cb52ed4b3bb94a7).
  The sources of TA-Lib/ta-lib list `CC       Claude Code (AI assistant)` among their
  contributors (`ta_codegen/input/pvt/pvt.c` at 58160778ecaa). `claude-trailer-32` reads these
  lines. It does not read CC at another address, CC with other initials and no address, or a line
  `MF,CC` without a key.
- **kres.** kres, the agent of masoncl/kres that "can also automatically generate fixes for the
  bugs it finds", writes `Assisted-by: kres (<model>)` into the commits of its fix workflow, and
  `Assisted-by: kres:<model>` since 2b947d87c2f2 of 2026-05-29 (`assisted_by_from_model_id` in
  `kres/src/main.rs`). `kres-trailer-01` reads both forms as `standard`, as in systemd/systemd
  [3f0aa5852ec4](https://github.com/systemd/systemd/commit/3f0aa5852ec4f53f7412cd8b86fd9d064bebd108)
  and torvalds/linux
  [88a76145451d](https://github.com/torvalds/linux/commit/88a76145451d703eedd867b5989bf73d17340399).
  The Claude model in the value is the model that kres ran, and no Claude row reads it. A bare
  `Assisted-by: kres` and a co-author line with the name Kres are not read.
- **Jules Agent.** Jules writes `Co-authored-by: Jules Agent` with an address that it makes up or
  with none, as `google-labs-jules[bot]` does in rsyslog/rsyslog
  [d17e9a766f6f](https://github.com/rsyslog/rsyslog/commit/d17e9a766f6f0ec381ee6baeea1dc7211b4f7323)
  and 786b56a447b1, and the squash merges of Jules's branches in rsyslog/rsyslog carry the line,
  as [5a63777ef3e7](https://github.com/rsyslog/rsyslog/commit/5a63777ef3e72917766014b8bb16891f15d25760)
  does. `jules-trailer-03` reads Jules Agent first in the value as a convention, as
  `capy-trailer-02` reads Capy Agent. Jules alone, a person named Jules and Jules Agentic are not
  read.
- **Trae AI.** `trae-trailer-01` also reads Trae AI, as in
  `Co-authored-by: Trae AI <noreply@trae.ai>` of the squash merge verl-project/verl
  [263653e74930](https://github.com/verl-project/verl/commit/263653e749306287a5032140cce3d7833c1959d0),
  which takes the line from
  [e502d7a9011f](https://github.com/verl-project/verl/commit/e502d7a9011f6bab542ccbbf35ec0e9dbb377310)
  of pull request 8274, a commit that states "AI assistance was used; the submitting human reviewed
  every changed line." AI is not a surname, and the address is TRAE's own, so the line names the
  tool and not a person, and `Trae Aiken` stays a person.
- **Kimi K2.7 Code.** Moonshot AI's model Kimi K2.7 Code ends in Code and names no tool
  ([moonshotai/Kimi-K2.7-Code](https://huggingface.co/moonshotai/Kimi-K2.7-Code) and
  [moonshotai/kimi-k2.7-code](https://openrouter.ai/moonshotai/kimi-k2.7-code), read 2026-11-08).
  `tool-not-named-trailer-05` therefore reads it, as in
  `Co-Authored-By: Kimi K2.7 Code <noreply@kimi.com>` of packit/ogr
  [a685227973e2](https://github.com/packit/ogr/commit/a685227973e2b2f6554b225f4632961e81863864) and
  `Co-authored-by: kimi 2.7 code <kimi@moonshot.cn>` of sebo-b/warp
  [79ac078ac2f6](https://github.com/sebo-b/warp/commit/79ac078ac2f65063e1ce02b9d75d3b25977bc1e3).
  The row leaves out Kimi Code, which `kimi-code-trailer-01` reads as a tool, and Kimi K3 Code,
  which no row reads.
- **Junie's GitHub Action.** `junie-trailer-01` also reads the noreply address of `junie-agent`,
  user 247260674, as `standard`. JetBrains' Junie GitHub Action commits with that identity
  (`src/github/operations/auth.ts` and `src/constants/github.ts` of JetBrains/junie-github-action
  at 7e0a3d3059e3), as in the squash merges JabRef/jabref
  [82184659b60b](https://github.com/JabRef/jabref/commit/82184659b60b190ff20c54b76fcaffde7de21e62)
  and [bc49dfd23f45](https://github.com/JabRef/jabref/commit/bc49dfd23f45a1d62416768743b416152aa1c0c6).
  `junie-author-02` reads the same identity as the author or the committer.
- **`Assisted-by: <Cursor>`.** `cursor-trailer-01` also reads Cursor in angle brackets, as in
  podman-desktop/podman-desktop
  [9ec28a6b0de0](https://github.com/podman-desktop/podman-desktop/commit/9ec28a6b0de0ab766f3e1bfba1ec3c488dbb6a30),
  one contributor's spelling of the line `Assisted-by: Cursor` that the repository's other
  contributors write. The repository prescribes no form of the line.
- **Composer at Cursor's address in a co-author line.** `cursor-trailer-17` credits Cursor's model
  Composer at Cursor's address by the decision of 2026-11-06
  ([The credits of the review of 2026-11-06](#the-credits-of-the-review-of-2026-11-06)), and the
  key does not change what the line names. The row therefore also reads the line under the keys
  `Co-authored-by` and `Authored-by`, as in `Co-authored-by: Composer <composer@cursor.com>` of
  go-gitea/gitea
  [fc23bd7b3a8a](https://github.com/go-gitea/gitea/commit/fc23bd7b3a8a0b6a5699cc1a0bae29402db4b1f1),
  `Co-Authored-By: Composer <noreply@cursor.com>` of zcash/zallet
  [09ce4406cf40](https://github.com/zcash/zallet/commit/09ce4406cf404ca9ba5712e1b9095e733802051b)
  and `Authored-By: Composer <noreply@cursor.com>` of konflux-ci/multi-platform-controller
  [b3b33dbfa74c](https://github.com/konflux-ci/multi-platform-controller/commit/b3b33dbfa74c2b1c2f7499a3a4ddb5d523fae5ff),
  and at an address at cursor.sh, which redirects to cursor.com (read 2026-11-09), as in
  `Co-authored-by: Composer AI <assistant@cursor.sh>` of openstack-k8s-operators/octavia-operator
  [600be2b9f5c3](https://github.com/openstack-k8s-operators/octavia-operator/commit/600be2b9f5c302a24f579b0adfc15fac01d8d586).
  apache/camel
  [dccac5e0a82c](https://github.com/apache/camel/commit/dccac5e0a82cd0033de5859f44ca5fe40f44d40c),
  which its co-author line of Claude credits to Claude, credits Cursor as well through
  `Co-authored-by: Composer <noreply@cursor.com>`. `Signed-off-by: Composer <noreply@cursor.com>`
  of confidential-containers/trustee does not credit authorship, and the row does not read it.
- **A model before an address with the local part `ai`.** `tool-not-named-trailer-07` also reads
  Opus, Sonnet, Haiku or Fable alone before an address with the local part `ai`, as in
  `Co-Authored-By: haiku <ai@salesforce.com>` of the plan commits that the Claude Code workflows of
  forcedotcom/salesforcedx-vscode
  ([5d4445d8eaf7](https://github.com/forcedotcom/salesforcedx-vscode/commit/5d4445d8eaf73cfc4141303bc3262c760d8df790))
  and forcedotcom/apex-language-support
  ([1d5c86671007](https://github.com/forcedotcom/apex-language-support/commit/1d5c86671007fcf9874e3281e9a1c6390efb36e6))
  make with the model haiku. The value names a model and no tool, as
  `Opus 4.8 <noreply@anthropic.com>` does, and a commit that also names Claude keeps Claude alone.
- **Copilot after `and`.** `copilot-trailer-04` credits
  `Assisted-by: ChatGPT (OpenAI) and GitHub Copilot` of kubernetes-sigs/kubebuilder
  [053fa07f54ec](https://github.com/kubernetes-sigs/kubebuilder/commit/053fa07f54ecbe2b71f869dcaf9bf2950d5809d1)
  to Copilot, which the line names as the second of two assistants. The Claude rows do not skip a
  line that names Copilot after `and`, so `Assisted-by: Claude and Copilot` credits both.
- **The bullet of a squash list.** The trailer scope reads `* Co-authored-by: ...` without its
  bullet ([Trailer extraction](#trailer-extraction)), as in comet-ml/opik
  [60258b3c235b](https://github.com/comet-ml/opik/commit/60258b3c235bbc78483bc43867e3300278605b6a),
  ydb-platform/ydb-dotnet-sdk
  [6915221e77fc](https://github.com/ydb-platform/ydb-dotnet-sdk/commit/6915221e77fcb0a7d56bed5181e56c8ada12b9ca)
  and mitodl/ol-infrastructure
  [618edef902cc](https://github.com/mitodl/ol-infrastructure/commit/618edef902cc030e04f99494587c3d1bac5a2488).
  It also reads the co-author line with an address that ends a top-level `* ` item, as in the
  squash merges of allenai/open-instruct
  [97590011918f](https://github.com/allenai/open-instruct/commit/97590011918f562621a994eeebdc4a83a1cbb9d4)
  and
  [e91ada420e12](https://github.com/allenai/open-instruct/commit/e91ada420e124205928d840340bcd85633f9243b),
  which list the subjects of commits of Claude Code with their co-author lines beside the
  suggestions of Gemini Code Assist that a person committed.
- **pi's footer of pi-config.** The package myk-org/pi-config for pi writes the footer `---` and
  `*Assisted-by: PI (<model>)*` into the body of a pull request that pi opens when its setting
  `comment_signature` is on (`commentSignatureFooter` in
  `extensions/orchestrator/enforcement-helpers.ts` at 616b809f644c). `pi-full-body-03` reads the
  footer as `standard`, as `koan-full-body-01` reads the footer of Kōan, as in the squash merge
  RedHatQE/openshift-virtualization-tests
  [62df7854443f](https://github.com/RedHatQE/openshift-virtualization-tests/commit/62df7854443f0e9872d9d185f7c89e29ab246324).
  The trailer scope still reads no line in emphasis, and the full-body rows read that form.
- **`AI-Assisted:` and `AI-Generated:` without a tool.** The CONTRIBUTING.md of secdev/scapy at
  d2a43b525e14 requires the key on every commit: "All commits should include the
  `AI-Assisted: (yes/no) [tool]` tag." Its contributors write a model where the tool goes, as in
  `AI-Assisted: yes (GPT-5.6-Cyber)` of
  [ac0ab2e06d1c](https://github.com/secdev/scapy/commit/ac0ab2e06d1c17796a49842bd3afa49c1b793d87),
  and other projects write the response alone, as in `AI-assisted: yes` of oxc-project/oxc
  [7a2491184635](https://github.com/oxc-project/oxc/commit/7a249118463517810990b7b145b98632200a8a62),
  or the model alone, as in `AI-assisted: GPT-6.1 Sol.` of ajnart/self-host
  [83ed9a6e1550](https://github.com/ajnart/self-host/commit/83ed9a6e1550ec49829f196a4b2e3283ca842b8d).
  `AI-Generated: true` of Checkmk/checkmk
  [00afbc566cb1](https://github.com/Checkmk/checkmk/commit/00afbc566cb16217ab217979060389701e85fcaa),
  `AI-Generated: tests` of vllm-project/guidellm
  [4545d7cdbab8](https://github.com/vllm-project/guidellm/commit/4545d7cdbab8753d780fa90083259e5a840b6782)
  and `AI-Generated: tests only` of Checkmk/checkmk
  [80ec22ed9710](https://github.com/Checkmk/checkmk/commit/80ec22ed971003ec9eee8c4db84259f70bd3a6f9)
  state that AI made the change or its tests, and not only the message. A model alone credits
  `tool not named`. `tool-not-named-trailer-22` reads `AI-Assisted:` with yes or true, alone or
  with a model in parentheses or brackets, or with the model alone, a version and at most one more
  word, and `tool-not-named-trailer-23` reads `AI-Generated:` with yes, true, tests or tests only.
  A value that names a tool is left to the rows of that tool, as the example of scapy,
  `yes (Claude Opus 4.7)`, credits Claude through `claude-full-body-08`. As for the labeled
  disclosure, a value that states that the model was not used, only reviewed, or only wrote the
  message, the description or a summary is not read, and neither are `AI-Assisted: no`, a sentence
  after yes and `AI-generated :(` of dotnet/roslyn
  [07ba3e2eb388](https://github.com/dotnet/roslyn/commit/07ba3e2eb38815d32225cfc44a816536d385d09f),
  which has a space before its colon and states that a removed test had been generated.
  `yes (Co-pilot for unit tests)` of secdev/scapy
  [bef887731f21](https://github.com/secdev/scapy/commit/bef887731f2166e75e149abfc79eee2b4658c196)
  names neither GitHub Copilot nor Microsoft Copilot, occurs in one commit and is not read. The
  commits of `circleci-app[bot]` with `AI-Generated: true`, as storybookjs/storybook
  [2e0c3598e3d8](https://github.com/storybookjs/storybook/commit/2e0c3598e3d80023facbf59c9820df468c97b885),
  take the label: CircleCI's agent Chunk "needs the GitHub App to recommend fixes and open pull
  requests" ([Chunk](https://circleci.com/docs/guides/toolkit/chunk-setup-and-overview/), read
  2026-11-08), but the app also authors commits that no agent made, such as the commits titled
  `CircleCI Commit` of the configuration editor, and a trailer heuristic cannot read the author.
  Checkmk/checkmk hides the attribution of Claude Code with `"commit": ""` in
  `.claude/settings.json` (at f085f79bc830), and its commits with the key name no tool either.
- **The AI attribution statement.** The AI Attribution Toolkit (https://aiattribution.github.io/)
  composes a statement of AIA, the proportion of AI use, the AI contributions, the initiative, the
  review, the "Model or application used" and the version, abbreviated as in
  `AIA PAI Ce Hin R claude-4.6-opus-1M v1.0` or with the names of the options joined by commas
  (`getStatementText` in `src/lib/services/utils-service.ts` of
  aiattribution/aiattribution.github.io at ef98db15e90a). The README.ai.md of
  openshift-kni/numaresources-operator at 1b7404423cb3 requires it beside an `Assisted-by` line:
  "The value of the tag must include the AI assistant being used and its version. Example:
  `Assisted-by: Cursor v1.2.2`" and "The tag must be an Abbreviated Statement in the
  https://aiattribution.github.io/ format. Example:
  `AI-attribution: AIA EAI Hin R claude-4-sonnet v1.0`". The statement therefore names the
  assistant where it names an application, as `Cursor 3.1.7` does in
  [01c6e61430d5](https://github.com/openshift-kni/numaresources-operator/commit/01c6e61430d51c5a55d3b92adf1056ae376072ca),
  and only the model where it names a model, as `claude-4.6-opus-1M` does in
  [0210ab72f121](https://github.com/openshift-kni/numaresources-operator/commit/0210ab72f121d70a9cc0398364f9a6ecb198768d).
  The same repository has the model of Cursor there, in
  [95a64f6375a8](https://github.com/openshift-kni/numaresources-operator/commit/95a64f6375a80ad8449eb93f68153945845ca627)
  with `Assisted-by: Cursor v1.1.6`, `Assisted-by-model: claude-4-sonnet` and
  `AI-attribution: AIA EAI Hin R claude-4-sonnet v1.0`. A Claude model in the statement therefore
  counts under `tool not named`, as every model alone does, and not for Claude, although
  `claude-trailer-01` reads `Assisted-by: claude-4.6-opus`: the key of that trailer names the
  assistant, and the field of the statement names the model or the application. The rows read the
  statement at the start of a line, under the key `AI-attribution:` or `AIA:`, under the labels
  `AI Attribution:` and `AIA Attribution:`, and without a label, as in
  `AIA: Primarily AI, New content, Human-initiated, Reviewed, Claude Opus 4.6 v1.0` of
  [1d8492afe1aa](https://github.com/openshift-kni/numaresources-operator/commit/1d8492afe1aac92c5559e9795d3e87ba3d8be69e),
  `AI Attribution: AIA Human-AI blend, New content, Human-initiated, Reviewed, Cursor 3.0.16 v1.0`
  of [0f3dfbe561cc](https://github.com/openshift-kni/numaresources-operator/commit/0f3dfbe561cc1ac2d6eb12d87c92dfddb4acbe16)
  and `AIA Primarily AI, Content edits, Human-initiated, Reviewed, Claude Opus 4.6 v1.0` of
  [ce194e94284f](https://github.com/openshift-kni/numaresources-operator/commit/ce194e94284f80d7d30b58fe03ea57d44f04fef4),
  also wrapped onto the next line after a comma, as in konveyor/tackle2-ui
  [5fba329e3491](https://github.com/konveyor/tackle2-ui/commit/5fba329e3491fca461d7de78910da580208befbe).
  The labels with a space and the statement without a label are no trailer lines, so the rows read
  the full body. `cursor-full-body-38`, `codex-full-body-46` and `claude-full-body-58` read Cursor,
  Codex or OpenAI Codex, and Claude Code first in the field, as in
  `AI-attribution: AIA,Primarily AI-generated,Human-initiated,Reviewed,OpenAI Codex,v1.0` of
  openshift-kni/lifecycle-agent
  [631b0f34ec56](https://github.com/openshift-kni/lifecycle-agent/commit/631b0f34ec561d78cc1bc6de686d6ac6274b6783)
  and `AIA PAI SeCeNc Hin R Claude Code Web Sonnet 5 Medium v1.0` of vibecoder-1z3r0/many-game-show
  [437d3de699fc](https://github.com/vibecoder-1z3r0/many-game-show/commit/437d3de699fc2104b0de43276b8370ec6f225f03),
  which GitHub's commit search found outside the collection. `cursor-full-body-38` also reads
  Cursor after the version, as in `AIA PAI Se Hin R v1.0 Cursor 3.16.17` of
  [8f702e19dc65](https://github.com/openshift-kni/numaresources-operator/commit/8f702e19dc65f1498d89dbed343d5b3fec0ce18f),
  which has no `Assisted-by` line. Only the application is out of the order that the toolkit
  writes. No statement among these lines names Codex or Claude Code after the version, so
  `codex-full-body-46` and `claude-full-body-58` do not read that order. `tool-not-named-full-body-06` reads a
  model of OpenAI, Anthropic, xAI, Z.ai, DeepSeek, Moonshot, Alibaba or MiniMax, or Composer, which
  the decision of 2026-11-03 calls a model. A statement of entirely human work (Eh, Entirely
  human-created or No AI), a statement without a model, as `AIA EAI Ce Nc Hin Nr v1.0` of
  openshift-kni/lifecycle-agent
  [591d85cf1f72](https://github.com/openshift-kni/lifecycle-agent/commit/591d85cf1f72b3aefa2640d06b99083e9ab7a18b),
  which has `Assisted-by: OpenAI Codex` beside it, are not read.
- **`AI-Model:` and `AI-Provider-Model:` with an OpenAI model.** The CONTRIBUTING.md of
  elizaOS/eliza at b430084cb0d1 prescribes a footer with
  `AI provider/model: <provider> / <exact-model-id>` and `Client / agent tooling: <client>`, and
  its contributors also write it as trailers, as in `AI-Model: OpenAI/gpt-5.6-sol` of
  [c9fc21f7b0e3](https://github.com/elizaOS/eliza/commit/c9fc21f7b0e38ff82e624536ce35317a816ba696).
  An OpenAI model alone counts under `tool not named`, and `tool-not-named-trailer-24` reads both
  keys when the model is the whole value, with a version and at most one more word, as in
  `AI-Model: OpenAI GPT-5 Codex` of moriatz-labs/corvid
  [fff8affd95de](https://github.com/moriatz-labs/corvid/commit/fff8affd95de888c49814120dab1e866e8a60415),
  because `ai-model:` is also the scope of a conventional commit. It does not read the line when a
  `Client:` or `AI-Client:` line follows, because the client names the tool that ran the model, and
  the commit is that tool's when a row reads the client, as `Client: codex` of
  [2b861fa2bd6f](https://github.com/elizaOS/eliza/commit/2b861fa2bd6fb406efb0455a1222492c837dd865)
  credits Codex. The xAI line of the footer, `AI-provider-model: xAI / grok-4.6`, is not read: each
  commit with it names its client below it, as `Client: Cursor` does in
  [bfea19e5a7b0](https://github.com/elizaOS/eliza/commit/bfea19e5a7b03d8aa54a9f991a0b92f1b7466f45)
  and `Client: Grok CLI via cmux` in
  [2701b99ab22d](https://github.com/elizaOS/eliza/commit/2701b99ab22d8bfd2b4e794ded10d3d577c87180),
  and the label would hide the tool that the commit names.
- **Kiro under `Co-developed-by:`, `X-AI-Tool:`, `AI-Tool:` and `AI-Agent:`.** `kiro-trailer-01`
  also reads `Co-developed-by: Kiro <noreply@kiro.dev>`, which ends one author's commits of
  alibaba/nacos below a Gerrit-style `Change-Id`, as in
  [fd8bd96a5a1b](https://github.com/alibaba/nacos/commit/fd8bd96a5a1b71f509d2ead070b354b3b6409259)
  and [4df0b8a6c201](https://github.com/alibaba/nacos/commit/4df0b8a6c2017160d323f43f674525ae9f6708b6).
  The commit-msg hook of ai-coding-workshop/commit-msg writes both lines when the commit runs in
  Kiro's desktop app, which it recognizes by the bundle identifier `dev.kiro.desktop`
  ([src/ai-tools/kiro.ts](https://github.com/ai-coding-workshop/commit-msg/blob/cc558d6271b5bff780ec5ae7a76df9aea0d6ecd6/src/ai-tools/kiro.ts),
  read 2026-11-08), and `cursor-trailer-07` reads the same hook's
  `Co-developed-by: Cursor <noreply@cursor.com>`. The files that these commits add carry
  `@author kiro` and numbered properties, such as `Property 5: listPipelines filter correctness`,
  which Kiro's spec workflow writes ("In the design phase, Kiro extracts properties from your
  requirements and generates test cases.", [Kiro](https://kiro.dev/docs/specs/correctness/), read
  2026-11-08), so that the changes are Kiro's. The line is a convention, as every
  `Co-developed-by` line is, and `kiro-trailer-02`, the row of Kiro's address, does not read it.
  The repositories of AWS write `X-AI-Tool:` beside an `X-AI-Prompt:` line with the prompt that the
  tool received, as in `X-AI-Prompt: Make _PipelineExecution public per GitHub issue` above
  `X-AI-Tool: kiro-cli` of aws/sagemaker-python-sdk
  [21aa5d68c3f1](https://github.com/aws/sagemaker-python-sdk/commit/21aa5d68c3f1ef36de11fc3e34a1eeb015206943),
  and `X-AI-Tool: Human` marks a part that a person made, as in aws/deep-learning-containers
  [6dd9fa88f03b](https://github.com/aws/deep-learning-containers/commit/6dd9fa88f03b1f2a57c1df952baf2e612397e33a),
  so the key records which tool made the change. `kiro-trailer-01` reads `X-AI-Tool:` with Kiro
  first, as in `X-AI-Tool: Kiro`, `Kiro IDE` and `Kiro CLI (sisyphus)`, and `kiro-trailer-03` reads
  `AI-Tool:` and `AI-Agent:` with Kiro as the whole value, also with CLI or IDE, a version or a
  parenthesis, as in `AI-Agent: Kiro 2025-06` of rsyslog/rsyslog
  [abc26c3f1898](https://github.com/rsyslog/rsyslog/commit/abc26c3f189853d8262c6fea340537613357db14),
  the footer that the CONTRIBUTING.md of rsyslog prescribes as `AI-Agent: Codex 2025-06`, as
  `codex-trailer-13` and `codex-trailer-14` read Codex, because `AI-agent:` is also the scope of a
  conventional commit, as in `AI-agent: add context7.json (#1150)` of timeplus-io/proton
  [5556a66559a9](https://github.com/timeplus-io/proton/commit/5556a66559a9d4d2469993f16019faad61778afa).
  `X-AI-Tool: sm-rb-cr-creator` of aws/sagemaker-python-sdk
  [8e07c371273c](https://github.com/aws/sagemaker-python-sdk/commit/8e07c371273c714350f31308b8e37f037ff5ced8)
  and 62475e2bd2fc names a tool that made the change from the `X-AI-Prompt:` beside it, but
  GitHub's code search finds the name in no file and its commit search only in these two commits
  (read 2026-11-08), so no page grounds the tool. No row reads the line, and it does not take the
  label `tool not named`, because it names a tool rather than only a model or AI.
- **`X-AI-Tool:` with Claude Code or Codex.** `claude-full-body-08` and `codex-full-body-07` also
  read the key `X-AI-Tool:` as they read `AI-Tool:`, with the tool at the start of the value, as in
  `X-AI-Tool: claude-code` below
  `X-AI-Prompt: Can you fix the dropped JumpStart tolerance flags in v3 too?` of
  aws/sagemaker-python-sdk
  [3c6d362f46b1](https://github.com/aws/sagemaker-python-sdk/commit/3c6d362f46b1ef703c2a41641342d53ee319f920),
  `X-AI-Tool: Claude` of
  [61213e938b66](https://github.com/aws/sagemaker-python-sdk/commit/61213e938b667a3c41167eb2b61844ec5356265d),
  and `X-AI-Tool: codex` of gkoreli/backlog-mcp
  [92a9fc742058](https://github.com/gkoreli/backlog-mcp/commit/92a9fc742058658a6125d6aa1a89ba9b59666e1b),
  which GitHub's commit search found. GitHub's commit search finds the key with none of Copilot,
  Cursor, Gemini and OpenCode, and the rows of those tools do not read it. `claude-full-body-08`
  skips a line that names Claude and then Copilot, Codex, Cursor or OpenCode after `via` or in
  parentheses, and `cursor-full-body-39`, the row of Cursor for these openers, credits
  `X-AI-Tool: Claude Code via Cursor` to Cursor, as it credits the same line after `AI-Tool:`. A
  Claude model after the
  key is not read as Claude, unlike after `AI-Tool:`. The only document that defines the value of
  the key as a model is the write-commit-message skill of alexw91/homedir, a repository outside the
  collection, which prescribes `X-AI-Tool: <AI model name>` and "the model name exactly as your
  session context reports it (e.g. `Claude Opus 5.5`)"
  ([SKILL.md at 683d662c29a5](https://github.com/alexw91/homedir/blob/683d662c29a59dc2b7288a5b387f2f47b914b56a/.kiro/skills/write-commit-message/SKILL.md),
  read 2026-11-09). `X-AI-Tool: Claude Opus 5.5` of
  [683d662c29a5](https://github.com/alexw91/homedir/commit/683d662c29a59dc2b7288a5b387f2f47b914b56a)
  and [3f6328986b51](https://github.com/alexw91/homedir/commit/3f6328986b510486c2343c39ec0c96aff068bdec),
  which change only files under `.kiro/`, therefore names a model and not a product, as
  `Impl-Model: claude-opus-5.5` does. `tool-not-named-trailer-27` counts a Claude model as the
  whole value of the key under `tool not named`.
- **Hermes Agent under `AI-Tool:` and `AI-Agent:`.** `hermes-agent-trailer-04` reads `AI-Tool:` and
  `AI-Agent:` with Hermes Agent as the whole value, as `codex-trailer-13` and `codex-trailer-14`
  read the keys for Codex. Examples are `AI-Tool: Hermes Agent (DeepSeek V4.1 Flash)` of
  JurajNyiri/HomeAssistant-Tapo-Control
  [5fdd5dbe7153](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/commit/5fdd5dbe7153c6f5cda9fd33b74db3ade946b8e5),
  above `AI-Contribution: located the resolution order and drafted the block move`, and
  `AI-Agent: hermes-agent` of edtechdev/aied
  [a8f9ac9d16c3](https://github.com/edtechdev/aied/commit/a8f9ac9d16c3150da11817c391e002df55482915),
  which GitHub's commit search found. A value that goes on after the name is not read, and the
  keys are a row of their own, because `hermes-agent-trailer-01` reads Hermes Agent first in a
  value of any length. GitHub's commit search finds no `X-AI-Tool:` with Hermes Agent, and the row
  does not read that key.
- **`AI-Assisted: yes` with Gemini, pi or ZCode.** The tag that secdev/scapy requires of every
  commit names the tool after `yes`. `gemini-unspecified-trailer-04` reads Gemini first in the
  parentheses or brackets after `yes` under `Gemini (unspecified)`, as in
  `AI-Assisted: yes (Gemini 3.8 Flash)` of
  [94a853bf59b4](https://github.com/secdev/scapy/commit/94a853bf59b43e75848716736a24e51064202e1a)
  and [f5efe231c587](https://github.com/secdev/scapy/commit/f5efe231c5872bf2bbd20d4950fd67c81e24eae6),
  and a line that names Gemini CLI, Gemini Code Assist or Antigravity, or Copilot, Codex, Cursor or
  OpenCode as the tool that ran the model, is that tool's. `pi-full-body-01` also reads `yes` before
  pi, as in `AI-Assisted: yes [pi]` of
  [1c477c45d27e](https://github.com/secdev/scapy/commit/1c477c45d27e837d7dc25316f43c6788ea6cb25d)
  and [b38b2e339e17](https://github.com/secdev/scapy/commit/b38b2e339e177cd19dc488814586db64f0053905),
  and not pi in parentheses after `yes`, because a parenthesis after `yes` can name Raspberry Pi
  hardware. `zcode-full-body-01` reads the labeled disclosure of `openclaw-full-body-05` with ZCode
  after the label, as in `AI-Assisted: yes (ZCode CLI, GLM model)` of
  [937b60992419](https://github.com/secdev/scapy/commit/937b60992419f69bf68374d3c9bd905f1a418f97),
  and not the value `no` right after the label. The disclosure names whatever tool was used, and
  the row reads it for ZCode as the rows of the six read it for their tools, so that
  `zcode-trailer-01` keeps the trailers. `AI-Assisted: no` is read by no row.
- **`AI: Codex`.** `codex-trailer-13` also reads the key `AI:` with Codex as the whole value, the
  last line of the commits of 2026-09-14 of rsyslog/rsyslog, such as
  [5c682b26c491](https://github.com/rsyslog/rsyslog/commit/5c682b26c49196a2f52900e0e79dccb1c8863adc)
  and [573b6cdee06e](https://github.com/rsyslog/rsyslog/commit/573b6cdee06ecce532a4ee991743df2d7b81f510).
  These commits follow the commit guidelines for AI agents of its CONTRIBUTING.md at 2f4ebd9a99d6:
  "If you use an AI agent (e.g. GitHub Copilot, ChatGPT, Codex), include a commit footer tag:
  AI-Agent: Codex 2025-06". The whole value leaves out the subjects `AI: Ignore codex file` of
  Nuitka/Nuitka a533fe1cd7ea and `ai: add codex skill` of bpowers/simlin b5d4b5d3bc13.
  `AI: GPT 5.2 Codex` of apmckinlay/gsuneido 947da773a771 names a model in one commit and is read
  by no row.
- **A disclosure that names AI after a label.** `tool-not-named-full-body-01` also reads the
  labels of the labeled disclosure before the disclosure, also in bold. Before the verb it reads
  `This PR was`, `This pull request was`, `This change was`, `This commit was`, `This patch was`,
  `This contribution was` or `This fix was`, also with `The`, and after the disclosure a clause
  that follows a period, a semicolon or a comma. Examples are
  `AI-assisted: This change was developed with AI assistance.` of k3s-io/k3s
  [43c121791c61](https://github.com/k3s-io/k3s/commit/43c121791c61988ac1e7969f2d252d2c2a6828d7),
  `AI-assisted: this PR was prepared with AI assistance; the contributor` of oxc-project/oxc
  [9a2788b3ae60](https://github.com/oxc-project/oxc/commit/9a2788b3ae60c033a969fbba63733748f8c6130a)
  and `AI Assistance: This change was developed with AI assistance. I fully` of envoyproxy/envoy
  [cb9848bce05e](https://github.com/envoyproxy/envoy/commit/cb9848bce05ea6d26a0c1371871977940eecf7c4).
  A tool in parentheses, after a colon or after from, by, via or using stays with the rows of that
  tool, and a disclosure of the description alone and a negation are not read.
- **A pull request that an AI agent prepared or generated.** `tool-not-named-full-body-04` also
  reads `PR` for pull request, `prepared` for authored, `an AI agent` or `an AI coding agent` for a
  coding agent, and the label `AI-generated:` or `Note:` before the sentence, and `generated` for
  authored only after the label `Note:`. Examples are
  `AI-generated: this PR was prepared by an AI agent (with human review before submission), per the repository's AI contribution guidelines.`
  of meta/pyrefly
  [3783f5c8433d](https://github.com/meta/pyrefly/commit/3783f5c8433dacaa6c8c12550f75613ad83e415a),
  which one contributor writes under its AI_POLICY.md ("Any LLM-generated text must be clearly
  marked"), `This PR was authored by an AI agent, directed by @person.` of pulumi/pulumi
  [6990661fa1bd](https://github.com/pulumi/pulumi/commit/6990661fa1bd77590fa88064c4c7f2229ad0c1a8),
  and `Note: this PR was generated by an AI coding agent and reviewed by the author.`, which closes
  the descriptions of the pull requests that one contributor opened in duckdb/duckdb, as the merge
  commits [325a2a3c76a0](https://github.com/duckdb/duckdb/commit/325a2a3c76a0d4ad683d1d4f514f335c93b6e53e)
  and [09456f1ce0ff](https://github.com/duckdb/duckdb/commit/09456f1ce0ff74b6a3a4ff46777b4c3a6d0b9812)
  copy them. Each line names an AI agent as the maker of the pull request and no tool, as marimo's
  disclosure does. A disclosure of the description of a pull request, an unticked box and a
  sentence that does not begin the line are not read. Without the label,
  `This pull request was generated by an AI coding agent.` and its form with `PR` are the footer of
  Overseer, the agent of GoogleCloudPlatform/k8s-config-connector, above the link of its agent
  session at `staging.repoagent.dev/#/overseers/kcc`, as in
  [0e5f156063bd](https://github.com/GoogleCloudPlatform/k8s-config-connector/commit/0e5f156063bdb4baf463874ee1075d14ea54d85b)
  and [d36b5b8b6d08](https://github.com/GoogleCloudPlatform/k8s-config-connector/commit/d36b5b8b6d08abf5bcb44058a83a5225e47c55b3).
  The row does not read the footer, because the label resolution mn-0114 reads the credit of
  Overseer as the repository's own agent
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
  It also leaves out
  `> This PR was generated by an AI coding agent (Jetski) on behalf of @person.` of dart-lang/http
  [05d0b18d7c39](https://github.com/dart-lang/http/commit/05d0b18d7c396d7c25c1d2e9256e33b1e65cd0b4),
  which names Jetski, a tool that stays out until a vendor page grounds it.
- **A note that an AI agent (Gemini) created the change.** `gemini-unspecified-full-body-09` reads
  `*Note: Created by an AI agent (Gemini).*` and `*Note: Updated by an AI agent (Gemini).*`, with
  which one developer of flutter/flutter-intellij ends the descriptions of pull requests that the
  squash merges copy, as in
  [1991e6a0bca6](https://github.com/flutter/flutter-intellij/commit/1991e6a0bca68f2bf487d3ba87c6d9e47990e8a8),
  [cd7cda33fc4c](https://github.com/flutter/flutter-intellij/commit/cd7cda33fc4c10e44e2bc72732c568e43340ec75),
  [73cbb3c50c64](https://github.com/flutter/flutter-intellij/commit/73cbb3c50c64c341323f8f4bf72213ae80f88f2b)
  and
  [77b65e4e8d06](https://github.com/flutter/flutter-intellij/commit/77b65e4e8d06d4779ed5cd8884d7d272bc18a625).
  The agent made the change and not only the description: In each of these pull requests, the
  first commit was authored seconds before the pull request was opened with the note, as
  0277ce6997 at 2026-10-21T19:58:10Z before
  [pull request 9168](https://github.com/flutter/flutter-intellij/pull/9168) at 19:58:33Z and
  3e4c5c0f7e at 2026-10-29T17:03:11Z before
  [pull request 9194](https://github.com/flutter/flutter-intellij/pull/9194) at 17:03:50Z. Pull
  request 9159 was opened with the `Created` note, and a later edit of its description wrote the
  `Updated` note (GitHub's API, read 2026-11-09). The line is the form of `claude-full-body-48`
  and `codex-full-body-05` with Gemini alone, which names no product
  ([Gemini naming no product](#gemini-naming-no-product-2026-09-27)), as
  `gemini-unspecified-full-body-05` reads the sentence of envoyproxy/envoy with Gemini alone. A
  parenthesis that names Gemini CLI stays with the Gemini rows, and
  `Description created by an AI agent (Gemini).` and a quote nested in a list item are not read.
- **The session footer of ClipboardHealth/core-utils.** `codex-full-body-47` reads the line with
  which the shipping skills of ClipboardHealth/core-utils name the session of Codex that committed
  the change and opened the pull request, which the squash merge copies from the description of the
  pull request, as ``Agent session: `codex resume <session>` ·`` with the id of the session, above
  ``Model: `gpt-6-1-sol` · Reasoning: `medium` `` of
  [bafb436cca4d](https://github.com/ClipboardHealth/core-utils/commit/bafb436cca4df1a4799de798ff7e6c4eb57a0c6f),
  and the first form `Agent session ID: codex <session>` of
  [7e06be3733eb](https://github.com/ClipboardHealth/core-utils/commit/7e06be3733eba226ad99ba7dc29133614f468afb).
  `claude-full-body-59` reads the same line for Claude Code, as
  ``Agent session: `claude --resume <session>` ·`` of
  [7eeb95cc13bd](https://github.com/ClipboardHealth/core-utils/commit/7eeb95cc13bd16f9f925894869557d88258ab192)
  and `Agent session ID: claude-code <session>` of
  [e397fcd566b7](https://github.com/ClipboardHealth/core-utils/commit/e397fcd566b717a20b759aceb18f616f6f233972).
  The skill commit-push-pr at
  [5df5a23c34c8](https://github.com/ClipboardHealth/core-utils/blob/5df5a23c34c8e358a5adaaada3253d62938e4f38/plugins/core/skills/commit-push-pr/SKILL.md)
  tells the agent to "Append `Agent session ID: <output of step 5>`", and its script prints `codex`
  with `$CODEX_THREAD_ID` for Codex and `claude-code` with the name of the transcript of Claude
  Code. The skill and its successor cb-ship later prescribe
  ``Agent session: `codex resume <id>` `` and ``Agent session: `claude --resume <id>` ``. Each
  skill commits the change in the session that the line names, so the line credits the tool that
  made the commit. Both rows require the id, so the placeholder of a message that changes the skill
  is not read, and neither is a bare id, as
  ``Agent session: `<session>` `` with the id alone, of 9c9a376c0dc5. Both rows read the
  full body, because the key contains a space, and both are conventions, because the repository's
  skill writes the line.
- **`Agent: ChatGPT`.** `tool-not-named-trailer-25` reads ChatGPT as the whole value of an `Agent:`
  or `AI-Agent:` trailer, also with a model suffix or a date, as `Agent: ChatGPT` above
  `Model: GPT-5.6 Sol` of deepmodeling/deepmd-kit
  [025f4f698d4f](https://github.com/deepmodeling/deepmd-kit/commit/025f4f698d4ff50c0b5538f19dc0b00ad1459ba3)
  and deepmodeling/dpgen
  [4f306808e073](https://github.com/deepmodeling/dpgen/commit/4f306808e0730386e334d658cfe2b62a95ebbb75),
  commits of the bot account of a maintainer from branches of its fork. ChatGPT names no coding
  agent ([A trailer that names no tool, 2026-09-26](#a-trailer-that-names-no-tool-2026-09-26)). The
  row also reads `AI-Agent: ChatGPT` of rsyslog/rsyslog
  [ba32958f09cb](https://github.com/rsyslog/rsyslog/commit/ba32958f09cb92bed5bde72052015b86e2db42ac),
  the footer that rsyslog's CONTRIBUTING.md prescribes for "an AI agent (e.g. GitHub Copilot,
  ChatGPT, Codex)". Every commit of rsyslog/rsyslog with the line that the row reads is the squash
  merge of a pull request from a `codex/` head branch, as ba32958f09cb merges pull request 6365
  from `codex/refactor-rsyslog-module-parameter-documentation-ecok1d` and
  [0c3529359e64](https://github.com/rsyslog/rsyslog/commit/0c3529359e64359784f8fa742cb7496dadd3509b),
  with `AI-Agent: ChatGPT-4o`, merges pull request 6132 from
  `codex/update-documentation-for-regex-type-lookup-tables`. rsyslog's commits with
  `AI-Agent: Codex` come from the same branches, as
  [e5791212a204](https://github.com/rsyslog/rsyslog/commit/e5791212a204dd94536951280115c5347710901b)
  merges pull request 5839 from `codex/add-github-action-for-ci-doc-build`, and its
  [AGENTS.md](https://github.com/rsyslog/rsyslog/blob/8b65504159c57a4cdef1ed94d2da1aaca6e2dbf9/AGENTS.md)
  at 8b65504159c5 tells an AI agent to "Include a **commit footer tag** like "AI-Agent: Codex"". The
  same holds for `AI-Assisted: yes` of
  [621ee6063c1b](https://github.com/rsyslog/rsyslog/commit/621ee6063c1b7c4ecef57c4a75a7a976f2e097be),
  which merges pull request 6908 from `codex/fix-imfile-stress-cluster` and which
  `tool-not-named-trailer-22` reads. These credits are lost to Codex by the reading rule and not in
  fact, and no row can recover them from the message. `AI-Agent: ChatGPT` of rsyslog/rsyslog does
  not count for Codex, by the decision of 2026-11-09 ([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)). The value has to end after the
  name, because `agent:` is also the scope of a conventional commit, and a hyphen list item such as
  `- AI-Agent: ChatGPT` of rsyslog/rsyslog 9e55d219b597 is no trailer.
  `tool-not-named-full-body-09` reads that commit through rsyslog's footer.
- **`Agent: codex/<actor>`.** `codex-trailer-13` also reads the name of an actor after a slash, as
  `Agent: codex/root` of kungfu-systems/kungfu
  [41a2f504e46f](https://github.com/kungfu-systems/kungfu/commit/41a2f504e46f0e34fcb604593f1f552331f8a526),
  which names its agents as actors such as `codex/pro-9598` in its records, and `Agent: codex/dev`
  of amirhoseinsalimi/music-tool-bot
  [01d322ac7734](https://github.com/amirhoseinsalimi/music-tool-bot/commit/01d322ac773497c3177e6e96001f1de064d3f80e)
  beside `Codex-Session:` lines. `codex/dev` is no model's name, so the near miss of 2026-10-29 is
  read now, and `Agent: codex-1` of loro-dev/loro stays unread. The name after the slash has to
  start with a letter and end the line, so a subject such as `agent: codex/sandbox fix` is not read.
  A model after the slash is not read either, as in `Agent: Codex/GPT-6` of spec-kitty/spec-kitty
  77ff9ae2d642, and neither is a slash after `openai-codex`, because `openai-codex/<model>` is how
  pi and OpenCode name a provider and a model (the limit "A slash after the tool's name" of
  [The attributions that no heuristic matched in the September collection](#the-attributions-that-no-heuristic-matched-in-the-september-collection-read-2026-09-25)),
  as in `Agent: openai-codex/gpt-5.6-sol` of corosolto/client
  [00e746b5ad91](https://github.com/corosolto/client/commit/00e746b5ad91526d498060c07811c5e36e58cf31)
  beside `Agent: OpenAI Codex (GPT-5.6)`, which the row reads.
- **`Agent: GitHub Copilot`.** `copilot-trailer-17` reads `Agent:` with GitHub Copilot as the whole
  value, also with a model after it, as `Agent: GitHub Copilot GPT-5.4` of ticketz-oss/ticketz
  [abee59b782ae](https://github.com/ticketz-oss/ticketz/commit/abee59b782ae99d50b375ed9de8ee0aa88663380),
  the form that `codex-trailer-13` reads for Codex. A slash directly after the name is not read,
  because `github-copilot/<model>` is how OpenCode names a provider and a model.
- **`Agent:` and `Tool:` with Claude Code.** `claude-trailer-33` reads `Agent:` and `Tool:`, also
  as `AI-Agent:` and `AI-Tool:`, with Claude Code or a Claude model as the whole value, as
  `Tool: Claude Code v1.2 (claude-sonnet-4-5-20250929)` of inklewriter/freeinklewriter
  [9c1187647c82](https://github.com/inklewriter/freeinklewriter/commit/9c1187647c825ab86c14517ecbadb65240cda5b9)
  and `Agent: Claude Code (claude-opus-5-5)` of software-mansion/react-native-audio-api
  [3745d6776674](https://github.com/software-mansion/react-native-audio-api/commit/3745d6776674beba82282eb9c17c494598ae2b98),
  the forms that `codex-trailer-13` and `codex-trailer-14` read for Codex. It skips a line that
  names another agent after `via` or in parentheses, as the other Claude rows do, and a value that
  goes on, because `agent:` and `tool:` are also scopes of a conventional commit. No commit of the
  collection writes these keys for Cursor, Gemini or OpenCode
  ([Conventions considered and not adopted](#conventions-considered-and-not-adopted)).
- **The bold label `**Tool:**`.** `claude-full-body-38` also reads the bold label `**Tool:**`, as
  `- **Tool:** Claude Code` in the AI Provenance section of the pull request template of
  feldroy/air, in
  [db80a6907486](https://github.com/feldroy/air/commit/db80a69074863a6e31bbd1683e84f998ed027f41),
  and the AI disclosures of Netflix/metaflow and WordPress/wordpress-playground write the same
  label. The template is tool-agnostic, and its label is matched for a tool once a commit shows it
  for that tool: The commit logs of the collection and GitHub's commit search, read 2026-11-08,
  show it with Claude Code only. The row skips a line that names another agent after `via` or in
  parentheses, as the other Claude rows do.
- **The agent-tool lines of schickling-assistant.** The account schickling-assistant, which its
  GitHub profile describes as the AI assistant of a maintainer, writes the lines `agent-identity`,
  `agent-tool`, `agent-runtime` and `agent-model` into its commits. `oh-my-pi-trailer-02` reads
  `agent-tool: OMP`, as in livestorejs/livestore
  [959a5368ef36](https://github.com/livestorejs/livestore/commit/959a5368ef36821b7108a8d1f8cf7e8abd6ce7a1)
  with `agent-runtime: OMP 18.12.5` and `agent-model: openai-codex/gpt-6.1-sol`, where 18.12.5 is
  a release of can1357/oh-my-pi and openai-codex one of its providers. Under this key OMP names the
  agent, and the row requires the whole value, because `omp` elsewhere names OpenMP or oh-my-posh
  ([The credits, read 2026-11-05](#the-credits-read-2026-11-05)). The model line credits nothing
  of its own, because oh-my-pi ran the model. The row also adds oh-my-pi to cachix/devenv
  [0b7e9055a961](https://github.com/cachix/devenv/commit/0b7e9055a96160091bc1b8dcea6e6771fcfd1a62),
  which `codex-trailer-01` credits to Codex through `Assisted-by: Codex (GPT-6.1)`.
  `tool-not-named-trailer-26` reads `agent-tool: unknown`, the line with which the account states
  that it does not know the tool that made the commit, as in
  [06c2668b9261](https://github.com/livestorejs/livestore/commit/06c2668b9261600cb1987c9b3df13c7a96c29cdc),
  beside `agent-runtime: unknown` and `agent-model: unknown`, as a row of its own beside
  `tool-not-named-trailer-18`, which reads Unknown under the keys of moeru-ai/airi.
- **`agent-tool: <copilot-chat>`.** `copilot-trailer-11` also reads one value in the angle
  brackets of the template of the pr-telemetry block of
  AzureAD/microsoft-authentication-library-for-js, as `agent-tool: <copilot-chat>` of
  [f8043926016a](https://github.com/AzureAD/microsoft-authentication-library-for-js/commit/f8043926016a9de007aeef2f3a2c301cec1ea7fe).
  The template line `agent-tool: <copilot-cli|copilot-chat|claude-cli|cursor|other|n/a>` names the
  tools as options and credits none of them, as a box that names tools as options credits none
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)),
  so the brackets have to close after one value.
- **Commonly's adapter claude.** `claude-trailer-34` reads the adapter `claude` of Commonly's
  `Agent-Model:` line, as `Agent-Model: claude/claude-haiku-5-5/high` of Team-Commonly/commonly
  [768f66deffbc](https://github.com/Team-Commonly/commonly/commit/768f66deffbcf45bfe4e03bf8c481ae30ca4e290),
  the counterpart of `codex-trailer-19`. The hook adds the line with `--if-exists=addIfDifferent`,
  so a seat under the adapter claude that rewrites the commits of a seat under the adapter codex,
  as in a rebase, adds its line below theirs, and such a squash merge credits Claude and Codex
  without a co-author line of Claude Code. Both rows are conventions, because the code of Commonly
  writes the line.
- **Agent Kanban's `Agent-Profile:`.** `agent-kanban-trailer-02` reads
  `Agent-Profile: https://agent-kanban.dev/agents/<id>`, the trailer with which Agent Kanban's
  agents end every commit message. The system prompt that
  `packages/cli/src/agent/systemPrompt.ts` of saltbo/agent-kanban writes for each agent session
  states "Every commit message must end with this trailer (after a blank line):" above
  `Agent-Profile: https://agent-kanban.dev/agents/${agent.id}` (read 2026-11-08), and the
  platform's skill prescribes the trailer as well. The line credits the agents' own commits at
  mails.agent-kanban.dev in saltbo/zpan, as
  [d7454a61fba3](https://github.com/saltbo/zpan/commit/d7454a61fba3dfd03f9ebebc5bb9bb881b1c03b6)
  does, and the squash merges of their pull requests, which keep the line of each listed commit and
  have no co-author line of the agent, as
  [232ad896c30f](https://github.com/saltbo/zpan/commit/232ad896c30fe3bf8d296399f02971b9af2b512f)
  does. It is `standard`, because the platform's code writes the instruction, as for Code
  Factory's line (`code-factory-trailer-01`). It records the platform and not the agent that ran,
  as for Multica. The row needs an id after `/agents/`, so the placeholders `{agent_id}` and
  `$AK_AGENT_ID` of the skill stay unread.
- **aider's chat.** `aider-full-body-01` reads `# Aider chat conversation:` with a `USER:` or
  `ASSISTANT:` line below it. Until 2024-06-11, aider wrote this header and the chat of the session
  into each commit of the files that it edited, as in poteat/hkt-toolbelt
  [9bbd01b61b73](https://github.com/poteat/hkt-toolbelt/commit/9bbd01b61b73b884cf919eb347dac7b061f2b2a1)
  and [16ca57bb0987](https://github.com/poteat/hkt-toolbelt/commit/16ca57bb0987329a2c0fa3278efc46c1f1a3144d).
  In `aider/repo.py` at v0.30.0 the header follows `if context:`, and only `auto_commit` passes the
  chat. aider v0.10.0 wrote the header on every commit, including the commit of a person's
  uncommitted changes and `/commit`, with nothing below it, as in Aider-AI/aider
  [750f8a45c644](https://github.com/Aider-AI/aider/commit/750f8a45c644e073052149f90286ed0bfbbacf93),
  so the row needs the chat below the header. The subject prefix `aider:` stays unread, because
  aider's page on git states that `--attribute-commit-message-committer` prefixes all commit
  messages "regardless of whether aider authored the changes or not". The row is `standard`,
  because aider's code writes the header.
- **`Harness:` with pi or Codex, and `Client:` with Codex or Cursor.** `pi-trailer-08` reads the
  plain key `Harness:` with pi as the whole value of the line, as in the dispatch items
  `Status: failed`, `Model: kimi-oai/kimi-k3:xhigh` and `Harness: pi` that the squash merges of
  learningeconomy/LearnCard
  [6c27d772ae41](https://github.com/learningeconomy/LearnCard/commit/6c27d772ae41ccbd8fdac6f9d40f93ed22569416),
  [092b1d36edff](https://github.com/learningeconomy/LearnCard/commit/092b1d36edff06595137715c971696fd32cad796)
  and [80d2ebf54bb5](https://github.com/learningeconomy/LearnCard/commit/80d2ebf54bb5a643808f8f7d908cf68758903dce)
  list. A developer's dispatcher writes the lines under the developer's identity into a commit
  with the agent's change, and the model lines have the form `provider/id:thinking` that pi's
  `--model` accepts. The line therefore names pi as the harness, as `Helix-Harness: pi` does, and
  credits pi as a convention. The value has to end the line, because `harness:` is also the scope
  of a conventional commit, as in `harness: add support for additional reporters` of
  coreos/coreos-assembler f1cf70829fb8. `codex-trailer-14` also reads the keys `Harness:`,
  `Client:` and `AI-Client:` with Codex as the whole value, also with `multi-agent` after the
  product, as in `Client: codex` below `AI-provider-model: OpenAI / gpt-5.6-sol` of elizaOS/eliza
  [7e2338f58c8e](https://github.com/elizaOS/eliza/commit/7e2338f58c8e3c5d21c26d207313c23e8956cac6),
  `Client: Codex desktop / multi-agent` in the squash merge
  [4733ec9170cb](https://github.com/elizaOS/eliza/commit/4733ec9170cbd7595a0b8471ff023eb5fea3ca3a),
  and `Harness: codex` between `Legatus: dick` and `Host: tom-nook` in the squash merge
  tsugiproject/tsugi
  [e293e9d97c81](https://github.com/tsugiproject/tsugi/commit/e293e9d97c81e4bcdac2b92afd56d129a916ca36),
  whose commit 064c0ae977bd of pull request 298 has the same lines. The key `Harness:` names the
  agent harness that ran the model, as it does under `pi-trailer-08`, and GitHub's commit search
  finds the key with Codex in forms that the row does not read, such as
  `Creditia-Harness: codex` of WroughtMind/creditia
  [0bb633aa70fa](https://github.com/WroughtMind/creditia/commit/0bb633aa70fab5cfa7154d0964f6b32af0026f64)
  and `AI Harness: Codex (GPT-6)` of jonathanong/no-mistakes
  [5520a1b06881](https://github.com/jonathanong/no-mistakes/commit/5520a1b0688195476b19aa20dee0f3d18179020d).
  `multi-agent` names Codex desktop in its multi-agent mode, and the whole-value rule guards only
  against conventional-commit scopes. `cursor-trailer-18` reads `Client: Cursor` with Cursor as
  the whole value, as in `AI-provider-model: xAI / grok-4.6` above `Client: Cursor` in the squash
  merges [bfea19e5a7b0](https://github.com/elizaOS/eliza/commit/bfea19e5a7b03d8aa54a9f991a0b92f1b7466f45)
  and [03a217f2eb37](https://github.com/elizaOS/eliza/commit/03a217f2eb37d7f02bfbb7692e1573b57bbe8390).
  Pull request 22006 of bfea19e5a7b0 states `Agent tooling: Cursor`, and the commits with that line
  that another heuristic credits carry Cursor's own co-author line at `cursoragent@cursor.com`. The
  value has to be Cursor alone, because `client:` is also the scope of a conventional commit, as in
  `Client: change cursor using node action information of pathfinder` of vcmi/vcmi 160fa3825448.
- **A list item labeled Harness, Tool or Agent.** `codex-full-body-48` reads a list item labeled
  `Harness:`, `Tool:` or `Agent:`, also in bold, with Codex as its whole value, as in
  `- Harness: Codex desktop` below
  `AI was used to prepare these changes. Human-reviewed by @person.` in swc-project/swc
  [47394b851f73](https://github.com/swc-project/swc/commit/47394b851f73489672016f30a24806f6edd74946),
  and `- Tool: OpenAI Codex.` under `## AI disclosure` of BasedHardware/omi 476feab2d745. The
  trailer scope reads a list item only behind the bullet `* ` at the start of a line
  ([Trailer extraction](#trailer-extraction)), as in `* Tool: Codex`. `codex-trailer-13` and
  `codex-trailer-14` therefore miss the same key and value behind a hyphen bullet or an indented
  bullet, with the label in bold, or with a period after the value. `copilot-full-body-43` reads the same list item for Copilot, with
  Copilot, GitHub Copilot, Copilot CLI or Copilot Chat as the whole value, as in
  `  - Tool: GitHub Copilot CLI.` above
  `  - Use: implementation, conflict resolution, local validation, and code review.` under the
  ticked box of the AI disclosure of documentdb/documentdb
  [8945225a1bda](https://github.com/documentdb/documentdb/commit/8945225a1bda7cd2ae0befb3eb14a2a0f8a528f9).
  The repository's template requests this disclosure as `**Tool(s) used:**`. A longer value stays
  unread, as in `  - Tool: GitHub Copilot Business, Claude Sonnet 4.6, agent mode` of
  scgrambow/berd-template-field-test-01 20b6756b1286, which lists the content of a disclosure
  section that the commit adds. The row does not read the `Use:` line, as `copilot-full-body-10`
  reads its labels whatever line follows.
- **The provenance footer of elizaOS/eliza.** The section Contribution Provenance of the
  CONTRIBUTING.md of elizaOS/eliza (develop at a319cd00b668, read 2026-11-08) prescribes the footer
  `AI provider/model: <provider> / <exact-model-id>` above `Client / agent tooling: <client>`. The
  repository's skill contribute-to-eliza has the agent end the bodies that it writes with this
  footer, and contributors also write it in trailer form, `AI-provider-model:` above `Client:`. The
  client line names the agent tooling that made the change. `codex-full-body-49` reads the footer
  with Codex as the whole value, also with `multi-agent` after the product, as in
  `Client / agent tooling: Codex desktop` of
  [1db15f11360a](https://github.com/elizaOS/eliza/commit/1db15f11360a894417fa0558e70c10e641fec493)
  and `Client / agent tooling: Codex` of
  [5cb54c90e384](https://github.com/elizaOS/eliza/commit/5cb54c90e3849604dee0bde8bd18af7c33627383),
  and `claude-full-body-60` with Claude Code, as in `Client / agent tooling: Claude Code` of
  [03bb824ab6d1](https://github.com/elizaOS/eliza/commit/03bb824ab6d158a646db70c008ec6cc49ad2f7ae).
  `grok-build-full-body-01` reads Grok CLI or Grok Build as the client, in either form of the key,
  as in `Client: Grok CLI via cmux` of
  [2701b99ab22d](https://github.com/elizaOS/eliza/commit/2701b99ab22d8bfd2b4e794ded10d3d577c87180)
  and `Client: Grok CLI` of
  [16383276b93c](https://github.com/elizaOS/eliza/commit/16383276b93c47f6a5966b052cd4f71bd910f785),
  below `AI-provider-model: xAI / grok-4.6`, and credits Grok Build and not superagent-ai/grok-cli:
  xAI's installer of Grok Build names itself `Grok CLI installer` at x.ai/cli, the same author
  wrote `Agent tooling: Grok Build` for the same model in pull request 20672, and the contribution
  skill that the pull requests cite names Grok Build as a client. cmux, the terminal after `via`,
  is not the tool. `grok-bot-full-body-01` reads Grok Bot as the client, also as Grok Bot.app, and
  `codebuff-full-body-01` Freebuff CLI or Codebuff ([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)). `hermes-agent-full-body-02` reads Hermes Agent as the client, as in
  `Client / agent tooling: Hermes Agent` of
  [57a917aee09b](https://github.com/elizaOS/eliza/commit/57a917aee09be44174d7ddccd1bf56e72ed156a4)
  and `Client: Hermes Agent` of
  [464a5398a6c1](https://github.com/elizaOS/eliza/commit/464a5398a6c17189ddca01ef7e4dd3a6657eb251).
  `gemini-full-body-34` reads Antigravity as the client, also as Google Antigravity and with CLI or
  IDE, in either form of the key, as in `AI provider/model: google / gemini-3.6-flash` above
  `Client / agent tooling: Antigravity CLI` in the first item of the squash merge
  [aae992246689](https://github.com/elizaOS/eliza/commit/aae9922466894e1eb299c1e23e75e3265f7e25c6),
  which other heuristics credit to Claude. Antigravity CLI is Google's successor to Gemini CLI, and
  a credit of Antigravity counts under Gemini, as the paragraph Gemini includes Antigravity of
  [Configuration artifact detection](heuristics.md#configuration-artifact-detection) states.
  The rows have the scope full_body because the trailer scope does not read a line with a space
  before its colon. `Client / agent tooling: Codex Desktop multi-agent review` of 713212431758 names
  a review and stays unread. `Client: grok` of 49b173b4dc5e and `Client / agent tooling: Grok` of
  a later item of aae992246689 name Grok alone, which can be Grok Build, Grok Bot or the model, and
  `Client / agent tooling: Hermes` names Hermes alone, so these lines stay unread.
- **AI with the tool in parentheses before `was used`.** `codex-full-body-36` also reads AI with
  the tool in parentheses, as in
  `Note: AI (Codex 5.6) was used to generate this set of PRs; I have` of envoyproxy/envoy
  [7791f7f3d35f](https://github.com/envoyproxy/envoy/commit/7791f7f3d35fb7487b9c651740746f3a4f1be05d)
  and [146da5578cd4](https://github.com/envoyproxy/envoy/commit/146da5578cd4d3cbbdd176445df2aa40a2cbcb83).
  Its parenthesis form leaves out a line about the summary or the commit message, as its first
  form does. The other five rows of the form keep their form. No line of the collection writes AI
  before another tool in parentheses, and `AI (Claude) was used to assist with root cause
  investigation`, which GitHub's search finds, would credit an investigation.
- **`Coding-Harness: claude-code`.** `claude-trailer-31` also reads
  `Coding-Harness: claude-code@<version>`, which SHIP, the agent platform of LetsShip, writes below
  the commits of its builder agent. Examples are elegantapp/pwa-asset-generator
  [3848b060fe49](https://github.com/elegantapp/pwa-asset-generator/commit/3848b060fe4934182debd96f356d1e2db240c95f)
  with `Model: claude-sonnet-5` and
  [a24d8fb1a826](https://github.com/elegantapp/pwa-asset-generator/commit/a24d8fb1a826249dea16f4a0e796a0ef74b6938e)
  with `Model: claude-opus-5`, squash merges authored by `letsship-staging[bot]`. The repository's
  ship.yml configures the builder with `harness: claude-code`, and letsship.ai describes the
  builder as "Writes the code in a sandbox." The line names the harness that ran the agent, as
  `Helix-Harness: claude-code` does, and credits Claude as a convention.
- **`Generated-by` with a space before the colon.** The six rows of a mistyped or spaced key also
  read `Generated-by` with a space before the colon, as in `Generated-by : Cursor/Claude` of
  kubernetes-sigs/kubebuilder
  [249064d3be5d](https://github.com/kubernetes-sigs/kubebuilder/commit/249064d3be5dd33f0e8c3c2559a6d5029b7358eb),
  which credits Cursor. Its author writes `Generated-by: Cursor/Claude` without the space in
  [9d309e19bc51](https://github.com/kubernetes-sigs/kubebuilder/commit/9d309e19bc51330091fdb86f0807efb8ea3cc685)
  and 2831857b5fd5, which `cursor-trailer-03` reads. The extraction of trailers stays as it is.
  Git reads a key with spaces before the separator as a trailer ("any number of regular space and
  tab characters are allowed between the _<key>_ and the separator",
  [git-interpret-trailers](https://github.com/git/git/blob/42017ff9792f75fec560b984edcc969eecbad15c/Documentation/git-interpret-trailers.adoc)),
  but reading such keys as trailers would make a typed
  `Co-Authored-By : Claude <noreply@anthropic.com>` the `standard` line of Claude Code, which no
  tool writes with a space.

No row reads the other key-value lines under the keys of this section, by decision:

- `Helped-by:` with a tool or a model, as in `Helped-by: Claude` of pulumi/pulumi
  [e72496a59e63](https://github.com/pulumi/pulumi/commit/e72496a59e63cec9945d5feb979f673ee3c71f7e)
  and `Helped-by: Claude-LLM` of `operator-framework/operator-registry`
  [6212dc2a245d](https://github.com/operator-framework/operator-registry/commit/6212dc2a245d99e0e828454186f3ab4046d2741f),
  which openshift/operator-framework-olm 421364d2aca0 carries over as the same change. The Git
  project defines the trailer for ideas and not for the change: "`Helped-by:` is used to credit
  someone who suggested ideas for changes without providing the precise changes in patch form."
  ([SubmittingPatches](https://github.com/git/git/blob/6d7be5f7fd46c2a79ecb69798183b200c341dc97/Documentation/SubmittingPatches),
  read 2026-11-08). The squash merge of `operator-framework/operator-registry` also marks the
  items that an LLM made with an asterisk and lists `* use claude to resolve unit tests after
  updates`. These lines of the body are no `Helped-by:` credit, and no row reads them.
- `Generated-by: ChatGPT 4o, Google Gemini Code Assistant.` of apache/ozone
  [7477431c93b3](https://github.com/apache/ozone/commit/7477431c93b3804af070a5b2f6d47f0bf02f7b73),
  a list with a model of OpenAI first and a Gemini assistant second, below a documentation page.
  `Generated-by: ChatGPT 4o` alone credits `tool not named` (`tool-not-named-trailer-02`), and no
  list row reads a Gemini item. The name does not settle whether it means Gemini Code Assist or an
  assistant built on a Gemini model, and the line is the only one of its form among these lines.
- `Assisted-by: <model>, OpenCode`, the form that the submitting-patches.rst of OVN and of Open
  vSwitch prescribe (`Assisted-by: Name of model, and/or AI Code Assistant`). The only such line in
  the configured repositories is the list item `- Assisted-By: GPT 5.6 Sol, OpenCode` of
  electron/electron d84976dd6420, which has no form of a trailer, as the list line of
  raycast/extensions be792fa72ac5 does not. ovn-org/ovn and openvswitch/ovs, which write the form
  as a trailer, have no configuration artifacts. A row would need the comma form in the skips of
  `claude-trailer-01`, `claude-trailer-02`, `claude-trailer-03`, `claude-trailer-07` and
  `claude-trailer-13` and of `gemini-unspecified-trailer-01` and `gemini-unspecified-trailer-02`,
  so that `Assisted-by: Claude Opus 4.6, OpenCode` of openvswitch/ovs aa9a66ccc8d4 credits OpenCode
  alone, and the rows stay unchanged until a configured repository writes the form as a trailer.
- BrAIn, the agent of tenstorrent/tt-metal, in `Co-authored-by: BrAIn <brain@tenstorrent.com>`,
  `BrAIn <brainclaw@tenstorrent.com>`, `BrAIn <brain-noreply@tenstorrent.com>` and
  `BrAIn (for <person>) <noreply@anthropic.com>`. The lines name no tool, and an agent account
  that names no tool stays out by the decision of 2026-10-30 unless the maintainer decides
  otherwise, as for the nightshift identities of `claude-trailer-30`. BrAIn's own commits point to
  Claude Code, as `🤖 Generated with BrAIn (Claude Code)` of 58748a0ea6cb and
  `Co-authored-by: Claude (BrAInClaw) <brainclaw@tenstorrent.com>` of 40a23ef3e742 do, but BrAIn
  also co-authors the squash merge 777cdc28f838 of an agentic workflow whose engine is Copilot.
  [The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09) reads its evidence again and leaves the lines unread.
- `Co-authored-by: llm-git <llm-git@ttll.de>` of BitGo/BitGoJS. llm-git is a plugin of the llm
  command that writes commit messages: its README says "Generate meaningful commit messages based
  on your changes", and `src/llm_git/config.yaml` at d2dd06a2568b adds the line only through the
  prompt `extend_prompt_commit_metadata` of `llm git commit --add-metadata`. The line records that
  a tool wrote the message of a change that a person made, as Aone Copilot's `Co-developed-by`
  does. `not_ai_commits.csv` has its row, which also keeps the line in the tracked archive of the
  unmatched attributions, because `unmatched reduce` keeps a name that a negative names.
- `Co-authored-by: loom <claude-max-1@openathena.ai>` of marin-community/marin. Loom is the agent
  platform to which the repository moved its agent workflows, and its profiles run different
  agents under one identity: `infra/loom/Pulumi.marin-loom.yaml` at 1cf5c009d358 gives most
  profiles `agent: codex` and others `agent: claude`. The line does not show which agent made the
  change, so Loom stays out, as Polygraph and Trevize do, and it names no model either. The
  nightshift lines of `claude-trailer-30` cover only the commits before the move to Loom.
- `grokspice <grok@spice.ai>`, Spice AI's account with Grok in the place of Claude in
  claudespice, for the reason of resolution mn-0166: it is Spice AI's own identity, and nothing
  shows which agent it runs. Spice AI's other bots, such as `Spice Schema Bot <schema-bot@spice.ai>`, name no tool.
- The SDK and release apps `stainless-app[bot]`, `openai-sdks[bot]` and
  `openhands-release-bot[bot]`, which commit generated SDK code and releases, such as
  `release: 0.82.0` of anthropics/anthropic-sdk-python c6499b9515a8, `release: 3.19.1` of
  openai/openai-python 4d1274681db7 and `chore(main): release 1.36.0` of OpenHands/OpenHands
  d30f1e45bbb6, and StepSecurity's apps, which harden workflows by rules, as
  `Co-developed-by: StepSecurity Bot <bot@stepsecurity.io>` of coconut-svsm/svsm 5ae195540cfb
  shows. They credit no coding agent, and `not_ai_commits.csv` explains them.
- `Co-authored-by: Castiron Bot <castiron@openai.com>` of OpenAI's SDK repositories. Castiron is
  the generator that owns the generated files of OpenAI's SDKs: the release notes of
  openai/openai-python have "Update generated-file header attribution to Castiron", and the
  commits concern generated files, such as `chore: enforce strict generated-file ownership` of
  openai/openai-go 2bd28ffdb83b. No page describes Castiron as a coding agent, and since no page
  says whether it uses a model, it gets no row of `not_ai_commits.csv` either.
- `OpenHands Bot <contact@all-hands.dev>` and `allhands-bot` of OpenHands/OpenHands. The account
  commits both SDK bumps and lock file updates, such as `chore: bump openhands SDK to v1.22.1`, and
  changes of the agent, and its line does not tell them apart, as for vercel[bot].
  `openhands-trailer-01` reads OpenHands' own address openhands@all-hands.dev.
- The local agent sessions of apache/opendal,
  `od-codex-review-050121 <od-codex-review-050121@slock.local>` and
  `od-kimi-scout-050121 <scout@opendal-reg.local>`. Codex and Kimi appear only inside a name that a
  user chose, which no row reads, and codex-review names a review, which does not count.
- `Co-authored-by: Astra <noreply@openai.com>` of dsldevkit/dsl-devkit. Astra alone can name
  OpenAI's model family GPT Astra or a persona that the user chose, and `tool-not-named-trailer-02`
  requires GPT before the model's name.
- `Client: grok` of elizaOS/eliza 49b173b4dc5e. grok names xAI's model as well as Grok Build and
  Grok Bot, which the same contributor writes as `Client: Grok CLI` and `Client: Grok Bot.app` in
  other commits, and the bare value occurs in this commit only. `grok-bot-full-body-01` reads
  `Client: Grok Bot.app` ([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)).
- `AI-assisted:` followed by what an unnamed agent did, as one contributor of Automattic/studio
  writes it in `AI-assisted: drafted the Buildkite group, Fastfile change, and PR body` of
  [094883fab9ab](https://github.com/Automattic/studio/commit/094883fab9ab042d73ae2f8aa6180ca33d572c12)
  and `AI-assisted: spotted the duplicated arch/version in the uploaded` of
  [373dc835e347](https://github.com/Automattic/studio/commit/373dc835e3470d0f10f05884034638989e8fcca6).
  The line names no tool and no model, and the activities alone do not tell a made change from a
  finding: the same contributor writes spotted, diagnosed and identified after the label.
- Single prose values after `AI-assisted:` that name AI or a model, because each occurs in one
  commit and the label alone does not separate a change from advice, such as
  `I used an AI coding assistant while preparing this PR.` of apache/datafusion 3614b2e5e446,
  `Muse Spark used for code research, fix implementation` of kubernetes/kubernetes, a model of
  Meta that no row reads, `od-kimi-scout-050121.` of apache/opendal e3ebf82668e4, and
  `DeepSeek v4-pro, reviewed by OpenDAL regression team` of apache/opendal
  [437ecc0865c1](https://github.com/apache/opendal/commit/437ecc0865c16b785630f4a1c4b559b553b0538a),
  in which the value goes on after the model and its version. The second
  line of `AI-assisted: the implementation, regression test, and pull request text` of
  sveltejs/svelte 2bace308e37a, `were prepared with OpenAI Codex`, stays unread for the same
  reason, because `codex-full-body-07` reads the tool on the line of the label.
- `AI-generated: agent "parrot", model openrouter/z-ai/glm-5.3-flash/high.` of
  NethermindEth/nethermind 94349c9e272d, which names an agent, parrot, that no page describes, so
  that it is no credit of a model alone.
- `AI-Attribution: self-reported` of elizaOS/eliza bc6897e074f5, which states how the footer was
  written and names no tool. The commit names its client in `AI-Client: Codex desktop`, which
  `codex-trailer-14` reads.
- The commits of `tykbot[bot]` in TykTechnologies/ai-studio, such as 14cea024cdcb, which list the
  subjects of the squashed commits as `AI: I've successfully implemented ...` and credit the change
  in `Generated by Probe AI for issue #177`. The `AI:` lines name no tool, and no row reads Probe AI
  until a page grounds the tool.
- A bare `Model:` line, which names a model and no tool, also where it is the only line of a commit
  that names AI, as `Model: gpt-6.1-sol` of loro-dev/loro 1cf1dc5ea511 and of securego/gosec
  ff25931a2edd, and as the `**Translation report**` of the translation account
  robot-infradocs-exp in ytsaurus/ytsaurus 181542d8c2d9 does. The key is also the key of
  configuration that messages quote, and the rows of `tool not named` read a model only under a key
  that credits it with a part of the change.
- The block `**Agent harness**` with `Copilot CLI version: 1.0.105` of microsoft/vscode
  444f098414ea, which GitHub's commit search finds in no other commit that keeps the block, and
  `Agent: yaml-loader-fallback (gpt-6-astra xhigh)` of skypilot-org/skypilot e88c0aa42a82, which
  names a task and a model and no tool.
- The subjects `agent: KiloCode w/ Sonnet 4.5 fixes Fusion upgrade issues` of
  Actual-Chat/actual-chat e1b722a6c45c and `harness: save uncommitted work from Developer/Reviewer/Analyst (cycle N)`
  of the identity `kindling <kindling@harness>` in redwoodjs/sdk, as ec8aae844b46, each in one
  repository's own words, and the `LLM:` lines of pantsbuild/pants, rust-lang/rust and
  Unleash/unleash, each in its own words in one commit.
- The disclosures after a `Note:` label, each written by one author or one change in its own words,
  as `**Note:** the code in this PR was generated mostly by an LLM.` of one change of Yandex's
  monorepo, exported as catboost/catboost fd9e54aed63d, ydb-platform/ydb 66d0c52de24f and
  ytsaurus/ytsaurus 3611519ce01b, `Note: This code has been generated with an LLM which I reviewed for`
  of gem5/gem5 f7f3036e69aa, `note: this is fully done by claude.` of tursodatabase/turso
  ee982456ffa1 and `Note: Code changed by Claude Sonnet 4.6.` of astral-sh/ruff 6c3dab47749b. They
  credit AI, so none of them goes into `not_ai_commits.csv`. `Note: Claude generated` of
  celestiaorg/celestia-app cd9b7f8e0443 names no object, and
  `NOTE: translated via Claude` of piratecash/piratecash 782aebeceeed belongs to one backported
  pull request of Dash.

brave/brave-core writes `Chromium change:` in the singular above a single quoted commit of
Chromium. [4b67ac68ac97](https://github.com/brave/brave-core/commit/4b67ac68ac9755759161bea52d2356ba86fda0bf)
quotes `AI-Model: antigravity` among the trailers of a commit of Chromium, and
[e2ca7c1287c6](https://github.com/brave/brave-core/commit/e2ca7c1287c69958d3a2e15cbb236118f8d882c1)
quotes `Then refined with gemini-cli`. `commits._CHROMIUM_CHANGES` reads the singular as it reads
`Chromium changes:`, and both scopes leave such a block empty. `AI-Model: Gemini 3 Flash` of
[7b5c4ccbedf4](https://github.com/brave/brave-core/commit/7b5c4ccbedf45e17eed162edffd09655baa14e7a)
and of the squash merge
[fd28b5f240e6](https://github.com/brave/brave-core/commit/fd28b5f240e65b3468614f879d495aed2be0fcbc)
is in a block below `Chromium changes:`. No other commit that no heuristic credited has `AI-Model:`
with a Gemini or Antigravity value, so no row reads that key with such a value. A quote below the
line without a line `commit <hash>` stays read. An example is the tab-indented message of Chromium
with `then Gemini CLI to fix all resulting build errors` in
[a5635403768a](https://github.com/brave/brave-core/commit/a5635403768ac2d0572814b4c4e7693709b03db1),
which no row reads.

`tests.test_commits.TestTheSixToolsInTheKeyValueLinesOf1108`,
`tests.test_commits.TestTheOtherToolsInTheKeyValueLinesOf1108` and
`tests.test_commits.TestTheCreditsThatNameNoToolInTheKeyValueLinesOf1108` contain the commits that these
rows read and the lines beside them that the rows leave unread,
`tests.test_commits.TestTheReviewOfTheKeyValueLinesOf1108` the commits and the near misses of
Composer at Cursor's address in a co-author line, Antigravity as the client, Trae AI, a Claude
model after `X-AI-Tool:`, the footer of Overseer and Kimi K3 Code,
`tests.test_commits.TestATrailerAfterTheBulletOfASquashList` the lines of a squash list, and
`tests.test_commits.TestTheCorrectionsOfTheKeyValueLinesOf1109` the commits and the near misses of
the note of flutter/flutter-intellij, of a co-author line that ends a `* ` item and of the lines
under `Generated-with:` and `Generated-with-ai:` that name the tool that ran Claude.

#### The keys left undecided on 2026-11-08, decided on 2026-11-09

[The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)
decided the lines of a list of keys and left out other keys with commits that no heuristic
credited and a value that names a tool or a model. GitHub's API supplied the messages of their
commits, and GitHub's commit search the commits of the same forms in other repositories, read
2026-11-09. The rows read these lines:

- **A mistyped or variant key of a co-author or an assistant.** The six rows of a mistyped or
  spaced key, `claude-full-body-53`, `codex-full-body-43`, `copilot-full-body-40`,
  `cursor-full-body-34`, `gemini-full-body-31` and `opencode-full-body-33`, also read the co-author
  line without `-by`, as in `Co-Authored: Claude Code <noreply@anthropic.com>` of
  RedHatQE/openshift-virtualization-tests
  [fe7983985d1d](https://github.com/RedHatQE/openshift-virtualization-tests/commit/fe7983985d1d3b18f5ee5206b4870e98a984a3ac)
  and `Co-authored: OpenAI Codex` of ansible-collections/community.general
  [261f89c63f0b](https://github.com/ansible-collections/community.general/commit/261f89c63f0b4b03fd8fec7049c44ae696637f62),
  the key `Co-author:`, also in bold, as in `Co-author: Claude` of apache/incubator-kie
  [71329960da73](https://github.com/apache/incubator-kie/commit/71329960da7327ca9a589a81b34806c34d8a1076),
  `**Co-Author:** Claude (AI Assistant)` of Khan/perseus
  [83c280a8e1a6](https://github.com/Khan/perseus/commit/83c280a8e1a67c048d98611d411c1f70c7dd851c) and
  `co-author: cursor & coderabbitAI` of openshift/cluster-capi-operator
  [555d36f96ed0](https://github.com/openshift/cluster-capi-operator/commit/555d36f96ed0ab87488be67c85d9ff4f8b73f7c8),
  the key `Co-authored-with:`, as in `Co-authored-with: Claude Code` of codeforboston/maple
  [34b31577275b](https://github.com/codeforboston/maple/commit/34b31577275b7e283ab94f579cf57f16c6694042),
  `AssistedBy:` without its hyphen, as in `AssistedBy: Claude` of
  arkmq-org/arkmq-org-broker-operator
  [28ed24b1d2bd](https://github.com/arkmq-org/arkmq-org-broker-operator/commit/28ed24b1d2bd8762fe2ab50b81c31d97dccaa90b),
  and `Co-Generated:`, also as `Co-Generatedy:`, as in `Co-Generated: Claude Opus 4.8 (1M context)`
  of apache/flink
  [9c674275ca28](https://github.com/apache/flink/commit/9c674275ca28acc05b9fc5755634ef8c39335c4f).
  GitHub's commit search finds these keys with Claude in other repositories as well, such as
  `Co-authored: Claude Code (Opus 4.8)` of apache/spark c15aa124de83 and `Co-author: Claude Opus 5`
  of SUSE-Enceladus/gcemetadata e0f45f2d7246.
  `Co-authored: copilot[bot] <Copilot@users.noreply.github.com>` of kubewarden/adm-controller
  [c38e545f0083](https://github.com/kubewarden/adm-controller/commit/c38e545f00834dcf7de77ced5c6cbde526d36765),
  a dependency update that a person amended with `Address linter warnings`, credits Copilot.
  `Co-Authored-By` with a space before the colon stays unread, as
  [The key-value lines that no heuristic credited](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)
  decided. `claude-trailer-27` also reads `AssistedBy: cloude-4-sonnet`, the misspelling of
  `claude-4-sonnet` that two developers write in openstack-k8s-operators/openstack-operator
  [e0db0abe619c](https://github.com/openstack-k8s-operators/openstack-operator/commit/e0db0abe619c3888cae1c21a42d5686ccbe8b5d0),
  containers/ramalama
  [e66e9d0ab8e3](https://github.com/containers/ramalama/commit/e66e9d0ab8e371a323f5e36bc1152b3a819b2c4b)
  and other repositories of openstack-k8s-operators, and `tool-not-named-trailer-01` reads
  `AssistedBy: AI` of apache/systemds
  [75d3941005a1](https://github.com/apache/systemds/commit/75d3941005a1599113db69a004d5de920f42578d).
- **`Implementation-By: Claude Code`.** `claude-trailer-35` reads Claude Code first after the key
  `Implementation-By:`, as in `Implementation-By: Claude Code (Opus 4.6)` of SSSD/sssd
  [68edad94bafc](https://github.com/SSSD/sssd/commit/68edad94bafc3bf1cf272181fa4f8245acc5fd20) and
  [086a52e5d1c0](https://github.com/SSSD/sssd/commit/086a52e5d1c0ec9454ca377438faef6d318d4a6a),
  one developer's fixes of compiler warnings. The key names what implemented the change. A model
  alone after the key is not read.
- **`ClaudeCode:<model>`.** `claude-full-body-62` reads the agent name `ClaudeCode` before a colon
  and a Claude model as the whole line, as `ClaudeCode:claude-opus-5` of scikit-hep/hist
  [248da32fceae](https://github.com/scikit-hep/hist/commit/248da32fceae91a583805fc051cbc771c9457614)
  and scientific-python/repo-review
  [8bcddb7f83a2](https://github.com/scientific-python/repo-review/commit/8bcddb7f83a235554fe23dad697960218b13a1ff).
  The same developer writes `Assisted-by: ClaudeCode:claude-opus-5`, the form
  `AGENT_NAME:MODEL_VERSION` that `claude-trailer-01` reads, and the squash merge scikit-hep/hist
  [dfcfe98ccead](https://github.com/scikit-hep/hist/commit/dfcfe98ccead5b73ec694d97e1ddaea89a4fe8e7)
  has both forms, so the bare line is the value of that trailer without its key. The row reads the
  full body, because the key has to begin the line, so that the value of an `Assisted-by` line
  stays with `claude-trailer-01`.
- **`Edited-by: Codex`.** `codex-trailer-28` reads Codex as the whole value of the key
  `Edited-by:`, as below the items of the squash merges expo/eas-cli
  [2dbaa1275076](https://github.com/expo/eas-cli/commit/2dbaa1275076cfb3eb089e46809b64637996df44) and
  [a586f0cb68fe](https://github.com/expo/eas-cli/commit/a586f0cb68fe3a9c72040a26aeeaf61a325257de),
  and in FiveDucks-5lee/dku-exchange-guide
  [0b4e6c3767f9](https://github.com/FiveDucks-5lee/dku-exchange-guide/commit/0b4e6c3767f9d20511ee724b7843d26d081853e9),
  which Codex authored. The key names what edited the files of the change.
- **`Codex-Model:`.** `codex-trailer-15` reads the key `Codex-Model:` as it reads `Codex-Agent:`,
  because the key names Codex, as in `Codex-Model: GPT-6` of VisActor/VChart
  [3363402f8677](https://github.com/VisActor/VChart/commit/3363402f8677bf4432ec5c88c0008224ff7ecd0f) and
  [35f5d74160cf](https://github.com/VisActor/VChart/commit/35f5d74160cfd71363e2ba1289d64b681e9eac48).
- **`Generated-with:` and `Generated-with-ai:`.** `claude-trailer-03`, `cursor-trailer-03` and
  `copilot-trailer-03` read these keys as they read `Generated-by:`, as in
  `Generated-with: Claude Code (https://claude.com/claude-code)` of google/gvisor
  [fbef16a1f874](https://github.com/google/gvisor/commit/fbef16a1f874a74f25de8ffdd34b3ec5ad7b9aec),
  `Generated-with: Cursor AI` of podman-container-tools/podman
  [96ab027a3c9e](https://github.com/podman-container-tools/podman/commit/96ab027a3c9eaf02faae9f4c8bd822f8471fbc2a),
  `Generated-with: GitHub Copilot` of TheFive/osmbc
  [4ecd123d41d1](https://github.com/TheFive/osmbc/commit/4ecd123d41d19219f09255be81189641689ac603)
  and `Generated-with-ai: claude-4-sonnet` of rhobs/observability-operator
  [d0e25a27bde3](https://github.com/rhobs/observability-operator/commit/d0e25a27bde3ba2ca6c39a6fb259e93c8ae8cc99),
  whose author writes `generated-with-ai: cursor` in openshift/cluster-monitoring-operator
  [ff39d39822b0](https://github.com/openshift/cluster-monitoring-operator/commit/ff39d39822b08d2cf14eacb13a61d86fd93bb626).
  No line among these names Codex, Gemini or OpenCode after the keys. Gemini has no row for these
  keys, and Codex and OpenCode have no row for their name at the start of the value under them.
  `codex-trailer-04` and `opencode-trailer-04` read the keys only in the forms after `via` or in
  parentheses, and `codex-trailer-04` also OpenAI Codex at the start of the value, so that a line
  that `claude-trailer-03` skips is credited to the tool that it names, as `copilot-trailer-04` and
  `cursor-trailer-05` credit such a line to Copilot and Cursor. `codex-trailer-04` therefore also
  reads `Generated-with: OpenAI Codex CLI (partial)` of mikefarah/yq
  [c6029376a59f](https://github.com/mikefarah/yq/commit/c6029376a59f08b6467e62001efca632d45d4d05),
  which credits Codex through its co-author line as well.
- **The authoring tool of inspect_ai.** The pull request template of UKGovernmentBEIS/inspect_ai at
  [697fde94e2bb](https://github.com/UKGovernmentBEIS/inspect_ai/blob/697fde94e2bb08fcba1b3bad65d8c752e1812111/.github/pull_request_template.md)
  states in its section `Agent review`: "If an agent worked on this PR, give the authoring tool,
  reviewer model/tool, fresh-context pass count, and findings fixed or dismissed with brief
  reasons." `claude-full-body-61` reads the item `- Authoring: Claude Code`, as in
  `- Authoring: Claude Code (Claude Opus 5.5).` of
  [bfc7c72f11d0](https://github.com/UKGovernmentBEIS/inspect_ai/commit/bfc7c72f11d009e62189c411d9a019a4f3923ae4),
  and not the item `- Review: Codex (GPT-6.1-sol).` beside it, because a review does not count.
- **The Agent-Signature trailer of beads.** The `engdocs/AGENT_SIGNING.md` of gastownhall/beads at
  [0e017821f49c](https://github.com/gastownhall/beads/blob/0e017821f49caa2512af45db0579b6d04d044c60/engdocs/AGENT_SIGNING.md)
  prescribes "Sign commits made by an agent with this trailer:" above
  `Agent-Signature: {agent_runtime}-{model}-{reasoning} on behalf of {user}`, and one developer
  signs commits of Kilo-Org/kilocode and TriliumNext/Trilium this way. `claude-trailer-36` reads the
  runtime `claude`, as in `Agent-Signature: claude-fable-5-high on behalf of (a person)` of
  Kilo-Org/kilocode
  [5ff16603baba](https://github.com/Kilo-Org/kilocode/commit/5ff16603babade4f0762900fe81436d9e036b06e),
  `codex-trailer-29` the runtime `codex`, as in `codex-gpt-6-astra-medium` of TriliumNext/Trilium
  [dad635ec88ce](https://github.com/TriliumNext/Trilium/commit/dad635ec88cee359e0ace5493801e2a26229720a),
  `amp-trailer-03` the runtime `amp`, as in `amp-gpt-5.6-sol-high on behalf of Amp` of
  [d4f6f625643a](https://github.com/TriliumNext/Trilium/commit/d4f6f625643a561c8a568b865ae21c84653ea3cc),
  which Amp authored, and `kilo-code-trailer-04` the runtime `kilo`, as in
  `kilo-kilo-auto/efficient-efficient` of Kilo-Org/kilocode
  [9c96d61049e4](https://github.com/Kilo-Org/kilocode/commit/9c96d61049e42b49af9cbc8e7eeb41b56499cf42).
  The runtime `claude` is Claude Code: TriliumNext/Trilium
  [610d2aaf80df](https://github.com/TriliumNext/Trilium/commit/610d2aaf80dfe5baf3d082de0f4d3873bd7593fc)
  has `Agent-Signature: claude-fable-5-1-high` above Claude Code's line
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. A signature without a runtime, as
  `unknown-model/unknown-reasoning` of gastownhall/beads, names no tool and is not read.

No row reads the other lines under these keys, by decision:

- `Tool-Assisted: Claude Code (<model>)` with a `Role:` line below it, which one developer writes in
  InsightSoftwareConsortium/ITK, BRAINSia/BRAINSTools and the developer's own repositories. The
  roles name root-cause analysis, compatibility analysis or an audit, as
  `Role: root-cause analysis, cross-project bisection` of InsightSoftwareConsortium/ITK
  [2b91d02c12b2](https://github.com/InsightSoftwareConsortium/ITK/commit/2b91d02c12b2d6ecf892bd85531d2c2d22384f45)
  does, and not a part of the change, so the key credits an analysis, which does not count.
- `Original-Co-Author: Claude Fable 5 <noreply@anthropic.com>` of Floorp-Projects/Floorp
  [731488a0cf71](https://github.com/Floorp-Projects/Floorp/commit/731488a0cf71e00da820d6d67d7de4aa30f5a582),
  which names the co-author of an original commit beside
  `Replacement-Classification: redesigned replacement; original commit not cherry-picked`. The
  change does not contain the original commit.
- `Sentinel-Harness:` in the commits that Meta exports from its agent Sentinel, as
  `Sentinel-Harness: claude` of meta/mcrouter
  [936cc70034a1](https://github.com/meta/mcrouter/commit/936cc70034a14de13bee7d15e774182324d224fa).
  The key lists each harness that ran in a council of the agent, also one that only verified:
  meta/time
  [a14b8363e96a](https://github.com/meta/time/commit/a14b8363e96a411107df95e5a3d3fdfa2f67d8d4)
  has `Sentinel-Harness: codex` and `Sentinel-Harness: claude` above
  `Model used: codex (original fix); Claude Opus 5 (claude-opus-5[1m]) (race-detector verification of both stats packages)`.
  A line alone therefore does not show which harness made the change, and the page that the
  commits link, at internalfb.com, is internal to Meta.
- `Docs-By: Claude Sonnet 4.5 <noreply@anthropic.com>` of tektoncd/pipeline
  [72c59cd34bef](https://github.com/tektoncd/pipeline/commit/72c59cd34bef4726609bcde69f3afe3e1f73ad8f)
  and `AI-Assistant: GPT-5.2 via GitHub Copilot` of nextcloud/appstore
  [1a9d116c8b8a](https://github.com/nextcloud/appstore/commit/1a9d116c8b8a591b19e0aaaeb8af81f5b1d7cc81),
  each of which GitHub's commit search finds in one commit only.
- A person after the keys, as `Co-author:` with a contributor of ArduPilot/ardupilot
  ff3c4b8bda6a, is no credit. `tool-not-named-full-body-07` reads a model alone after a mistyped
  key, as `Assited-by: ChatGPT (OpenAI)` of kubernetes-sigs/kubebuilder d036bdd36b34
  ([The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09)).

`tests.test_commits.TestTheKeysLeftUndecidedOn1108` contains the commits that these rows read and
the lines beside them that the rows leave unread. [The keys and open items decided on 2026-11-09](#the-keys-and-open-items-decided-on-2026-11-09) decides the keys of the list
that this section left open, such as `Author:` and `Agent-Authored-By:`.

#### The keys and open items decided on 2026-11-09

A scan of the commit logs of the collection of the run that started on 2026-11-03, with the
heuristics of
[The keys left undecided on 2026-11-08, decided on 2026-11-09](#the-keys-left-undecided-on-2026-11-08-decided-on-2026-11-09),
listed every commit that no heuristic credits and that has a key-value line whose value names a tool
or a model. The keys of that list and the open items of the key-value
lines read on 2026-11-08 were decided on 2026-11-09, with the messages from GitHub's API and the
commits of the same forms that GitHub's commit search finds in other repositories. The rows read
these lines:

- **Antigravity in rsyslog's footer.** `gemini-full-body-02` also reads Antigravity, also as Google
  Antigravity, as the agent of rsyslog's footer, as in `With the help of AI-Agents: Antigravity` of
  rsyslog/rsyslog
  [0674571ab395](https://github.com/rsyslog/rsyslog/commit/0674571ab3952e3654e1f49d5da0db764f5a5710),
  `With the help of AI Agent: Google Antigravity` of
  [3ee31a8d0578](https://github.com/rsyslog/rsyslog/commit/3ee31a8d05780b281b431799d9becb5eef72f2b0)
  and `With the help of AI Agent: Antigravity` of rsyslog/liblognorm
  [d0a830479dd1](https://github.com/rsyslog/liblognorm/commit/d0a830479dd11ccd44d508ae3ae15f4546d8be63).
  Antigravity counts under Gemini ([Gemini naming no product](#gemini-naming-no-product-2026-09-27),
  and the paragraph Gemini includes Antigravity of [the heuristics](heuristics.md)), and the row read
  only Gemini CLI and Gemini Code Assist, so that the footer with Antigravity credited no tool.
  rsyslog's commit skill names a branch "`i-<issue-number>` or `<agent-name>-i-<issue-number>`",
  and GitHub's API links 0674571ab395 to a pull request from `antigravity-i-imbeats-hardening`.
  `gemini-full-body-04` already credits `AI-Agent: Antigravity` of the same author to Gemini, as in
  1ab067b680cd. `With the help of AI-Agents: Antigravity & Mistral Vibe` of
  [d9f69e4100a0](https://github.com/rsyslog/rsyslog/commit/d9f69e4100a0f91d2196c482e6daa7de58ee5258)
  credits Gemini, and Mistral Vibe, which no other commit names in the footer, stays unread as a
  single commit. Antigravity in parentheses or after a hyphen, as in `Google-Antigravity`, is not
  read.
- **A model alone in rsyslog's footer.** `tool-not-named-full-body-09` reads rsyslog's footer with a
  model alone as the whole value, ChatGPT or a GPT model, also after OpenAI and with a version, or
  Composer, in every opener that the rows of the six tools read, as in
  `With the help of AI-Agent: ChatGPT` of rsyslog/rsyslog
  [01f57e26ebc7](https://github.com/rsyslog/rsyslog/commit/01f57e26ebc7b5c047b1d23a02bb022c0edf48bc),
  `With help from AI-Agents: ChatGPT` of
  [c89113d53120](https://github.com/rsyslog/rsyslog/commit/c89113d53120a11744088ada9f4e7b3c47ad2a27),
  `With the help of AI-Agents: OpenAI GPT-5.3-Codex` of
  [640d9227d8f9](https://github.com/rsyslog/rsyslog/commit/640d9227d8f9e1ffe59e1dfe009f08e3a632fab6),
  `With the help of AI-Agents: Composer` of
  [7fea30ea76a5](https://github.com/rsyslog/rsyslog/commit/7fea30ea76a5712a9878c61796c9399213adbd15)
  and `With the help of AI Agent: GPT-5` of rsyslog/liblognorm
  [2a4812be2e27](https://github.com/rsyslog/liblognorm/commit/2a4812be2e27194530451b0b3a7f0af0c16da396).
  The rows of the six tools read the footer as the credit of the agent that it names, and a model
  alone names no tool: ChatGPT names no coding agent
  ([A trailer that names no tool, 2026-09-26](#a-trailer-that-names-no-tool-2026-09-26)), and
  Composer names a model by the decision of 2026-11-03. GitHub's API links the commits with ChatGPT
  or GPT in the footer to pull requests from `codex/` head branches, except c89113d53120, from a
  branch of the maintainer's fork, and 930270507387, from `cursor/critical-correctness-bugs-bc7c`,
  and it links 7fea30ea76a5 to `cursor/fix-proprepltest-rfctag-udp-flake-018a`. The same value thus
  stands for different agents, and the credits stay under the label, as `AI-Agent: ChatGPT` does.
  `tool-not-named-full-body-02` keeps its alternative for a GPT-x Codex model after
  `With the help of AI-Agents:`, because it also reads the model before more text. A value that
  goes on after the name, as `ryslog doc assistant (openai based)` of 99d08b60f605, is not read,
  and neither are the misspelled openers `With help form AI-Agent:` of 2b626e7cf6e5 and
  `Wtiht the help of AI-Agent:` of e55614861269 and the opener `With AI-Agent:` of 5d787b359c63,
  which the rows of the six tools do not read either.
- **`AI-Agent: ChatGPT` of rsyslog/rsyslog.** The line does not count for Codex, and
  `tool-not-named-trailer-25` and `tool-not-named-trailer-22`, which reads `AI-Assisted: yes` of
  621ee6063c1b, stay as they are. ChatGPT names no coding agent
  ([A trailer that names no tool, 2026-09-26](#a-trailer-that-names-no-tool-2026-09-26)), and the
  only sign of Codex is the head branch of the pull request, which the squash message does not
  contain. Every pull request with the line comes from a `codex/` head branch, which Codex creates
  in its web version (users on OpenAI's forum write that Codex "always sets it to
  codex/change-description",
  [the forum thread](https://community.openai.com/t/branch-naming-is-there-any-way-to-set-it/1315157)),
  but a Codex row would read the line wherever it stands. hedydev/ioant-AI-Minecraft-Alice writes
  `AI-Agent: ChatGPT` above `AI-Session:` on a commit without a pull request
  ([89f9a0b59e60](https://github.com/hedydev/ioant-AI-Minecraft-Alice/commit/89f9a0b59e6033a9f5315c3472be3177ea37f41c)),
  and MulattoTech/MJobs writes `AI-Agent: ChatGPT-Web` and `AI-Agent: Codex` for different items of
  one squash merge that also has `Co-authored-by: Codex <codex@openai.com>`
  ([c8cb79728aaa](https://github.com/MulattoTech/MJobs/commit/c8cb79728aaa6aa9e3500470c7640626c9bc6993)).
  rsyslog's CONTRIBUTING.md names ChatGPT and Codex as different agents ("e.g. GitHub Copilot,
  ChatGPT, Codex"), and rsyslog's footer has ChatGPT on a commit of a branch that Codex did not
  create, [c89113d53120](https://github.com/rsyslog/rsyslog/commit/c89113d53120a11744088ada9f4e7b3c47ad2a27).
  These credits are lost to Codex by the reading rule and not in fact, as for GPT-5 Codex in
  rsyslog's footer, and the decision covers rsyslog's footer with ChatGPT or a GPT model, which
  `tool-not-named-full-body-09` reads.
- **Code, Implementation or an agent before the key, and a stray letter after it.** The six rows of
  a mistyped or spaced key, `claude-full-body-53`, `codex-full-body-43`, `copilot-full-body-40`,
  `cursor-full-body-34`, `gemini-full-body-31` and `opencode-full-body-33`, also read Code before
  `Generated-by:`, as in `Code Generated-by: Cursor AI` of apache/impala
  [6899d95119d2](https://github.com/apache/impala/commit/6899d95119d2aaaa515d7be222d8c4d29cd72545),
  [18ed94dadb7b](https://github.com/apache/impala/commit/18ed94dadb7b9267c37cc60862fa0892581f02a3)
  and [e1ca23d62753](https://github.com/apache/impala/commit/e1ca23d627532bb17228e3d455c55a03b3e28f49),
  Code before `Assisted-by:`, as in `- Code Assisted-by: Claude Opus 4.8` under the heading
  Authorship of open-telemetry/opentelemetry-collector-contrib
  [d77f821f05e2](https://github.com/open-telemetry/opentelemetry-collector-contrib/commit/d77f821f05e295ec1842881ac46d53cf73d8311e),
  [94104ef2a248](https://github.com/open-telemetry/opentelemetry-collector-contrib/commit/94104ef2a24813646875a557885514412e25de25)
  and [86b043658b9a](https://github.com/open-telemetry/opentelemetry-collector-contrib/commit/86b043658b9a45264a0a137af88bd773cf842402),
  whose
  [AGENTS.md](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/1bd082faaa0c3caa2f90ed32b912a3dff3d4ffc3/AGENTS.md)
  states that such use "should be disclosed through an Assisted-by: commit message trailer", and
  Implementation before `assisted-by:`, as in `Implementation assisted-by: Claude Code (Opus 4.8)` of
  SSSD/sssd [79821dc9ab9f](https://github.com/SSSD/sssd/commit/79821dc9ab9fb04f6e6b6fa09cb5474f0ec11685)
  and `Implementation assisted-by: Claude Code (Opus 4.6)` of
  [622fa7f7b443](https://github.com/SSSD/sssd/commit/622fa7f7b44340f5bdbeed3cccc2dc1728b1e6bb) and
  other commits of the developer who writes `Implementation-By: Claude Code` (`claude-trailer-35`).
  The first word names the code or the implementation as the part of the change that the tool
  produced, as Partially does in `Partially generated-by:`.
  `Commit message Assisted-By: Claude Code (Opus 4.8)` of the same developer in
  [42db412156ee](https://github.com/SSSD/sssd/commit/42db412156eeae2fe47b5a76519cd79d6ecf61d1)
  credits the message and stays unread, as `Commit-Message-Assisted-by` does, and so does
  `Code-Generated-by:` with a hyphen. The rows also read the key `Co-authored-by agent:`, which the
  [AGENTS.md of rizinorg/rizin](https://github.com/rizinorg/rizin/blob/a0da72a5c98db4d32f2b8aa050b0d97bb7b0c751/AGENTS.md)
  prescribes: "If an agent was involved in ANY code changes the commit message MUST end with:
  `Co-authored-by agent: <AGENT_NAME>/<MODEL_NAME>`." and "Variables `<AGENT_NAME>` and
  `<MODEL_NAME>` MUST be set by the agent." The name before the slash is therefore the agent that
  changed the code, as in `Co-authored-by agent: Codex/GPT-6` of
  [5e570784ecf8](https://github.com/rizinorg/rizin/commit/5e570784ecf845442e887625611477f5afeb66db),
  `Co-authored-by agent: Claude/claude-opus-4-8` of
  [155ead682236](https://github.com/rizinorg/rizin/commit/155ead682236a92d02e3785c3867173320328ce3)
  and `Co-authored-by agent: cursor/grok-4.7` of
  [3c8ff1288e28](https://github.com/rizinorg/rizin/commit/3c8ff1288e2814310ffdabc6dd074a8da90a49ce).
  They also read `Assisted-by:` with a stray `i` after the colon, as in
  `Assisted-by:i Cursor with Claude Opus 4.6 <noreply@anthropic.com>` of go-git/go-git
  [0d5c1c0d6501](https://github.com/go-git/go-git/commit/0d5c1c0d6501a2c4f713ecc7551281d32464fa96),
  whose author writes `Assisted-by: Cursor with Claude Sonnet 4.6 <noreply@anthropic.com>` in
  [6109f8f51462](https://github.com/go-git/go-git/commit/6109f8f5146289ada9ac713ce666c3b577c5ecb8),
  which `cursor-trailer-01` reads, as `Generated-by : Cursor/Claude` of kubernetes-sigs/kubebuilder
  is read beside its author's form without the space. `cursor-full-body-34` also reads Cursor
  misspelled as Cursour, as in `Generate-by: Cursour/Claude` of kubernetes-sigs/kubebuilder
  [f7d3c3d049a9](https://github.com/kubernetes-sigs/kubebuilder/commit/f7d3c3d049a9a3eed9f5d74810e160b6866518d5),
  whose author writes `Assisted-by: Cursour/Claude` in
  [7c9eeb5b839d](https://github.com/kubernetes-sigs/kubebuilder/commit/7c9eeb5b839dc99d144578cb254b5c2c87c44c5f),
  which `cursor-trailer-13` reads. No line under these keys names Copilot, Gemini CLI, Gemini Code
  Assist, Antigravity or OpenCode.
- **oh-my-pi and pi as the agent of rizinorg/rizin.** `oh-my-pi-full-body-02` reads `omp` or
  `oh-my-pi` and `pi-full-body-04` reads `pi` before the slash of `Co-authored-by agent:`, the line
  that the agent fills in with its own name under the AGENTS.md of rizinorg/rizin.
  `Co-authored-by agent: omp/openai-codex/gpt-6-astra` of
  [3c8ff1288e28](https://github.com/rizinorg/rizin/commit/3c8ff1288e2814310ffdabc6dd074a8da90a49ce)
  names oh-my-pi, whose command is `omp` (`oh-my-pi-trailer-01`), with a provider and a model in the
  form of pi's model catalog, so that openai-codex is the provider of the model and not Codex. The
  same commit also has the lines of Codex and Cursor, which `codex-full-body-43` and
  `cursor-full-body-34` read. `Co-authored-by agent: pi/deepseek-flash-guan` of
  [b80b3862479c](https://github.com/rizinorg/rizin/commit/b80b3862479c6789dd8f5b69de1c8830c8c8119c)
  names the Pi coding agent and is the only credit of the commit. Each name occurs in one commit,
  but the key is prescribed and occurs in more commits, as `amp-trailer-03` reads the runtime `amp`
  of the Agent-Signature trailer from one commit. Both rows read only the field before the slash,
  because `omp` also stands for OpenMP and `pi` alone can name a Raspberry Pi.
  `Co-authored-by agent: root/GPT-5` of
  [73b070d39772](https://github.com/rizinorg/rizin/commit/73b070d3977235c84a03fa3eaf910da5137a2a34)
  stays unread: the field of the agent holds `root`, the name of a system account and of no coding
  agent, so the line names neither a tool nor a model alone, and it occurs in one commit.
- **A model alone after a mistyped or spaced key.** `tool-not-named-full-body-07` reads a model alone
  after the keys of the six rows of a mistyped or spaced key and after `Assisted by:` or
  `Generated by:` with a space where the trailer has a hyphen: Opus, Sonnet, Haiku or Fable with a
  version, GPT or ChatGPT with an optional version, or a provider and a model joined by a slash,
  each also followed by LLM, by OpenAI, Anthropic or a context size in parentheses, or by an
  address. The line credits the change to the model and names no tool, as `Assisted-by: Opus 4.8`
  does under `tool-not-named-trailer-07` and `Assisted-by: ChatGPT (OpenAI)` under
  `tool-not-named-trailer-02`. One maintainer of the Humanitarian OpenStreetMap Team ends commits in
  hotosm/drone-tm, hotosm/openaerialmap, hotosm/ui and hotosm/fAIr with `Assisted by: Opus 4.8 LLM`,
  as in [496f8d726e71](https://github.com/hotosm/openaerialmap/commit/496f8d726e71b0f15bccbaf3722ecffca77a553b),
  and the
  [CONTRIBUTING.md of hotosm/drone-tm](https://github.com/hotosm/drone-tm/blob/01a380d61da8c48476d0b7eab9035940f362ba44/CONTRIBUTING.md)
  states "**Disclose it**: Pick an AI assistance level (0-5) in the PR template." Other
  contributors write `Assisted by: ChatGPT` in dbeaver/dbeaver, as in
  [99a015a841d7](https://github.com/dbeaver/dbeaver/commit/99a015a841d78ce5f963be0f088a93abf4e9910e),
  `Assisted by: GPT-5.2-Codex` in pulp/pulp_rpm
  [21ace236011b](https://github.com/pulp/pulp_rpm/commit/21ace236011b0264d84e8b32a4d7e5c3830e5312),
  `Assisted by : Opus 5.5` in PlotPyStack/guidata
  [cc2603988b37](https://github.com/PlotPyStack/guidata/commit/cc2603988b3751c6ded3f21ac09ec12c6a18e033)
  and `Assisted by: GPT-6` in ansible/eda-server
  [f572026f7927](https://github.com/ansible/eda-server/commit/f572026f7927feb3bdc9750db5941f2c5c827c46).
  The row also reads `Assited-by: ChatGPT (OpenAI)` of kubernetes-sigs/kubebuilder
  [d036bdd36b34](https://github.com/kubernetes-sigs/kubebuilder/commit/d036bdd36b34051991f27ecc28e68c08d7dbdf42),
  whose author also writes `Assisted by: ChatGPT (OpenAI)` in
  [5795f0cceb40](https://github.com/kubernetes-sigs/kubebuilder/commit/5795f0cceb40a147d9868a2e683c0faf222e964b)
  and `Assisted-by: ChatGPT (OpenAI)`, which `tool-not-named-trailer-02` reads, and
  `Co-authored : Opus 4.6 <noreply@anthropic.com>` of RedHatQE/openshift-virtualization-tests
  [e71cd6dde221](https://github.com/RedHatQE/openshift-virtualization-tests/commit/e71cd6dde221a4b19f41c74000a65d20650ed4d4).
  `Assisted by: Google/Gemini 3 Pro` of kubernetes-sigs/kubebuilder
  [3077ba6c9ab5](https://github.com/kubernetes-sigs/kubebuilder/commit/3077ba6c9ab530c71f3d10dd588905a2686a2a7e)
  counts under `tool not named` and not under `Gemini (unspecified)`, because a provider and a model
  joined by a slash count under the label in the trailer form (`tool-not-named-trailer-08`), as the
  notes of `gemini-unspecified-trailer-03` state for `google/gemini-2.5-pro`. A clause after a
  period, a comma or a semicolon stays read, as in `Assisted by: Opus 4.7 LLM, fixes #828` of
  [202658044fbc](https://github.com/hotosm/drone-tm/commit/202658044fbc782e2a4c6b7efdfc055fcce84c91),
  unless it names a review, a plan, feedback, a verification, tests, the message, the description,
  a summary, a negation or a tool, the words that `tool-not-named-trailer-22` leaves out. A commit
  that a row names a tool for keeps that tool, as hotosm/drone-tm
  [662738a96806](https://github.com/hotosm/drone-tm/commit/662738a968068c7caa12deae84f54a01e4dbac69)
  keeps Codex. No row reads the other values under `Assisted by` of the same maintainer:
  `Assisted by: Opus 4.8 LLM for review of options. Fixes #834` of
  [aecbfc7c123e](https://github.com/hotosm/drone-tm/commit/aecbfc7c123e9686a95e6501ddfb073db34ea6a1),
  `Assisted by: Opus 4.6 to plan based on ScaleODM API` of
  [88ed3cdef2d6](https://github.com/hotosm/drone-tm/commit/88ed3cdef2d6483a563bae5f12f1d1ff65795ad2),
  `Assisted by: Opus 4.7 for plan` of
  [9f87b9d3cdf7](https://github.com/hotosm/openaerialmap/commit/9f87b9d3cdf72c47d2c3ad6da42b08fb791eabab),
  `Assisted by: Opus 4.6 + Codex 5.4 review` of
  [920f2bcee873](https://github.com/hotosm/drone-tm/commit/920f2bcee87359deecb2224e3749b9e0516c5785)
  and `Assisted by: Opus 4.6 & Codex 5.4 feedback loop` of
  [93370363bb74](https://github.com/hotosm/drone-tm/commit/93370363bb748f46d5a488a2af93cf1fdedce69b)
  credit planning, a review or feedback, which do not count.
  `Assisted by: Opus 4.8 to plan, pinpoint some changes, write some sql` of
  [30b5b4a4dd0a](https://github.com/hotosm/drone-tm/commit/30b5b4a4dd0aea472be157421bafb04f77ab7eee)
  and `Assisted by: Opus 4.7 for initial draft, revised` of
  [0829be07d1e1](https://github.com/hotosm/openaerialmap/commit/0829be07d1e1bf1f6c077fccd107eed0427fd516)
  name a part of the change beside planning or a person's revision, but each wording occurs in one
  commit, and the row reads the model as the whole value or before a separated clause.
  `Assisted by: Opus 4.6 LLN` of
  [3db569aa4a39](https://github.com/hotosm/drone-tm/commit/3db569aa4a398274b9e9b99f01dbd4ba93e06aae)
  and `Assisted by: Opus Fable LLM` of hotosm/ui
  [fa3ae06b40f2](https://github.com/hotosm/ui/commit/fa3ae06b40f2c3e56a0f38188d055f930d2290df)
  misspell the model, each in one commit.
- **`AI Co-author:` with a GLM model.** `tool-not-named-full-body-08` reads a model of Z.ai's GLM
  family as the whole value of `AI Co-author:`, the last line of one developer's commits in
  wkgcass/vproxy, as in
  [e270d4f1a61f](https://github.com/wkgcass/vproxy/commit/e270d4f1a61f5ec8aa2a9e3560d4fdebf4aee6fc)
  and [fba80ecf654c](https://github.com/wkgcass/vproxy/commit/fba80ecf654cb6c188e601880ae042384ddcb9b9),
  with `AI Co-author: glm-5.1`. The same developer writes `Co-Authored-By: glm-5.1` in
  [1075b196ee2b](https://github.com/wkgcass/vproxy/commit/1075b196ee2bf046450d014f8cadac8f9a99df04),
  which `tool-not-named-trailer-06` reads, and GitHub's commit search finds `AI Co-author: GLM-5.2`
  in dolibali/zhuma fc787c24ca6d (read 2026-11-09). The key names the model as a co-author of the
  change and names no tool. `AI co-authors: Claude (Anthropic) & Codex (OpenAI)` in a list item of
  robocup-junior/soccer-referee-app 8b9ce099085f, about an About page, stays unread.
- **A model alone in the disclosure of jupyterlab/jupyterlab.** The pull request template of
  jupyterlab/jupyterlab at
  [7cc43c0627f4](https://github.com/jupyterlab/jupyterlab/blob/7cc43c0627f4d29f36bed788d213773bd76cc1ff/.github/pull_request_template.md)
  has the items `- **<!-- YES or NO -->**: Some or all of the content of this PR was generated by AI.`
  and `- AI tools and models used: <!-- FILL IN -->`, and the squash merges of the repository copy
  the description of the pull request. `tool-not-named-full-body-10` reads a model alone after the
  second label when the first item stands above it with YES, as in
  `- AI tools and models used: Opus 5.5` of
  [014df187f7fb](https://github.com/jupyterlab/jupyterlab/commit/014df187f7fbd1d91755b1f7d655a281c95acce0),
  `ChatGPT 5.5` of
  [06dfe441e3b4](https://github.com/jupyterlab/jupyterlab/commit/06dfe441e3b42e94ad169aef91e74c5217c0f88d)
  and `Opus 4.7 & GPT 5.5` of
  [299b5c416b2f](https://github.com/jupyterlab/jupyterlab/commit/299b5c416b2fda18ccd4e8f35cb02876f471c124).
  A credit that names only a model counts under `tool not named`, as
  `- AI tools and models used: 5.3 Codex` does through `tool-not-named-full-body-02`, and
  `claude-full-body-32` and `gemini-unspecified-full-body-06` keep the values that name Claude or
  Gemini. The row does not read a value that goes on after the models, such as
  `Opus 5 for generation, Fable 5 for review` of 4a570ff048e6 and
  `ChatGPT for consultation regarding where` of c46e998a19ec, a model in a chat, such as
  `Claude Opus 5 (chat), DeepSeek-V3 (chat)` of 86818bb432cd, a model that another tool ran, such as
  `OpenAI GPT-5.5 via OpenClaw` of a58d45b3951a, or a first item that states another use, such as
  `- **YES**: To find the right place to add this, and understand how the` of 9eb7d7fbfcec. A value
  that ends in a semicolon continues on the next line, and in 4bbf19131849 that line names Codex
  (`Codex (GPT-6; reasoning effort unavailable).`), so the row leaves such a value out. No row reads
  the Codex of 4bbf19131849 or the OpenClaw of a58d45b3951a until the maintainer decides.
- **A model or a vendor's assistant in a labeled disclosure.** `tool-not-named-full-body-11` reads
  the labels of the labeled disclosure, `Disclaimer:` included, with a GPT model, ChatGPT, Opus,
  Sonnet, Haiku, Fable or Kimi K, or with an Anthropic or OpenAI coding assistant, when the line
  gives the model a part of the change and names no coding agent. The row reads a verb of making
  with by, with, using or from, `Used <model>` alone or before to help implement, write, fix,
  generate or draft, `Some <model> usage`, an implementation by or with the model, the model or the
  assistant as the subject of a verb of making with the change as its object, and
  `asked <model> to` followed in the same sentence by `kept` and the fixes. One contributor writes
  the disclosure in commits of rust-lang/rust-analyzer and rust-lang/rust, as in
  `AI disclosure: Code partly written by GPT-6.1 Sol, code review and` above `commit message by me.`
  of rust-lang/rust-analyzer
  [ff4a83e3bb2f](https://github.com/rust-lang/rust-analyzer/commit/ff4a83e3bb2ff1f65961c916160962bbb180f9a3),
  and other projects write it too, as in
  `AI assistance: GPT-5.6 Luna implemented the change; GPT-6 Astra` of oxc-project/oxc
  [9a469d2cc531](https://github.com/oxc-project/oxc/commit/9a469d2cc531fb0d120f46715c63bc24a36b54c3)
  and `Disclosure: this patch written by GPT-5.6 Sol.` of mozilla/sccache
  [b32002531bf9](https://github.com/mozilla/sccache/commit/b32002531bf94c63b66a0c20462f276c3c0b6a7a).
  `AI disclosure: I fixed the first few manually, then asked GPT-5.5 to` above
  `look for additional cases and kept all the obviously reasonable fixes.` of
  rust-lang/rust-analyzer
  [160b5af39cb8](https://github.com/rust-lang/rust-analyzer/commit/160b5af39cb80ecec42a20adcbbe88e1497b0ac7)
  counts, because the author kept the fixes that the model made for the cases after the first, and
  the rows of the six tools read the same line with their tool. rust-lang/rust de3c5b647b55 copies
  the same line.
  `AI assistance: drafted with the help of an Anthropic coding assistant.` of apache/datafusion
  [5841c7feefe3](https://github.com/apache/datafusion/commit/5841c7feefe30eeb13b610f2f2f02d102febcc83)
  and `AI assistance: OpenAI's assistant prepared and reviewed this focused test-only change` of
  tursodatabase/turso
  [4280b68054e0](https://github.com/tursodatabase/turso/commit/4280b68054e0748e41d650c5df1dc7e1a3700999)
  name a vendor and no product, and they count under `tool not named`, as
  `Co-authored-by: OpenAI <noreply@openai.com>` does
  ([A trailer that names no tool](#a-trailer-that-names-no-tool-2026-09-26)). The row does not read
  a negation, a line that gives the model only the description, the summary, the commit message or
  the title, text or body of the pull request, a repro, a bisection or a search alone, such as
  `Repro constructed with Fable 5`, or Grok alone, such as
  `disclosure: drafted with Grok assistance; I reviewed and tested.` of redis/go-redis 732baca3ef9b.
  `AI assistance disclosure:` with the model on the next line, as in foxglove/mcap 60e73f4b4ec0,
  occurs in one commit and is not read.
- **Gemini alone in a labeled disclosure.** `gemini-unspecified-full-body-10` reads the labels of the
  labeled disclosure, `Disclaimer:` included, with Gemini alone after a verb of making with by,
  with, using or from, or as the subject of a verb of making with the change as its object, as in
  `AI Usage: Change generated by Gemini and reviewed/improved by` of envoyproxy/envoy
  [bfbd8f29e134](https://github.com/envoyproxy/envoy/commit/bfbd8f29e134002d1be74ce2bddb668b8d66466e)
  and `AI Usage: Gemini partially generated these changes.` of
  [ade332db0fd5](https://github.com/envoyproxy/envoy/commit/ade332db0fd533b6cd9519572d3c9c8cd7172857),
  the disclosures that the pull request template of envoyproxy/envoy at 11ed11067295 requests ("You
  need to disclose this usage in the PR description to ensure transparency."), and
  `Disclaimer: this code is largely generated by Gemini (with multiple` of project-oak/oak
  [ca2c75034927](https://github.com/project-oak/oak/commit/ca2c7503492732bcfd7e179f69716ef166fb1501).
  Gemini alone names no product ([Gemini naming no product](#gemini-naming-no-product-2026-09-27)),
  and a line that names Gemini CLI, Gemini Code Assist, Antigravity, Jules or another of the six
  tools stays with the rows of that tool. `Disclaimer: Brought by vibing with Gemini.` of
  react/react-native 208d445afa81 and react/yoga 984334ac83ca and `Disclosure: Gemini-assisted.` of
  bazel-contrib/buildtools d2a20dc9d547 name no part of the change and are not read.
- **The label `Disclaimer:`.** The rows of the labeled disclosure, `claude-full-body-08`,
  `codex-full-body-07`, `copilot-full-body-10`, `cursor-full-body-07`, `gemini-full-body-04` and
  `opencode-full-body-01`, and the rows of its form for other tools and for no tool,
  `tool-not-named-full-body-01`, `zcode-full-body-01`, `pi-full-body-01` and
  `openclaw-full-body-05`, also read the label `Disclaimer:`, which contributors use as they use
  `Disclosure:`, as in `Disclaimer: Claude Code was used to generate SOME of the code but mainly` of
  RimSort/RimSort
  [86d26f409177](https://github.com/RimSort/RimSort/commit/86d26f409177a65f258e92112d3addd0abd8d0fc),
  `Disclaimer: This PR has been co-authored by Copilot.` of mikro-orm/mikro-orm
  [673c1bb1c425](https://github.com/mikro-orm/mikro-orm/commit/673c1bb1c4250a67d65b301852af4b2d7c8195b1)
  and `Disclaimer: This PR is entirely claude+opencode generated.` of vercel/next.js
  [a4c1a0cd72e0](https://github.com/vercel/next.js/commit/a4c1a0cd72e0cfbe4d62fe244364a98fa209d463),
  which credits Claude and OpenCode. The limits of the labeled disclosure apply, so that
  `Disclaimer: I used Claude Code to evaluate and confirm comments from` above `#23328.` of
  pantsbuild/pants bf9bd54d0e30 stays unread. `claude-full-body-08` skips a line that names a Coder
  Agent, as `*Disclaimer: implemented by a Coder Agent using Claude Opus 4.6*` of coder/coder
  [90d8f56cc27b](https://github.com/coder/coder/commit/90d8f56cc27b2fafc8a4b551b1cb6612a785ca6f)
  does, because Coder Agents ran the model there, and `coder-agents-full-body-02` credits the line.
  The rows of a label of a pull request template, `claude-full-body-32` and the rows of its form,
  do not read the label, because they would also read `- Disclaimer: Claude came up with this` of
  apache/thrift 5abe53feaf59 and `Disclaimer: got Claude to cherry-pick and handle conflicts - I
  don't` of kagent-dev/kagent bddc80b6062b, which the labeled disclosure leaves unread.
- **The prefix of one developer's Copilot agent.** `copilot-full-body-44` reads
  `Copilot agent :copilot: (on behalf of @<login>):` at the start of a line, also in italics. One
  developer's Copilot agent begins the descriptions of the pull requests that it opens and its
  replies to review comments in Azure/azure-sdk-for-js and Azure/typespec-azure with it, and the
  squash merges copy it, as in Azure/typespec-azure
  [6f1688dda9cf](https://github.com/Azure/typespec-azure/commit/6f1688dda9cfe0b70833668910a833bacb209581)
  and Azure/azure-sdk-for-js
  [af5fa63c56e8](https://github.com/Azure/azure-sdk-for-js/commit/af5fa63c56e82dc5595f253b4531d2de666686d9).
  The commits of these pull requests carry no Copilot line. The agent still made them, because the
  pull requests were opened seconds after their first commit, and in pull request
  [40042](https://github.com/Azure/azure-sdk-for-js/pull/40042) the agent's reply under the prefix,
  `Updated all three affected suites to use ordinary describe`, describes the edit of commit
  57066ee47b2c. Every commit with the prefix in the commit CSV of the release credits Copilot
  through a line of Copilot CLI or through Copilot's cloud agent as the author, as
  [1c0a9668e4f7](https://github.com/Azure/azure-sdk-for-js/commit/1c0a9668e4f7b71e15b4093f0cbcfb44bba4e423)
  and [7e738d7554b7](https://github.com/Azure/azure-sdk-for-js/commit/7e738d7554b7848eb9f12ae2c8b3bc3b8eba5c20)
  do. A quoted line and the prefix without its colon are not read.
- **`Implementation and review: Codex`.** `codex-full-body-50` reads Codex first after the labels
  `Implementation and review:` and `Implementation and self-review:`, also in bold. One developer
  ends the squash merges of ccfddl/ccf-deadlines with them, as in
  `Implementation and self-review: Codex (GPT-6).` of
  [861254e3f6f5](https://github.com/ccfddl/ccf-deadlines/commit/861254e3f6f5c9a2401afff6bf12b8acd48d40ac)
  and `Implementation and review: Codex (GPT-6).` of
  [ba8cc5cd1608](https://github.com/ccfddl/ccf-deadlines/commit/ba8cc5cd160834813ec3ebd305523a921b8d54c3).
  The label names Codex as the agent that implemented the change, and a review by the same agent
  does not take the implementation away. GitHub's commit search finds the review form with Codex in
  other repositories as well, such as YUIAOI7592/BiliCDN_TW fc2b3b5ed58e.
  `AI-assisted implementation and review: Codex` stays with `codex-full-body-02`, and the label
  without its colon, as in `feat: serialize implementation and review Codex sessions`, is not read.
- **`AI authorship: Codex`.** `codex-full-body-51` reads Codex first after the key `AI authorship:`,
  as in `AI authorship: Codex (GPT-6, /root).` above
  `Independent review: Codex (GPT-6, /root/reconnect_review).` of fedimint/fedimint
  [df1d91bce450](https://github.com/fedimint/fedimint/commit/df1d91bce450a4037099050c5afe8f665fafd582)
  and its squash merge
  [0ac138f7bbc0](https://github.com/fedimint/fedimint/commit/0ac138f7bbc027582596c466840ef2be2e41443e).
  `/root` is the path of the root agent of Codex (`codex-rs/protocol/src/agent_path.rs` of
  openai/codex at
  [b528ff02b650](https://github.com/openai/codex/blob/b528ff02b6504e8399a5826900ada9a392e6bb48/codex-rs/protocol/src/agent_path.rs)),
  so the key credits the Codex agent that wrote the change. The review line names a subagent that
  only reviewed the change, and the row does not read it. GitHub's commit search finds the key with
  Codex in fedibtc/manifold and obafgkm42/random-marginalia as well.
- **`Contribution-attribution:` of elizaOS/eliza.** `codex-trailer-04` also reads the key
  `Contribution-attribution:`, as in
  `Contribution-attribution: OpenAI/gpt-5.6-sol via Codex Desktop multi-agent` of elizaOS/eliza
  [249c8881bdea](https://github.com/elizaOS/eliza/commit/249c8881bdea04cc290d3c41edfdf494a5175ba3)
  and [b68a23b6a3f7](https://github.com/elizaOS/eliza/commit/b68a23b6a3f752f2c9d747c998c657bef3f69b28),
  commits of one maintainer. The value joins the provider, the model and the client of the
  provenance footer of the repository's CONTRIBUTING.md, and the client after `via` names the agent
  tooling that made the change, as `Client: Codex desktop / multi-agent` does under
  `codex-trailer-14`. A value without a client is not read, and neither is the marker
  `<!-- contribution-attribution:v1 -->`.
- **`Assisted-by: Devin: <model>`.** `devin-trailer-03` also reads Devin followed by a colon and a
  model of Anthropic or OpenAI. That is the form that the AGENTS.md of apache/tinkerpop at
  [cbffc7a1406d](https://github.com/apache/tinkerpop/blob/cbffc7a1406dda8200f874069435c96370ec3711/AGENTS.md)
  prescribes "When AI tooling authored a non-trivial portion of a change", as in
  `Assisted-by: Devin: Claude Opus 4.7` of
  [8f88bba804ac](https://github.com/apache/tinkerpop/commit/8f88bba804accb2569eeb8f50b24ec8593c74907)
  and [0f592d4007ff](https://github.com/apache/tinkerpop/commit/0f592d4007ff2622785af4761fc090c426c75093),
  commits of one contributor. exa-labs/exa-js e6c0b7f7a25f has `Assisted-by: Devin:claude-opus-4.6`
  above Devin's own co-author line. The model is the model that Devin ran, and no Claude row reads
  it. `Assisted-by: Devin (GTP-5.6)` of appneta/tcpreplay 4d7f8c46af72, a misspelled model in one
  commit, stays unread.
- **`Initial prototype: Claude`.** `claude-full-body-63` reads `Initial prototype: Claude` when only
  Code, a model or the cost of the session follows up to the end of the line, as the maintainer of
  AntennaPod/AntennaPod writes it in `Initial prototype: Claude, $2.59` of
  [9a4a50ad7a66](https://github.com/AntennaPod/AntennaPod/commit/9a4a50ad7a66cf359de97d131daae26e3c2fc16f)
  and `Initial prototype: Claude, $1.99` of
  [d8623ecf0978](https://github.com/AntennaPod/AntennaPod/commit/d8623ecf097879014a8468cf4f3f6f46edc11c31).
  The line states that Claude produced the first version of the change, as `Initially generated by`
  does under `claude-full-body-05`. In the other commits that GitHub's commit search finds for the
  phrase, the text after the colon names the prototyped project, as in
  `Initial prototype: Claude token utilization dashboard` of harishnegi85/Token_Usage_Dashboard
  dd7d8a3316e0, and the row does not read them.
- **The model tag of ONLYOFFICE/DocSpace-client.** `tool-not-named-full-body-12` reads a model of
  Anthropic without Claude, in parentheses after `with AI` or at the end of a line that translates,
  adds translations or rewords. Developers of ONLYOFFICE/DocSpace-client tag the locale changes that
  a model made this way, as in `Translate new keys with AI (Opus 5)` of
  [44626506bb94](https://github.com/ONLYOFFICE/DocSpace-client/commit/44626506bb943f142edb3db74aa8d4309710f556)
  and `Common: Translate the link preview description (Opus 5)` of
  [c50620d197e7](https://github.com/ONLYOFFICE/DocSpace-client/commit/c50620d197e7852ec3008e344eaa5ab565b0dfac),
  commits that change only locale files. The repository's skill translate-locales
  (`.claude/skills/translate-locales/SKILL.md` at 71848af53588) has the agent "Translate new or
  broken i18n keys across all supported languages". A model alone counts under `tool not named`.
  The scan lists only the commits with a scope before a colon, and GitHub's commit search found the
  others in the collected history. A line about a model, as
  `Shared:Mock: add haiku and sonnet inside model aliases` of 62de9ec73066, is not read.
- **The Claude tag of ONLYOFFICE/DocSpace-client.** `claude-full-body-64` reads `AI translate` at the
  start of a line with `with` and a Claude model at the end, also with `v` before the version. That
  is the earlier tag of the same developer, as in `AI translate keys with Claude Opus v4.8` of
  [99fae7685d69](https://github.com/ONLYOFFICE/DocSpace-client/commit/99fae7685d692867d7a5ee033c7552c56fed55ae)
  and `AI translate new keys with Claude Opus v4.8` of
  [d0942bc69582](https://github.com/ONLYOFFICE/DocSpace-client/commit/d0942bc69582664dfdbb7c64f8ba4c93971dd3d6),
  commits that change only the Common.json files of the locales. The developer also writes
  `AI translate new keys (claude opus 4.8)`, which `claude-full-body-25` reads. A Claude model counts
  for Claude, and `Apply 542 translation fixes from Claude Sonnet 4 spell-check` of 95c861c7138c is
  not read.
- **The Claude form under the openers of `claude-full-body-08` and under `Author:`.**
  `copilot-full-body-45`, `codex-full-body-52`, `cursor-full-body-39` and `opencode-full-body-37`
  read a line that names Claude and then their tool after `via` or in parentheses. The line is
  under `AI-Tool:`, `X-AI-Tool:`, `AI-Agent:`, `AI-Assistant:`, `Coding agent:` or `Tools used:`, or
  under `Author:` with Claude Code or Claude before a model family, as
  `X-AI-Tool: Claude Code via Cursor` and `Tools used: Claude Code (opencode)`.
  `claude-full-body-08` and `claude-full-body-65` skip such a line because the other tool ran the
  model of Claude. `copilot-full-body-10`, `codex-full-body-07`, `cursor-full-body-07` and
  `opencode-full-body-01` read these openers only with the tool at the start of the value, so the
  line credited no tool. The rows follow `copilot-trailer-04`, `codex-trailer-04`,
  `cursor-trailer-05` and `opencode-trailer-04`, which read the Claude form of a trailer. They leave
  out what `claude-full-body-08` leaves out: a tool that was not used, code that is all the author's
  own and Claude Code used to confirm. No commit of the commit logs has such a line. The openers are
  those of aws/sagemaker-python-sdk
  [3c6d362f46b1](https://github.com/aws/sagemaker-python-sdk/commit/3c6d362f46b1ef703c2a41641342d53ee319f920)
  and gkoreli/backlog-mcp
  [92a9fc742058](https://github.com/gkoreli/backlog-mcp/commit/92a9fc742058658a6125d6aa1a89ba9b59666e1b).
- **`Author:` with Claude Code or a Claude model.** `claude-full-body-65` reads Claude Code, or
  Claude before a model family, as the value of the key `Author:`. Examples are
  `**Author:** Claude Code (claude-opus-4-7)` of jj1bdx/airspy-fmradion
  [29fa28bf3cd9](https://github.com/jj1bdx/airspy-fmradion/commit/29fa28bf3cd918bfa41ba68a94bf9448022918cd)
  and [32a0b8ca63e4](https://github.com/jj1bdx/airspy-fmradion/commit/32a0b8ca63e4b9913f50644913c5b792673c2478),
  `- Author: Claude Code (claude-opus-5-5), headless, with Dev Desk.` of UKGovernmentBEIS/inspect_ai
  [27f18ff7d5bd](https://github.com/UKGovernmentBEIS/inspect_ai/commit/27f18ff7d5bd4b8d7752912c1e0cd28e2e912e88)
  and `Author: Claude Opus 5.5 (claude-opus-5-5)` of mikemccand/luceneutil
  [db58689ed8e9](https://github.com/mikemccand/luceneutil/commit/db58689ed8e9ad370ac413bdba456ae50e06eb29).
  mikemccand/luceneutil 3fbe8fdd7b45 writes
  `Author: Claude Opus 4.7 (1M context, default thinking mode -- not /fast)`. The key names what
  wrote the change. In jj1bdx/airspy-fmradion the line heads the record of the fixes that the
  message carries below `Suggested by Claude Code, verified by Kenji Rikitake`. Its list of the
  files modified is the list of the files that the commit changes. It states that
  `cmake --build build --target all` succeeded after all changes and that `clang-format -i` was
  applied, and it marks V25 as not applied per user instruction, so Claude Code made the change and
  the person verified it. The commits of inspect_ai and luceneutil already credit Claude through
  other rows. The row does not read a bare Claude, as in the header of a git log, or Claude before
  another capitalized word, because these can name a person.
  `**Reviewer:** Claude Code (claude-sonnet-4-6)` of 32a0b8ca63e4 names a review and is not read. A
  line with another tool after `via` or in parentheses goes to the row of that tool.
- **`Author: Codex`.** `codex-full-body-53` reads Codex as the value of the key `Author:`. One
  example is the line below the squash list of machinewrapped/llm-subtrans
  [e188600a2ef9](https://github.com/machinewrapped/llm-subtrans/commit/e188600a2ef9835c00a4c91ee15b143da3599bf9),
  the squash merge of pull request 353 from the head branch
  `codex/fix-gap-field-calculation-in-addline`, whose description ends with the link of a task of
  Codex cloud. Another is `Author: Codex (GPT-5)` below an item of the squash list of
  0xPolygon/polygon-cli
  [15eaa215d174](https://github.com/0xPolygon/polygon-cli/commit/15eaa215d174eb8e55162aea3bd9037efc911d4e),
  which other rows credit to Codex. Codex before another tool in parentheses is not read.
- **The header `Author:` with the address of Cursor's agent.** `cursor-full-body-40` reads
  `Author:` with the address `cursoragent@cursor.com`. One example is the listed commit
  4570d9c4b5e5 below `Squashed commit of the following:` in AnswerOverflow/AnswerOverflow
  [8e594791d951](https://github.com/AnswerOverflow/AnswerOverflow/commit/8e594791d95105c722cb4774b18b0e06aacb53d2),
  which adds the package that the listed commit introduces. Another is the header below an item of
  the squash list of medplum/medplum
  [7607b750e956](https://github.com/medplum/medplum/commit/7607b750e9566ce4f5f8992e79d997a72e193da3).
  The squash records the change of the listed commit, and git writes the header from the identity
  that Cursor's agent writes for its commits, so the line is `standard`, as `cursor-trailer-06` reads
  the address in a co-author line. The row reads the header line alone.
- **The header `Author:` with the address of Copilot's apps.** `copilot-full-body-46` reads `Author:`
  with the noreply address of the GitHub account Copilot, apart from that of the person 292387.
  Examples are the listed commits 83ab706d94e9 and d1a49c93fcfc of `copilot-swe-agent[bot]` in
  corepunch/open-realm
  [24354a8c147d](https://github.com/corepunch/open-realm/commit/24354a8c147de9430ba124076813896346abdd7e)
  and the listed commit b9e1046d5fbc in Belphemur/SoundSwitch
  [875c619a7849](https://github.com/Belphemur/SoundSwitch/commit/875c619a7849ebf0ba55ae9bc42d4fa2085a1bf0).
  Each squash changes the files of the listed commits of Copilot's cloud agent, and the line is
  `standard`, as `copilot-trailer-05` reads the address in a co-author line.
  corepunch/open-realm 24354a8c147d, credited to Claude, gains Copilot. In pollinations/pollinations
  [9224acc1abb9](https://github.com/pollinations/pollinations/commit/9224acc1abb9a77dbf7b2dda843d2f36fc87cede)
  and 3253fa9a720e, the listed commit effc335a5410 reverts the pull request of the listed Copilot
  commits. This is the known limit of a squash list, and `copilot-full-body-01` credits both
  commits already.
- **`Author: Emma Antigravity`.** `gemini-full-body-35` reads Antigravity in the value of the key
  `Author:`, as the whole body of mas-bandwidth/netcode
  [39b19b616fee](https://github.com/mas-bandwidth/netcode/commit/39b19b616feec9027ac50bdfc191359cbeec9402)
  and mas-bandwidth/yojimbo
  [3dc9bd1e81cb](https://github.com/mas-bandwidth/yojimbo/commit/3dc9bd1e81cb3c7ae83c4f1e7916315cf9e4ed17).
  Emma is an agent of the developer that runs under Antigravity.
  [docs/FAQ.md of mas-bandwidth/nova at 18625aaaa892](https://github.com/mas-bandwidth/nova/blob/18625aaaa892910d720e372e4434685cf8137e38/docs/FAQ.md)
  states "Emma took the foundation under Antigravity on 2026-09-08" beside "Stella Codex has
  continued under Codex since 2026-09-04". mas-bandwidth/nova-tools
  [a5d220057c7e](https://github.com/mas-bandwidth/nova-tools/commit/a5d220057c7e6cee7e505211ec405225cd3dc6b1),
  which implements the adapter of the Antigravity harness, ends in
  `Co-authored-by: Emma Antigravity <emma@mas-bandwidth.com>`, which `gemini-trailer-02` reads. A
  credit of Antigravity counts for Gemini, as `gemini-trailer-02` reads it, and a line that ends in
  `(review)` is not read.
- **`Agent-Authored-By:` of the skill authorship-disclosure.** The skill of joshlf/anneal at
  [65de4e8bd5f0](https://github.com/joshlf/anneal/blob/65de4e8bd5f08f010418b5d3a3828d99285d491b/skills/authorship-disclosure/SKILL.md)
  prescribes `Agent-authored-by: AI agent acting on <user>'s behalf`, where "`AI agent` is literal
  wording, not a placeholder for ChatGPT, Codex, a model, vendor, or individual agent name". It
  adds the suffix ` (content only)` for an agent that contributed only to the change and
  ` (commit message only)` for one that contributed only to the message.
  `tool-not-named-trailer-28` reads the line, as in rust-lang/rust
  [4b5434b7106e](https://github.com/rust-lang/rust/commit/4b5434b7106ea7150c1bf7de4f6baf8dab16f5b3),
  [c4d1307f90f9](https://github.com/rust-lang/rust/commit/c4d1307f90f94506be5fa46bab83c47b2e31e339),
  [ac2995485253](https://github.com/rust-lang/rust/commit/ac2995485253da106df073b7011bc90987aa17ac)
  and [eb7625d36112](https://github.com/rust-lang/rust/commit/eb7625d36112714dd2f074cf41d86f29356fcbff).
  It does not read the suffix `(commit message only)`, because a message that a tool writes about a
  person's change does not count, and `Commit-Message-Authored-By:` is another token. The scan of
  the key-value lines leaves the line out because the value names no tool, and GitHub's commit
  search supplied the commits.
- **Amp's identity as the author.** `amp-author-01` reads `Amp <amp@ampcode.com>` as the author, as
  `account`. It is the identity under which Amp's agent commits in orbs when a project credits its
  commits to Amp. Amp's page [GitHub & Git](https://ampcode.com/docs/github) states "Amp makes Amp
  <amp@ampcode.com> the author and adds you as a co-author." and "Until you choose, new Amp-hosted
  projects make you the author and other new projects make Amp the author." (read 2026-11-09).
  Examples are BasedHardware/omi
  [6b6929eb84d1](https://github.com/BasedHardware/omi/commit/6b6929eb84d1ba770be3845a84a2e124bd28b7e2)
  and tursodatabase/turso
  [af57876d7764](https://github.com/tursodatabase/turso/commit/af57876d77645406a347641c0508d9aa0ec2c0a9),
  and buildkite/agent
  [3cfaf69d567c](https://github.com/buildkite/agent/commit/3cfaf69d567c9bdd3154a5ab6c54c3845857da0a),
  which a person committed. When Amp starts an orb, "it sets GIT_AUTHOR_* and GIT_COMMITTER_* from
  the selected identity" ([No Mailmap Required](https://ampcode.com/news/no-mailmap-required), read
  2026-11-09). Amp's own commit therefore has Amp as the author, and Amp as the committer of another
  author's commit marks a commit that an orb rewrote. An orb rewrote the history of
  darrenburns/posting on 2026-10-12: the maintainer's commits, such as
  [4ce59865e4b8](https://github.com/darrenburns/posting/commit/4ce59865e4b828eb86934ccf4c0f002352ab807f)
  of 2026-10-01, commits with Claude Code's co-author line, and the merge of pull request 387,
  which GitHub merged on 2026-09-30 and which the history now holds as
  [91f3d2be3e26](https://github.com/darrenburns/posting/commit/91f3d2be3e26559a132367335b6c8efde9d67c99).
  Amp did not produce these changes, so the row reads the address only as the first address of the
  author and committer fields, which is the author's, as `aider-author-01` reads aider's name only
  as the author's. tursodatabase/turso
  [3fc34bf6a572](https://github.com/tursodatabase/turso/commit/3fc34bf6a572bfe43eec4c0718a34de65b38264c),
  with Claude Code's address as the author and Amp as the committer, credits Amp through its
  `Amp-Thread-ID:` line.
- **Amp's commits that Claude Code rebased.** TriliumNext/Trilium has commits that
  `Amp <amp@ampcode.com>` authored on 2026-09-02 and that Claude Code's address committed on
  2026-09-13, such as
  [709d32a2206a](https://github.com/TriliumNext/Trilium/commit/709d32a2206abdbe8b4cdc724211baf4cdc0c15e)
  and [f24026457e49](https://github.com/TriliumNext/Trilium/commit/f24026457e497602aee3f9977966c89b12fb6b65),
  each with `Agent-Signature: amp-gpt-5.6-sol-high on behalf of Amp`. A session of Claude Code
  rebased the commits of Amp, and the changes are Amp's, which `amp-author-01` and
  `amp-trailer-03` credit. `claude-author-04` credits Claude as well, because it reads Claude Code's
  address as the committer of the commits of an Anthropic-hosted session, which have a person as
  the author, and the author and committer fields do not tell a rebase from such a commit. The
  credit of Claude on these commits stays as a known limit.
- **Amp's `Amp-Thread-ID:` trailer.** `amp-trailer-04` reads the trailer with which Amp's agent
  links the thread that made a commit, as `standard`. The page
  [GitHub & Git](https://ampcode.com/docs/github) states "Commits the agent makes through its shell
  also carry an Amp-Thread-ID: trailer with the URL of the thread" and "Threads that run on a runner
  or in a local CLI session use the Git configuration and credentials of that machine." (read
  2026-11-09).
  The line is therefore the only credit of a commit that a person authored when the project leaves
  out Amp's co-author line, as in tursodatabase/turso
  [4ba892105f97](https://github.com/tursodatabase/turso/commit/4ba892105f97086bee4191a6ce91e873cf9cb6ba).
  A squash merge lists the line under the commits that Amp made, as foundry-rs/foundry
  [4a76e92c7d0c](https://github.com/foundry-rs/foundry/commit/4a76e92c7d0c6bb63970d30210efb6b232454657)
  does, and a squash merge that lists commits of several tools credits each of them. The row also
  reads the id of the thread without the URL, as in the commit of Amp of laravelio/laravel.io
  [81b4680eb8b0](https://github.com/laravelio/laravel.io/commit/81b4680eb8b0307e5cd22ffd23220309dfa4e383),
  and the key `Amp-Thread:`, as in 567-labs/instructor
  [4a73da05b8da](https://github.com/567-labs/instructor/commit/4a73da05b8da25c69f83e67cc4bfab019ad9de7b).
  A Markdown link to a thread in prose, as the link `implementation thread` of
  Comfy-Org/ComfyUI_frontend
  [17b0d4e40088](https://github.com/Comfy-Org/ComfyUI_frontend/commit/17b0d4e400883436662fc5b53371e239b3811542),
  and the URL of a thread on a line of its own are not read, because such a link can point to a
  thread that only planned the change.
- **`Amp-Thread-ID:` wrapped by GitHub.** `amp-full-body-01` reads the key of `amp-trailer-04` at the
  end of a line with the URL of the thread, or a Markdown link to it, at the start of the next line,
  as `standard`. GitHub wraps the line this way in the message of a pull request that it merges
  under the request's description, in a squash merge and in a merge commit alike, as in the merge
  commit 567-labs/instructor
  [fe83629b1016](https://github.com/567-labs/instructor/commit/fe83629b1016a514d29bfe58b49ee740900331d5),
  the squash merge coder/coder
  [9780d0295cd9](https://github.com/coder/coder/commit/9780d0295cd9b67bf0dd31c191e618d526c63781)
  and oxidecomputer/console
  [129a4a5529b9](https://github.com/oxidecomputer/console/commit/129a4a5529b9de0007f3d0966f7c802c272b48eb).
  In oxidecomputer/terraform-provider-oxide
  [846cc65ff947](https://github.com/oxidecomputer/terraform-provider-oxide/commit/846cc65ff947ad282af66344b498998170c11959)
  the next line holds a Markdown link, and its pull request 883 has the commit fe7fca49f5e5 with
  `Amp-Thread-ID:` and `Co-authored-by: Amp <amp@ampcode.com>`. A key without a value on its line is
  no trailer line, so `amp-trailer-04` cannot read it, and a key followed by an empty line is not
  read.
- **Amp in a labeled disclosure.** `amp-full-body-02` reads Amp after a verb of making and by, with,
  using or via in a labeled disclosure, as `convention`, as one contributor of rolldown/rolldown
  writes it: `AI disclosure: this fix was generated by Amp.` of
  [ffb9395b6c6d](https://github.com/rolldown/rolldown/commit/ffb9395b6c6d9f5578288ce91e5d699164bc79c8),
  `AI disclosure: I implemented this change using Amp.` of
  [ec5bab883204](https://github.com/rolldown/rolldown/commit/ec5bab883204dcce5fd431facad77917dd00bde7),
  `AI disclosure: this entire fix was implemented with Amp.` of
  [c4096ce04636](https://github.com/rolldown/rolldown/commit/c4096ce04636cc34b501cf56e441faa11330cb11)
  and `AI disclosure: all the code in this PR was written using Amp, pointed at` of
  [f80cd6500f27](https://github.com/rolldown/rolldown/commit/f80cd6500f27f17c0cb2406219807d8ad3072d94),
  each followed by a link to the Amp thread that produced the change. Amp's site states "Amp is a
  coding agent and development environment built for the frontier."
  ([ampcode.com](https://ampcode.com), read 2026-11-09). Amp has to end the clause, because AMP also
  names automatic mixed precision and Accelerated Mobile Pages. A negation, a disclosure of the
  description alone and AMP in a later sentence are not read, as for the labeled disclosures of the
  other tools.
- **The identities of Agent Kanban's agents.** `agent-kanban-author-02` reads an address at
  `mails.agent-kanban.dev` as the author or the committer, as `account`, because the platform's code
  writes it. `buildAgentEnv` in `packages/cli/src/daemon/dispatcher.ts` of saltbo/agent-kanban at
  [70a1f3548f8a](https://github.com/saltbo/agent-kanban/blob/70a1f3548f8a27edbb99ba846af66fd9f741a742/packages/cli/src/daemon/dispatcher.ts)
  and `agentGitIdentityEnv` in `apps/web/server/taskDispatch.ts` at
  [33bd22f23648](https://github.com/saltbo/agent-kanban/blob/33bd22f236489b58110ecff8e1fcbc98d2aef072/apps/web/server/taskDispatch.ts)
  set `GIT_AUTHOR_*` and `GIT_COMMITTER_*` to the agent's name and
  `${agentUsername}@mails.agent-kanban.dev` (read 2026-11-09). Examples are
  `Noah Reed <noah-reed@mails.agent-kanban.dev>` of saltbo/zpan
  [63ceeab67826](https://github.com/saltbo/zpan/commit/63ceeab6782668e82ecd9c786cbaa72a9579f4b5) and
  `Marina Zhou <marina-zhou@mails.agent-kanban.dev>` of
  [d7454a61fba3](https://github.com/saltbo/zpan/commit/d7454a61fba3dfd03f9ebebc5bb9bb881b1c03b6),
  which a person committed and which `agent-kanban-trailer-02` also credits through their
  `Agent-Profile:` lines. The identity names the platform and not the agent that ran, as
  `agent-kanban-author-01` does. The row reads the committer too, because the commit logs have no
  commit with the address as the committer of another author's commit, the case that made
  `amp-author-01` read the author alone.
- **The identity of Junie's GitHub Action as the author.** `junie-author-02` reads
  `junie-agent <247260674+junie-agent@users.noreply.github.com>` as the author or the committer, as
  `account`. `src/github/operations/auth.ts` of JetBrains/junie-github-action at 7e0a3d3059e3 states
  "Always use junie-agent account for git commits" and sets `git config user.name` and
  `git config user.email` from the constants of `src/constants/github.ts` (read 2026-11-09), as in
  hossain-khan/hossains-dev-bytes
  [7cc88c1f4fcd](https://github.com/hossain-khan/hossains-dev-bytes/commit/7cc88c1f4fcd2bd488af65276ec066cfbdb8ba27)
  and nikships/keep-agent-mem
  [1e7dbea1edc8](https://github.com/nikships/keep-agent-mem/commit/1e7dbea1edc86edd8d72b19b70bc2e816a1a78e3).
  The configured repositories squash the action's pull requests, so that the identity is in their
  commit logs only in the co-author lines that `junie-trailer-01` reads, and GitHub's commit search
  finds the address as the committer only of commits that it also authored.
- **Grok Bot.** Grok Bot, the agent app of SpaceXSI, counts as a tool of its own, neither Cursor nor
  `tool not named`. xAI launched it on 2026-08-11 with "its own usage, separate from your Grok and
  Cursor plans" ([xAI](https://x.ai/news/introducing-grok-bot)), xai-org/grok-build defines its
  harness tools, and Cursor, which documents it and provides its account and its cloud computer,
  states "Grok Bot is not tied to a single model." and that it "is not governed by your team's
  Cursor model allowlist or blocklist" ([Cursor](https://cursor.com/help/grok-bot/models)), both read
  2026-11-09. `grok-bot-trailer-01` reads Grok Bot first in a co-author, assistant or generator line
  or after `Impl-Model:`, as `Assisted-by: Grok Bot` of ansible-collections/community.general
  [e731634293f9](https://github.com/ansible-collections/community.general/commit/e731634293f979accf72f3aaba8be76bdae6a84d),
  `Co-authored-by: Grok Bot (AI agent) <noreply@x.ai>` of lnreader/lnreader-plugins
  [b8e22019bbb6](https://github.com/lnreader/lnreader-plugins/commit/b8e22019bbb6f0c4ee61956ba1ab99a73bffd7e1)
  and `Impl-Model: grok-bot` of DemonDamon/AgenticX
  [7e9a4b9d6d6b](https://github.com/DemonDamon/AgenticX/commit/7e9a4b9d6d6b233c6552593d70d2c2767c7b4352),
  and Grok Bot in parentheses, also after via, as
  `Co-authored-by: Shai Shmuel (via Grok Bot) <noreply@example.invalid>` of the squash merge
  superlinked/sie
  [93efb5605537](https://github.com/superlinked/sie/commit/93efb5605537118f94bfaf9f57baa45706855ffc),
  whose pull request commit
  [1f89cfd67c98](https://github.com/superlinked/sie/commit/1f89cfd67c9860a78e5b1023b26def81848ef337)
  has that author and the committer `Cursor Agent <cursoragent@cursor.com>`.
  `tool-not-named-trailer-03` leaves Grok Bot out of the Grok models, and
  `tool-not-named-trailer-15` leaves out a name that contains Grok Bot. `Plan-Model: grok-bot` stays
  unread, as `Plan-Model:` does for every model. Grok Bot pushes its commits as
  `Cursor Agent <cursoragent@cursor.com>`, which `cursor-author-04` credits to Cursor until the
  maintainer decides on such a commit.
- **Tali.** `grok-bot-trailer-02` reads the co-author line of Tali, the agent account talibot-agent,
  at its GitHub noreply address and at talibot@mail.grokbot.com, as `standard`, and
  `grok-bot-author-01` reads Tali's git identity `Tali (AI agent) <talibot@mail.grokbot.com>` as
  `account`, as in BasedHardware/omi
  [59276ad5b7c4](https://github.com/BasedHardware/omi/commit/59276ad5b7c4a4a9962f5ebb149d33e8bb9c4ee1)
  and the squash merge
  [8e528d159c2f](https://github.com/BasedHardware/omi/commit/8e528d159c2f557da58833271b210ef04ddbb7cf).
  Tali's own repository grounds the tool: "The code, tests, README and reports were written by Tali,
  an autonomous AI agent (Grok Bot), working for Mykel Nelson." (README.md of
  talibot-agent/namescout at
  [4f501c4eca6c](https://github.com/talibot-agent/namescout/commit/4f501c4eca6c3f523de83655bf324d72fb74a0c4),
  read 2026-11-09), as the README of lodekeeper grounds Lodekeeper for OpenClaw. Another address at
  mail.grokbot.com stays unread (see the address at mail.grokbot.com below).
- **`Client: Grok Bot.app`.** `grok-bot-full-body-01` reads Grok Bot as the client of the provenance
  footer of elizaOS/eliza, also as its macOS app Grok Bot.app, in either form of the key, as in
  `Client: Grok Bot.app` below `AI-provider-model: xAI / grok-4.6` of
  [f6b26ae1f344](https://github.com/elizaOS/eliza/commit/f6b26ae1f3446a092c53ec68b0d24ebaa261fb0a),
  [68fd3469d911](https://github.com/elizaOS/eliza/commit/68fd3469d911276bda58c073da37cf858eedb957)
  and [785ee54579bf](https://github.com/elizaOS/eliza/commit/785ee54579bf9ef1492fab2e38bb9a3153cebfd5).
  The description of pull request 23199 states `Agent tooling: Grok Bot.app`. `Client: grok` of
  49b173b4dc5e stays unread for the reason that
  [The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)
  gives.
- **An AI coding assistant (Grok Bot).** `grok-bot-full-body-02` reads Grok Bot in parentheses after
  an AI agent, assistant or tool, after a verb of making or I used, the form of
  `cursor-full-body-29`, as
  `Use of AI: fix and tests written with an AI coding assistant (Grok Bot).` of
  vllm-project/llm-compressor
  [afad978ad0cd](https://github.com/vllm-project/llm-compressor/commit/afad978ad0cda27434dc30abf0b1dc7ee972cbd3)
  and `The fix and the test were written with an AI coding assistant (Grok Bot).` of
  vllm-project/guidellm
  [9e0a49a9a8d4](https://github.com/vllm-project/guidellm/commit/9e0a49a9a8d4b7f219db3706a6dcb8d7de341297).
  One contributor wrote these commits, and each changes a source file and a test.
- **Grok CLI is Grok Build.** `grok-build-trailer-02` and `grok-build-full-body-02` take over the
  patterns of `grok-cli-trailer-01` and `grok-cli-full-body-01`, which
  `REMOVED_COMMIT_HEURISTIC_IDS` now lists, and credit Grok Build, as `grok-build-full-body-01` reads
  `Client: Grok CLI`: xAI's installer at x.ai/cli names itself `Grok CLI installer`, and Warp's
  [2718b665844e](https://github.com/warpdotdev/warp/commit/2718b665844ed332f4abb3ecdc0110df493db7c1)
  adds "first-class Grok Build (SpaceXAI's `grok` CLI)" and closes the issue "Add Grok CLI (xAI) as
  a supported third-party CLI coding agent". superagent-ai/grok-cli has no commit after 2026-05-15
  and lists no model after grok-4.3, while `Assisted-by: Grok CLI:grok-4.7` of RustPython/RustPython,
  as in
  [0ebc6570e3a5](https://github.com/RustPython/RustPython/commit/0ebc6570e3a53cc1c60e0bdf0d054cf70749829a),
  dates from 2026-09-23 to 2026-09-25, and
  `Implemented with Grok (effort unknown) in Grok CLI via Claude Code.` of
  stickerdaniel/linkedin-mcp-server
  [f45c41337eaa](https://github.com/stickerdaniel/linkedin-mcp-server/commit/f45c41337eaa514621fc3eb7ecee6d353a5c135a)
  from 2026-10-31. The same author writes
  `Assisted-by: Grok Build (model: unknown) and Claude Code (model: Claude Opus 5.5), autonomous` in
  github/spec-kit
  [e9ebeeaec6bd](https://github.com/github/spec-kit/commit/e9ebeeaec6bdae2b7079be4a0c68a9e802a248b2).
  The label Grok CLI goes away.
- **Grok Build 0.1 and Kimi Code 2.7 in sebo-b/warp.** The AGENTS.md of sebo-b/warp at
  [7654eeb2ea29](https://github.com/sebo-b/warp/blob/7654eeb2ea29f4ba8d18f3f326c703157adebcdb/AGENTS.md)
  tells an agent to "attribute yourself with a `Co-Authored-By:` trailer under your own agent
  identity (name + email)" and lists models as the identities, among them `GLM 5.2 <glm-5.2@z.ai>`,
  `Grok Build 0.1 <noreply@x.ai>` and `Kimi Code 2.7 <noreply@moonshot.ai>`. xAI lists the model Grok
  Build 0.1, `grok-build-0.1`, as "SpaceXSI's intelligent coding model for agentic software,
  engineering, and workflow tasks." ([xAI](https://docs.x.ai/developers/models/grok-build-0.1), read
  2026-11-09), and the CLI of Grok Build, the npm package `@xai-official/grok`, had moved to version
  0.2.0 on 2026-05-26, before
  [74d3bbd25624](https://github.com/sebo-b/warp/commit/74d3bbd25624764badde69cc6612dd2988feccfb) of
  2026-06-15. Kimi Code CLI had no version 2.7: `@moonshot-ai/kimi-code` was at versions 0.19.0 to
  0.23.0 when
  [510889b38b06](https://github.com/sebo-b/warp/commit/510889b38b06ec8b886826788be82e7fe26dd780) and
  [e9226d00caf6](https://github.com/sebo-b/warp/commit/e9226d00caf654beed3c37b896f705698933fabc)
  were written, and the section Vision delegation (Kimi Code) of the AGENTS.md at 40eca9585156
  names the model Kimi K2.7 Code, `neuralwatt/kimi-k2.7-code`. `grok-build-trailer-01` therefore
  leaves out `Grok Build 0.1` at an address of xAI, `kimi-code-trailer-01` leaves out
  `Kimi Code 2.7` at an address of Moonshot AI, and `tool-not-named-trailer-03` and
  `tool-not-named-trailer-05` read the two as models. `Co-Authored-By: kimi code <kimi@moonshot.cn>`
  of [f55a0d8be0fa](https://github.com/sebo-b/warp/commit/f55a0d8be0fa074748f4bd424429b2bc612105d4),
  a single line without a version, stays with Kimi Code. The maintainer checks the exclusion and
  this line again once Kimi Code CLI publishes a version 2.7.
- **Freebuff CLI.** `codebuff-full-body-01` reads Freebuff CLI or Codebuff as the client of the
  provenance footer of elizaOS/eliza, also with Buffy or Buffy agent in parentheses, as
  `Client / agent tooling: Freebuff CLI (Buffy agent)` below `AI provider/model: mimo / mimo-v2.5` of
  [70cf040550ce](https://github.com/elizaOS/eliza/commit/70cf040550ce1f6fe7cd7184f17b719c39228820)
  and [74d03c28aac2](https://github.com/elizaOS/eliza/commit/74d03c28aac2dc92b9d02bfdcb17c87427fa2855),
  and credits Codebuff. The README of CodebuffAI/freebuff, the repository of `codebuff-trailer-01`
  under its new name, lists "Freebuff CLI" and states "Freebuff is built on
  [Codebuff](https://codebuff.com), the open multi-agent framework that powers its orchestration,
  tools, and SDK." (663b77429c97, read 2026-11-09), and its agent Buffy is "the AI agent behind the
  product, ${isFreebuff ? 'Freebuff' : 'Codebuff'}" (agents/base2/base2.ts). Freebuff alone stays
  unread, because "Freebuff Desktop can also run locally installed Claude Code and Codex agents".
- **`Chromium change:` in the singular.** `commits._CHROMIUM_CHANGES` reads the singular, so that both
  scopes leave the commit of Chromium that brave/brave-core quotes below it empty, as
  [The key-value lines that no heuristic credited, read 2026-11-08](#the-key-value-lines-that-no-heuristic-credited-read-2026-11-08)
  describes for 4b67ac68ac97 and e2ca7c1287c6.

No row reads these lines, by decision:

- `Written by AI: claude-sonnet-5, claude-opus-5` of k0sproject/rig
  [502c8006792e](https://github.com/k0sproject/rig/commit/502c8006792ea762edb3880307fbb97e2a9b3042),
  `Written by AI: claude-opus-5` in an item of the squash merge
  [6a62a4e4bfa8](https://github.com/k0sproject/rig/commit/6a62a4e4bfa8f430b22da1ed7d33a741026dabfc)
  and `Written by AI: claude-sonnet-5` of
  [f0156f358c4a](https://github.com/k0sproject/rig/commit/f0156f358c4aa01489ec5c052730a6f355a87da8).
  The line is the attribution that the instructions file
  [platform/rules/ai-attribution.md](https://github.com/james-nesbitt/collab-agent/blob/20f9a879eb69500792c03cd0fbb6b8bdd24f89d0/platform/rules/ai-attribution.md)
  of one of the two developers prescribes for "issue bodies, issue comments, PR bodies, and PR
  review comments", in the format "`Written by AI: <model-name>`", and it states "It does NOT apply
  to non-free-text field updates ... nor to source code." k0sproject/rig ends issues with the same
  line, and the commits carry it from a pull request description that a squash merge copied or
  from the message of a commit of the pull request. The line therefore records that a model wrote
  the text, which does not count
  ([Disclosures written into a message body](#disclosures-written-into-a-message-body-read-2026-09-26)).
- `Assisted-by:` with an agent before a model that no page describes as a coding agent, in the form
  `AGENT_NAME:MODEL_VERSION` that Zephyr, mudler/LocalAI and apache/tinkerpop prescribe, where the
  first field names the agent: `localai-org-maint-bot:glm5.2 [gh]` of mudler/LocalAI
  [51c0a44bcadd](https://github.com/mudler/LocalAI/commit/51c0a44bcadda88839ccdc3a3c84faa70807c856)
  and other commits, the account that opens most pull requests of the LocalAI organization,
  `LocalAI-Agent:qwen3.8-27b-heretic-abliterated-uncensored` of
  [281b43a6938d](https://github.com/mudler/LocalAI/commit/281b43a6938d4400d049162f8ef1dd7c388818cb),
  `docs-library-luna:GPT-6` of `SocketDev/socket-registry`
  [fb281a9d19ac](https://github.com/SocketDev/socket-registry/commit/fb281a9d19ac478d5138a36963992f0353740df0),
  `dot:GPT-6.1` of RustPython/RustPython
  [f0b1e8265260](https://github.com/RustPython/RustPython/commit/f0b1e82652607742adc93bd59f51595b362d5c59),
  `WarpForgeAgent:DeepSeek-v4-flash` of
  [5c07fa4bd007](https://github.com/RustPython/RustPython/commit/5c07fa4bd00760a31f3d2b4705e35f09102f9116),
  `YuanSheng:DeepSeek-V4-Flash` of MariaDB/server
  [3cdcd64fb042](https://github.com/MariaDB/server/commit/3cdcd64fb042f4a31e82613ef2835c37fcafcc3d),
  `henk:claude-fable-5` of ggml-org/llama.cpp
  [08b1d2aea577](https://github.com/ggml-org/llama.cpp/commit/08b1d2aea577ed407c11ab13fbb174a28f6e65dd)
  and ggml-org/whisper.cpp
  [fbcb94d07ffd](https://github.com/ggml-org/whisper.cpp/commit/fbcb94d07ffdde4a46f98476ed29e970ee0ef493),
  `atlas:deepseek-v4-pro` of hyperspy/hyperspy
  [c19802b58d8a](https://github.com/hyperspy/hyperspy/commit/c19802b58d8a9c61c4bfcf35388f4aa8361af1d9),
  `agent-hamza:gpt-5` of nextcloud/contacts
  [edaed5ba4391](https://github.com/nextcloud/contacts/commit/edaed5ba4391ac760fe1166ca552cc58e1579ad5),
  `ByteAsk / GPT-5.4` of AcademySoftwareFoundation/OpenImageIO
  [ca7248c82961](https://github.com/AcademySoftwareFoundation/OpenImageIO/commit/ca7248c829617f6c2a4f065c163f004e6a53866c),
  `Chai Bot (Claude Opus 5)` of flightctl/flightctl
  [bcf967844d42](https://github.com/flightctl/flightctl/commit/bcf967844d427238c4145ddac43da306714ea5c1)
  and `chai-bot & Claude Sonet 4.6` of coreos/coreos-assembler
  [c17f00d04382](https://github.com/coreos/coreos-assembler/commit/c17f00d043825532d206c79c7183199f23b2793f).
  Each name is a persona, a project's own bot or one developer's agent, as WChat, Teammate Indigo,
  Codet and docs-surfaces-luna are
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
  A line that names an agent beside a model is no credit of a model alone, as the decision on
  `AI-generated: agent "parrot", ...` of NethermindEth/nethermind states, so `tool not named` does
  not apply either. `Bob (Claude)` of apache/artemis
  [088355af01b1](https://github.com/apache/artemis/commit/088355af01b1261814e52899483b3a190a951d7f),
  `TraeCode:GPT-5` of antgroup/vsag
  [876fb737bf83](https://github.com/antgroup/vsag/commit/876fb737bf830cc4e47d59676e9280070be55412)
  and `GPTCodex:GPT-5.4` of zephyrproject-rtos/zephyr
  [792718a5a3b0](https://github.com/zephyrproject-rtos/zephyr/commit/792718a5a3b059bc657f88d8622934af806b614a)
  each occur in one commit. `ibm-bob-trailer-01` requires IBM beside Bob, no page grounds TraeCode as
  a product of Trae, and GPTCodex can name Codex or a GPT Codex model. A row needs a page that
  describes the agent as a coding agent.
- Single `Assisted-by:` lines that credit a finding or nothing that the line states, or that name no
  tool in a form that a row reads. `Assisted-by: qwen3.8-flash-next  # finding the bug` of
  bluez/bluez
  [c8e2b951b07f](https://github.com/bluez/bluez/commit/c8e2b951b07fc9b84fa7f3e70dcde2b61aab3ad8)
  limits the model to finding the bug, which is no part of the change.
  `Assisted-By: "claude my eyes right out"` of containerd/containerd
  [e01c004cc6ed](https://github.com/containerd/containerd/commit/e01c004cc6edaf1c671d91dc303ec1e302373d95)
  is a pun in quotes that does not state what Claude did. `Assisted-by: ChatGPT Code` of
  openemr/openemr
  [67384985e4d9](https://github.com/openemr/openemr/commit/67384985e4d9f4615407528b134447c6cd51d917)
  names no product. `Assisted-By: ddalcu/Qwen3.6-27B-4bit-MTP-MLX-Serve` of josh-project/josh
  [8c1fb052eb10](https://github.com/josh-project/josh/commit/8c1fb052eb1016a933c9cd1f9d82f3c850d7f723)
  names a model with the account that uploaded it to Hugging Face, in a version bump.
  `Assisted-by: GLM 4.6 via Conductor` of TecharoHQ/anubis
  [a709a2b2daec](https://github.com/TecharoHQ/anubis/commit/a709a2b2daecea6b893de6bb4224bab85ae200d8)
  names Conductor, which runs other agents, and neither the agent nor a model alone.
  `Assisted-by: Devin (GTP-5.6)` of appneta/tcpreplay
  [4d7f8c46af72](https://github.com/appneta/tcpreplay/commit/4d7f8c46af72f78a048af86877c0852e144efdad)
  misspells the model in parentheses, and `devin-trailer-03` reads Devin only alone, beside
  Cognition or before a model after a colon, because people are named Devin. Each line occurs in one
  commit.
- Keys that occur in one commit each: `Helped by: Claude Opus 5.5` of dbeaver/dbeaver
  [c9a4b5b573a0](https://github.com/dbeaver/dbeaver/commit/c9a4b5b573a0343e0e17a758b9e8c44ca4e92955),
  `Assisted with: Claude Sonnet 4.6` of asbplayer/asbplayer
  [a357c2e7ffd8](https://github.com/asbplayer/asbplayer/commit/a357c2e7ffd8198e8480e3ef42d3ee8e70941d11),
  `Porting assisted by: Claude Sonnet 5.` of dogtagpki/pki
  [a2baa8863c04](https://github.com/dogtagpki/pki/commit/a2baa8863c046f97ab66258a08026a0dc18a2579),
  `Assistant used: Claude Opus 4.7 (1M context)` of pytorch/torchtitan
  [0acaaa1d7ff9](https://github.com/pytorch/torchtitan/commit/0acaaa1d7ff900c6e065c25f9edbf9c0eb824f31),
  `Primary author: Claude` of machinewrapped/llm-subtrans
  [d71746d2c3e3](https://github.com/machinewrapped/llm-subtrans/commit/d71746d2c3e314b578565a921b547d0cb3bac874),
  `Tests Generated by: Claude Code (opus 4.6)` of openshift/managed-upgrade-operator
  [03b807035e17](https://github.com/openshift/managed-upgrade-operator/commit/03b807035e17f8e0fe1a839bbf63788d87a59c0c),
  `docs assisted by: Opus 4.6 LLM` of hotosm/drone-tm
  [75d9a536d990](https://github.com/hotosm/drone-tm/commit/75d9a536d990268a08bdf49110af94ae2603f496),
  `AI Contribution: Codex Astra + Claude Fable` of linebender/vello
  [877474f8bb54](https://github.com/linebender/vello/commit/877474f8bb5405acc98e26432bfc17c843425176),
  `Deflaking agent: Codex` of commonfabric/labs
  [b794ce0c646b](https://github.com/commonfabric/labs/commit/b794ce0c646bc2a431f1b035036c20ec15e83cb0),
  `Reference author: Codex <noreply@openai.com>` of wonderwhy-er/DesktopCommanderMCP
  [9d262150cf95](https://github.com/wonderwhy-er/DesktopCommanderMCP/commit/9d262150cf950b34c3d777c3049f6970f9358115),
  `Done by: gemini-cli` of grpc/grpc
  [c9ced9d44d13](https://github.com/grpc/grpc/commit/c9ced9d44d133e05823f7b954a5571152f01d9fd),
  `Signed-off and authored by: Gemini` of google-gemini/gemini-cli
  [62455ade9d60](https://github.com/google-gemini/gemini-cli/commit/62455ade9d609569ffde6015dea0fc501bf960cd)
  and `Created with the assistance of: claude-4.5-sonnet` of ogx-ai/ogx
  [6147321083cc](https://github.com/ogx-ai/ogx/commit/6147321083cc0ecd0d4314eb83c8e55add538d45). No
  other commit of the key-value lines has one of these keys, and each line is one person's key in
  one commit, as `Docs-By:` and `AI-Assistant:` are in
  [The keys left undecided on 2026-11-08, decided on 2026-11-09](#the-keys-left-undecided-on-2026-11-08-decided-on-2026-11-09).
  That section also decides `Original-Co-Author:`, `Commit message assisted-by:` credits the message
  ([Trailer extraction](#trailer-extraction)), and `Assisted-by AI: Claude 4.6 Opus (Anthropic) via
  Cursor IDE` is among the lines that the six rows of a mistyped or spaced key leave unread.
- `Generated by: Claude with sonnet` of Azure/ARO-HCP
  [fdc385bc4d62](https://github.com/Azure/ARO-HCP/commit/fdc385bc4d622d4a847abcb61f0f8b66c254ce1b).
  `claude-full-body-07` reads `Generated by: Claude` and Claude before a model name, and not Claude
  before `with`, so that a person named Claude is not read. The line occurs in one commit and stays
  unread until a second commit with the form appears.
- `Wordsmithed-by: cursor/grok` of containers/kubernetes-mcp-server
  [c67341fa129a](https://github.com/containers/kubernetes-mcp-server/commit/c67341fa129a9384aecd06d9c17ad825503e0d8f),
  a commit that softens the wording of the documentation of access controls. The line can mean
  that Cursor reworded the documentation or the message and the pull request description, which
  carry the same text, and a message that a tool wrote about a person's change does not count.
  GitHub's commit search finds the key in no other commit, and the other hits use the verb for a
  message, as `(Commit message wordsmithed by kevans)` of freebsd/freebsd-src 276d9b88a9e6, so the
  line alone does not settle it, as `Docs-By:` did not.
- The other values under `Assisted by` that name an agent that no page describes, or that put a
  model where the other items of the commit name an agent: `llama-ui (ggml-org/Qwen3.8-Flash-Next:Q8_0)`,
  `Delta (zai-org/GLM-5.3)` and `DeepSeek-V4.1-Flash (deepseek-ai)` in the items of the squash merge
  ggml-org/llama.cpp
  [84746c5c94c2](https://github.com/ggml-org/llama.cpp/commit/84746c5c94c229af4bd90070faba495cb07c3278),
  which have the form `<agent> (<provider>/<model>)`, and `omo:GLM-5.3` of hyperspy/exspy
  [0a66f3504fb2](https://github.com/hyperspy/exspy/commit/0a66f3504fb29a5756e839cf253bc8d7185728b5),
  where omo is the name of an agent that no page read here describes.
  `Assisted by: Opus4.5 and Codex GPT5.2` of AcademySoftwareFoundation/OpenImageIO
  [556198f9909a](https://github.com/AcademySoftwareFoundation/OpenImageIO/commit/556198f9909a7d5fd975d6dfab15cdace63cc28b)
  names Codex after `and` as one of two assistants, in one commit, and no Codex row reads Codex in
  the second place of such a line.
- `AI Disclosure: all the tests were generated using Kiro (!)` of hydro-project/hydro
  [a4bdf399f905](https://github.com/hydro-project/hydro/commit/a4bdf399f90581f409c89a72ae960405998fb33b)
  and `AI disclosure: assisted by AI (Hermes/Sera); human-reviewed.` of oxc-project/oxc
  [ebb22f15fa70](https://github.com/oxc-project/oxc/commit/ebb22f15fa7086c0b3a44180a788077d6d0a094e).
  The rows of Kiro and Hermes Agent read trailers, keys and identities, and each line is the only
  labeled disclosure of its tool in the commit logs of the collection. GitHub's commit search finds
  the Kiro line only in this commit and in its copy in thysics/hydro-demo (read 2026-11-09).
  `Hermes/Sera` leaves open whether Hermes is Nous Research's agent Hermes Agent or a Hermes model,
  and the commit names `Agent-Owner:sera` below it.
- `- AI tool or model used: Copilot` of the section AI Assistance of the pull request template of
  mod-playerbots/mod-playerbots, as in
  [d5835ac9f405](https://github.com/mod-playerbots/mod-playerbots/commit/d5835ac9f405dbdefa7affa420673ce1baa173ae)
  and [17b8d7f68b4e](https://github.com/mod-playerbots/mod-playerbots/commit/17b8d7f68b4e7fe9626eebcd3ea319419b4e0f8e).
  The item below it states the purpose, `- Purpose : To fill this template (I was lazy)` and
  `- Purpose of usage: Translate this PR text from french to English`, so Copilot made only the
  description of the pull request, which credits no change by the decision of 2026-11-07
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
- `Unlinked contributors: Copilot.` of WordPress/plugin-check
  [8341b177f294](https://github.com/WordPress/plugin-check/commit/8341b177f29468788cafaac5a08d68de614d6690)
  and `Unlinked contributors: claude.` of WordPress/two-factor
  [6b5dfeb64bbc](https://github.com/WordPress/two-factor/commit/6b5dfeb64bbce9a309b141fff164de813b00aa67),
  the list of credits that WordPress's props bot writes. The bot collects everyone who took part in
  the pull request: "Contributors are collected from the pull request's commits, including people
  credited in `Co-authored-by` trailers, its reviews and comments, and the issues it closes and
  their comments." (README of WordPress/props-bot-action at 704ed96918df, read 2026-11-09). A name
  in the list therefore also stands for a review or a comment and does not show that the tool made
  a part of the change.
- Credits that one commit writes in its own words, which stay unread as the residual phrasings of
  [The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)
  do: `Assisted in Test: Gemini` of tektoncd/pipeline
  [72c59cd34bef](https://github.com/tektoncd/pipeline/commit/72c59cd34bef4726609bcde69f3afe3e1f73ad8f)
  and `Assisted on Tests: Gemini` of tektoncd/results
  [417a891f6838](https://github.com/tektoncd/results/commit/417a891f683857c6026f59798e80d90c0057c268),
  one developer's two spellings, which do not say whether Gemini wrote tests or helped to run them;
  `Code: Claude` above `Reviews: Codex, Claude, Gemini_` of Khan/perseus
  [9cba5197a28f](https://github.com/Khan/perseus/commit/9cba5197a28f6ab0a783c2bb4f57c12590281048),
  under a key that also begins ordinary lines, such as `code: normalize enum names (llama/5697)` of
  ggml-org/whisper.cpp fac5b4383008; `> **Maintainer update:** Claude Code (Claude Opus 5.5, max
  effort).` of jupyterlab/jupyterlab
  [f997f3ee4d99](https://github.com/jupyterlab/jupyterlab/commit/f997f3ee4d9962cf9bd68acdf90b084e3a06e730),
  a form of the maintainer's line that `claude-full-body-56` reads only with
  `for the maintainer changes`; `PS2: Claude's POC` and `PS4: Claude's POC for etag handling` of
  UI5/openui5
  [99294cc98b96](https://github.com/UI5/openui5/commit/99294cc98b9693c8c4665195521aa85461c70774),
  patch sets that later patch sets of the same change revised;
  `- This change and analysis were authored by **Codex** (AI coding agent).` above
  `- CLI used: codex-cli 0.77.0.` of openai/codex
  [9ce6bbc43e2d](https://github.com/openai/codex/commit/9ce6bbc43e2da2f8c8c08fa8d2cdaf496d35cb9c), a
  compound subject that `codex-full-body-19` does not read; the summaries `Summary: Done by claude.`
  and `Code written by ChatGPT 4.` of meta/sapling
  [7fb16d50ad87](https://github.com/meta/sapling/commit/7fb16d50ad871c6a1de186904084d75ad8ad7e5f) and
  [45c8d5e5f045](https://github.com/meta/sapling/commit/45c8d5e5f0453066163e275d10212cd1d7cb7c50) and
  `Summary: Claude helped generate this.` of CommE2E/comm
  [06bd90759920](https://github.com/CommE2E/comm/commit/06bd90759920bf4caa1172c48641fb895c9fb06c);
  and `Temp: Add copilot changes` of 4C-multiphysics/4C
  [4185208f9e2a](https://github.com/4C-multiphysics/4C/commit/4185208f9e2a8e87563648eb08e473a0b7ae4ad3),
  which does not say whether Copilot made the changes.
- `AI tool: Claude Code` with a space in the key, of getarcaneapp/arcane
  [7f05d1797992](https://github.com/getarcaneapp/arcane/commit/7f05d1797992acb6a15f7a55a93e107eda998f62)
  and apache/hbase
  [de30a2bdb083](https://github.com/apache/hbase/commit/de30a2bdb08326ec69195bf0da52e4fa2d18311a).
  arcane writes it under the template heading `AI Tool Used` above
  `Assistance Level: Minor - AI wrote the plan and validated changes once complete.`, which gives
  the tool planning and a verification, and only hbase records a change that the tool made, in a
  subject that ends in `by AI` and a body with `Summary by AI:`. The labeled disclosure reads the key
  only with the hyphen
  ([The forms of the six tools in the commit logs, read 2026-10-30](#the-forms-of-the-six-tools-in-the-commit-logs-read-2026-10-30)),
  and one commit grounds no wider form.
- `Notice:` with a disclosure of Claude in pantsbuild/pants, as in
  `Notice: Claude used for code drafting, tests and verification.` of
  [2ad2906e5cbf](https://github.com/pantsbuild/pants/commit/2ad2906e5cbfce142a230a70f116900f6186fc03),
  `Notice: Test generated by Claude` of
  [432cd7b84da9](https://github.com/pantsbuild/pants/commit/432cd7b84da99b4d23e214434d59685aeaec38d7)
  and `Notice: This idea was suggested by Claude while going over pinact output` of
  [03dd363d0556](https://github.com/pantsbuild/pants/commit/03dd363d0556e0474d714b3ae8d08b8327b92116).
  The section LLM Assistance Notice of the contribution guide of pantsbuild/pants
  (`docs/docs/contributions/index.mdx` at d99d3112785c) states that "the extent of the LLM
  *generation* must be disclosed in the Pull Request description" and gives example sentences
  without a label. Contributors write `Notice:` before a sentence of their own that names drafting,
  tests, verification, benchmarking or an idea. A row would need the analysis of activities of
  `claude-full-body-32`, which credits the idea of 03dd363d0556 under its labels, and the label is
  also PHP's, as in `Notice: Undefined offset: 0` of Automattic/liveblog 0e7c3df7340d. The lines stay
  unread, as the `LLM:` lines of pantsbuild/pants and the disclosures after `Note:` do, until the
  analysis refuses an idea. They credit AI, so none of them goes into `not_ai_commits.csv`.
- `Original translator lane: anthropic/claude-sonnet-5-5 (parent-selected).` of
  damianvtran/local-operator, as in
  [4522c6724b66](https://github.com/damianvtran/local-operator/commit/4522c6724b661aadef12b0a0db91711b3463d3c7)
  and [69d52932e2a8](https://github.com/damianvtran/local-operator/commit/69d52932e2a86e28e15927867b765b61c82544c5).
  Local Operator is itself "An open-source AI agent hub" (README.md at f995fbb1edea). Its
  docs/I18N.md (at f995fbb1edea) has the translation delegated "only to an LLM `i18n-translator`"
  and requires that the ledger record "the actual agent and model identities". The line names the
  model that the repository's own translator agent ran after its parent agent chose it, and no
  tool, so it is no credit of a model alone, as the footer of Overseer is not
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)).
  The `i18n-auditor` rounds beside it name a review.
- Lines that name a tool or a model as the reviewer, the designer or the verifier of a change,
  under `Reviewer:`, `Opposite-model reviewer:`, `Independent review:`, `review:`, `design model:`
  and similar keys, as `Reviewer: ** Claude (automated, final authoritative review)` of
  facebookresearch/balance 8890349df4ae, `Opposite-model reviewer: anthropic/claude-fable-5` of
  vercel/turborepo f68d214cab60, `Independent review: Codex (GPT-6, /root/reconnect_review).` of
  fedimint/fedimint 0ac138f7bbc0, `design and final overall review: gpt-6.1-sol / xhigh` of
  matrixorigin/matrixone 463803dfbbae and
  `main-direct model: Codex ('codex exec --full-auto') runs an adversarial review` of crowi/crowi
  ca606cdb991d, and test results under `validation:`, `tested:` and `probe:`. Design, review and
  verification credit no change, by the decision of 2026-11-07
  ([The credits of the final review of 2026-11-05](#the-credits-of-the-final-review-of-2026-11-05)),
  and a change that a person makes by hand from a review comment is the person's. Lines about fixes
  made in response to a review, such as `review: Implemented Copilot review suggestions` of
  DialmasterOrg/Youtarr e46fb3feb47e and `rev2: copilot comments`, do not show that the reviewer's
  suggestion was committed as the reviewer wrote it, so they stay unread as well.
- Lines that link a plan, as `Plan: https://claude.ai/artifact/<id>` below the description of the
  squash merges of 18xx-maker/18xx-maker, such as
  [3819a460a7ba](https://github.com/18xx-maker/18xx-maker/commit/3819a460a7ba0702a0d28080065478dd15537b42),
  `Plan: copilot/plans/remove_dead_code.md` of dds-bridge/dds 71d148b58396, the plans under
  `~/.claude/plans` of sozu-proxy/sozu 1b1a7188137a and vyos/vyos-1x 80e1764619b2, and Warp notebooks
  in warpdotdev/warp 7a60bb9bf5a1. A plan link does not show which agent made the change, and the
  CLAUDE.md and AGENTS.md of 18xx-maker/18xx-maker (at bba4dacb2bc7) say nothing of the link. Lines
  that credit a finding, an audit or a review, as `jit: Gemini caught a few bugs in the Arm64 JIT`
  of danoon2/Boxedwine 55673ab89d00 and `reported by: copilot-pull-request-reviewer` of
  uxlfoundation/oneDAL 852d95192bc7, credit no change either, by the decision of 2026-11-07. A credit
  of 18xx-maker needs an instruction or a line that shows that the agent that wrote the plan also
  made the commits.
- `provenance: claude-code session <date>, branch ...` in the HTML comment that ends the pull
  request descriptions of one developer of Project-MONAI/MONAI, which the squash merges
  [33564c19cb2b](https://github.com/Project-MONAI/MONAI/commit/33564c19cb2bb5aa435038d1806b4e7f9c683cd4),
  [20df60253d36](https://github.com/Project-MONAI/MONAI/commit/20df60253d364be69601114adae3bd609e0cb6f6)
  and [83e0ddec9aa8](https://github.com/Project-MONAI/MONAI/commit/83e0ddec9aa8db58221dbb93ca6cd11af3d2d8f7)
  copy. The developer's skill gh-triage-pr (`skills/gh-triage-pr/SKILL.md` of
  hjmjohnson/itk_forest_build_testbed at
  [94a772715890](https://github.com/hjmjohnson/itk_forest_build_testbed/blob/94a772715890c49178125191ad8342ab8ba25a3f/skills/gh-triage-pr/SKILL.md))
  prescribes the comment for every text that a session posts: "All text posted to GitHub must
  follow `~/.claude/rules/pr-message-format.md`", with "Provenance, file/SHA references for future
  Claude sessions, and structured task metadata go inside HTML comments" and the template
  `provenance: claude-code session YYYY-MM-DD`. The line therefore names the session that wrote the
  description and not what made the change. The commits of the pull requests carry only
  `Signed-off-by`, while the same developer credits Claude Code with its co-author line in other
  commits. `Provenance: otelc:/otelc-registry/groups/anthropic.yaml` of
  open-telemetry/opentelemetry-go-compile-instrumentation b2a30844f6e3 is the output of a schema
  check.
- The author model of vercel/turborepo, as ``- Author: `openai/gpt-5.6-sol` `` of
  [f3b0fa9f8b46](https://github.com/vercel/turborepo/commit/f3b0fa9f8b467c0bdf1c46e0a7d4d61b721984b5)
  and ``**Author model:** `openai/gpt-5.6-sol` `` of
  [7c993ef2fc50](https://github.com/vercel/turborepo/commit/7c993ef2fc50e3d8098dd2aeb1e37220f15fcbb4).
  These are squash merges of performance pull requests that the apps vercel-gh-bot-2[bot] to
  vercel-gh-bot-5[bot] open from head branches `agents/performance-*`. The lines come from the
  repository's own agent Factory.
  [apps/factory/README.md at e9d0b1bba7a6](https://github.com/vercel/turborepo/blob/e9d0b1bba7a6c43aa30520a65b2fcd8538758be3/apps/factory/README.md),
  titled "Turborepo Agents", states "Factory inspects the repository, edits files, and runs
  validation directly with its sandbox shell and file tools", and
  [daily-performance-improvement.ts](https://github.com/vercel/turborepo/blob/e9d0b1bba7a6c43aa30520a65b2fcd8538758be3/apps/factory/agent/lib/daily-performance-improvement.ts)
  prescribes "a body containing the author model, reviewer model, methodology, before/after
  evidence, and review summary". The agent names no tool, so it stays out as Overseer does under
  the label resolution mn-0114, and the reviewer model names a review. `Author: DeepSeek v4.` of
  machinewrapped/llm-subtrans
  [c38fc4fc1d61](https://github.com/machinewrapped/llm-subtrans/commit/c38fc4fc1d61eec9726cebafbd61e1f925b4cb2c)
  names a model alone in one commit.
- `Author:` with a person or another identity, which credits what it names. Examples are
  `Balos, Cody Joe <balos1@llnl.gov>` of llnl/sundials, `Devin Rousso <devinrousso@microsoft.com>`
  in a quoted commit of Chromium in brave/brave-core, `Trae Yelovich` of zowe/zo,
  `Cloud Composer Team <no-reply@google.com>` of googleapis/google-cloud-node, `fabric-composer-app`
  of accordproject/concerto and the people in the squash lists of invoke-ai/InvokeAI and
  celo-org/social-connect. The names match words of the tools, but none names a tool or a model,
  and no row of `Author:` reads them.
- An address at mail.grokbot.com under a name that names no tool, as
  `Co-Authored-By: Grok <seteiro@mail.grokbot.com>` of RT-Thread/rt-thread
  [dff75f072fe2](https://github.com/RT-Thread/rt-thread/commit/dff75f072fe2b047325a312d6b944dd3acce8676)
  and [54f5cac5b4cc](https://github.com/RT-Thread/rt-thread/commit/54f5cac5b4cc81f7d28ff783791950ed7e8f1d53),
  which `tool-not-named-trailer-03` counts under `tool not named`, and the author and committer
  `ChaosEngine <chaosengine@mail.grokbot.com>` of commits of ShaftHQ/SHAFT_ENGINE that no row
  credits, such as
  [76ee6debf825](https://github.com/ShaftHQ/SHAFT_ENGINE/commit/76ee6debf825ecb6ca4440b1ade04d54f7c6520e)
  and [e9a3b4155c48](https://github.com/ShaftHQ/SHAFT_ENGINE/commit/e9a3b4155c4821cd5c66cad0f24b6a7494bf2176).
  Grok Bot lets an account claim one address for all its Bots: "You can claim an email address for
  your Bots at mail.grokbot.com" and "The address belongs to you, not to one Bot."
  ([Cursor](https://cursor.com/help/grok-bot/agent-email), read 2026-11-09). ChaosEngine is the
  harness of ShaftHQ/SHAFT_ENGINE: "ChaosEngine is a provider-neutral operating system for agentic
  software work." (`chaos-engine/README.md` at 76ee6debf825), `chaos-engine/commit_identity.py` sets
  `CHAOSENGINE_EMAIL = "chaosengine@mail.grokbot.com"`, and
  `chaos-engine/references/host-parity-matrix.md` runs "one portable implementation" on Claude
  Code, Codex, Grok, Gemini, GitHub Copilot, OpenCode, Cursor and Grok Bot. Cursor's cloud agent
  commits under the identity, as
  [6e45a84819fa](https://github.com/ShaftHQ/SHAFT_ENGINE/commit/6e45a84819faf04041f8705e76aa43c51bc7a9d1)
  of pull request 7773 with the committer `Cursor Agent <cursoragent@cursor.com>` shows. The address
  does not show which agent made a commit, so ChaosEngine stays out as an agent account that names
  no tool, by the decision of 2026-10-30, and the RT-Thread lines keep the label `tool not named`.
  The identity is an agent's and gets no row of `not_ai_commits.csv`.
- `Agent-Harness: grok-bot` of elsa-workflows/elsa-core
  [f7d5fd076f20](https://github.com/elsa-workflows/elsa-core/commit/f7d5fd076f20fb03d509435b6d6cdc2008766ba7),
  the squash merge of pull request 8814 from the branch `agent/grok-bot/link-org-agents-protocol`.
  No page defines the key: the organization's protocol, AGENTS.md of elsa-workflows/.github,
  prescribes the branch `agent/<worker>/<issue>-<slug>` and no trailer, and GitHub's commit search
  finds the key in this commit only.
- BrAIn, the agent of tenstorrent/tt-metal, in `Co-authored-by: BrAIn <brain@tenstorrent.com>`,
  `BrAIn <brainclaw@tenstorrent.com>`, `BrAIn <brain-noreply@tenstorrent.com>` and
  `BrAIn (for <person>) <noreply@anthropic.com>`, and as the author `BrAIn <brain@tenstorrent.com>`,
  as in
  [d8bb6e44c0f3](https://github.com/tenstorrent/tt-metal/commit/d8bb6e44c0f3fdb975a54e671c08ca09ef583809).
  The lines name no tool, and an agent account that names no tool stays out by the decision of
  2026-10-30. The repository was read again on 2026-11-09. BrAIn runs on NanoClaw: issue
  [45064](https://github.com/tenstorrent/tt-metal/issues/45064) describes a "Lightweight HTTP handler
  on nanoclaw host" and a "nanoclaw task dispatch". NanoClaw uses Claude Code by default and states
  "Other providers are drop-in options", among them Codex, OpenCode and Cursor, and "Provider is
  configurable per agent group." (`README.md` of nanocoai/nanoclaw at
  [bb6a355cfa9e](https://github.com/nanocoai/nanoclaw/blob/bb6a355cfa9ed5675a74f5e735098a5d9201f03b/README.md)).
  BrAIn's line also stands on the changes of other tools: on
  [d43abe6ae548](https://github.com/tenstorrent/tt-metal/commit/d43abe6ae548fb33165e416e95b1b6b8041e3c19),
  which `github-actions[bot]` authored with the co-author line of Copilot CLI, and in the squash
  merges
  [7ed196e0cfdd](https://github.com/tenstorrent/tt-metal/commit/7ed196e0cfdd9a0da434dc99a69f2139b75becfd)
  and
  [4f767e344a25](https://github.com/tenstorrent/tt-metal/commit/4f767e344a2515f63d0ab50db9d0f12371844642)
  beside the line of Cursor alone. A line of BrAIn therefore does not show which agent made the
  change. BrAIn's forms that name Claude stay credited: `(Claude, via BrAInClaw)` through
  `claude-full-body-48`, `Claude (BrAInClaw)` through `claude-trailer-02` and
  `BrAIn (Claude) <noreply@anthropic.com>` through the Claude trailer rows.
  `🤖 Generated with BrAIn (Claude Code)` of
  [58748a0ea6cb](https://github.com/tenstorrent/tt-metal/commit/58748a0ea6cbb20e8d598c57c9ba1808b47928a5)
  and `This PR was created by **BrAIn** (Claude AI assistant).` of
  [13eaab0ad8d7](https://github.com/tenstorrent/tt-metal/commit/13eaab0ad8d72ee077533b06488917ce89557dbc)
  each occur in one commit, and no row reads them. BrAIn is an AI agent and gets no row of
  `not_ai_commits.csv`.
- `codebuff public sync bot` at the address of `github-actions[bot]`, the author and committer of
  the commits `Sync public snapshot from freebuff-private` of CodebuffAI/freebuff, such as
  [538c587d6ef8](https://github.com/CodebuffAI/freebuff/commit/538c587d6ef8bb7ddda6d154e83c13ac5b87f282).
  A workflow of the repository copies snapshots of the private repository, and the commits name no
  agent. The row `github-actions[bot]` of `not_ai_commits.csv` already explains the identity,
  because the address contains its pattern. `Codebuff <team@codebuff.com>`, the account
  codebuff-team, authored
  [029efd764896](https://github.com/CodebuffAI/freebuff/commit/029efd764896cc0c54a6c4a50ed23bffce510542),
  an edit of a workflow file on GitHub with no line of an agent, and no row is added for a single
  commit.
- A co-author line with Hermes alone, as `Co-authored-by: Hermes <hermes@erwan.sh>` of
  ArduPilot/MethodicConfigurator
  [35a34b3a5c29](https://github.com/ArduPilot/MethodicConfigurator/commit/35a34b3a5c29b2d43287f835277cc84ee055bb67)
  and
  [481d10350cbc](https://github.com/ArduPilot/MethodicConfigurator/commit/481d10350cbc0863c79cc41acc7ee1ac96c5025b).
  Hermes alone names people as well as Hermes Agent, as it does in `Client / agent tooling: Hermes`,
  and the rows of Hermes Agent read its full name or the addresses at nousresearch.com.
  `Co-authored-by: Hermes <hermes@nous.research>` of NYRDS/remixed-dungeon 0e0aa250a26a, which names
  Nous Research in its address, occurs in one commit.
- `Codex:` at the start of the descriptions of the pull requests of one maintainer of
  tenstorrent/tt-metal, which the squash merges copy, as
  `Codex: Add Hyunggi Chang's Qwen-Image-2.1 implementation for one` of
  [38cbf88164ae](https://github.com/tenstorrent/tt-metal/commit/38cbf88164aef1548c65af9a15d68315fcd67304)
  and `Codex: Large Qwen3.8-27B cache allocations currently construct zeros on` of
  [782c5ec7339b](https://github.com/tenstorrent/tt-metal/commit/782c5ec7339b94d6ed0e0175794276b70b10af59).
  The label can mark that Codex wrote the description, which does not count, or that Codex made the
  change, and GitHub's code search finds no file of the repository that defines it (read
  2026-11-09).
- `Assisted-by: Antigravity` at the end of the subject of a listed commit in a squash merge, as
  `* UI : fix SSE transport detection and routing through CORS proxy. Assisted-by: Antigravity` of
  ggml-org/llama.cpp
  [bae36efa3001](https://github.com/ggml-org/llama.cpp/commit/bae36efa30012d5dbaf842f893c5d034c0ba0157).
  Of a top-level `* ` item that a subject continues, the trailer scope reads only a co-author line
  with an address ([Trailer extraction](#trailer-extraction)), and the line occurs in one commit.
  Once a second commit joins an assistant credit to a subject, the extraction can read it, and this
  credit would count for Gemini, because Antigravity counts under Gemini.
- The `SQUASHED:` lines of LivelyKernel/lively4-core list the subjects of the commits that a squash
  replaced. lively4-server writes the subject `AUTO-COMMIT <path>` on every save
  (`git commit -m "AUTO-COMMIT ${filepath}"` in `src/httpServer.js` of LivelyKernel/lively4-server,
  read 2026-11-09), so a tool in such a path, as
  `AUTO-COMMIT-src-ai-workspace-components-lively-opencode.html` of
  [2aeafea162a1](https://github.com/LivelyKernel/lively4-core/commit/2aeafea162a1b481a53811b2477c2037ff0916f2),
  names a file. The subjects `AUTO_COMMIT_AI_<files>`, as
  `AUTO_COMMIT_AI_opencode_apply_patch_tool.js_opencode_base_tool.js_opencode_batch_tool.js_and_more`
  of [16559771fef0](https://github.com/LivelyKernel/lively4-core/commit/16559771fef0b56ede99497d7813c7695f915cad),
  name AI, but GitHub's code search finds the name in no file of LivelyKernel (read 2026-11-09).
  Nothing shows what writes them, so no row reads them.
- A key-value line under the scope of a conventional commit, a ticket, requirement or chip id, a
  field of Dependabot's metadata, a configuration key or a heading of the message, when its value
  names a tool or a model as what the change updates, builds, configures, documents, measures or
  quotes, because a subject about a tool does not show that the tool made the change. Examples are
  `dependency-name: anthropics/claude-code-action`, which Dependabot writes into its updates, as in
  AliAkhtari78/SpotifyScraper
  [41229d3d670b](https://github.com/AliAkhtari78/SpotifyScraper/commit/41229d3d670b3153aab28fa67d68aad65d76df75),
  `init: CLAUDE.md file (#2000)` of 0xPolygon/bor
  [1b69780ae843](https://github.com/0xPolygon/bor/commit/1b69780ae84346e66d1aca6da1940cb102995c12),
  `CLID-512: claude: add slash command for ISC generation` of openshift/oc-mirror
  [09fdb6001e25](https://github.com/openshift/oc-mirror/commit/09fdb6001e2505c82e9b31908149f6e6f7037858),
  `default model: claude-sonnet-4-6` of StampyAI/stampy-chat
  [2b57e49df532](https://github.com/StampyAI/stampy-chat/commit/2b57e49df532d5eed97d46428af066468c713451),
  `identity: claude[bot]` of assistant-ui/assistant-ui
  [73f0bc7711ca](https://github.com/assistant-ui/assistant-ui/commit/73f0bc7711cad4d7be3e0544bbd2345b0ddd7da5),
  which names the account of the workflow that the change edits, and
  `Expand AI agent detection: Goose, Amp, Augment, Copilot (VS Code), Kiro, Windsurf (#1394)` of
  databricks/databricks-sdk-py
  [f03b6da45a25](https://github.com/databricks/databricks-sdk-py/commit/f03b6da45a25f325e7c365861a3bdf8d6c430e59).
  In such subjects, Grok names the Grok Connect service, the grok command and the GROK tickets of
  datagrok-ai/public, as in `GROK-12728: Grok Connect: MongoDB harmonization, intermediate commit` of
  [5cbc73637247](https://github.com/datagrok-ai/public/commit/5cbc736372476f42edbfb242d42a9dd6da57604f).
  Composer names PHP's Composer, cursor a text, database or pagination cursor, augment the verb,
  Opus the audio codec, Haiku the operating system, AMP automatic mixed precision or AMP pages, and
  GLM the math library. The people whose names match a tool, such as the developer before
  `Nick: gemini 3.5 flash` of firecrawl/firecrawl ae1cd9d81e7a, are no credits either
  ([A person with a tool's name](#the-attributions-that-no-heuristic-matched-in-the-september-collection-read-2026-09-25)).
- Credits that one commit writes in a subject or under a prose label, in its author's own words,
  which stay unread for the reason of the disclosures after a `Note:` label: the subjects
  `wip: Claude Agent + me finishing the task.` and
  `wip: Junie moves a bunch of properties to RpcRouteState.` of ActualLab/Fusion
  [87944c66e752](https://github.com/ActualLab/Fusion/commit/87944c66e752345215187c94f6c7e2d1d25612fa)
  and [8effba7df12c](https://github.com/ActualLab/Fusion/commit/8effba7df12c2b0ab40af166b841216a837acb43),
  the listed subject `add: Kimi to resolve conflict` in the squash merge
  tenstorrent/tt-inference-server
  [6a8ca24ba7f8](https://github.com/tenstorrent/tt-inference-server/commit/6a8ca24ba7f80c62000b0ac43a8230ddc90dfb43),
  `tools: translate the documentation with claude (#1357)` of ArthurSonzogni/FTXUI
  [f265aad4f103](https://github.com/ArthurSonzogni/FTXUI/commit/f265aad4f1034d2e56fa7ab10131379b94436ca5),
  `internal: Add some docstrings using GPT-4 (#2317)` of PRQL/prql
  [31a7f90ff91e](https://github.com/PRQL/prql/commit/31a7f90ff91e434fbb4982537eca4b20742420d7),
  `JIT: Update and add some textual summaries via Copilot (#115604)` of dotnet/runtime
  [42a0b427132e](https://github.com/dotnet/runtime/commit/42a0b427132e55f11e9f7310a1c449575367b023),
  `web: codebuff improved the navbar randomly` of CodebuffAI/freebuff
  [d341faaa2c7a](https://github.com/CodebuffAI/freebuff/commit/d341faaa2c7a0b06664ff9f7aaae57d1009c97c5),
  `Tooling: Claude with Claude Code. Implemented test-first` of mozilla/pilo
  [06914cce9c69](https://github.com/mozilla/pilo/commit/06914cce9c696ec381c39b295dfffad7f5b6f8af)
  and `Note: This PR has been partially edited using the Claude coding assistant.` of
  AcademySoftwareFoundation/OpenImageIO
  [30142ed8d3c9](https://github.com/AcademySoftwareFoundation/OpenImageIO/commit/30142ed8d3c9d85d0c93adf9e61c56bdd7810117).
  They credit AI, so none of them goes into `not_ai_commits.csv`.
  `NOTE: Cursor was used for help in this PR (Opus 4.8)` of PennyLaneAI/pennylane
  [7d00f6f834b7](https://github.com/PennyLaneAI/pennylane/commit/7d00f6f834b7a628a6119095390b58a191270287)
  and `(with the help of Opus 5)` after a new unit test in the `Test Plan:` of meta/pyrefly
  [61d7fc0a5513](https://github.com/meta/pyrefly/commit/61d7fc0a55137c5510517d6825266706092611e5)
  leave open what the help was. `Description: Generated by Claude Fable 5.` of fedimint/fedimint
  [13bd097bf257](https://github.com/fedimint/fedimint/commit/13bd097bf2574d2023a377d72ae7f16343578f4f)
  credits the description, `Test Plan: Trusting Claude Code` of CommE2E/comm
  [4c3c981e8360](https://github.com/CommE2E/comm/commit/4c3c981e836093ba8fd8e8d5d021e1b723133b25)
  states how the change was verified, and `(with Claude Opus 4.7 1M context xhigh review)` after the
  author under the key `documentation:` of InsightSoftwareConsortium/ITK
  [28d4e32157df](https://github.com/InsightSoftwareConsortium/ITK/commit/28d4e32157dff6995747806502bc4c821cdf2967)
  credits a review, and none of these counts.
- Subjects and lists that name the reviewer of the change credit no tool, as a subject such as
  `address copilot comments` does not
  ([The forms that no heuristic matched in the collection started on 2026-09-27](#the-forms-that-no-heuristic-matched-in-the-collection-started-on-2026-09-27-read-2026-09-28)).
  Examples are `DCON-2341: Copilot review fixes` and `DCON-3179: Copilot review fixes` in the squash
  merges of icanbwell/fhir-server
  [61894c941051](https://github.com/icanbwell/fhir-server/commit/61894c941051a6c138724ccfcba4955f4c759c37)
  and [bd54764cb67b](https://github.com/icanbwell/fhir-server/commit/bd54764cb67b5e1122eb580eac5f119f332936f6),
  `HTCONDOR-3247: Copilot Review: better negative int guards` of htcondor/htcondor
  [4d4367203595](https://github.com/htcondor/htcondor/commit/4d436720359570c5c1770a298e6807f491abf4f7),
  `client: fix the issues pointed by gemini-code-assist` of TriliumNext/Trilium
  [93c7b8dea7d7](https://github.com/TriliumNext/Trilium/commit/93c7b8dea7d73754488b8f7369cb90c0c4ad6064)
  and `fixed: Copilot suggestions.` of project-chip/connectedhomeip
  [30e60291d71d](https://github.com/project-chip/connectedhomeip/commit/30e60291d71d6887252dc4caf408dcf273449d5a).
  They do not show whether a person typed the change or committed the suggestion of the review app.
  A suggestion that a person commits on GitHub carries GitHub's subject, such as
  `Apply suggestion from @Copilot`, and the co-author line of the app, which the reviewer rows read.
  `Reviewers: claude[bot], ...` in the land summaries of Khan/perseus and Khan/wonder-blocks, below
  `Author:` and above `Approved By:`, as in
  [7ca90d366801](https://github.com/Khan/perseus/commit/7ca90d3668010bd6d1c91a8d0250a4c1e77fe860),
  and `Reviewer model:` in the descriptions of vercel/turborepo, as in
  [c8347b1c52f5](https://github.com/vercel/turborepo/commit/c8347b1c52f57d7299a1bec1d7514fc7fb1b9c20),
  name the reviewers of the change, and a review does not count.

No row reads these lines until the maintainer decides:

- `Claim-Publisher:` of nerds-odd-e/doughnut. The skill dough-execute-plan writes it into the
  commits `Take queued work: <identity>` and `Admit accepted work: <identity>`
  (`claimCommitMessage` in
  `.agents/skills/dough-execute-plan/scripts/workspace-publication-ownership.mjs` at be08b6f9264d),
  as `Claim-Publisher: codex-deliberate-cucumber-proof-<date>` of
  [b34d5c232bc4](https://github.com/nerds-odd-e/doughnut/commit/b34d5c232bc41b8be1ae08d939302ed0a0ea4d58)
  and `Claim-Publisher: claude-<uuid>` of
  [923b5a63e650](https://github.com/nerds-odd-e/doughnut/commit/923b5a63e650e573fd9e1c35d16bb378f3d0b45b).
  These commits change only files under `.planning/`, such as the backlog and the profile of the
  agent that authors them. The value is the "stable execution publisher ID" that the agent
  supplies, and the skill prescribes no form for it. The skill also has the agent "Supply your own
  `--host` (`claude`, `codex`, or `cursor`)", and the host goes into the profile, as
  `"host": "codex"` in `.planning/agents/yui-chan.json` of b34d5c232bc4, and not into the message. In
  each commit read, the prefix of the publisher ID was the host of the profile. The maintainer
  decides whether a claim commit that the script of the repository makes in the session of an agent
  counts for the host, as the empty "Initial plan" commit of Copilot's cloud agent counts, and
  whether a prefix that the skill does not prescribe may name the host as a convention. Other
  commits of the repository already credit Claude, Cursor and Codex. `Plan-Model: GPT-5 (Codex)` of
  DemonDamon/AgenticX credits nothing
  ([The confirmed errors of the evaluation of 2026-11-04](#the-confirmed-errors-of-the-evaluation-of-2026-11-04-checked-2026-11-08)).
- `openinference-factory[bot]` of Arize-ai/openinference, as the author and in its co-author line.
  Its `factory/README.md` (at 6be7118f789c) describes "Claude Managed Agents that triage a new
  OpenInference issue as soon as it is opened, take a triaged issue to a PR that is ready for human
  review, and review a pull request a maintainer points them at", as in the fix
  [c2c1176bac05](https://github.com/Arize-ai/openinference/commit/c2c1176bac058e5b629c8a06c6d0545c82d729d8).
  Anthropic describes Claude Managed Agents as a "Pre-built, configurable agent harness that runs in
  managed infrastructure"
  ([Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview), read
  2026-11-09), a product of the Claude Platform that the table of
  [AI contribution detection](heuristics.md#ai-contribution-detection) does not list among the
  products of Claude Code. The app also authors release commits, such as
  `chore: release main (#4232)` of
  [c0f69d05ab28](https://github.com/Arize-ai/openinference/commit/c0f69d05ab284bbc215f151b2d0b232b92e790ce),
  which an author row cannot tell apart from the fixes of the agents. The maintainer decides
  whether Claude Managed Agents counts as Claude, as a tool of its own or not at all.

`tests.test_commits.TestRsyslogsFooterWithAntigravityOrAModelAlone`,
`tests.test_commits.TestTheKeysAndAgentsDecidedOn1109`,
`tests.test_commits.TestTheLabeledDisclosuresDecidedOn1109`,
`tests.test_commits.TestTheAgentLabelsDecidedOn1109`, `tests.test_commits.TestTheKeyAuthorDecidedOn1109`,
`tests.test_commits.TestTheIdentitiesOfAmpAgentKanbanAndJunie` and
`tests.test_commits.TestGrokBotGrokBuildCodebuffAndKimiCode` contain the commits that these rows read
and the lines beside them that the rows leave unread.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| Commit body has no trailers | `_extract_trailers` returns `""`, and trailer-scoped patterns therefore do not match |
| Author identity matches a heuristic without representing tool use | A matching identity pattern can give a false positive. A person's bare name "Claude" does not match. Reading the commit history uses `check_author=True` |
| `co-authored-by:` in prose description (not trailer) | The trailer scope prevents a match, because only lines in the form of a trailer are checked |
| Trailer followed by a squashed commit's message | Still extracted, because the trailer does not have to be the last line of the body |
| Two co-authors split into separate blocks by a blank line | Both extracted |
| Trailer above a `(cherry picked from commit ...)` line | Still extracted. The cherry-pick line is not in the form of a trailer and is dropped |
| `ai-generated` in commit description | Not a pattern in this module, and descriptions of AI-generated content therefore do not trigger detection |
| Mixed-case trailer token (`Co-Authored-By:` vs `co-authored-by:`) | `re.IGNORECASE` on all patterns, and both therefore match |
| Model name between tool name and email (`Claude Sonnet 4.6 <...>`) | The Claude co-author heuristic matches Claude followed by a model family, a version, Code or punctuation, or Anthropic's domain in the line. A person's surname after Claude is none of those |

## The AI-commit CSV

`output/all_ai_commits.csv` is the commit CSV: one row per commit that a heuristic in
`heuristics/ai_commit_patterns.csv` matches, with `matched_heuristics` naming the rows that matched
by their ids and `attributions` recording how the commit credits each tool. `output/other_ai_commits.csv` has the same columns for the
tools outside the six, from `heuristics/other_ai_commit_patterns.csv` ([The tools outside the six](#the-tools-outside-the-six-2026-09-25)). The cloning step writes both. After it has read the histories of the files that
it copied, and before it deletes the clone, it resolves every ref and HEAD and runs
`git log --stdin` over those commit hashes. `git log` reads the hashes from its standard input,
because tens of thousands of tags would exceed the system's limit on the size of the arguments.
The step classifies every commit and keeps one commit in memory at a time. Each worker writes one
shard per repository under `collect_commits/`, which git ignores, and at the end of the run the
step assembles the shards of the repositories in the input list into the CSV. A run that stopped therefore keeps
the histories that it read, and the next run's CSV contains them, while a repository dropped from
the list drops out of the CSV. Beside the CSV, the cloning step writes:

- `output/all_ai_commits-unmatched.csv`, every attribution in an unmatched commit that no heuristic
  explains, a trailer that credits somebody or an identity that looks like a bot, with up to
  <!--s:parameters.agentcontext.commits.UNMATCHED_SAMPLE-->20<!--/s--> shas and the whole lines that it came from. `unmatched commits` ranks it, with the credits after
  a prefix that the heuristics read, which it reads from the commit logs
  ([Trailer extraction](#trailer-extraction)).
- `output/all_ai_commits-mentions.csv`, every line of an unmatched commit that names one of
  the six tools outside a path, grouped by its phrasing and ranked by repositories. It records
  attributions written in prose, such as "Generated with Cursor", which have no trailer
  and no identity and are therefore in no row of the file above. `docs/steps/repository_cloning.md` describes how a
  line is grouped and bounded.
- `output/ai_commits-repos.csv`, one row per repository in the run: the outcome (a clone
  failure's reason, `no_files` when nothing was copied, `history_read`, `history_read_failed`, or
  `history_unavailable` when the files were copied and the history fetch did not bring the
  history), `commits_read`, `commits_matched`, `commits_other_tools`, and up to <!--s:parameters.agentcontext.commits.NO_SIGNAL_SAMPLE:words-->five<!--/s--> shas of
  commits that have no attribution at all. Without this file, a failed clone and a repository with no AI commit would look the same
  in the commit CSV. Checking the commits without any attribution by hand, together with the
  ranking of the unmatched attributions, bounds the miss rate.

The CSV, the unmatched attributions, `ai_commits-repos.csv` and the shards are not in the cloning step's `produces`.
The runner records the step by a checksum manifest of what it declares and by the inputs that
decide it. The detection step rewrites the CSV in place, and collection can also run with commit
scanning disabled. The runner's record of the step therefore does not show whether the commit
history was read, and `ai_commits-repos.csv` records it for each repository.

The cloning step keeps the commit log that it classified, `collect_commits/<owner§repo>.log.xz`, and the
per-repository commit record lists the commits of each configuration artifact's history by hash (`path_commits`)
and the heuristics that classified them (`classified_by`). `classified_by` is a digest of the
columns of the two commit heuristics files that the classification reads,
`commits.CLASSIFICATION_COLUMNS` (`id`, `tool`, `scope`, `attribution` and `pattern`, row by
row in file order), and of the code of `commits.py` and `heuristics.py` without comments,
docstrings and log calls. `source`, `doc_url`, `doc_verified`, `evidence` and `notes` record
where a row comes from and classify no commit, so an edit to them leaves every record current,
as `reuse.COLLECTION_COLUMNS` leaves the collection of a repository current after such an edit
to the path heuristics. `classify` is the one loop over a
repository's commits. The cloning step runs it over the commit log that it just wrote, and
`reclassify` runs it over the commit log of an earlier collection. The `commit_reclassification`
step therefore applies a heuristic added after a collection to every commit that the collection
read and rebuilds the five CSVs and the AI columns without a clone.
`docs/steps/commit_reclassification.md` describes what the step skips and what it cannot classify
again. Collections before 2026-09-28 kept no commit log, and a heuristic derived from the run
started on 2026-09-27 cannot be applied to the commits from which it was derived without
cloning the sample again.

Reading the commit history covers the recorded refs and HEAD. The clone is single-branch, and
fetched tags can add reachable history, but the reading does not cover every remote branch. The
step reads the commit history of every repository that contributed a file, more repositories than
the detection step publishes. `agentcontext/steps/detection.py` therefore restricts the CSV to the
repositories in `output/repos.csv` after it writes the mechanism CSVs, and logs how many rows it
left out. A missing CSV stays missing. Extracting a tracked archive is separate from running a new
commit scan.

```shell
python3 -m agentcontext repository-cloning --repo-csv LIST                 # reads the commit histories and assembles the five CSVs
python3 -m agentcontext repository-cloning --repo-csv LIST --no-ai-commits # copies and measures, reads no commit history
```

`--commits-dir`, `--ai-commits-csv` and `--ai-authorship` set the paths of the shards, the CSV and
the AI cache, as `--file-history` sets the path of the file-history cache. A test run passes all
of them, because each default is in the stage directory. `python -m agentcontext run
--with-commit-reclassification` reclassifies commit logs after the cloning step and runs the two
checks over the cloning step's output as the last steps of the collection, before the
publication: `commit-attribution-validation`, which exits 2 when a row of the CSV differs from the
result of the current heuristics over the columns that the CSV stores, and `unmatched commits`,
which ranks the attributions that no heuristic explains into `output/unmatched-commits.csv` and
exits 0, because the step applies no threshold.

Checks over stored output CSVs do not test the collection, which needs a clone. The subsample
run tests it, and so do the fixtures in `test_steps_repository_cloning.TestReadingTheCommitHistory`,
which cover what the subsample cannot: a commit body that is not UTF-8, a repository with no
matching commit, a bound that stops the reading of the commit history, and a run stopped and resumed.

## Reproducibility and current input

A per-repository commit record of the current collector records the resolved refs, the source and
retrieval date, the revision, the log encoding, the checksum and the commit count. The commit
logs keep complete messages with NUL-delimited fields. The copied paths, the path commits and
the classification checksums let the reclassification validate its input before it replaces
the outputs. A repository with missing evidence stays incomplete even when its record has
the current classification digest. [The reclassification step](steps/commit_reclassification.md)
specifies the schema, the validation and the coverage outcomes.

The current commit CSVs describe the collection of the run that started on 2026-10-23. Their
tracked forms are the volumes `output/all_ai_commits.csv.7z.001` and on and
`output/other_ai_commits.csv.7z`. Setup extracts them when the CSVs are absent, and
extracting them needs every numbered volume. Analysis can read the archives without a
new collection.

The current collector keeps full commit logs and, in the per-repository commit records, the
commits of each copied path, and `commit-reclassification` applies revised heuristics to those
inputs without another clone. Where a commit log is missing, a check over the messages cannot
reconstruct missing author or committer fields or recover a commit that no heuristic matched.
[Dataset facts](dataset.md) records current coverage and unresolved differences.

Regression tests use independently specified identities, messages and expected
classifications. Historical publication totals do not establish correctness.

## The library and the two checks

[Context](#context) describes how `agentcontext/commits.py` applies the heuristics of `heuristics/ai_commit_patterns.csv`, which attribute a commit to Claude, Copilot, Cursor, Codex, Gemini or OpenCode, in the three scopes of [Detection scopes](#detection-scopes), and why the module has no command line of its own. The two checks over the CSV that the cloning step writes are `agentcontext/checks/commits.py` and the `commits` subcommand of `agentcontext/checks/unmatched.py`. Nothing here needs a pip package, and reading the commit history needs `git` on PATH.

`assemble_ai_commits` turns the shards into `data/collection/output/all_ai_commits.csv`, `other_ai_commits.csv`, `all_ai_commits-unmatched.csv`, `all_ai_commits-mentions.csv` and `ai_commits-repos.csv`. See [The AI-commit CSV](#the-ai-commit-csv) and [AI authorship](caches.md#ai-authorship).

Run `commit-attribution-validation` to classify the commit CSV by detection source and to check it against the heuristics. Its exit code is a verdict: 0 when every row agrees with what the heuristics give over the columns that the CSV stores, 2 when a row does not, 1 when the CSV cannot be read. A CSV that the cloning step wrote has `matched_heuristics`. Those heuristics have to name the row's tools, and the message heuristics among them have to match the message. A legacy CSV without the column is checked for the tools that the message heuristics give. The check is also the runner's `commit_attribution_validation` step, run with `--with-commit-reclassification`.

```shell
python3 -m agentcontext commit-attribution-validation -i data/collection/output/all_ai_commits.csv
```

Run `unmatched commits` to rank the attributions that no detection heuristic explains. The check measures the recall of the heuristics, as `unmatched paths` does for the path heuristics: The commit CSV shows what the patterns match, and this check shows what they miss. A listed attribution credits a tool that no heuristic covers, an automation that is not a coding agent, or a person. An attribution of one of the six tools becomes a row of `ai_commit_patterns.csv`, of a tool outside the six a row of `other_ai_commit_patterns.csv`, and of an automation a row of `heuristics/not_ai_commits.csv` with a reason. A convention that the heuristics do not read on purpose gets a row with its reason in [Conventions considered and not adopted](#conventions-considered-and-not-adopted). A person gets no row anywhere, and the check therefore lists the person again on every run. The check is also the runner's `unmatched_commits_report` step, which applies no threshold and exits 0, and `docs/checks/unmatched.md` gives the reason.

```shell
python3 -m agentcontext unmatched commits
```

To measure how many configured repositories have an attributed commit, count the distinct
configured repositories in the current commit CSV and divide by the number of configured
repositories of the same collection, the rows of `output/repos.csv`.
State an incomplete commit reclassification separately. Current evidence and consistency
checks decide whether a release is ready, and no audit against an earlier publication does.

The regression tests of commit attribution distinguish a Claude model run by another tool from
Claude Code, in the co-developed, signed-off and spaced disclosure forms as well.
No row matches a person's name in a spaced disclosure. An unknown `Co-developed-by`
attribution enters the unmatched attributions, and a `Signed-off-by` alone does not.

A credit to Gemini in prose counts as `Gemini (unspecified)` only when the same line
names no coding product. The products that rule it out are Claude Code, Antigravity,
Gemini CLI and Gemini Code Assist, and Copilot, Codex, Cursor and OpenCode in the `via` and
parenthesized forms that the rows match. A credit that names Gemini alone still counts.

The full-body matching converts CRLF line endings to LF before it applies the heuristics.
The commit CSV stores messages with the same conversion, and collection and reclassification
therefore agree on disclosures that a pattern anchors to the start of a line.

A path record keeps the collected `artifact` and, when a directory symlink changes
the queried path, the `source_path` that the repository committed. The commit hashes and the
configuration authorship derived from them come from the history of that target, while the
cache key still identifies the collected configuration artifact.

## Commit histories that could not be read, missing logs and unreadable shards

Collection can finish a repository while its commit evidence is incomplete.
Inspect `output/ai_commits-repos.csv`, the per-repository commit record under
`collect_commits/`, and `output/commit-reclassification.json` together.

| Message or outcome | Consequence | Recovery |
|---|---|---|
| `Could not read the commit history`, `history_read_failed` | The cloning step removes the repository's commit shards and commit log. The file history measured before remains. The per-repository commit record contains the exception and whether a resumed run retries it (`transient`) | OS errors are marked transient and retried on resume. A timeout needs a larger `--clone-timeout`, with its justification. For any other failure, read the error in the per-repository commit record. A new Start alone does not guarantee that the cloning step reads the history again |
| `Could not keep the commit log` | Compressing or saving the log failed. The cloning step still classifies the uncompressed log into shards and then deletes it, and commit counts can therefore exist without the evidence that a reclassification needs. An older compressed log is removed | Keep the project folder and the log line with the file system error, fix the storage and resume the collection. A record in the current format of a history that was read without its commit log is pending, and the collection reads the repository again automatically and records its new revision and retrieval date. The cloning step exits 4 while a required commit log is missing, whatever its ceiling of clone failures. The file history step cannot recreate the full commit log |
| `shard_unreadable` | The assembly could not parse a per-repository JSON record or a CSV header and leaves out that repository's commit rows | Keep the damaged files and compare them with the matching handover. Restore verified copies, or regenerate the classifications from validated retained evidence. Do not read the missing rows as zero AI contributions |
| A shard header differs from the current columns | The assembly maps the existing columns by name and leaves new columns blank | Inspect the revision that wrote the shard and validate the retained evidence before the reclassification. Renaming columns does not supply missing values |

A successful assembly is not proof that the commit history of every repository was read or that
its evidence was retained. [Reclassification recovery](steps/commit_reclassification.md#recovery)
defines the coverage checks and the distinction between missing and invalid evidence.
