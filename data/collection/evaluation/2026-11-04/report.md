# Detection accuracy

`python -m agentcontext evaluation report` wrote these estimates, each with its interval at a confidence of 0.95.

## Path precision

Precision, weighted by the rows of each tool and mechanism: 0.999 [0.944, 1.000] over 806 rows.

| Mechanism | Precision |
|---|---|
| commands | 0.981 [0.879, 1.000] |
| context_file | 1.000 [0.886, 1.000] |
| hooks | 1.000 [0.860, 1.000] |
| lsp | 1.000 [0.844, 1.000] |
| mcp | 1.000 [0.923, 1.000] |
| other | 1.000 [0.915, 1.000] |
| plugins | 0.978 [0.865, 1.000] |
| rules | 1.000 [0.873, 1.000] |
| settings | 0.993 [0.874, 1.000] |
| skills | 1.000 [0.886, 1.000] |
| subagents | 0.991 [0.884, 1.000] |

## Path recall

Repositories without detected configuration that contain a configuration path: 0.005 [0.001, 0.028], an estimated 150 of the 29955 repositories that the stratum held when the sample was drawn.
Repositories with detected configuration in which the detection missed a configuration path: 0.020 [0.005, 0.070].
Configuration paths that the detection found in the repositories with detected configuration: 0.992 [0.977, 0.997].

## Commit precision

Precision over the tools of the commit CSV, weighted by their commits: 1.000 [0.966, 1.000] over 360 commits.

| Stratum | Precision |
|---|---|
| other_tools | 0.983 [0.911, 0.997] |
| tool:Claude | 1.000 [0.940, 1.000] |
| tool:Codex | 0.983 [0.911, 0.997] |
| tool:Copilot | 1.000 [0.940, 1.000] |
| tool:Cursor | 1.000 [0.940, 1.000] |
| tool:Gemini | 0.983 [0.911, 0.997] |
| tool:OpenCode | 1.000 [0.940, 1.000] |

Attributions correct: 0.995 [0.985, 0.998].

## Commit recall

19 of 1000 commits drawn at random from the commit logs credit a tool. The heuristics find 0.947 [0.754, 0.991] of them.

## Unmatched mentions

Phrasings labeled attribution that no heuristic matches: 0.030 [0.014, 0.064], in 50 commits.

## Import precision

Precision, weighted by the rows of each stratum: 1.000 [0.905, 1.000] over 157 imports of strata with 10659 rows.

Precision without the strata drawn from the repositories that the evidence of the heuristics links: 1.000 [0.905, 1.000] over 154 imports of strata with 10656 rows.

The draw took these strata from those repositories, because they hold every row of each: copilot-import-02/pruned (2 rows), copilot-import-06/missing (1 row). The heuristics of these strata were read off the same repositories, and the precision over all strata can therefore be biased toward the heuristics.

| Heuristic and status | Rows | Precision |
|---|---|---|
| claude-import-01/found | 2679 | 1.000 [0.646, 1.000] |
| claude-import-01/missing | 1367 | 1.000 [0.646, 1.000] |
| claude-import-01/not_read | 4 | 1.000 [0.206, 1.000] |
| claude-import-01/outside | 15 | 1.000 [0.646, 1.000] |
| claude-import-01/pruned | 1 | 1.000 [0.206, 1.000] |
| claude-import-02/found | 2 | 1.000 [0.342, 1.000] |
| claude-import-02/missing | 2 | 0.500 [0.095, 0.905] |
| claude-import-03/found | 1 | 1.000 [0.206, 1.000] |
| claude-import-04/found | 3 | 1.000 [0.342, 1.000] |
| claude-import-04/missing | 82 | 1.000 [0.646, 1.000] |
| claude-import-05/found | 186 | 1.000 [0.646, 1.000] |
| claude-import-05/missing | 645 | 1.000 [0.646, 1.000] |
| claude-import-05/outside | 8 | 1.000 [0.646, 1.000] |
| claude-import-06/missing | 2 | 0.500 [0.095, 0.905] |
| copilot-import-01/found | 5 | 1.000 [0.566, 1.000] |
| copilot-import-01/missing | 100 | 1.000 [0.646, 1.000] |
| copilot-import-02/found | 356 | 1.000 [0.646, 1.000] |
| copilot-import-02/missing | 1188 | 1.000 [0.646, 1.000] |
| copilot-import-02/outside | 17 | 1.000 [0.646, 1.000] |
| copilot-import-02/over_limit | 12 | 1.000 [0.646, 1.000] |
| copilot-import-02/pruned | 2 | 1.000 [0.342, 1.000] |
| copilot-import-03/found | 2702 | 1.000 [0.646, 1.000] |
| copilot-import-03/missing | 1110 | 1.000 [0.646, 1.000] |
| copilot-import-03/outside | 18 | 1.000 [0.646, 1.000] |
| copilot-import-03/pruned | 1 | 1.000 [0.206, 1.000] |
| copilot-import-04/missing | 1 | 1.000 [0.206, 1.000] |
| copilot-import-05/found | 2 | 1.000 [0.342, 1.000] |
| copilot-import-05/missing | 2 | 1.000 [0.342, 1.000] |
| copilot-import-06/missing | 1 | 1.000 [0.206, 1.000] |
| cursor-import-01/found | 9 | 1.000 [0.646, 1.000] |
| cursor-import-01/missing | 2 | 1.000 [0.206, 1.000] |
| gemini-import-01/found | 100 | 1.000 [0.646, 1.000] |
| gemini-import-01/missing | 29 | 1.000 [0.646, 1.000] |
| gemini-import-02/found | 1 | 1.000 [0.206, 1.000] |
| gemini-import-02/missing | 1 | 1.000 [0.206, 1.000] |
| gemini-import-05/found | 1 | 1.000 [0.206, 1.000] |
| gemini-import-05/missing | 2 | 1.000 [0.342, 1.000] |

## Planning document precision

Precision, weighted by the rows of each stratum: 0.998 [0.933, 1.000] over 366 planning documents of strata with 27593 rows.

Precision without the strata drawn from the repositories that the evidence of the heuristics links: 0.999 [0.925, 1.000] over 160 planning documents of strata with 24496 rows.

The draw took these strata from those repositories, because they hold every row of each: awesome-copilot (1 row), backlog-md (2265 rows), beans (117 rows), bmad-method (143 rows), cc-sdd (233 rows), copilot (25 rows), humanlayer (25 rows), hve-core (2 rows), matt-pocock-skills (78 rows), opencode (84 rows), pilot-shell (2 rows), planning-with-files (101 rows), spec-driven-workflow (20 rows), task-master (1 row). The heuristics of these strata were read off the same repositories, and the precision over all strata can therefore be biased toward the heuristics.

| Framework or tool | Rows | Precision |
|---|---|---|
| ai-dev-tasks | 8 | 1.000 [0.342, 1.000] |
| awesome-copilot | 1 | 1.000 [0.206, 1.000] |
| backlog-md | 2265 | 1.000 [0.839, 1.000] |
| beans | 117 | 1.000 [0.839, 1.000] |
| bmad-method | 143 | 1.000 [0.839, 1.000] |
| cc-sdd | 233 | 1.000 [0.839, 1.000] |
| claude | 83 | 0.850 [0.640, 0.948] |
| compound-engineering | 107 | 1.000 [0.839, 1.000] |
| conductor | 24 | 0.000 [0.000, 0.176] |
| copilot | 25 | 1.000 [0.839, 1.000] |
| cursor | 236 | 1.000 [0.646, 1.000] |
| get-shit-done | 1626 | 1.000 [0.839, 1.000] |
| humanlayer | 25 | 1.000 [0.839, 1.000] |
| hve-core | 2 | 0.000 [0.000, 0.658] |
| matt-pocock-skills | 78 | 1.000 [0.839, 1.000] |
| opencode | 84 | 1.000 [0.839, 1.000] |
| openspec | 15307 | 1.000 [0.839, 1.000] |
| pilot-shell | 2 | 1.000 [0.342, 1.000] |
| planning-with-files | 101 | 0.950 [0.764, 0.991] |
| prp | 24 | 1.000 [0.772, 1.000] |
| spec-driven-workflow | 20 | 1.000 [0.839, 1.000] |
| spec-kit | 2868 | 1.000 [0.839, 1.000] |
| superpowers | 4213 | 1.000 [0.839, 1.000] |
| task-master | 1 | 1.000 [0.206, 1.000] |

## Agreement of the two passes

| Sample | Items | Agreement | Kappa |
|---|---|---|---|
| path_precision | 162 | 0.9877 | -0.0031 |
| path_recall | 27 | 1.0 | 1.0 |
| commit_precision | 115 | 0.9913 | 0.0 |
| commit_recall | 200 | 1.0 | 1.0 |
| mentions | 40 | 1.0 | 1.0 |
| import_precision | 32 | 1.0 | 1.0 |
| planning_precision | 74 | 0.9459 | 0.5807 |
