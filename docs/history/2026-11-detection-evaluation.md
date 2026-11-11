# The detection accuracy evaluation of the collection of 2026-11-04

This record states how the labels and the resolutions in `data/collection/evaluation/2026-11-04/`
came about. It was written on 2026-11-11 from the transcripts of the Claude Code session that
produced them.

## Who labeled

Claude Code labeled and resolved every sample of this evaluation. All agents ran in workflows of
one local Claude Code session and used Claude Opus 5.5 (`claude-opus-5-5`), as each agent
transcript records, and no workflow set another model. No person labeled an item.

| Workflow | Started (UTC) | What it did | Agents |
|---|---|---|---|
| `label-path-and-recall-samples` | 2026-11-07 15:03 | Labeled the path precision, path recall and commit recall samples in both passes | 16 |
| `resolution-proposals-early` | 2026-11-07 15:28 | Proposed resolutions for the items of an earlier draw; its two agents recorded no model | 2 |
| `label-paths-and-commit-recall-1108` | 2026-11-07 23:48 | Labeled the items of the final draw that no earlier label covered, in both passes | 16 |
| `label-final-commit-samples` | 2026-11-08 05:17 | Labeled commit precision, the mentions and the changed commit recall items in both passes | 11 |
| `resolve-final-evaluation` | 2026-11-08 05:36 | Proposed a resolution with its evidence for each of the 22 items to review | 4 |
| `evaluate-imports-and-planning-documents` | 2026-11-08 12:31 | Added the samples of the imports and the planning documents to the evaluation tool | 3 |
| `label-imports-and-planning-documents` | 2026-11-08 14:19 | Labeled the import and planning document samples in both passes | 5 |
| `resolve-imports-and-planning-documents` | 2026-11-08 14:40 | Proposed a resolution with its evidence for each of their 28 items to review | 3 |

## The two passes

Each labeling workflow ran the first and the second pass in parallel, in separate agents with
separate shards. The brief told an agent of the second pass not to open the shards of the first
pass, the merged labels, `review.csv` or the report. The second pass is therefore a second blind
labeling by the same model, not by an independent rater. Both passes saw which heuristics matched
an item, and the people and agents that labeled the samples also wrote the heuristics.

## The resolutions

`evaluation report` listed in `review.csv` every item with an uncertain label or with passes that
disagree. Agents of the same model proposed a resolution with its evidence for each item, and the
lead session of Claude Code, which also used Claude Opus 5.5, wrote the resolutions into
`labels/resolutions.csv`. The brief foresaw that the maintainer confirms or changes each proposal.
On 2026-11-07, when the lead session put eight open cases to the maintainer, the maintainer
delegated such decisions to the lead session ("figure this out"), and the lead session decided
them as it had recommended. These decisions are the section "Decisions of 2026-11-07" of the brief.
No person confirmed the resolutions of this evaluation. The only edits by a person were the
masking of a display name in `mn-0086` and of two logins in `mn-0109`.

The commit that brought the labels of the import and planning document samples into the checkout,
`c9a86795` of the private repository `se-uhd/agenttorrent-pipeline-internal`, carries a `Claude-Session` trailer that names a cloud session, but the labeling and the
resolution of these samples ran in the local session of this record.

## The files of this record

- `2026-11-detection-evaluation/labeling-brief.md`: the brief that every labeling and resolving
  agent read, in its version of 2026-11-08, with the section "Decisions of 2026-11-07".
- `2026-11-detection-evaluation/maintainer-decisions.md`: the cases that the lead session prepared
  for the maintainer on 2026-11-07 and then decided itself.
- `2026-11-detection-evaluation/workflow-plan.md`: the plan of the labeling workflows.
- `2026-11-detection-evaluation/workflows/`: the script of each workflow of the table, with the
  prompts that its agents received.

The files are the originals, except that `<checkout>` and `<scratchpad>` replace the local paths
of the checkout and of the session's scratch directory.

## The transcripts

The transcript of the session and those of every agent of these workflows quote repository
content, including the names and addresses of commit authors, which the evaluation masks, and they
are therefore not public. The research group keeps them, with the unredacted scripts and the
preparation files of the evaluation, in `agenttorrent-detection-evaluation-2026-11-04-transcripts.7z`
in the folder `Forschung/AgentTorrent` of its storage, with the SHA-256
`6675c9edc0163241176cb601925f3ca5c9d1ed19e6482866364a055f703d6110`.
