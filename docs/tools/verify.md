# Verification suite

Before each push, commit the completed batch and run `python3 -m agentcontext verify [report.txt]`
from the repository root, on macOS and Linux through `mise exec -- bash dev/verify.sh` (below).
While you implement, run the affected suites instead, such as
`mise exec -- python3 -m unittest tests.test_paths`.

Run it without `AGENTCONTEXT_DATA_ROOT`. While the variable names a data root, the command logs
one line that names the variable and its value and exits 1 without running a suite. Every suite
and check would otherwise read the data root that the variable names, such as the working copy of
a handover, rather than the `data/` of the checkout, which CI checks and which `git status`
inspects at the end.

Before the first suite, the command compares the environment that runs it with the environment in
which CI runs the suites ([Python environments](../development.md#python-environments)). It logs
one line for each difference and exits 1 without running a suite when:

- the interpreter is another release of Python than the one that `mise.toml` pins, as in a
  `.venv` that mise created before the pin changed, which you remove and create again with
  `mise exec -- bash dev/setup.sh`
- the `python3` on `PATH`, which the suites and the steps of the runner start by name, is the
  interpreter of another environment, as when you start `.venv/bin/python` by its path from a
  shell in which mise's shared installation of Python comes first
- a distribution is installed that the dependency lock `locks/development.txt` does not list for
  the platform, or one that it lists is missing or installed at another version
- a distribution has a `direct_url.json`, which pip and uv write for an install from a URL or a
  local path (PEP 610), such as a package built from a local directory
- `agentcontext` is not installed editable from this checkout, as CI and `dev/setup.sh` install it
- site-packages holds a file or a directory that the RECORD of no distribution lists, such as a
  module put there by hand, outside the `__pycache__` directories that Python writes on import.
  `dev/setup.sh` leaves such an entry in place, and the command logs the remedy: Remove `.venv`
  and run `mise exec -- bash dev/setup.sh`, which creates it again with the locked distributions
  only. When another environment runs the command, such as the virtual environment of the
  development container, or the entry is outside `.venv`, the command names the environment that
  runs it instead
- a file differs from the SHA-256 that the RECORD of its distribution lists, or is missing, as
  after an edit of an installed package

The environment may hold pip beside the distributions of the dependency lock and the editable
install. A suite that passes in another environment, such as mise's shared installation of Python
with its packages, can fail in CI and in the image, which run the pinned release with the
dependency locks only. The command reads the distributions of the site-packages of its
environment only. An `*.egg-info` folder in the working directory, which a build of the package
with setuptools can leave in the root of the checkout and `.gitignore` hides, is no installed
distribution, although `python -m` puts that directory first on `sys.path`.

The command does not detect a distribution that was built from other sources at a locked version and
installed from a local index or a folder of wheels. Its metadata names the locked version, it has no
`direct_url.json`, and its RECORD lists the files that it installed with their hashes, so it agrees
with itself. An installed distribution records no hash of the wheel that it was installed from, so
the command cannot compare its files with the wheels whose hashes the dependency lock lists.
`dev/setup.sh` replaces such a distribution, because it installs every locked distribution again
from its locked file. Of two distributions with one name, the command compares only the first on
`sys.path` with its RECORD, the one that an import finds. In the development container, that leaves
out the pip of the base image, whose tests the base image deletes and which the pip of the virtual
environment hides.

The command then runs the test modules that `.github/workflows/pipeline-tests.yml` lists, each
step with the interpreter options that the workflow gives it, then these checks in this order:

- `check_reproduction.py` in `4_data-analysis/`, the reproduction of the analysis
- `regressions check`, the regression outputs
- `snapshot write --check --outputs-only`, the output snapshot
- `unmatched paths --fail-above 2`, the unmatched paths
- `output-cross-check`, the output cross check
- `4_filter_ghs.py --check-licenses` in `1_sampling/`, the licenses of the last sampling frame
- `statistics --check`, [the stated statistics](statistics.md)

Last, it checks with `git status` that the working tree is clean.

The suite uses test fixtures and the tracked dataset outputs. It does not collect the full sample,
process the complete handover or validate a production build, which are separate
[pre-deployment checks](../deploy.md#pre-deployment-checks). A passing suite does not replace
them.

On Linux, pass `--analysis-output-dir DIR` to reproduce the analysis outputs in a separate
directory. The reproduction check then compares the two generated runs and leaves the tracked
outputs unchanged. Without this option, it also compares the generated outputs with the tracked
files. Keep the output directory and the report outside the checkout.

The command exits 0 when every command passed and the working tree is clean. It exits 2 when a
command failed or a file changed. When `git status` fails, the state of the tree is unknown, and the
command exits 1, as it does in an environment that differs from the one of CI and while
`AGENTCONTEXT_DATA_ROOT` is set. Every suite and check
runs with the interpreter that runs the command and without `PYTHONPATH`, as CI runs every step
whose `env:` does not set it, so that a directory that your shell adds to `PYTHONPATH` cannot give a
suite a module that CI does not have. The suites and checks import `agentcontext` from the editable
install of the checkout. A suite whose step sets `PYTHONPATH`, the sampling runner suite, runs with
this checkout as its only entry. CI runs that suite with `-S`, which imports no site packages,
before the install of the dependency lock, in the `.venv` that mise created and that holds no
distribution. The command runs it with `-S` too, with the interpreter of an empty virtual
environment that it creates from its own interpreter with the `venv` module in a temporary
directory, and with that environment activated in place of its own: the bin directory of its own
environment leaves `PATH`, as `deactivate` removes it. The child processes of the suite, which start
that interpreter, then import what they import in CI, the standard library and the package of the
checkout, and find none of the console scripts in the bin directory of the environment of the
command, such as `coverage`. A program of the same name in another directory of `PATH` stays visible
to them, such as a shim of mise or a script that pip installed into `/usr/local/bin` of the
development image. The command removes the environment when the suites have run. The report lists
the status of each command and includes the output of the failed commands. In every line of the
report, including the message of git when `git status` fails, a path in the checkout is relative to
the checkout, the checkout itself is `.`, and another path in your home directory begins with `~`,
because `docs/validation/` keeps reports and a report should name no directory of the machine that
ran it. This holds also for a path after a short option, such as `-o/code/data/sampling` in the
command of a message that names the archive to extract again, and for a `file://` URL, which
becomes the path. A longer path and a URL of another scheme that contain the checkout or the home
directory stay as they are, such as `/opt/code/bin` and `https://example.com/code` when the
checkout is `/code`. The command writes the report to the log, and also to `report.txt` when you
give that path.

`mise exec -- bash dev/verify.sh [report-dir]` runs the command with the `python3` of `.venv`,
which `mise exec` activates and puts first on `PATH`, and exits with its exit status. It writes
`verify-<commit>.txt` and `verify-<commit>.log` into the report directory, by default `$TMPDIR` or
`/tmp`, and on Linux it passes `--analysis-output-dir <report-dir>/analysis`. Before the run, it
removes the report of an earlier verification of the same commit, and when the command exits
before it writes a report, such as after a refusal of the environment, it prints the log instead.
It leaves any changes in the working tree for you to inspect. See [development.md](../development.md) for the
macOS, Linux and container workflows.

`tests/test_tools_verify.py` checks that the verification runs every tracked test module from its
directory with the interpreter options of its workflow step, every check that CI runs and the four
checks over the local data, each with the interpreter that runs the verification and not with a
`python3` first on `PATH`, and the sampling runner suite in an empty virtual environment, in which a
child process finds no distribution in its site-packages and none of the console scripts in the bin
directory of the environment that runs the verification. It also checks that only the suite whose
step sets `PYTHONPATH` gets it, also when the shell sets one, that a check in a stage directory
imports the package of this checkout, the report and the exit status, including a failure of
`git status` and a failing wrapper, and that the report names neither the checkout nor the home
directory by its absolute path, also in the message of a failed `git status`, in the command of the
message of `CopyDiffers`, after another short option and in a file URL, while a longer path and a
URL of another scheme that contain either stay as they are. Over invented releases, `python3`
programs and sets of installed distributions, it checks that the verification refuses to start
under another release, with a `python3` on `PATH` of another environment, for an unlisted, a
missing and a different distribution, for one installed from a local directory and without the
editable install of the checkout, and that it starts in the environment of the dependency lock. It
checks that an `*.egg-info` folder in the working directory neither hides the editable install nor
counts as an installed distribution. Over site-packages that it writes, it checks that a module that
no RECORD lists, an edited file and a missing file are differences, that the command names how to
create `.venv` again for a module in `.venv` that no RECORD lists and the environment that runs it
for a module elsewhere or in another environment, that a distribution hidden by an earlier one of
its name is not compared, and that the site-packages of the environment that runs the suite agree
with their RECORD files.

`TestCurrentDatasetRestore` in the same module checks that `dev/setup.sh` extracts the current
commit archives, including numbered volumes, keeps an existing CSV, and rejects an incomplete
archive without installing a partial CSV or leaving its temporary file. It also checks that the
setup extracts the missing files of the archives of the sampling frame and the labels and derives
the list of the sample from the labels, keeps files that match their archive, and keeps newer
labels without extracting another file of their archive or deriving the list again. The setup
installs no file and leaves no temporary directory when a file of the archive fails its CRC, when
the archive stores the file in a directory, and when a termination signal stops the setup during
the extraction. The setup never reconstructs the inputs of earlier collections.
