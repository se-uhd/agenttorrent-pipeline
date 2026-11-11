# Detection accuracy

`python -m agentcontext evaluation report` wrote these estimates, each with its interval at a confidence of 0.95.

## Path precision

Precision, weighted by the rows of each tool and mechanism: 0.996 [0.941, 1.000] over 763 rows.

| Mechanism | Precision |
|---|---|
| commands | 0.996 [0.896, 1.000] |
| context_file | 1.000 [0.887, 1.000] |
| hooks | 1.000 [0.860, 1.000] |
| lsp | 1.000 [0.894, 1.000] |
| mcp | 0.965 [0.874, 1.000] |
| other | 1.000 [0.908, 1.000] |
| plugins | 0.992 [0.877, 1.000] |
| rules | 1.000 [0.873, 1.000] |
| settings | 1.000 [0.881, 1.000] |
| skills | 0.997 [0.883, 1.000] |
| subagents | 0.932 [0.819, 1.000] |

## Path recall

Repositories without detected configuration that contain a configuration path: 0.000 [0.000, 0.019], an estimated 0 of the 29926 repositories that the stratum held when the sample was drawn.
Repositories with detected configuration in which the detection missed a configuration path: 0.020 [0.005, 0.070].
Configuration paths that the detection found in the repositories with detected configuration: 0.995 [0.986, 0.998].

## Commit precision

Precision over the tools of the commit CSV, weighted by their commits: 1.000 [0.967, 1.000] over 360 commits.

| Stratum | Precision |
|---|---|
| other_tools | 1.000 [0.940, 1.000] |
| tool:Claude | 1.000 [0.940, 1.000] |
| tool:Codex | 1.000 [0.940, 1.000] |
| tool:Copilot | 1.000 [0.940, 1.000] |
| tool:Cursor | 1.000 [0.940, 1.000] |
| tool:Gemini | 1.000 [0.940, 1.000] |
| tool:OpenCode | 1.000 [0.940, 1.000] |

Attributions correct: 0.977 [0.960, 0.987].

## Commit recall

26 of 1000 commits drawn at random from the commit logs credit a tool. The heuristics find 1.000 [0.871, 1.000] of them.

## Unmatched mentions

Phrasings labeled attribution that no heuristic matches: 0.070 [0.042, 0.114], in 551 commits.

## Agreement of the two passes

| Sample | Items | Agreement | Kappa |
|---|---|---|---|
| path_precision | 153 | 0.9869 | 0.4934 |
| path_recall | 25 | 1.0 | 1.0 |
| commit_precision | 97 | 1.0 | 1.0 |
| commit_recall | 200 | 1.0 | 1.0 |
| mentions | 40 | 0.975 | 0.7872 |
