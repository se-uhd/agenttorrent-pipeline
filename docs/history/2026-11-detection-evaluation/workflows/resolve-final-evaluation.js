export const meta = {
  name: 'resolve-final-evaluation',
  description: 'Propose a resolution with evidence for each of the 22 items to review of the final detection accuracy evaluation',
  phases: [{ title: 'Resolve' }],
}
const S = '<scratchpad>'
const F = `${S}/detection-eval-final`
const BRIEF = `${S}/detection-eval-prep/2-labeling-brief.md`
const GROUPS = [
  { key: 'paths-a', items: ['pp-0198', 'pp-0214', 'pp-0218', 'pp-0246', 'pp-0320'] },
  { key: 'paths-b', items: ['pp-0365', 'pp-0374', 'pp-0688', 'pp-0797', 'pr-0140', 'pr-0252'] },
  { key: 'commits', items: ['cp-0098', 'cp-0290', 'cp-0439', 'cp-0563', 'cr-0044', 'cr-0503', 'cr-0613', 'cr-0891'] },
  { key: 'mentions', items: ['mn-0067', 'mn-0111', 'mn-0187'] },
]
const SCHEMA = { type: 'object', properties: { resolutions: { type: 'array', items: { type: 'object', properties: {
  item: { type: 'string' }, label: { type: 'string' }, tool: { type: 'string' }, attribution_correct: { type: 'string' }, paths: { type: 'string' }, note: { type: 'string' }, detection_error: { type: 'string', description: 'the detection or heuristic defect that the item confirms, with the fix, or empty' } },
  required: ['item', 'label', 'tool', 'attribution_correct', 'paths', 'note', 'detection_error'] } } }, required: ['resolutions'] }
const results = await parallel(GROUPS.map(g => () => agent(
`Resolve these items of the detection accuracy evaluation of the collection of 2026-11-04: ${g.items.join(', ')}. They are in ${F}/2026-11-04/review.csv with both passes' labels, evidence and notes; the merged labels are in ${F}/2026-11-04/labels/<sample>.csv and the sample rows in ${F}/2026-11-04/<sample>.csv. The criteria are in the brief ${BRIEF}, with its section "Decisions of 2026-11-07"; the decisions of 2026-11-08 add that mapping out or navigating the code, a run of a repro, a reproduction or an experiment, installing, guarding out and demonstrating make nothing, that "used <tool> as a <role> to" reads as "used <tool> to", and that a first sentence that only says the tool was used takes its activities from the following sentences with It or the tool as subject. The maintainer delegated these decisions: decide each item on the evidence (the vendor pages, the shipped code, GitHub's records through gh api, the validated tree of the checkout <checkout>/data/collection/data/validated) and the documented decisions in docs/heuristics.md and docs/commits.md of the checkout, and do not leave an item open. For each item give the final label and the columns that its sample needs (tool for path precision with wrong_tool, attribution_correct yes or no for commit precision, paths for path recall, tool for commit recall with credits, empty otherwise), a note of one or two sentences that states the reason with the source, and, where the item shows a defect of the detection or the heuristics, the defect and its fix. Write people as (a person). Stay read-only: no edits, no docker, pkill or killall; scratch only under ${F}/scratch/resolve-${g.key}/.`,
  { label: `resolve:${g.key}`, phase: 'Resolve', schema: SCHEMA })))
return results.filter(Boolean).flatMap(r => r.resolutions)
