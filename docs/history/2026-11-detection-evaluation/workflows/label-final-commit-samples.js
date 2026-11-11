export const meta = {
  name: 'label-final-commit-samples',
  description: 'Label commit precision, mentions and the few changed commit recall items of the final draw, both passes in parallel',
  phases: [{ title: 'Label' }],
}
const S = '<scratchpad>'
const WORK = `${S}/detection-eval-final`
const W = `${WORK}/shards`
const BRIEF = `${S}/detection-eval-prep/2-labeling-brief.md`
const SCHEMA = { type: 'object', properties: { shard: { type: 'string' }, labels_file: { type: 'string' }, items: { type: 'integer' }, labeled: { type: 'integer' }, uncertain: { type: 'integer' }, notes: { type: 'string' } }, required: ['shard', 'labels_file', 'items', 'labeled', 'uncertain', 'notes'] }
phase('Label')
const results = await parallel(args.map(s => () => agent(
`Label the shard ${W}/${s.reads} of the detection accuracy evaluation of the collection of 2026-11-04 in pass ${s.pass}. Follow the brief at ${BRIEF} in full, with WORK=${WORK}, the evaluation directory ${WORK}/2026-11-04 and the shard directory ${W}. Its section "Decisions of 2026-11-07" settles the questions that earlier labels left open: label by it. The decisions of 2026-11-08 add to it: mapping out or navigating the code, a run of a repro, a reproduction or an experiment, installing, guarding out and demonstrating are activities that make nothing too, a disclosure that uses the tool "as a <role> to" an activity is read as "used <tool> to" that activity, and a first sentence that only says that the tool was used takes its activities from the following sentences of the paragraph whose subject is It or the tool. Write your labels to ${W}/${s.writes} and nothing else outside your own scratch directory under ${WORK}/scratch/${s.shard.replace('/', '-')}/.

The checkout at <checkout> holds the validated tree of this collection under data/collection/data/validated/ and its heuristics (main at e6458dea) are those that the draw read; the checkout's data/collection/output is from before the final regeneration, so take the rows and messages from the sample itself. The shard holds only the items that no earlier label covers; a file ${W}/${s.writes}.carried.csv, if it exists, holds labels of other items of the same shard and is not your concern. ${s.pass === '2' ? 'You label the second pass: never open anything under ' + W + '/pass1/, any labels file, any .carried.csv file, review.csv or a report.' : 'You label the first pass.'} Label every item of the shard, and set uncertain for an item that the brief tells you to leave to the maintainer or that the sources do not settle. Do not run docker, pkill or killall, and do not edit the repository. Report the counts.`,
  { label: `label:${s.shard}`, phase: 'Label', schema: SCHEMA })))
return results.filter(Boolean)
