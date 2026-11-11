export const meta = {
  name: 'resolution-proposals-early',
  description: 'Propose a resolution for every uncertain, disputed or erroneous item of the path and commit recall samples of the detection accuracy evaluation',
  phases: [{ title: 'Propose' }],
}
const S = '<scratchpad>'
const W = `${S}/detection-eval-2026-11-04`
const BRIEF = `${S}/detection-eval-prep/2-labeling-brief.md`
const ITEMS = args.items
const SCHEMA = { type: 'object', properties: { file: { type: 'string' }, proposals: { type: 'integer' }, errors_confirmed: { type: 'array', items: { type: 'string' } }, questions_for_maintainer: { type: 'array', items: { type: 'string' } } }, required: ['file', 'proposals', 'errors_confirmed', 'questions_for_maintainer'] }
phase('Propose')
const jobs = []
for (const [sample, list] of Object.entries(ITEMS)) {
  const chunks = []
  for (let i = 0; i < list.length; i += 12) chunks.push(list.slice(i, i + 12))
  chunks.forEach((chunk, n) => jobs.push({ sample, chunk, n }))
}
const results = await parallel(jobs.map(j => () => agent(
`Prepare resolution proposals for items of the sample ${j.sample} of the detection accuracy evaluation of 2026-11-04: ${j.chunk.join(', ')}. Follow the section Resolutions of the brief at ${BRIEF}, with WORK=${W}. Read both passes of each item in ${W}/shards-all/pass1/out/ and pass2/out/ (the labels files of the sample), the item's row in ${W}/2026-11-04/${j.sample}.csv, the files and sources that the brief names, and the checkout at <checkout>, whose data/collection holds the outputs and the validated tree of this collection.

For each item, decide the label that the criteria of the brief give, with the source that decides it, and say whether the heuristics or the code make an error that a fix should correct (errors_confirmed, one line each with the item, the error and the fix), or whether the item needs a decision of the maintainer that no earlier decision covers (questions_for_maintainer, one line each, plainly worded with the options). Apply every decision that the brief or the earlier resolutions record, and cite it. Write the proposals to ${W}/resolution-proposals-${j.sample}-${String(j.n + 1).padStart(2, '0')}.csv with the columns item,label,tool,attribution_correct,paths,note,evidence. Do not run docker, pkill or killall, do not edit the repository, and write nothing else outside ${W}/scratch/resolution-${j.sample}-${j.n + 1}/.`,
  { label: `propose:${j.sample}:${j.n + 1}`, phase: 'Propose', schema: SCHEMA })))
return results.filter(Boolean)