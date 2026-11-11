# Development scripts

Run these scripts from the repository root with `mise exec --`, which activates `.venv`, the
virtual environment that mise creates from the Python of `mise.toml`, and puts the pinned uv, git,
rsync and 7-Zip first on the `PATH`. The [development instructions](../docs/development.md)
describe the macOS, Ubuntu and Linux x86-64 container environments.

- `setup.sh` installs the distributions of the dependency lock `locks/development.txt` and the
  editable package into `.venv` with the commands that CI runs, removes every distribution that the
  dependency lock does not list and installs every listed one again from its locked file. It
  refuses to install when `.venv` of the checkout is not the active virtual environment, and when
  `.venv` runs another release of Python than the one that `mise.toml` pins. It then extracts from
  its tracked archive each commit CSV that is not on disk or differs from the archive. It also
  extracts each missing file of the archives of the sampling frame and the labels, keeps a file of
  them that differs from its archive, and from labels that match their archive writes the list of
  the sample, `data/classification/repos_annotated_included.csv`, with
  `engineered-project-selection`. `--data-only` skips the installation.
- `lock.sh` writes the dependency locks under `locks/` from the requirements files with the uv
  that `mise.toml` pins ([locks/0_README.md](../locks/0_README.md)). Run it after you change a
  pin, and then `setup.sh`.
- `verify.sh [report-dir]` runs the full verification and saves its report and log in the report
  directory, by default `$TMPDIR` or `/tmp`. On Linux, the analysis outputs go into the report
  directory and are compared between two runs. On native macOS, the verification also compares
  the tracked outputs.

- `export-public.py <directory> [<commit>]` writes the tree of a commit, by default HEAD, as the
  tree of the public repository se-uhd/agenttorrent-pipeline. It leaves out the paths that
  `.publicignore` lists and the blocks of the Markdown documents between a line `<!--internal-->`
  and a line `<!--/internal-->`, and it exits 1 when a link to a left-out path remains.

Run targeted tests while you edit. The full verification must pass on the committed state before
every push. Keep the report directory outside the checkout.
