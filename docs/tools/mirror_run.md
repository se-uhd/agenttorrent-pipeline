# The subsample run

Local Git mirrors are bare copies of repositories, used as fixed test inputs. Because the
collector clones from them instead of from GitHub, repeated runs use the same repository
revisions. A subsample run tests the collector before a new full collection.

`python -m agentcontext mirror-run` runs the collection and the publication over local Git
mirrors in a disposable checkout. It makes the history fetch of one repository fail and stops the
first collection with SIGTERM after `--kill-after` seconds. The cloning step must then stop on its
own with exit 143 within the grace period, which is the bound of the step's own stop plus five
seconds, and a repository must have finished before the kill. The resumed collection has to take over each finished repository
without collecting it again. The tool then runs the remaining steps and compares the outputs with
a reference run. Run `python -m agentcontext mirror-run --help` for the required paths and bounds.

For `--history-fails`, choose a repository with copied configuration artifacts. One failed
repository is within the cloning step's ceiling, and the tool therefore requires
`commit-reclassification` to exit 0, as the step does in the container. The step has to record
`complete: false`, with exactly that repository's commit history as incomplete. The tool then runs
the remaining steps once, in the runner's order, through the publication, and checks that the file
history step fills the repository's Git columns from the full mirror. The tool collects nothing
again after the reclassification, because the runner does not. The collection summary runs
`--without-fetched-files`: A checkout tracks `data/collection/fetched_files.csv` when files were
fetched into the development dataset after its collection, while this run collects its own tree
and fetches nothing into it ([snapshot](../checks/snapshot.md)).

The publication reads labels and a sampling frame, which this run does not produce. Before the
steps after the reclassification, the tool therefore extracts these files of the development
dataset ([dataset](../dataset.md)) from their tracked archives in the checkout
(`INPUT_ARCHIVES`): `repos_annotated.csv` and `repos_annotated.json` with the labels,
`repos_excluded.csv` with the repositories in the sampling frame that are not in
`repos_annotated.csv`, the copies that the classification labeled `true` included, and
`ghs_filtered.json` with the sampling frame. It extracts each file only when the file is not yet
beside its archive.

A repository of the run list without a mirror cannot be cloned, and the run list includes the
clone outcomes of the subsample this way, such as a repository that is gone from GitHub. The
outcome of such a repository has to be `unavailable`. Population coverage is complete only when
the list contains no such repository, as for a collection with clone failures. The
reclassification report has to cover exactly the repositories of the run list, and its counts
have to be those that `commit_reclassification.outcome_counts` gives for its repository outcomes.
Any other missing or invalid evidence fails the run.

The tool copies the coverage report into the log directory as
`reclassification-incomplete.json`, and writes the version of git that the run used there as
`git-version.txt`. Without `--logs`, the log directory is `<checkout>-logs-<commit>` beside the
checkout, because the next run's `git clean` removes everything in the checkout.

Set up the host as [development.md](../development.md) describes for macOS or Ubuntu, with the
pinned interpreter, the requirements and the editable package. The checkout must be a disposable
local clone with this repository as its origin, because the tool resets it to the committed
revision and removes untracked files. Keep the mirrors, the references and the expected findings
outside that checkout. Copy nothing from the checkout's data root into this repository, because
its outputs describe the run list, and the tracked outputs and caches come from the latest
complete handover. For example, from the repository root:

```shell
mirror_work=$(mktemp -d)
git clone --no-hardlinks . "$mirror_work/checkout"
python3 -m agentcontext mirror-run \
  --checkout "$mirror_work/checkout" \
  --repo-csv /absolute/path/to/run_list.csv \
  --mirrors /absolute/path/to/mirrors \
  --history-fails owner/repository \
  --reference /absolute/path/to/reference/data/collection \
  --logs "$mirror_work/logs"
```

On macOS, prefix the Python command with `mise exec --`. Lay out the mirrors as
`<owner>/<repo>.git`. The run list must have at most <!--s:parameters.agentcontext.tools.mirror_run.MAX_ROWS-->200<!--/s--> rows, and the reference must contain
`output/`. Make each mirror with
`git clone --mirror https://github.com/<owner>/<repo>.git <mirrors>/<owner>/<repo>.git`, which
copies every ref with its commit history. The tool removes `GITHUB_TOKEN` and `GH_TOKEN` from the
environment of the steps and rewrites the clone URLs to point to the local mirrors.
`--min-free-gb` defaults to the collector's production minimum. Set a lower value only after you
check the storage that the repositories of the run list need.

The tool runs every step with the checkout first on `PYTHONPATH` and with
`AGENTCONTEXT_DATA_ROOT` set to the checkout's `data/`. An editable install binds
`import agentcontext` to the checkout that it was made from. Without these settings, the steps
would run that checkout's code and write into its data root. Before the first step, the tool
imports the package in that environment from the checkout's `data/collection/`. It refuses to run
unless the import loads the checkout's own package and stage directory.

In a subsample run, git ignores `--filter=blob:none` for a `file://` URL unless the mirror sets
`uploadpack.allowFilter`. Every clone and history fetch therefore transfers the file contents,
and neither the fetch of blobs by `git log --follow` nor the cloning step's retry without the
filter runs. The run sends no GitHub access token and meets no rate limit of GitHub. Each mirror
keeps the head and the default branch that `git clone --mirror` gave it. To run at the heads that a
handover collected, move the default branch of each mirror to the head that
`output/ai_commits-repos.csv` records for its repository, with
`git -C <mirror> update-ref refs/heads/<branch> <head>`. No head moves between two runs. A run over so few repositories shows neither the time nor the storage that the full sample
needs. The sampling, classification and analysis stages do not run.

The comparison ignores only `scanned_at`, which records the time of the new run, and the keys of
the JSON records that change with it:

- `scanned_at_window`
- `artifact_mtime_window`, the window of the times at which the collected files were written
- `collection_sha256`, the digest of the collected list, which has `scanned_at` in its rows

A run that reproduces the reference therefore exits 0. The comparison reads the list of mechanism
CSVs from `heuristics.MECHANISM_CSVS`. It covers both `output/all_ai_commits.csv` and
`output/other_ai_commits.csv`, including `matched_heuristics`, and the files that the publication
builds under `release/`.

Take the reference from a subsample run at the commit before the changes under review, over the
same mirrors and with the same git, so that each difference that the comparison lists comes from
those changes. New mirrors need a new reference run. Before you replace a reference, attribute
every added row, removed row and changed value to the code or heuristic changes under review.

The run uses the git on the `PATH`, and the history columns depend on its version
([git.md](../git.md)). Run with the git that made the reference, and when you replace a reference,
keep the run's `git-version.txt` beside it. `mise exec` puts the git that `mise.toml` pins first
on the `PATH`, the git of the production image.

`--expected-findings` names a file of the differences that you have already assessed, one per
line. The run passes when the comparison finds exactly these differences. The tool logs each
finding that the file does not list and each listed finding that the comparison did not find, and
it does not accept a changed output. See [the comparison documentation](../checks/regressions.md)
for the row keys and exit codes, and the
[deployment instructions](../deploy.md#pre-deployment-checks) for the required pre-deployment
runs.

Record the repository list, the mirror sources and revisions, the code revision, the heuristic
checksums, the coverage report and the reviewed output differences in `docs/validation/`, where
the record of a production build, such as `production-build-e99d5614.json`, keeps the result of
its subsample run under `subsample_run`. When you test a changed commit heuristic, also run
`commit-reclassification` without Git and network access, and compare the commit CSVs and the AI
columns of `context-files.csv` with the commits in the mirrors.
