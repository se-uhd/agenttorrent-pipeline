# The tool selection

`python -m agentcontext tool-selection --file-lists DIR` counts, in the file lists of a
collection, the repositories and the owners with a root-level path of each tool that
`data/collection/evaluation/tool-selection/signatures.csv` lists. It writes the counts and two
cuts of their distribution to `counts.json` in the same directory. Beside the five tools of the
survey, the dataset covers the tools that `counts.json` lists in `cuts.added`, as
[the heuristics](../heuristics.md#the-tools) describe.

```shell
python -m agentcontext tool-selection --file-lists data/file_lists
```

Run it from the `data/collection/` directory of a handover's working copy, in which
`data/file_lists` holds the file lists of the collection, `<owner>§<repo>.ls-tree.xz`. The
cloning step keeps these files beside its tree, and a Git checkout has none. The command reads
`signatures.csv` and writes `counts.json` in `collection/evaluation/tool-selection/` of the data
root, which is the checkout's `data/` when `AGENTCONTEXT_DATA_ROOT` is unset. With the variable
set to a working copy, pass the checkout's file with `--signatures` and the path to write with
`--output`.

## The signatures

`signatures.csv` has one row per tool, with the columns `tool`, `category`, `paths`, `doc_url`,
`doc_verified` and `notes`. `category` is `survey` for the five tools of the 2025 Stack Overflow
Developer Survey, `agent` for another coding agent and `review` for a product that reviews
a change rather than writing one. `paths` lists the tool's paths, joined by `|`: A path ending in
`/` names a directory at the repository root and matches every file below it, and any other path
names a file at the root. `doc_url` is the vendor page that names the paths, read on
`doc_verified`, and `notes` gives what the page states. For the five, the paths are those of the
rows of `heuristics/config_path_patterns.csv`. A row without `doc_url` names a tool with a page
that could not be read, and its notes link the files in the file lists that its paths come from.

`signatures.csv` has no row for a tool if its documentation names no path in the repository,
such as OpenClaw, which keeps its workspace in `~/.openclaw/workspace`, or Hermes Agent, which
keeps its skills in `~/.hermes/skills/`. Nor does it have a row for an add-on that runs inside
another tool, such as oh-my-opencode or Serena.

## The counts

An owner is the account in a repository's name. An organization that keeps one layout in many
repositories therefore counts once. Only root-level paths count, so that every tool is counted
alike: Otherwise, a nested configuration of one tool and a vendored copy of another would count
differently. A file with a directory's name, such as a link that the file list records as
`.opencode`, matches no directory, because the file list does not record where the link leads,
which can be out of the repository.

## The cuts

The cuts are over the tools of the categories `survey` and `agent` with at least one owner, in the
order of their owners.

- The largest step: the largest ratio between the owners of two neighbors, from the last tool of
  the survey down. The tools between the last tool of the survey and the step, `added`, are the
  tools that the dataset adds.
- Head/tail breaks (Jiang 2013, "Head/tail breaks: A new classification scheme for data with a
  heavy-tailed distribution", The Professional Geographer 65(3)): the tools above the mean of the
  owners, split again at the mean of that head while the head is less than two fifths of the
  tools that it was split from and has two tools or more. `counts.json` gives the breaks over all
  the tools and over the tools outside the survey.

`counts.json` has `file_lists`, `repositories` and `owners` by tool, the name and the checksum of
the signatures, and under `cuts` the order, every step, the largest step, the added tools and the
breaks.

## Exit codes

The command exits 0 when it wrote the counts and 1 when the signatures or the file lists could not
be read.
