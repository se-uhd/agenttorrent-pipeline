export const meta = {
  name: 'label-imports-and-planning-documents',
  description: 'Label the import and planning document precision samples of the evaluation of 2026-11-04, both passes in parallel',
  phases: [{ title: 'Label' }],
}
const S = '<scratchpad>'
const EVAL = '<checkout>/data/collection/evaluation/2026-11-04'
const W = `${S}/detection-eval-final/shards-im-pd`
const BRIEF = `${S}/detection-eval-prep/2-labeling-brief.md`
const SCHEMA = { type: 'object', properties: { shard: { type: 'string' }, labels_file: { type: 'string' }, items: { type: 'integer' }, labeled: { type: 'integer' }, uncertain: { type: 'integer' }, notes: { type: 'string' } }, required: ['shard', 'labels_file', 'items', 'labeled', 'uncertain', 'notes'] }
phase('Label')
const results = await parallel(args.map(s => () => agent(
`Label the shard ${W}/${s.reads} of the detection accuracy evaluation of the collection of 2026-11-04 in pass ${s.pass}. Follow the brief at ${BRIEF} in full, in particular its sections "## ${s.sample}" and "Decisions of 2026-11-07", with the evaluation directory ${EVAL} (read-only) and the shard directory ${W}. Write your labels to ${W}/${s.writes} and nothing else outside your own scratch directory under ${S}/detection-eval-final/scratch-im-pd/${s.shard.replace('/', '-')}/.

The checkout at <checkout> holds the final outputs and the validated tree of this collection under data/collection/ and the heuristics that the draw read (main at 3eaccefd); read the trees there, and the raw files on GitHub at the head_sha of the row where the tree does not hold a file. ${s.pass === '2' ? 'You label the second pass: never open anything under ' + W + '/pass1/, any labels file, review.csv or a report.' : 'You label the first pass.'} Label every item of the shard, and set uncertain for an item that the brief tells you to leave to the maintainer or that the sources do not settle. Do not run docker, pkill or killall, and do not edit the repository. Report the counts.`,
  { label: `label:${s.shard}`, phase: 'Label', schema: SCHEMA })))
return results.filter(Boolean)
