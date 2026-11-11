# The workflow of the detection accuracy evaluation of 2026-11-04

The plan runs every labeling agent at once. The second pass depends on nothing that the first
pass writes, because `second_pass` is drawn with the samples, so both passes start together, and
the work waits only at the draw, at the report and at the maintainer's resolutions. The commands
are in [the command sequence](1-commands.md), and every agent receives
[the brief](2-labeling-brief.md).

## Before the draw

While the regeneration runs, settle the two decisions of
[the maintainer's decisions](4-maintainer-decisions.md). Each decision changes a paragraph of the
brief and, when the maintainer chooses a change of the detection, the heuristics that the draw
reads, so a decision that comes before the draw saves a round of resolutions. Replace the last
section of the brief with the decisions.

## The shards

`make_shards.py` splits each sample into shards of about equal size, keeps a pair of a tool and a
mechanism of path precision and a stratum of commit precision in one shard, so that one agent
reads one heuristic and its vendor page for all rows of a pair, and keeps a repository of path
recall with all of its candidate paths. The sizes follow the work per item: A row of path
precision needs the configuration artifact, its heuristic and often the vendor page or a link
target on GitHub, while most commits of commit recall need only the screen.

| Sample | Items in pass 1 | Shards in pass 1 | Items in pass 2 | Shards in pass 2 |
|---|---:|---|---:|---|
| `path_precision` | 779 | 7: six of 121 to 128 items and one of 30 | 156 | 2 of 78 |
| `path_recall` | the repositories with a candidate, about 2,700 to 7,000 candidate paths | 3 to 9 of at most 1,000 candidate paths | a fifth of them | 1 or 2 |
| `commit_precision` | 493 | 4: 133, 120, 120 and 120 items | 99 | 1 |
| `commit_recall` | 1,000 | 2 of 500 | 200 | 1 |
| `mentions` | 200 | 1 | 40 | 1 |

The shards of `path_precision` and `commit_precision` are the ones that `make_shards.py` writes
for the samples that the output CSVs of the handover give: `scripts/shards_from_handover.py`
draws both samples and their second pass from the archive and splits them with the functions of
`make_shards.py` (`dryrun/handover-sizes/shards.json`). A stratum of `commit_precision` stays
whole, so the 73 strata of one heuristic each and the 60 items of the other tools make the first
shard of 133 items, and the six tools with 60 items each fill the other three, two to a shard.
The shards of path recall depend on the candidate paths, which only the draw reads off the file
lists. A shard of path recall closes once it holds an equal share of the candidate paths, the
total divided by the number of shards of 1,000 that the total needs, rounded up, or before a
repository that would take it above 1,000. A repository has at most 200 candidate paths, so a
shard that closes before a repository holds more than 800. Every shard but the last therefore
holds more than 800 candidate paths, except for totals of 3,001 to 3,200, which four shards share
and where a shard can close at 751 to 800. For 2,700 to 7,000 candidate paths
([dry runs](1-commands.md#dry-runs)), the first pass has 3 to 9 shards, and the second pass 1 or
2 for the fifth of them. The draw over the regenerated outputs fixes every count.

That makes 17 to 23 agents in the first pass and 6 or 7 in the second, all started together,
each with one shard. No agent labels an item of both passes, and an agent of the second pass
never receives a first-pass shard.

## What each agent reads

| Sample | Its shard | The files of the items | The rules |
|---|---|---|---|
| `path_precision` | `pass<N>/path_precision-NN.csv`: the row, `local_path`, `github_link`, `cell_rows` | the validated tree of the checkout at `data/collection/<local_path>`, the repository's other files beside it (plugin manifests, link markers), GitHub at `head_sha` of `WORK/ai_commits-repos.csv` for a link target or a file that the tree lacks | the rows of `heuristics/config_path_patterns.csv` for the pair, their `doc_url`, `notes` and `evidence`, `heuristics/not_configuration.csv`, `docs/heuristics.md` |
| `path_recall` | `pass<N>/path_recall-NN.csv` and `-candidates.csv`: the repositories, their candidate paths with `detected`, `local_path` and `raw_url` | the validated tree for a detected candidate, `raw_url` or `gh api .../contents` at the collected commit for the others | the path heuristics and the negatives, `docs/heuristics.md` |
| `commit_precision` | `pass<N>/commit_precision-NN.csv`: the commit, `matched_heuristics`, `attributions`, `commit_message` | `gh api repos/OWNER/REPO/commits/SHA` for the identities | the rows of the commit heuristics by `id`, the Attributions table of `docs/heuristics.md`, `docs/commits.md` |
| `commit_recall` | `pass<N>/commit_recall-NN.csv`: the full identities and message, `detected`, and in pass 1 `screen_hits` | the shard alone, and GitHub for an identity that it does not settle | the attribution criterion, the commit heuristics, `heuristics/not_ai_commits.csv` |
| `mentions` | `pass<N>/mentions-NN.csv`: `line`, `line_as_collected`, `pattern`, `read_now`, `example_sha`, and with the optional lookup `repo_name` and `github_link` | GitHub's commit search for a phrasing that the line does not decide | the commit heuristics, `docs/commits.md` |

All agents share one GitHub access token, with 5,000 REST requests an hour and 30 requests a
minute of the search API. The validated tree and the shards hold what most items need, and the
brief tells the agents to request GitHub only for what they lack. Path precision and commit
precision make most of the requests.

The prompt of an agent names the brief, the shard, the pass, the file that it writes and `WORK`:

```
Label the shard WORK/shards/pass1/path_precision-03.csv of the detection accuracy evaluation of
2026-11-04 in pass 1. Follow the brief at
<scratchpad>/detection-eval-prep/2-labeling-brief.md
in full, with WORK=<the work directory>. Write your labels to
WORK/shards/pass1/out/path_precision-03.labels.csv and nothing else outside your own scratch
directory.
```

Brief each agent fully at its start, because a message to an agent of a running workflow starts
a second agent in the same place.

## The second pass

`evaluation sample` marks a seeded fifth of the items of each sample with `second_pass`
(`20261013:second:<sample>`, at least one item, and for path recall only among the repositories
with a candidate). `make_shards.py` writes those items into the shards of `pass2/`, with the same
columns as the first pass and nothing that a first-pass agent writes. The second pass stays blind
to the first in three ways:

- it starts at the same time as the first, so the first labels do not exist yet
- its agents receive only `pass2/` shards, and the brief forbids them `pass1/`, `labels/`,
  `review.csv` and the report
- its shards of commit recall have no `screen_hits`, so its agent reads all 200 commits and
  checks the screen of the first pass rather than repeating it

`merge_labels.py` writes the second-pass labels with `pass` 2 beside the first-pass labels, and
`evaluation report` computes the agreement and Cohen's kappa over the items with both.

## The report and the resolutions

Once every labels file exists, `merge_labels.py` checks and joins them, and `evaluation report`
writes `review.csv`: every item with an uncertain pass or with passes that disagree. The items of
the two open questions are among them until their decision is in the brief.

A person resolves the items of `review.csv`, as `docs/tools/evaluation.md` states and the paper
reports ("a person resolved uncertain and conflicting labels"). Agents prepare the resolutions:

1. Split `review.csv` by sample and start one agent per sample with items. Each agent follows
   the section [Resolutions](2-labeling-brief.md#resolutions) of the brief: it reads both
   passes, their evidence and the sources of the brief, and writes
   `WORK/resolution-proposals-<sample>.csv` with the columns
   `item,label,tool,attribution_correct,paths,note,evidence`: the label that it proposes, and in
   `evidence` the source that decides it. A proposal that applies a decided case names its item
   and evaluation, as the resolutions of 2026-10-26 do ("as pp-0427 of the evaluation of
   2026-10-05").
2. The maintainer reads the proposals and confirms or changes each. A question of principle that
   the maintainer decides gets the date in its `note`, such as "(decided 2026-11-09)", and goes
   into the brief and into `docs/tools/evaluation.md`.
3. The lead writes the confirmed rows, without `evidence`, into
   `WORK/2026-11-04/labels/resolutions.csv` (`item,label,tool,attribution_correct,paths,note`)
   and runs `evaluation report` again until `review.csv` lists no item. A resolution replaces both
   passes of its item, and its `attribution_correct` is judged under the meaning that the passes
   used.

## After the resolutions

The final labels confirm errors: a row of path precision that is not `correct`, a repository of
path recall that is `missed`, a commit of commit precision that is not `correct` or has
`attribution_correct` `no`, a credit of commit recall that the heuristics do not find, and a
phrasing labeled `attribution` that no heuristic reads. Each becomes a fix with a regression test
that fails without it, or a limit that `docs/dataset.md` or `UNREAD_ATTRIBUTIONS` states with its
reason. The fixes are independent of each other and can run in parallel worktrees, each from
current `main`. A heuristic read off an item links the item in `evidence` and names it in `notes`
as `<sample> <item> of the evaluation of 2026-11-04`. The evaluation keeps its sample after the
fixes, as the evaluation of 2026-10-26 did, because it measures the outputs that `sample.json`
records. `TODO.md` completes the task once the report of the published collection has no
unresolved item and its confirmed errors are fixed.
