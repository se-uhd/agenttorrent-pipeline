export const meta = {
  name: 'resolve-imports-and-planning-documents',
  description: 'Propose a resolution with evidence for the 28 items to review of the import and planning document samples',
  phases: [{ title: 'Resolve' }],
}
const S = '<scratchpad>'
const E = `${S}/detection-eval-final/eval-im-pd/2026-11-04`
const BRIEF = `${S}/detection-eval-prep/2-labeling-brief.md`
const GROUPS = [
  { key: 'imports', items: ['im-0026', 'im-0059'] },
  { key: 'conductor', items: ['pd-0124','pd-0125','pd-0126','pd-0127','pd-0128','pd-0129','pd-0130','pd-0131','pd-0132','pd-0133','pd-0134','pd-0135','pd-0136','pd-0137','pd-0138','pd-0139','pd-0140','pd-0141'] },
  { key: 'planning-other', items: ['pd-0086','pd-0090','pd-0095','pd-0209','pd-0210','pd-0238','pd-0239','pd-0242'] },
]
const SCHEMA = { type: 'object', properties: { resolutions: { type: 'array', items: { type: 'object', properties: {
  item: { type: 'string' }, label: { type: 'string' }, tool: { type: 'string' }, attribution_correct: { type: 'string' }, paths: { type: 'string' }, note: { type: 'string' }, detection_error: { type: 'string' } },
  required: ['item', 'label', 'tool', 'attribution_correct', 'paths', 'note', 'detection_error'] } } }, required: ['resolutions'] }
const results = await parallel(GROUPS.map(g => () => agent(
`Resolve these items of the detection accuracy evaluation of the collection of 2026-11-04: ${g.items.join(', ')}. They are in ${E}/review.csv with both passes' labels, evidence and notes; the merged labels are in ${E}/labels/import_precision.csv or planning_precision.csv and the sample rows in ${E}/import_precision.csv or planning_precision.csv. The criteria are in the brief ${BRIEF} (sections "## import_precision" and "## planning_precision"), with docs/tools/evaluation.md, docs/heuristics.md (Imports, Planning documents) and heuristics/planning_document_patterns.csv of the checkout <checkout> (read-only). The maintainer delegated these decisions: decide each item on the evidence (the vendor and framework pages and code, GitHub's records through gh api, the validated and imported trees of the checkout) and the documented decisions, and do not leave an item open. ${g.key === 'conductor' ? 'For the conductor items, first establish what the row of planning_document_patterns.csv that labels them names: Google\'s Conductor (gemini-cli-extensions/conductor) as a product, or the layout that it writes. Then decide whether the files of educelab/volume-cartographer, which follow the templates of the conductor plugin in wshobson/agents, are planning documents of the framework that the row names (correct) or of another framework (wrong_framework, with that framework in tool). Quote the decisive passages of both projects.' : ''} For each item give the final label and the columns that its sample needs (paths for import_precision with wrong_target, tool for planning_precision with wrong_framework), a note of one or two sentences that states the reason with the source, and, where the item shows a defect of the detection or the heuristics, the defect and its fix. Write people as (a person). Stay read-only: no edits, no docker, pkill or killall; scratch only under ${S}/detection-eval-final/scratch-resolve-im-pd/${g.key}/.`,
  { label: `resolve:${g.key}`, phase: 'Resolve', schema: SCHEMA })))
return results.filter(Boolean).flatMap(r => r.resolutions)
