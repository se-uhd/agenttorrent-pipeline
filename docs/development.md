# Development

Run the commands from the repository root unless a section names another directory. `mise.toml`
pins the release of Python, uv, git, rsync and 7-Zip. `mise install` installs the same Python,
git, rsync, and `7zz` versions as the production image,
using conda-forge's packages for git and rsync and the pinned 7-Zip release for `7zz`.
`mise exec` puts them first on `PATH`. These pins matter because history columns depend on the
git version ([git](git.md)), cloning copies files with rsync, and releases use 7-Zip archives.
Use Poppler (`pdftotext`) as well. The checks of the
website's JavaScript use Node.js, at a release that no file of the checkout pins. You need Docker
only for container development and deployment.

The project runs in its own virtual environment, `.venv` in the checkout, never in mise's shared
installation of Python. mise creates `.venv` from the pinned Python when it is missing and
activates it under `mise exec` and in a shell in which mise is active (`_.python.venv` in
`mise.toml`). `dev/setup.sh` installs the distributions of the dependency lock
`locks/development.txt` into it, and nothing else ([Python environments](#python-environments)).
Run every suite, script and command of the documents under `mise exec --`, or in a shell in which
mise is active and `.venv` therefore is. A bare `python3` in another shell is the one of mise's
shared installation of Python, which has none of the locked distributions, and a suite that
passed there could fail in CI.

## Test workflow

Run the targeted regression tests and the affected test modules while you edit. Finish all
fixes and documents, commit the batch, then run the full verification and review the committed
batch as a whole. You do not need the full verification after each fix or intermediate local
commit. Before every push, the full verification must pass on the committed state that you push.
After a correction that the final verification or review called for, repeat the affected tests,
and run the full verification again if the committed state changes.

## Test data

Unit tests use small fixtures and local Git repositories. The tracked dataset and the full
verification use the collection that [Dataset facts](dataset.md) names, with its source handover
and its evidence limits. Corrected heuristics may change the outputs. A test expectation needs
evidence of the behavior, not agreement with the outputs of an earlier release.

Use the complete handover to investigate cases that the small fixtures miss. Extract relevant
examples into focused regression fixtures, and keep the references to their source repository,
commit and documentation. Do not require every unit test to open the complete handover. macOS
and Linux use the same data and expectations.

Test observable results against expectations specified independently. Comparing two aliases of
the same function or constant cannot establish correctness. Historical batch counts do not test
the current batch builder. Prefer generated requests, written CSVs, preserved files and exit
codes. Reintroduce the defect to confirm that a regression test detects it. An existing fixture
may keep old data when that data isolates a useful case. Document the source of such a fixture,
and do not present it as the current collection.

## macOS

Install mise, Poppler and Node.js with Homebrew as `brew install mise poppler node`. Then install
the project interpreter, uv and the pinned git, rsync and 7-Zip, and install the development
dependencies into `.venv`:

```shell
mise trust
mise install
mise exec -- bash dev/setup.sh
```

Activate mise in your shell, for example with `eval "$(mise activate zsh)"` in `~/.zshrc`, so that
the commands of the documents that call `7zz`, `git`, `rsync` or `python3` without `mise exec --`
find the pinned versions first on the `PATH` and `python3` is the one of `.venv`. Point the
interpreter of your editor at `.venv/bin/python`. Homebrew installs no 7-Zip for the pipeline any
more.

`dev/setup.sh` installs the distributions of `locks/development.txt` and the editable package into
`.venv` with the commands that CI runs. It removes every distribution that the dependency lock does
not list and installs every listed one again from its locked file, which replaces a package that
you installed by hand at a locked version. It refuses to run when `.venv` of the checkout is not
the active virtual environment, when `.venv` runs another release of Python than the one that
`mise.toml` pins, and when git, `7zz`, rsync, `pdftotext` or uv is not on the `PATH`. mise
creates `.venv` once and keeps it when the pin changes, so after a new pin, remove `.venv` and run
`mise exec -- bash dev/setup.sh` again, which creates it from the new pin. The script then
extracts each current commit CSV that is missing or differs from its tracked archive, as after a
pull that changed the archive. It keeps a CSV that matches its archive and does not regenerate
outputs. It also extracts each file of the archives of the sampling frame and the labels that is
missing, and from labels that match their archive it writes the list of the sample,
`data/classification/repos_annotated_included.csv`, with `engineered-project-selection`. A
sampling frame or a file of the labels that differs from its archive stays, because the filter and
the labeling write newer files there, and the script names it and extracts no file of that
archive. The script does not check for `node`. Without `node`, the website suite skips the tests
that run the JavaScript of `_website/index.html`, which CI runs, and the full verification still
passes. In a checkout, an analysis script stops on an extracted commit CSV, sampling frame or
`repos_annotated.csv` that differs from the archive that git tracks beside it
([archives](archives.md)). Run selected tests while you edit, then commit and run the full
verification:

```shell
mise exec -- python3 -m unittest tests.test_paths tests.test_steps_commit_reclassification
mise exec -- bash dev/verify.sh /tmp/agentcontext-verification
```

The full verification passes only when every check passes and the working tree stays clean. It
compares the regenerated analysis outputs with the tracked macOS outputs.
`4_data-analysis/rq2/content_quality.py` reads the validated tree at
`data/collection/data/validated/`, of which git tracks only `repos.csv`, and only the validated tree
of a handover supplies its repositories ([handover working copy](#handover-working-copy)). In a
clone without it, the reproduction check skips the script and exits 1, and the verification exits
2.

## Linux (Ubuntu)

Use an unprivileged account for development and tests. Root bypasses file permissions, and the
permission tests would then test nothing. Install curl, git, Poppler and Node.js from Ubuntu, and
mise at the release that `deploy/install_tools.sh` installs into the images and CI runs
(`MISE_VERSION` in that script). Activate mise in your shell, so that the commands of the documents
that call `7zz`, `git`, `rsync` or `python3` without `mise exec --` find the pinned versions before
those of Ubuntu on the `PATH` and `python3` is the one of `.venv`. Run these commands from the
root of the checkout:

```shell
sudo apt-get install -y curl git poppler-utils nodejs
curl https://mise.run | MISE_VERSION="v$(sed -n 's/^MISE_VERSION=//p' deploy/install_tools.sh)" sh
echo 'eval "$(~/.local/bin/mise activate bash)"' >> ~/.bashrc
```

Open a new shell, then install all stage and development dependencies into `.venv`, as on macOS,
and run selected tests:

```shell
mise trust
mise install
mise exec -- bash dev/setup.sh
mise exec -- python3 -m unittest tests.test_paths tests.test_steps_commit_reclassification
```

For the full verification on Linux, write the regenerated analysis outputs outside the
checkout. `dev/verify.sh` does so on Linux, and the reproduction check then compares two runs with
each other, not with the tracked macOS outputs, because PDF bytes and the last digits of some
floating-point results can differ between Linux and macOS. Commit the change before you run the
verification:

```shell
mise exec -- bash dev/verify.sh /tmp/agentcontext-verification
```

## Linux x86-64 development container

The development image uses `linux/amd64`, the architecture of the NAS and of GitHub's
runners. Docker Desktop emulates x86-64 on an ARM Mac. Use its results to check
compatibility, not performance, because its timings do not represent native Linux
performance.

Build the image with the dependencies of every stage:

```shell
mise exec -- bash deploy/build_development_image.sh
```

The build sends only `locks/build.txt`, `locks/development.txt`, `mise.toml`,
`deploy/install_tools.sh` and the development Dockerfile to Docker. The image includes the
distributions of `locks/development.txt`, which pip installs into the interpreter of the base
image in hash-checking mode, the git, rsync and 7-Zip that `mise.toml` pins, and Poppler, curl and
Node.js, on the base image of the production image, Debian's slim image of the release of Python
that `mise.toml` pins. It has no `.venv` and no mise. It also has no `shasum`, so the tests of
`deploy/build_image.sh` over a stub Docker, `TestTheBuildScriptOverAStubDocker` in
`deploy/test_container.py`, skip in a container of it. Native macOS, native Ubuntu and CI run
them. You mount the source code and the data at runtime. For native development on Ubuntu, follow the Linux instructions above.

The image holds what these five files gave when it was built. In a container, the verification
compares the release of Python with `mise.toml` and the installed distributions with
`locks/development.txt`, and refuses to start when either differs. Nothing in a container
compares the git, rsync and 7-Zip of the image with the pins of `mise.toml`, or the image with
`deploy/install_tools.sh` and `deploy/Dockerfile.development`. After a change of
`locks/build.txt`, `locks/development.txt`, `mise.toml`,
`deploy/install_tools.sh` or `deploy/Dockerfile.development`, build the image again before you use
a container of it, with the command above or, on Apple silicon, with
`mise exec -- bash deploy/build_development_image.sh arm64`. A container of an earlier image runs
the commands of a working copy, which set `PYTHONPATH=/code` and check no environment, with other
distributions than the dependency locks, CI and the production image, and its results do not
cover those.

Use a disposable checkout to keep generated files apart from your working checkout. The clone
contains only committed changes, so commit before you create it.

```shell
container_work=$(mktemp -d)
git clone --no-hardlinks . "$container_work/checkout"
mkdir "$container_work/results"
docker run --rm -it --init --platform linux/amd64 \
  --user "$(id -u):$(id -g)" --env HOME=/tmp/development-home \
  --mount "type=bind,src=$container_work/checkout,dst=/workspace" \
  --mount "type=bind,src=$container_work/results,dst=/results" \
  agentcontext-development:latest
```

`--init` reaps the child processes that the process-control tests start. With the user mapping,
the tests run without root, which the permission tests require, and the generated files belong to
the host user. Inside the container, prepare the editable environment. The virtual environment
sees the distributions of the image through the system's site packages, and pip adds only the
editable package, built with the setuptools of the image. Run every suite and command below in
this virtual environment, the container's counterpart of `.venv`:

```shell
mkdir -p "$HOME"
python3 -m venv --system-site-packages /tmp/development-venv
source /tmp/development-venv/bin/activate
export PYTHONPATH=/workspace
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
```

For a focused smoke test, run:

```shell
python -m unittest tests.test_git_bounds tests.test_tools_verify \
  tests.test_steps_commit_reclassification \
  tests.test_paths.TestCopilotPathCase \
  tests.test_paths.TestCopiedFileSymlinkMarkers \
  tests.test_paths.TestDeclaredRootSkillCollection \
  tests.test_runner.TestPreChecks \
  deploy.test_container.TestTheMountsAreWritable \
  deploy.test_container.TestTheFlockProbe
```

To run the verification suite, extract the tracked commit CSVs, the sampling frame and the labels
first. The clone has no validated tree, so the reproduction check skips `content_quality.py` and
exits 1 ([macOS](#macos)):

```shell
bash dev/setup.sh --data-only
python -m agentcontext verify --analysis-output-dir /results/analysis \
  /results/verification.txt
```

The verification report and the generated analysis outputs remain in
`$container_work/results` after the container exits. The verification suite checks:

- every CI test module
- the reproduction of the analyses
- the regression outputs
- the output snapshot
- the unmatched paths
- the output cross check
- the licenses of the last sampling frame
- the stated statistics
- the status of the working tree

The same environment can run individual step commands and the pipeline runners that
[architecture.md](architecture.md) describes. Run them over a data root outside the checkout, as
the container on the NAS does. In the checkout, git tracks the archives of the sampling frame,
the labels and the commit CSVs. The filter, the labeling and the collection write newer files
beside them, and the analysis stops on such a file until you pack its archive again
([archives](archives.md#packing-the-archives-again)). Copy the data root of the checkout to
`/results/data`, and set `AGENTCONTEXT_DATA_ROOT` to the copy. The copy holds the caches that git
tracks, and the labeling reuses the cached label of each repository with an unchanged README,
description, model and classification parameters. Over an empty directory instead, the run starts
as the first Start on the NAS does, and the model labels the whole sampling frame. To run the
full pipeline, run these commands inside the container from `/workspace`:

```shell
cp -a /workspace/data /results/data
export AGENTCONTEXT_DATA_ROOT=/results/data
python run_pipeline.py --list
python run_pipeline.py --with-commit-reclassification --scratch-dir /results/clones
```

The runner skips the steps with matching completion records. Use `--from NAME` to run a stage
or a step again, together with every step that is computed from it. The full pipeline can
download data, clone repositories and call the classification model. It requires the inputs and
credentials that the stage READMEs describe. To run one stage instead, with the same variable:

```shell
(cd 1_sampling && python run_sampling.py)
(cd 2_classification && python run_classification.py)
(cd "$AGENTCONTEXT_DATA_ROOT/collection" && python -m agentcontext run --with-commit-reclassification)
(cd 4_data-analysis && python run_analysis.py)
```

Classification requires an OpenRouter API key for its default model, and the GitHub query of the sampling stage requires a
GitHub access token. Steps that fetch repositories or vendor pages need network access. The
stages read their inputs from the data root that `AGENTCONTEXT_DATA_ROOT` names and write their
outputs there, and `$container_work/results/data` keeps them after the container exits. Unset the
variable before you run the verification suite, which checks the data root of the checkout and
exits 1 while the variable is set. To process a complete collection, mount the working copy of
its handover and set `AGENTCONTEXT_DATA_ROOT` to its data root instead, as [Handover working
copy](#handover-working-copy) shows. Mount the credentials file and name it with
`--env AGENTCONTEXT_CREDENTIALS=/path/in/the/container`, or pass the variables with Docker
`--env NAME` when you create the container. The image contains no GitHub access tokens, no API
keys, no local mirrors and no handover from the NAS.

## Python environments

Every Python environment of the project installs its distributions from a dependency lock under
`locks/`, which lists every distribution with its exact version and the SHA-256 of each of its
files for macOS on arm64 and Linux on x86-64 and arm64 ([locks/0_README.md](../locks/0_README.md)).
The requirements files beside the scripts pin the direct dependencies, and `dev/lock.sh` writes the
dependency locks from them with the uv that `mise.toml` pins. Every install requires hashes and
refuses a distribution that its dependency lock does not list:

- On macOS and Linux, `dev/setup.sh` installs `locks/build.txt`, then `locks/development.txt`,
  which removes every other distribution and installs every listed one again, then the editable
  package into `.venv`. The installs read no configuration file of uv and none of the variables
  that make uv build a distribution from its source distribution instead of installing its wheel,
  install the package from a wheel rather than editable (`UV_NO_EDITABLE`), or constrain, override
  or exclude distributions of the dependency lock (`UV_CONSTRAINT`, `UV_OVERRIDE`, `UV_EXCLUDE`).
  They name the interpreter of `.venv` with `--python`, so that a `UV_PYTHON` or
  `UV_SYSTEM_PYTHON` of your shell cannot direct them into another environment, such as mise's
  shared installation of Python.
- CI runs the same three installs into the `.venv` that `jdx/mise-action` creates and activates
  from `mise.toml`, as `mise exec` does, so every suite runs with the distributions of a
  development machine. Only the sampling runner suite runs before the install, with `python3 -S`,
  which imports no site packages. Its step is the only one that sets `PYTHONPATH`, to the
  checkout. The full verification runs the suite the same way, in an empty virtual environment
  that it creates for the suite, and every other suite and check without `PYTHONPATH`.
- The production image installs `locks/pipeline.txt` and the development image
  `locks/development.txt` with pip into the interpreter of the base image. Neither image has a
  virtual environment or mise.

Every environment runs the release of Python that `mise.toml` pins: mise installs it on a
development machine and in CI, and both Dockerfiles name the base image of that release by digest.
`deploy/install_tools.sh` fails the build of either image when the interpreter of the base image is
another release, because Docker pulls the digest without reading the tag.
CI installs the release of mise that `deploy/install_tools.sh` installs into the images. To move to
a new release of Python, change `mise.toml` and the `FROM` line of both Dockerfiles in one commit,
with the digest that `docker buildx imagetools inspect python:<release>-slim` prints, then remove
`.venv`, run `mise exec -- bash dev/setup.sh` and build the development image again
([Linux x86-64 development container](#linux-x86-64-development-container)).

After you change a pin in a requirements file, write the dependency locks again and install the
development lock. Change the pin in every requirements file that names the distribution, because
`tests/test_tech_stack.py` requires one version per distribution:

```shell
mise exec -- bash dev/lock.sh
mise exec -- bash dev/setup.sh
```

Then build the development image again
([Linux x86-64 development container](#linux-x86-64-development-container)), because a container
of an earlier image holds the distributions of the earlier lock.
uv keeps every version of the existing dependency locks that the requirements files still allow.
`mise exec -- bash dev/lock.sh --upgrade-package NAME` moves a distribution that no requirements
file pins to its newest version, and `--upgrade` resolves every distribution again. Commit the
requirements files and the dependency locks together. `tests/test_tech_stack.py` fails when a
dependency lock lists a pin of its requirements files at another version or not at all. The full
verification refuses to start when the release of Python, the `python3` on the `PATH` or the
installed distributions differ from those of CI. It does not compare git, rsync or 7-Zip with the
pins of `mise.toml`, which `mise exec` puts first on the `PATH`.
A change of a version can change the bytes of the analysis outputs, which the verification
compares on macOS, so regenerate them as [Continuous improvement from
handovers](#continuous-improvement-from-handovers) describes.

## Credentials

Write `credentials.csv` at the repository root from `deploy/credentials.csv.example`, one
credential per row under the header `service,account,credential`. `docs/credentials.md` gives
the format. The file is gitignored and never committed. The runners, the GitHub query of the
sampling stage, the scripts of the classification stage, `snapshot validation` and
`measure sizes` read it after `GITHUB_TOKENS`, `GITHUB_TOKEN` and `OPENROUTER_API_KEY`, and
`OPENAI_API_KEY` for an OpenAI model that a command names, which take precedence. No test suite
reads it.

## Handover working copy

Keep the downloaded handover unchanged and extract a working copy. On Linux, extract into a
local directory with `7zz x -snld`. On macOS, extract and process the copy only inside a Linux
Docker volume, never on the host. In the review of the handover of 2026-10-06, the host lost
collected files in two ways. On the default APFS volume, which ignores case, two collected paths
that differ only in case became one, and 7-Zip reported success. On a case-sensitive volume, a
security scanner of the host later removed a collected file, and validation then failed with
"Operation not permitted". The downloaded handover stays on the host. List or test it there with
the pinned `7zz`, as `mise exec -- 7zz t <archive>`.

On Apple silicon, the x86-64 image runs emulated. In the review of the handover of 2026-10-06,
it reclassified the repositories far more slowly than an image built for arm64. Build the arm64
image with `bash deploy/build_development_image.sh arm64`, and use
`--platform linux/arm64` with `agentcontext-development:arm64` in the commands below.
`commit-attribution-validation` was killed with exit 137 in a Docker virtual machine with
less memory than it needs. Over the commit CSV of the October 2–3 collection, it needed
<!--s:commit_attribution_validation.peak_memory_gb-->13.6<!--/s--> GB ([the record of the build of the clean collection](validation/production-build-22fab80f.json)),
and the commit CSV of the development dataset is larger.
Give the virtual machine at least that much memory in the resource settings of Docker Desktop, or
run `python3 -m agentcontext commit-attribution-validation` natively over the extracted commit
CSV.

```shell
docker volume create agentcontext-handover-work
docker run --rm --init --platform linux/amd64 \
  --mount type=volume,src=agentcontext-handover-work,dst=/work \
  --mount type=bind,src=/absolute/path/to/downloads,dst=/input,readonly \
  agentcontext-development:latest \
  sh -c 'mkdir -p /work/handover && 7zz x -snld /input/agentcontext-handover-<time>-complete.7z -o/work/handover -y && chown -R 1000:1000 /work/handover && chmod -R u+w /work/handover'
```

Before you run the command, replace `/absolute/path/to/downloads` with the directory that contains
the handover and `agentcontext-handover-<time>-complete.7z` with its file name. The extraction
runs as root only to set the ownership, and processing and tests run as an unprivileged user.
`-snld` keeps the symlinks with a target outside their directory, such as a `.claude/commands`
entry that names `../skills/<name>/SKILL.md`. Without the switch, 7-Zip refuses them as dangerous
link paths and exits 2. 7-Zip also leaves a 0-byte file in
place of each refused link, and validation deletes these files as empty. Extract into an empty
directory, or overwrite with `-aoa`. `find -type l` then lists as many symlinks as the archive
contains. Compare the archive listing with the extracted files, because 7-Zip
reports success when it extracts two paths of the archive to one file. In a shell of the
development image with the two mounts of the extraction command, run:

```shell
7zz l -ba -slt /input/agentcontext-handover-<time>-complete.7z | sed -n 's/^Path = //p' | sort > /tmp/listed
(cd /work/handover && find . -mindepth 1 | sed 's|^\./||' | sort) > /tmp/extracted
comm -3 /tmp/listed /tmp/extracted
```

`comm` prints nothing when every listed path was extracted. Do not leave out a file only because
the host refuses it.

To process the handover of a stopped, failed or killed run, also extract the `-trees` archive that
contains its trees: the archive packed with it, or the earlier archive that the container's log
names when the trees had not changed. Also copy `collect_commits/`, `clone_logs/` and
`data/file_lists/` of the same collection from `data/collection/` in the project folder on the NAS
into the `data/collection/` of the working copy, because neither partial archive contains these
directories of evidence. See
[handover recovery](deploy.md#recovery-from-handover-and-storage-errors).

Mount the current checkout as code, the working volume as data, and the directory of the
downloaded handover, which `statistics --collection --archive` records by its SHA-256:

```shell
docker run --rm -it --init --platform linux/amd64 --user 1000:1000 \
  --mount "type=bind,src=$PWD,dst=/code,readonly" \
  --mount type=volume,src=agentcontext-handover-work,dst=/work \
  --mount type=bind,src=/absolute/path/to/downloads,dst=/input,readonly \
  --env PYTHONPATH=/code \
  --env AGENTCONTEXT_DATA_ROOT=/work/handover/data \
  --workdir /work/handover/data/collection \
  agentcontext-development:latest bash
```

The publication also reads `sampling/ghs_filtered.json` and `classification/repos_annotated.csv` in
the data root. The handover of a run of every stage contains both. A handover of a collection over
the inputs of March 2026 does not, and the tracked archives do not contain those inputs. They
contain the sampling frame and the labels of the run that started on 2026-10-23. To restore
`sampling/ghs_filtered.json` and `classification/repos_annotated.csv` from these archives in a
working copy that lacks them, run inside the container:

```shell
mkdir -p ../sampling ../classification
7zz x /code/data/sampling/ghs_filtered.json.7z -o../sampling -y
7zz x /code/data/classification/repos_annotated.7z -o../classification -y
python3 -m agentcontext engineered-project-selection
```

The last command writes `classification/repos_annotated_included.csv`, the list of the sample that
the collection collected. Run it again whenever you extract a newer `repos_annotated.7z` over an
earlier copy, in a working copy or in the checkout. Otherwise the list that it derived from the
earlier labels stays beside the new labels, and `engineered-project-selection --check` exits 2.
Then record the restored collection with
`python3 -m agentcontext run --mark-complete repository_cloning`. The runner records the
collection with the inputs that `collection_inputs` in `agentcontext/runner.py` lists: that list,
the collector's code, the version of git, the path heuristics and the import heuristics with the
code of the imports. Without the list, the command exits 1. After a later change to one of these
inputs other than the version of git, the next run runs the cloning step. `--mark-complete`
removes the records of the collections, and the step therefore begins a new collection, which
queries the head of every repository. It takes over only a repository with a line in
`finished_repositories.jsonl` that records `collected_under`, with a head that did not move, and
only if the change does not reach the repository. A handover from a collector without
`collected_under` has no such line, and the step would collect the whole sample again. A new pin
of git in `mise.toml` alone keeps the step with a `WARNING`. In a working copy that should stay
offline, record the step again after every change of one of these inputs.
When the change alters what the collector copies, record the step once `data/collected/` holds
the files that the change adds. Add a row for each fetched file to
`data/collection/fetched_files.csv`. `snapshot write` leaves the files of that list out of
`artifact_mtime_window`, because a fetched file has the time of the fetch rather than of the scan
([the snapshot record](checks/snapshot.md#input-provenance-and-verification)). Name in
`docs/dataset.md` each change that the tree predates, as
[Development dataset](../CLAUDE.md#development-dataset) describes.

Development on a Linux host can use the same layout, with `AGENTCONTEXT_DATA_ROOT` set to the
data root of the working copy. Keep the existing inputs that came with the dataset. Unset the
variable before you run the verification in the checkout, which refuses to start while it is set.

## Continuous improvement from handovers

1. Record the checksum of the handover, the collection dates, the code revision and the
   checksums of the heuristics. Keep the collected files, the file lists, the deletion records,
   the per-repository commit records and the run reports.
2. Run the steps after the collection on the working copy and inspect each exit status.
   `commit-reclassification` checks the provenance of the scope, the checksums, the recorded
   revisions and the coverage of the paths. It exits 0 with `complete: false` while the
   repositories with missing or invalid evidence stay within the ceiling that the cloning step
   accepts. It exits 2 above that ceiling, and 1 when an input is missing or the assembly fails.
   Read `complete`, not the exit status. Its JSON report,
   `output/commit-reclassification.json`, gives the outcome of every repository. Review the
   affected repositories there, and distinguish expected gaps in the input from processing
   defects before you continue. `complete` covers the repositories with a commit history to
   read, and `population_complete` also requires that no clone failed. Both stay false until the
   assembly succeeds.
3. Inspect the unmatched paths, the unmatched attributions, the mentions in commit messages, the
   deletion findings, the gaps in the metadata and the changed classifications. When the working
   copy contains `sampling/ghs_licenses.csv`, as the handover of a run of every stage does, run
   `python3 1_sampling/4_filter_ghs.py --check-licenses` from the checkout with
   `AGENTCONTEXT_DATA_ROOT` set to the data root of the working copy. Read the text of each
   license that it names, and record the license in `1_sampling/config/licenses.csv` as software
   or excluded, with the reason quoted from the license, the page and the date. Also read the
   license files of the examples of `Other`, the licenses that GitHub could not identify.
   Compare the evidence from configured and unconfigured repositories. Read the vendor
   documentation for how a tool loads its configuration, and use repository evidence for
   conventions that the vendor does not document.
4. For each confirmed defect, add a focused regression test that fails without the fix.
   Change the implementation or the heuristic, and update the check that missed the defect.
   Include negative examples so that a new pattern does not match unrelated files or
   messages. Record unresolved evidence instead of inventing an expected value.
5. Run the affected tests and the processing again. Inspect every output difference before
   you update the regression manifest, the development subsample, the dataset overview, the
   analysis outputs, the stated statistics and the release. Generated counts describe the
   result. They do not show whether a heuristic is correct.
6. Complete the changes and documents, commit, run the full verification and review the
   committed batch, then push. Repeat the full verification if the committed state changes.
   Follow this procedure for each new handover and each reviewed change of a heuristic.

Run these commands one at a time in the stage directory of the working copy,
`/work/handover/data/collection` in the container above, and inspect each result:

```shell
python3 -m agentcontext collected-file-cleanup
python3 -m agentcontext commit-reclassification
python3 -m agentcontext detection
python3 -m agentcontext file-history
python3 -m agentcontext import-detection
python3 -m agentcontext output-cross-check
python3 -m agentcontext unmatched paths --min-repos 2 --out output/unmatched-paths.csv
python3 -m agentcontext unmatched commits
python3 -m agentcontext deleted-files-report --out output/deletions.csv
python3 -m agentcontext snapshot write
python3 -m agentcontext statistics --collection --archive /input/agentcontext-handover-<time>-complete.7z
python3 -m agentcontext unmatched reduce
python3 -m agentcontext collected-files-archive
python3 -m agentcontext release
```

The command of a step is its key with hyphens, such as `repository-cloning`,
`collected-file-cleanup`, `detection`, `file-history` and `release`. Older commands have no
aliases. The pipeline execution state uses the step keys, which join words with underscores.

`subsample`, which chooses the development subsample, reads the collected and validated trees,
which only the working copy holds in full. It also reads the sizes in `output/repo-sizes.csv`,
which `measure sizes` writes outside a run. In the container, first copy the sizes of the
checkout over the older copy in the working copy with
`cp /code/data/collection/output/repo-sizes.csv output/`. Then check the subsample against the
list of the checkout with
`python3 -m agentcontext subsample --check --subsample /code/data/collection/dev_subsample.csv`.
When the check exits 2, inspect the change and write the list of the working copy with
`python3 -m agentcontext subsample`.

To bring a regeneration into the checkout, copy the files below that the regeneration wrote from
the `data/collection/` of the working copy into the `data/collection/` of the checkout:

- under `output/`: `repos.csv`, `excluded.csv`, the mechanism CSVs with `other.csv`,
  `planning-documents.csv`, `imports.csv`, `provenance.json`, `consistency-report.csv` and
  `snapshot.json`
- after a commit reclassification, also `output/ai_commits-repos.csv` and
  `output/commit-reclassification.json`
- `output/all_ai_commits.csv`, `output/other_ai_commits.csv`, `deletion.csv`,
  `file_history.csv` and `ai_authorship.csv`
- `collection_statistics.json`, the collection statistics file, which `statistics --collection
  --archive` wrote from the data root after `snapshot write`
  ([stated statistics](tools/statistics.md#the-statistics-of-a-collection))
- `dev_subsample.csv`, when you wrote it again
- after `unmatched reduce`, the archive that it wrote. Delete every other
  `output/all_ai_commits-unmatched-*.csv.7z` in the checkout, because only one may exist.

On macOS, copy them through a container, as the `rsync` command further below copies the
validated tree. Copy no other file of `output/`. The working copy holds the older copies of
`repo-sizes.csv`, `identity-*.csv` and `baseline.json` from the handover, which tools write
outside a run. It also holds older copies of the commit archives, which the commands below
rebuild. The checkout neither tracks nor ignores the other files that the regeneration writes,
such as `unmatched-paths.csv` and `all_ai_commits-validated.csv`, and the verification fails on
a tree with untracked files. Git ignores the three copied commit and deletion CSVs and tracks
their archives. Before you run the analysis, pack the archives of the two commit CSVs again in
the checkout with the commands of [archives](archives.md#packing-the-archives-again), because its
scripts stop on a commit CSV that differs from the tracked archive beside it. Pack the archive of
the deletion log again from the root of the checkout too. Remove the old archive first, because
`7zz a` adds to an existing archive:

```shell
(cd data/collection && rm -f deletion.csv.7z && 7zz a -mx=9 deletion.csv.7z deletion.csv)
```

The handover of a run of every stage also contains the sampling frame and the labels of that
run. Copy `sampling/ghs_filtered.json` from the data root of the working copy into the
`data/sampling/` of the checkout, and `classification/repos_annotated.csv`,
`repos_annotated.json` and `repos_excluded.csv` into its `data/classification/`. A handover of a
collection over the inputs of March 2026 contains none of them, and the checkout then keeps its
archives. The analyses and the stated statistics read the sampling frame and the labels from
`ghs_filtered.json.7z` and `repos_annotated.7z`. Without the copy, they describe the previous
run, and the archive check passes, because each extracted file matches its own archive. An
analysis that joins `repos.csv` to the sampling frame, such as
`4_data-analysis/rq2/language_tool_association.py`, then leaves out every repository that the
older sampling frame lacks. Pack both archives again with the commands of
[archives](archives.md#packing-the-archives-again), and derive the list of the sample from the
new labels with `python3 -m agentcontext engineered-project-selection`.

Then, from the root of the checkout and without `AGENTCONTEXT_DATA_ROOT`, update the regression
manifest, `output/baseline.json`, with `python3 -m agentcontext regressions check --write`. The
tracked analysis outputs are those of macOS, which the verification on macOS compares. Regenerate
them in the checkout on macOS with `mise exec -- python3 4_data-analysis/run_analysis.py`. Its
content quality step reads the validated tree of the checkout. First replace that tree with the
validated tree of the working copy through a container, because the container above mounts the
checkout read-only:

```shell
docker run --rm --init --platform linux/amd64 --user 1000:1000 \
  --mount type=volume,src=agentcontext-handover-work,dst=/work,readonly \
  --mount "type=bind,src=$PWD/data/collection/data/validated,dst=/validated" \
  agentcontext-development:latest \
  rsync -a --delete /work/handover/data/collection/data/validated/ /validated/
```

Then update the stated statistics with `python3 -m agentcontext statistics`.

`file-history` can fetch a missing file history, which requires network access. It does not
rebuild a missing commit log, which only a new collection supplies. Use the working copy to
review the current outcomes and develop fixes, and keep the downloaded handover unchanged.

## Collection validation

Local Git mirrors provide fixed repository revisions for [the subsample run](tools/mirror_run.md),
which covers stop and resume, an injected history failure, the incomplete commit reclassification
that the failure causes and the Git columns that the file history step then fills from the full
mirror. [The build records](validation/README.md) give the subsample run and the full
verification of each image that ran the development dataset or the October 5–6 collection. The
tracked outputs are the handover that [dataset.md](dataset.md) names, regenerated with the current
code.

When the next collection finishes, inspect its handover, its coverage and its failures, and
validate the evidence that it kept. Then regenerate and review the outputs in the order above.
Each collection has its own dates and provenance. After a change to the collection code, the
heuristics or the inputs, run the pre-deployment checks that the change affects, and verify a new
image and bundle before the deployment. [Deployment](deploy.md) describes the builds, the mounts
and how to start the container on the NAS.

## Verification results

`dev/verify.sh [report-dir]` writes `verify-<commit>.txt` and `verify-<commit>.log` into the
report directory, by default `$TMPDIR` or `/tmp`, and exits with the exit status of the
verification command. It removes the report of an earlier verification of the same commit before
the run and prints the log when the run writes no report. It leaves generated differences and
other edits in the working tree for you to inspect. The command exits 0 only when all checks pass
and the working tree is clean. It runs the `python3` that comes first on the `PATH`, which
`mise exec` makes the one of `.venv`.

While `AGENTCONTEXT_DATA_ROOT` is set, the full verification logs only the line that names the
variable and exits 1 before any suite. Otherwise it first compares the running environment with
the one of CI, and it logs one line for each difference and exits 1 before any suite when the
interpreter is another release than the pin, when the `python3` on the `PATH` is the interpreter
of another environment, when the distributions differ from `locks/development.txt`, when
`agentcontext` is not installed editable from the checkout, or when a file of site-packages
differs from the RECORD of its distribution or belongs to none ([verification
suite](tools/verify.md)). Without a difference, it runs every suite that the CI workflow lists,
the consistency checks of the tracked outputs and the reproduction check over the analysis
scripts, each with the interpreter that runs the verification and without `PYTHONPATH`, except
the sampling runner suite, which runs in an empty virtual environment with the checkout on
`PYTHONPATH`, as in CI. The analysis on macOS, Ubuntu and Linux x86-64 runs with the distributions
of `locks/development.txt`. The dataset tables and figures are regenerated from each release. The
processing of a complete handover, the subsample run and the checks of the production build run
separately. Keep their results beside the verification report before you start the full
collection.

## Replication packages

Make the four 7z archives of a replication package, one per stage, at the repository root with
the commands below. Git does not track them. Each archive contains the code directory and the data
directory of a stage:

```bash
7zz a 1_sampling.7z 1_sampling data/sampling -xr'!.DS_Store' -xr'!__pycache__' -xr'!.pytest_cache' -x'!1_sampling/.claude'
7zz a 2_classification.7z 2_classification data/classification -xr'!.DS_Store' -xr'!__pycache__' -xr'!.pytest_cache' -x'!2_classification/.claude'
7zz a 3_data-collection.7z data/collection -xr'!.DS_Store' -xr'!__pycache__' -xr'!.pytest_cache' -x'!data/collection/.claude'
7zz a 4_data-analysis.7z 4_data-analysis data/analysis -xr'!.DS_Store' -xr'!__pycache__' -xr'!.pytest_cache' -x'!4_data-analysis/.claude'
```

The code of the collection stage is the package, and its archive contains only its data
directory. Every script of the other stages imports `agentcontext.logs`, and the runner of each
stage imports `agentcontext.runner`. A replication package of any stage is therefore its archive
plus `agentcontext/` and `heuristics/`, and for the collection stage also `tests/`, `docs/` and
`deploy/`.

Each command limits the `.claude` exclusion to its first directory instead of excluding the name
at every depth. `-xr'!.claude'` matches the name at any depth, and `data/validated/`
contains collected `.claude` directories that a recursive exclusion would leave out of the stage 3
archive. `release/repos_data.7z`, the published archive of
collected files that `agentcontext/steps/collected_files_archive.py` writes, does not use these
commands.

## Codex and Claude Code

`CLAUDE.md` contains the shared project instructions. Claude Code loads it directly.
`AGENTS.md` tells Codex to read it. The initial instructions of Codex stay small, and `AGENTS.md`
needs no copy of the instructions in `CLAUDE.md`. See the [Codex instruction documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

Claude Code supports `AGENTS.md` from v2.1.277 and prefers an existing `CLAUDE.md`.
The two files also support older Claude Code sessions. See [Claude Code
project memory](https://code.claude.com/docs/en/memory#when-claude-code-reads-agentsmd).
