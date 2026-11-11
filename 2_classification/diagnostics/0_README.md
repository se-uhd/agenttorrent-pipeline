# Classification diagnostics

The `calibration_*.py` scripts measure what the classification reads over the calibration
sample, and [the reading rules](../0_README.md#the-reading-rules) were set from their results. They
read and write `../../data/classification/calibration/`, which tracks the results compressed.
[The record of the calibration](../../docs/history/2026-10-classification-calibration.md) gives
the results. The scripts import the `agentcontext` package. `calibration_measure.py` and
`calibration_language_variants.py` also import modules of `2_classification/`, and
`calibration_summaries.py` imports `openai`, so these scripts need the packages pinned in
`../requirements.txt`. From the repository root, install the dependency lock
`locks/development.txt`, which locks them, and `agentcontext` into `.venv`:

```bash
mise exec -- bash dev/setup.sh
```

| Script | What it does |
|---|---|
| `calibration_measure.py` | Draws the calibration sample from the sampling frame with `draw --size N --seed S`, then measures, one phase per command, the READMEs, the file trees, the pages that the READMEs link to and the github.com links, from GitHub and the web |
| `calibration_analyze.py` | Logs the distribution of each measurement and the share of the sample that a candidate limit cuts, with its Wilson interval |
| `calibration_summaries.py` | Summarizes a random set of the linked pages with a model. Because it costs money, run it only with the approval of the person who pays for it |
| `calibration_language_variants.py` | Reads the READMEs of the calibration sample again and tests other language checks against the model's judgment in `language_check.csv` |

The reproduction check runs none of them, because three of them read GitHub, the web or an
OpenAI model, and `calibration_analyze.py` writes no file.

The scripts that plotted the intermediate files of the batch classification of March 2026 and
their sample data are in the history of the private repository `se-uhd/agenttorrent-pipeline-internal`.
