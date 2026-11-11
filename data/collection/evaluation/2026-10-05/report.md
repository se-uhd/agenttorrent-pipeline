# Detection accuracy

`python -m agentcontext evaluation report` wrote these estimates, each with its interval at a confidence of 0.95.

## Path precision

Precision, weighted by the rows of each tool and mechanism: 0.994 [0.937, 1.000] over 627 rows.

| Mechanism | Precision |
|---|---|
| commands | 0.999 [0.879, 1.000] |
| context_file | 0.999 [0.888, 1.000] |
| hooks | 0.997 [0.857, 1.000] |
| lsp | 1.000 [0.810, 1.000] |
| mcp | 0.991 [0.900, 1.000] |
| other | 0.989 [0.890, 1.000] |
| plugins | 1.000 [0.864, 1.000] |
| rules | 0.952 [0.831, 1.000] |
| settings | 0.978 [0.854, 1.000] |
| skills | 0.999 [0.884, 1.000] |
| subagents | 0.988 [0.875, 1.000] |

## Path recall

Repositories without detected configuration that contain a configuration path: 0.000 [0.000, 0.019], an estimated 0 of the 28105 repositories that the stratum held when the sample was drawn.
Repositories with detected configuration in which the detection missed a configuration path: 0.000 [0.000, 0.037].
Configuration paths that the detection found in the repositories with detected configuration: 1.000 [0.992, 1.000].

## Commit precision

Precision over the tools of the commit CSV, weighted by their commits: 1.000 [0.968, 1.000] over 300 commits.

| Stratum | Precision |
|---|---|
| other_tools | 1.000 [0.940, 1.000] |
| tool:Claude | 1.000 [0.940, 1.000] |
| tool:Codex | 1.000 [0.940, 1.000] |
| tool:Copilot | 1.000 [0.940, 1.000] |
| tool:Cursor | 1.000 [0.940, 1.000] |
| tool:Gemini | 1.000 [0.940, 1.000] |

Attributions correct: 0.993 [0.978, 0.997].

## Commit recall

19 of 1000 commits drawn at random from the commit logs credit a tool. The heuristics find 1.000 [0.832, 1.000] of them.

## Unmatched mentions

Phrasings labeled attribution that no heuristic matches: 0.070 [0.042, 0.114], in 404 commits.

## Agreement of the two passes

| Sample | Items | Agreement | Kappa |
|---|---|---|---|
| path_precision | 126 | 0.9841 | 0.916 |
| path_recall | 27 | 1.0 | 1.0 |
| commit_precision | 80 | 1.0 | 1.0 |
| commit_recall | 200 | 1.0 | 1.0 |
| mentions | 40 | 0.975 | 0.7872 |
