# The two unmatched checks

`agentcontext/checks/unmatched.py` provides both checks. `unmatched paths` ranks the path globs in
`output/other.csv` that no heuristic explains, and `unmatched commits` ranks the attributions among
the cloning step's unmatched attributions that no commit pattern explains. `unmatched reduce`
writes the attributions of a collection that name a bot or a tool and no person to a tracked file,
against which the tests check the rows of `not_ai_commits.csv`.

```shell
python3 -m agentcontext unmatched paths      # the path globs in other.csv
python3 -m agentcontext unmatched commits    # the attributions that no heuristic explains
python3 -m agentcontext unmatched reduce     # after a collection: its lines that name a bot or a tool, tracked
```

## One module for both checks

Both checks list what the heuristics missed in their input file. A heuristic that matches is easy
to see, because the row that it produces is in a CSV. A path that no heuristic matches appears in
no mechanism CSV, and nobody sees it until someone reads a column looking for it. Because no check
listed them, `.claude/rules` and `.claude/skills` symlinks stayed in `other` for months.

The two checks find the rows that the heuristics files need and that no vendor page covers. The
documentation watch, `docs`, reads the pages that the documented rows cite again, while the two
unmatched checks read the sample. `heuristics/config_path_patterns.csv` has no `source` column,
because every path heuristic is derived from a page. `heuristics/ai_commit_patterns.csv` has one, because <!--s:heuristics.ai_commit_patterns.sample_rows-->420<!--/s--> of its <!--s:heuristics.ai_commit_patterns.rows-->431<!--/s--> heuristics were derived from the commits.

## Inputs and findings

`unmatched paths` reads `output/other.csv`, reduces each path to the path glob that it shares
with its neighbors, counts the distinct repositories per path glob, and lists the path globs that
the heuristics do not explain. For a file behind a symlinked directory, the path is `link_path`,
the path at which the tool reads the file. `skills/README.md` behind a `.claude/skills` link is
thus judged as `.claude/skills/README.md`. Each listed path glob is a mechanism to add to
`config_path_patterns.csv` or a row with a reason to add to `heuristics/not_configuration.csv`.
See [Keeping up with the tools](../heuristics.md#keeping-up-with-the-tools).

The path glob is taken from the path below the spec directory that contains the file, because a
reason of `not_configuration.csv` and a path of the heuristics name a place in a tool's
directory, and the detection step reads a spec directory at any depth (`path_glob`).
readest/readest keeps the memory notes of Claude Code in `apps/readest-app/.claude/memory/`,
which the row `.claude/memory/**` explains, as it explains the same folder at the root. The same
place in two repositories is counted together: olegbl/d2rmm's `.claude/patterns.md` and wso2/oxygen-ui's
`packages/oxygen-ui/.claude/patterns.md` are one path glob in two repositories. A path with a
spec directory inside another one, such as `.claude/.claude-plugin/skills/bump.sh`, has a path glob
below each, and a row for the outer one, here `.claude/.claude-plugin/**`, explains the file.
Otherwise the check counts the file under the innermost one, the place that a missing heuristic
would name. A row of `not_configuration.csv` therefore never names a spec directory below the
root, such as `frontend/.claude/**`, which no path glob can equal. An ignore file, such as
StarRocks/starrocks's `contrib/starrocks-python-client/.cursorignore`, has its file name as its
path glob, because its row of `config_path_patterns.csv` names it by that name at any depth. A
path in no spec directory, such as a plugin manifest at the root, keeps the path glob of its own
path.

`other.csv` also contains the items of an Antigravity default place that the top-level `exclude`
of a registration file leaves out. An example is `packages/app/.agents/agents/defaultone.md` under
a `packages/app/.agents/agents.json` with `"exclude": ["defaultone"]`, which the detection step
reads as no item of the place. The exclude explains such a file, and no heuristic is missing for
it. The check reads the validated tree (`--validated-dir`) for the files that
`paths.antigravity_left_out` returns. It also reads every file in the folder of a skill left out
that way, because a skill that the detection step counts covers its whole folder. The check counts
these files separately in one line of the log and ranks none of them. The files of a repository
without a directory in the validated tree are ranked with the rest, as in a checkout, which
contains no repository's directory. One line of the log counts such repositories.

The check reads a second input for the same purpose: the repositories that the collected file
cleanup dropped because they matched no mechanism, from `deletion.csv` and the collected tree. It
lists them in a block of their own. Such a repository is in no output CSV, and a path that occurs
only in dropped repositories is outside every other input of this check. From a dropped repository, the check reads only what the cleanup keeps
of a repository that it does not drop. `paths.copy_tree` copies the tree as it does for the
cleanup, at any depth and without the `@eaDir` directories that Synology DSM writes. The cleanup's
own `scan_and_clean_repo` then runs over the copy before anything is counted, as it runs before
`other.csv` is written. It removes submodule contents, vendored subtrees, the parts of a `.github`
outside the paths that the heuristics name, and a context file name in the wrong case. The check
also leaves out the planning documents and their anchors that `planning.find` returns, because a
heuristic explains them, as `other.csv` leaves them out of a published repository.

A dropped repository can keep files that the cleanup removes from a repository that it keeps: The
collector copies a project's CI files with the whole of a `.github` that contains any path that the
heuristics name, and it copies library pages named `agents.md` or `gemini.md` to check their case.
Read after `scan_and_clean_repo` has run over the copy, as the check reads it, the dropped block
lists none of them. Both blocks count toward `--fail-above`,
because a path glob at the threshold needs reading wherever it occurs. The ranked list keeps the
two blocks apart: A path glob from `other.csv` is in the published data and a path glob from the
dropped repositories is not, and a reader acts differently on each.

`unmatched commits` reads `output/all_ai_commits-unmatched.csv`, which records, from the commits
that the cloning step read, the unmatched trailers and the unmatched identities that look like
a bot or have one of the six tools in their name or their address, such as
`opencode <person231@example.org>`. It removes the recorded
tokens that `heuristics/not_ai_commits.csv` lists and ranks the remaining attributions, without
applying the positive commit heuristics again.

The check adds the trailers that credit somebody under a token after a prefix that the
heuristics read as the token itself (`prefixed_credits`, [trailer extraction](../commits.md#trailer-extraction)),
such as `AI-assisted-by: Jcode (OpenAI)` of caddyserver/caddy
[401582cccfb3](https://github.com/caddyserver/caddy/commit/401582cccfb3) and
`Co-Assisted-By: CVE Fixer Bot` of tektoncd/operator
[19d139ab1a2b](https://github.com/tektoncd/operator/commit/19d139ab1a2b). The cloning step lists
none of them, because it lists a trailer only under a token of `commits.ATTRIBUTION_TOKENS`.
The check reads them from the commit logs that the cloning step keeps in `collect_commits/`
beside `output/` (`--commits-dir`), in the commits that no heuristic of either commit heuristics
file matches, and ranks each under its whole token. It reads a commit log commit by commit only
when a line of the log begins with such a token, and otherwise only searches it. A change of what
the cloning step records would change the collector's code and the digest of the commit
classification, `classified_by`. A new image would then take the collection as collected under
other code and classify every kept commit log again.

An older unmatched CSV can therefore include attributions that newer heuristics already
recognize. Before you treat those tokens as unknown, write the CSV again from the commit logs with
`python3 -m agentcontext commit-reclassification`. For a current finding, add an attribution of
one of the six tools to `ai_commit_patterns.csv`, of a tool outside the six to
`other_ai_commit_patterns.csv`, or of other automation to `not_ai_commits.csv`, with its evidence.
See [commits.md](../commits.md).

## The tracked unmatched attributions

`unmatched reduce` writes the attributions of one collection that name a bot or a tool and no
person as `output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z`, with only the <!--s:parameters.agentcontext.checks.unmatched.UNMATCHED_COLUMNS_READ:words-->four<!--/s--> columns that
`unmatched commits` reads, `kind`, `token`, `commits` and `repos`. It removes the archive that it
replaces and any CSV extracted beside either archive. The collection's own file is <!--s:collection.unmatched_csv_mb-->54<!--/s--> MB, every
run rewrites it, and most of its lines name people, such as the co-authors of commits that no
heuristic matched. That full list stays in the handover of each run, where `unmatched commits`
ranks it, so that finding new tools does not depend on the tracked archive. The repository tracks
the reduced archive, and a suite can check whether a row still matches anything. The archive
contains the credits after a prefix that `unmatched commits` reads from the commit logs, which
`reduce` reads from `--commits-dir` in the same way. The `shas` and `lines` columns of the
collection's file are samples for a person to read, and the tracked archive leaves them out.
`tests/test_checks_unmatched.py` checks every row of `not_ai_commits.csv` against the archive. The
suite reads the archive when the CSV is not extracted beside it, and it skips only where `7zz` is
missing. The release builds `unmatched-attributions.csv` from the full list by the same rule
([the release](../steps/release.md#key-decisions)).

### What the archive keeps

The rule is an inclusion rule (`checks.unmatched.keeps`). A line stays only when its form shows
that it names a bot or a tool and when nothing in it names a person, and every other line is
dropped. Dropping costs little, because the full list stays in the handover, and keeping a person
would publish a name. The forms that name a bot or a tool are:

- the mark of a GitHub App, `[bot]`, or a name that repeats the app of the address, as in
  `mergify <37929162+mergify[bot]@users.noreply.github.com>`
- the word `bot` as a word of its own, as in `release bot` and `snyk-bot`, and no other spelling, so
  that a name such as Abbot, Talbot or Lebot is not a bot
- a row of `not_ai_commits.csv` in the name, before any parenthesis
- the name of a tool of the commit heuristics at the start of the name, alone or followed by a model
  after a colon or a slash, a parenthesis that ends the name, a version, or one of the words
  `agent`, `ai`, `assistant`, `code` and `cli`
- a trailer token that marks what it credits as a coding agent, such as `Co-authored-by-agent:` and
  `AI-assisted-by:`

Each of these checks then takes a line out, because the form alone does not rule out a person:

- A name with an at sign, an apostrophe or, for a bot, a parenthesis often adds a person, as in
  `Name (bot)` and `a bot of @handle`.
- The list of the collection itself shows which names are people's (`personal_names`): the names of
  the trailers without a form of a bot or a tool. A name of a bot that begins with a first word that
  several such names share, that begins with the two words of such a name, or that is one such word,
  is dropped. This takes out a surname Bot or Le Bot and the bot of a person. The first-word check
  does not apply to a name or an address with the mark of a GitHub App, because a surname does not
  bring the mark. A plain name that the list shows as the account of an app at GitHub's address is
  not a person's, as in `co-authored-by: mergify` beside the address above, because only such an
  account shows that the app exists. A bot without the mark can still be dropped.
- A name under a trailer token that marks a coding agent is dropped when the list shows the whole
  name as a person's or when its first word is one that several such names share. Such a trailer
  does not itself count as a person's name.
- A tool alone is dropped when a name in the list begins with it, because `co-authored-by: cody`
  can then be a person. A person with the first name of a tool is no form of a tool at all, as in
  a name of a tool followed by a surname, and the name of a tool after a person's name, as in
  `Name (aider)`, does not count.
- The address of an author line has to be the account of a bot: GitHub's address of an account that
  ends in `[bot]`, `-bot` or `_bot`, or a mailbox `bot@`, `noreply@`, `no-reply@`, `support@` or
  `*-bot@` at a domain that fits the name, where a country's second-level suffix, as in `.co.uk`, is
  not the domain. An address with a plus sign, an address at a provider of personal mail and a
  mailbox that ends in `.bot` are a person's, as in `first.bot@example.org` and
  `first+bot@example.org`. A row of `not_ai_commits.csv` can have a mailbox that repeats its name at
  any domain, as in the identity under which a vendor's release workflows commit.

The rule cannot tell an account of a project from an account that a person named for a login that
the list does not show, such as `handle-bot`. Such an account stays, as a GitHub account does in an
evidence link.

The date in the name is the day on which the collection started, and it shows which collection
the rows of `not_ai_commits.csv` are checked against. The command reads the date from the name of
a collection, which begins with the time at which the collection started. During a collection, it
takes that name from `finished_repositories.collection.json`, the record of the collection in
progress beside `finished_repositories.jsonl`. A completed collection has removed that record,
and the command then takes the latest collection name among the lines of
`finished_repositories.jsonl`, because each line contains the name of the collection that wrote
it. The earliest `scanned_at` is not that day, because a repository that the collection took over
keeps the line and the `scanned_at` of the earlier collection that collected it, which can be a
month before. A list in which no line records how its repository was collected (`collected_under`)
holds no repository taken over, because the next collection collects such a repository again. In
such a list, the earliest `scanned_at` is the day. The command exits 1 on a list with lines that
record how their repositories were collected and contain no collection name.

After each collection and after each commit reclassification, run
`python3 -m agentcontext unmatched reduce` where the collection's handover is extracted, in the
checkout or in a working copy with `AGENTCONTEXT_DATA_ROOT` set to its data root. There,
`output/all_ai_commits-unmatched.csv` and `finished_repositories.jsonl` are where the command reads
them. For a handover extracted elsewhere, pass the two files with `--unmatched-csv` and
`--finished`. From a working copy, copy the archive into the checkout and delete the checkout's
other archive, as [development](../development.md#continuous-improvement-from-handovers)
describes. Commit the new archive together with the removal of the old one. The archive stores no
timestamps, and one handover gives the same bytes on every run. If you add a row to
`not_ai_commits.csv` from the new collection before you replace the archive, the test fails when
the older collection lacks the row's attribution.

As for `output/all_ai_commits.csv`, the extracted CSV is gitignored and the archive is tracked,
and `tests/test_data_layout.py` checks both. A checkout's `output/` contains the tracked archive,
and the release step's inventory lists the archive and its CSV by their dated names.
`unmatched commits --unmatched-csv` over the extracted CSV ranks the bots and the tools of that
collection again. To rank all of its attributions, run it over the full list in the handover.

## Key decisions

- **Both are steps of the pipeline, and neither step applies a threshold.** `unmatched paths` is
  the runner's `unmatched_paths_report` step, and `unmatched commits` is the
  `unmatched_commits_report` step. Each step logs its findings and exits 0.
  `python -m agentcontext verify` and CI run the paths check with `--fail-above 2`.
  `not_configuration.csv` gives a reason for every path glob in two or more repositories of the
  tracked `other.csv` that no positive heuristic explains, and the threshold is therefore <!--s:parameters.agentcontext.tools.verify.unmatched_paths_fail_above-->2<!--/s-->. A path glob that the heuristics do not explain fails the check once it is in <!--s:parameters.agentcontext.tools.verify.unmatched_paths_fail_above-->2<!--/s--> repositories. CI reads only the
  tracked `other.csv`, because `deletion.csv` is tracked only as its archive and the collected tree
  not at all. The dropped repositories are checked only where the check runs over an extracted
  stage directory. So are the files that an exclude leaves out, because the checkout's validated
  tree contains no repository's directory.

  The commits check has no automated threshold, because of what it ranks. It lists every
  attribution on an AI commit that no heuristic explains. In a real collection, these attributions
  are mostly the human co-authors of those commits and the CI bots that `not_ai_commits.csv`
  does not list. The tracked archive alone holds <!--s:collection.unmatched_attributions_unexplained-->2,702<!--/s--> bots and tools that
  `not_ai_commits.csv` does not explain. To empty the list, the heuristics would have to name every
  automation account on GitHub and every common committer name. No real collection could stay
  below that threshold with `--fail-above` on that step, and the threshold would have stopped the
  next collection. For the same reason, neither the full verification nor CI runs
  `unmatched commits` with a threshold over the tracked unmatched attributions. A person reads the
  step's ranked list, `output/unmatched-commits.csv`, in the handover, or runs
  `unmatched commits --unmatched-csv` over the handover's `output/all_ai_commits-unmatched.csv`
  with a `--min-repos` chosen for the question.

  The paths step applies no threshold, because a repeated collection finds paths that the tools
  added since the heuristics were last read. A threshold would stop the run before the
  archive and the publication, which take minutes and build a local directory, while the upload to
  Zenodo is a person's step. The stop would protect nothing, and the container would have to be
  started again. A run that is meant to be repeated has to deliver the whole
  dataset and the list of what is unmatched. In the verification suite, a threshold costs seconds,
  and CI fails a pull request on it too
- **The exit code is the verdict.** 0 means that the check ran and nothing reached a threshold, 2
  means that `--fail-above` was given and something reached it, and 1 means that the check could
  not run. Returning 1 for both "could not read the input" and "found something" would present a
  check that never ran as a check that found a problem. `tests/test_check_modes.py` checks both
  directions
- **What each unmatched check skips is in the heuristics, not in the code.**
  `heuristics/not_configuration.csv` and `heuristics/not_ai_commits.csv` have one row per path
  glob and per attribution that the unmatched checks skip, with the tool, whether a vendor page or
  the sample supports the row, the page and the date on which it was read, and the reason. Kept in
  code, the rows would have no source, no date and no check that notices when an entry stops
  matching anything. `test_checks_unmatched.py` checks that every path row has its reason and its
  page or evidence, because a later collection need not contain the path. It also checks that
  every commit row matches at least one of the tracked unmatched attributions. The loader raises
  `ValueError` for a row with no reason
- **Each writes its ranked list where `--out` names a file, and logs only the counts.** The runner
  passes `output/unmatched-paths.csv` and `output/unmatched-commits.csv`. The handover includes
  them. The release publishes the paths file. It withholds the commits file and the full list of
  the unmatched attributions, because most of their attributions name people, and it publishes
  the lines that name a bot or a tool as `unmatched-attributions.csv`
  ([the release](../steps/release.md#key-decisions)). No step reads them, and the next run's
  input never depends on what the last run found. With `--out`, the log gives the number of
  entries and the file that contains them, and lists none of the entries. Without `--out`, a
  person who runs the check by hand gets every row. Logged in full, the list of the commits check
  would fill the container's log with co-author names, and a log of its first rows would leave the
  rest of the list in no file.
  The paths file has a `found_in` column, `other.csv` or `dropped repositories`, and also holds
  the path globs of the repositories that the cleanup dropped

## Error handling & edge cases

| Case | Behavior |
|---|---|
| `output/other.csv` is missing | `ERROR` naming it, the remedy `python -m agentcontext detection`, exit 1 |
| `deletion.csv` or the collected tree is missing | The ranked list contains only the `other.csv` block, one line names what was not read, and the exit code is unchanged. Neither absence is an error: You can remove the tree to save space, and the log belongs to the run that wrote it |
| The validated tree is missing, or has no directory for a repository in `other.csv` | A file that the top-level `exclude` of an Antigravity registration file leaves out is ranked with the unmatched files, one line names the tree or the number of repositories without a directory, and the exit code follows the ranking |
| `output/other.csv` is there and is not the CSV | `ERROR` naming the missing column(s), exit 1. Both readers check their header. Without that check, an empty or truncated file would read as a CSV of no rows, and the check would find nothing unmatched and exit 0 over a file that it never read |
| `output/all_ai_commits-unmatched.csv` is missing | `ERROR` naming it, exit 1 |
| The directory of the commit logs is missing, or `--unmatched-csv` is in no `output/` directory and `--commits-dir` is not given | A `WARNING` names what is missing. The ranking of `commits` and the archive of `reduce` lack the credits after a prefix, and the exit code is unchanged |
| An input that is not UTF-8, or `output/all_ai_commits-unmatched.csv` with a count that is not an integer | `ERROR` naming the file, exit 1 |
| The input is there and contains no unmatched path glob | The log states that, exit 0 |
| A glob or an attribution reaches `--fail-above` | Listed with its repositories, exit 2. Neither runner step passes a threshold, and a pipeline run therefore logs the findings and continues |
| `--min-repos` above every count | An empty ranked list, exit 0. The count is still logged |
| `unmatched reduce`: the collection's full list or `finished_repositories.jsonl` is missing, `7zz` is not on PATH, or 7zz fails | `ERROR` naming what is missing or 7zz's last lines, exit 1. Nothing is written and nothing removed |
| `unmatched reduce`: `finished_repositories.jsonl` lists no repository, or its lines record how their repositories were collected and contain no collection name | `ERROR` naming the file, exit 1. Nothing is written and nothing removed |
| `unmatched reduce`: the archive cannot be written into `--output-dir`, such as on a full disk | `ERROR` naming the archive and the operating-system error, exit 1. The command writes the archive under a `.partial` name and renames it onto the tracked archive. The tracked archive and the copies that it would replace therefore stay unchanged |
| No tracked archive under `output/`, or two | `tracked_archive` raises, and the suite that checks the rows fails rather than skipping |

**Language:** Python 3.14

Without `--out`, both commands log the ranked findings to stderr. With `--out`, they log the counts
and write the findings to the CSV.
