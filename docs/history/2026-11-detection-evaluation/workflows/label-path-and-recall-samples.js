export const meta = {
  name: 'label-path-and-recall-samples',
  description: 'Label the path precision, path recall and commit recall samples of the detection accuracy evaluation of 2026-11-04, both passes in parallel',
  phases: [{ title: 'Label' }],
}
const S = '<scratchpad>'
const W = `${S}/detection-eval-2026-11-04/shards-all`
const BRIEF = `${S}/detection-eval-prep/2-labeling-brief.md`
const SHARDS = args.shards
const SCHEMA = { type: 'object', properties: { shard: { type: 'string' }, labels_file: { type: 'string' }, items: { type: 'integer' }, labeled: { type: 'integer' }, uncertain: { type: 'integer' }, notes: { type: 'string' } }, required: ['shard', 'labels_file', 'items', 'labeled', 'uncertain', 'notes'] }
phase('Label')
const results = await parallel(SHARDS.map(s => () => agent(
`Label the shard ${W}/${s.reads} of the detection accuracy evaluation of 2026-11-04 in pass ${s.pass}. Follow the brief at ${BRIEF} in full, with WORK=${S}/detection-eval-2026-11-04 and the shard directory ${W}. Write your labels to ${W}/${s.writes} and nothing else outside your own scratch directory under ${S}/detection-eval-2026-11-04/scratch/${s.shard.replace('/', '-')}/.

The checkout at <checkout> now holds the outputs and the validated tree of this collection under data/collection/, and its heuristics are those that the draw read. Read the validated tree there. ${s.pass === '2' ? 'You label the second pass: never open anything under ' + W + '/pass1/, any labels file, review.csv or a report.' : 'You label the first pass.'} Label every item of the shard, and set uncertain for an item that the brief tells you to leave to the maintainer or that the sources do not settle. Do not run docker, pkill or killall, and do not edit the repository. Report the counts.`,
  { label: `label:${s.shard}`, phase: 'Label', schema: SCHEMA })))
return results.filter(Boolean)