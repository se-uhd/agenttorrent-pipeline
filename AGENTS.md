# Repository instructions

Before you change this repository, read [CLAUDE.md](CLAUDE.md) in full. It contains the project
instructions that Codex and Claude Code share, including the test commands, the documentation
rules and the order in which to regenerate the dataset. Open the file yourself, and do not assume
that it was loaded automatically. Keep the detailed project rules in `CLAUDE.md` and nowhere else.
[GLOSSARY.md](GLOSSARY.md) defines the terms.

Set up macOS, Ubuntu or the x86-64 container as [docs/development.md](docs/development.md)
describes. Use the latest complete handover as the evidence for improving the pipeline, the
grounded heuristics, the tests and the verification. [docs/dataset.md](docs/dataset.md) names
that handover, and corrected results do not necessarily match the counts of an earlier release. Run
targeted tests while you implement a change. Before every push, run the complete verification on
the committed state.

Follow the section [Documents](CLAUDE.md#documents) of `CLAUDE.md` for every handover and every
change to the documentation. That section requires current instructions for macOS, Ubuntu and the
x86-64 container, and the terms that GLOSSARY.md defines. Keep the limitations of the dataset in
`docs/dataset.md`.
