# Git

`agentcontext/git/` contains the code through which the steps use git: how the GitHub access token reaches a clone in `auth.py`, the bounds on a command in `bounds.py`, the clone with its retries in `clone.py`, and the history fetch with the per-path queries in `history.py`.

## Bounds on one git command

The cloning step and the file history step clone a repository to read the history of its
configuration artifacts. `bounds.py` bounds the time and the size of such a clone and ends a
transfer that stalls. The cloning step sets the size bound of its own clone with `--max-clone-mb`
and the time bound with `--clone-timeout`. It records a clone over the size bound as
`size_bound_exceeded` in `clone_failed.csv`, and only a run with a larger `--max-clone-mb` clones
the repository again. It retries a clone that the time bound stopped as it retries a network
failure, until `clone.TIMEOUT_TRIES` attempts have ended at the time bound. It then records the
clone as `time_bound_exceeded`, or the repository keeps its earlier collection. A run with a larger
`--clone-timeout` or the next collection clones the repository again. Within the same collection,
no further run clones it under the same time bound, because every further attempt would hold a
worker for the whole bound again ([the cloning step](steps/repository_cloning.md)). The file
history step makes no further attempt after one that the time bound or the size bound stopped.

**Stalled transfers.** Every git command runs with `http.lowSpeedLimit=1` and `http.lowSpeedTime`
(`bounds.LOW_SPEED_SECONDS`, <!--s:parameters.agentcontext.git.bounds.LOW_SPEED_SECONDS-->600<!--/s--> seconds) and over HTTP/1.1 (`bounds.git_network_options`). A transfer
that moves less than one byte per second for that long, such as over a connection that stays
open after the network dropped it, ends as a failure of the network. Without these options, a
history fetch that stalls would wait for its time bound on both attempts and be recorded as too
large for the time bound, and no later run of the collection would fetch the repository again.
GitHub itself sends nothing while it prepares a large pack. The bound gives that preparation ten
minutes without data and lies well below the time bound, so that a fetch during which GitHub
prepares a pack does not end at the bound in every attempt of every run, and a transfer over a
connection that the network dropped still ends as a failure of the network. The bound limits a
silence, not a transfer: a fetch that keeps moving data runs until its time bound.

**Size.** The caller of every clone names the directory that the clone fills, and a watcher
measures that directory at an interval. When the directory grows past `--max-clone-mb` (<!--s:parameters.agentcontext.steps.repository_cloning.max_clone_mb_default-->32,768<!--/s--> by default), the
watcher stops the command. The step records the repository as stopped by the size bound, and the
files of the repository get no history. `--filter=blob:none` leaves the historical file contents
on the server and saves most of the transfer. A repository with a very large commit graph still
transfers gigabytes. A time bound cannot limit that transfer, because the timeout has to be long
enough for an ordinary repository on a slow uplink.

**Submodules.** `git submodule update --init --recursive` follows every submodule of every
submodule, from servers unrelated to the repository being measured. Only a submodule that could
contain one of the paths that the step looks up can change a measurement. `wanted_submodules`
selects those submodules, and no other submodule is fetched. Only the file history step initializes
submodules, through `measure_repo`, and only so that a file inside a submodule gets its history. The
submodule update rewrites GitHub's SSH and git URLs to https URLs, which need no SSH key, with one
`insteadOf` value per URL prefix among its `-c` options (`submodule_url_options`). git passes `-c`
options on to the `git clone` of each submodule. That clone does not read the configuration of the
clone that contains it, and a value set in that configuration would not reach it.

**Children.** Every command runs in its own process group and is stopped by group. Stopping only
the clone would leave its submodule clones downloading into the directory that the watcher had
just measured over the size bound.

The id of the group is the pid of its leader. The kill goes to the group even when the leader has
already exited and left children running. An exited leader keeps its pid until this process reaps
it, and until then the pid cannot belong to another process. Calling `poll()` before the kill
would reap such a leader and then leave its group alone. A child that still had the pipes open
would keep running, and `run_bounded` would wait twice `REAP_SECONDS` past the time bound. A leader
that this process has reaped is left alone, because its pid, and the id of the group once the
group is empty, may belong to another process by then. `run_bounded` reaps only after the pipes
have closed, and a group that it leaves alone for this reason has none of the pipes open.

In the file history step, a repository with a clone or a history fetch that the size bound or the
timeout stopped gets the outcome `bound_exceeded`. Its rows are kept, and nothing is deleted,
because the pipeline could not measure the repository, and a failed measurement differs from
finding nothing there. The retry rounds of the step do not measure such a repository again,
because an attempt under the same bounds would be stopped again. A later run fills in the
measurements, and `--max-clone-mb 0` turns the size bound off for a run that accepts the cost.

`run_bounded` returns captured output as text and decodes anything that is not UTF-8 with a
replacement character. Strict decoding inside `communicate()` would raise `UnicodeDecodeError` at
the first commit message in another encoding that a `git log` returned. No caller would count that
error as a failure: The path would get no history, and the row would read as complete. git 2.55
re-encodes such a message on output, and older versions pass the bytes through.
`test_output_that_is_not_utf8_is_read_rather_than_dropped` in `tests/test_git_history.py`
requests Latin-1 output from git, which every version honors.

A caller can also pass an open file as `stdout_file`, which the cloning step does to keep the
`git log` of a repository out of memory, and an open file as `stdin_file`, which the command
reads as its standard input. The cloning step passes the commits of every ref to
`git log --stdin` that way, because on the command line the commits of tens of thousands of tags
would exceed the system's limit on the size of the arguments.

## The git client

`history.py` contains the per-path history queries. `GitFileMetadata` runs every command through
`bounds.run_bounded`, which gives each command a time bound and its own process group. A size
bound applies where the caller names the directory that a command fills: the clone, the history
fetch and the submodule update. The per-path queries and the other commands in a clone, such as
the checkout and the root commit query, run under no size bound. They run under
`GIT_TIMEOUT_SECONDS` (<!--s:parameters.agentcontext.git.history.GIT_TIMEOUT_SECONDS.hours:words-->one<!--/s--> hour), whatever `--clone-timeout` the cloning step was given,
although `git log --follow` on a blobless clone fetches blobs into the clone
([the cloning step](steps/repository_cloning.md)).

The GitHub access token reaches git through the credential helper of `git.auth`, not through the
clone URL. git runs the helper only after GitHub responds with 401, as GitHub does for a repository
that is not public. The exception is the head query of the cloning step, which passes
`auth.PROACTIVE_AUTH` so that its first request carries the GitHub access token ([the cloning
step](steps/repository_cloning.md)). The helper is a script in the temporary directory. The script
contains no GitHub access token, and `auth.py` removes it at exit. A process that a stop ends with
`os._exit`, such as a collection worker, the parent process of the cloning step or the file history
step, removes the helper first, because `os._exit` runs no `atexit` handler.

`read_artifact_history` reads the history of one path from a checkout. `measure_artifact`
derives the <!--s:parameters.agentcontext.heuristics.HISTORY_COLUMNS:words-->four<!--/s--> git columns from that history, and `measure_repo` runs the whole sequence
for a caller with no clone yet.

Reading the history of a path takes <!--s:parameters.agentcontext.git.history.PATH_QUERIES:words-->three<!--/s--> `git log` queries over the path, each parsed into one record per
commit with the author, the committer, the dates and the message, oldest first:

- a query with `--follow`, up to the checkout, with the creation commit as its first record,
  unless the path was deleted and added again
- a query without `--follow`, up to the checkout, with the last commit as its last record
- a query with `--all`, over every ref of the clone

The queries run over an empty working tree (`git --work-tree=<empty directory>`). They compare
trees, and at a gitlink git reads the `.gitmodules` of the working tree. A `.gitmodules` there
that git cannot parse, such as one with committed merge-conflict markers, would end the query with
exit 128 and leave the history of the file unread in every run. Over an empty working tree, git
reads the file from the index, where a parse error yields only a message. The queries read no
other file of the working tree, because their format prints the identities as recorded, without
the mailmap.

Every clone, in the cloning step and in the file history step alike, is `--single-branch`. Its
refs are one branch and the tags that point into the history of the branch, and the queries see
no other branch. `#commits` is the number of distinct commits that the <!--s:parameters.agentcontext.git.history.PATH_QUERIES:words-->three<!--/s--> queries return,
that is, every commit of that branch that touched the file under any of its names, the same set
that the AI columns count. A commit on another branch is not counted. The pipeline keeps all <!--s:parameters.agentcontext.git.history.PATH_QUERIES:words-->three<!--/s-->
queries because the columns end at different commits: `--all` reaches the tip of the branch,
while a commit made after the collected commit must not become `last_commit_sha`. In the cloning
step, the tip is the collected commit unless the branch moved before the history fetch, and
`#commits` counts up to the tip. The file history step clones the branch again later and checks
out the collected commit. There, the commits that the branch gained in between count toward
`#commits` and toward no other column. `history_from` in `history.py` and `ai_authorship_from` in
`agentcontext/commits.py` derive the <!--s:parameters.agentcontext.heuristics.HISTORY_COLUMNS:words-->four<!--/s--> git
columns and the <!--s:parameters.agentcontext.heuristics.AI_COLUMNS:words-->four<!--/s--> AI columns of `context-files.csv` from the results of these queries.
`history_from` converts the committer date of the creation commit, `%cI`, to UTC, and keeps git's
text where the date does not parse ([caches](caches.md#file-history)).

The `--follow` query lists the newest commit first, and `path_commits` reverses the result. The
query leaves out `--reverse`, because `git log --follow --reverse` over a renamed file lists the
rename commit and nothing else. Until AgentTorrent 1.0.0, the query included it. `created_at` and
`first_commit_sha` then named the rename, and the AI columns missed the commits under the old name.
The published values of March 2026 were measured that way and still name the rename. A collection
from AgentTorrent 1.0.0 on reads the commit that created the file under whatever name it had.
`--follow` also follows a copy, to its source, a file of similar content that the commit kept. git
has no flag that follows renames alone. The query therefore includes `--name-status`, and
`renames_only` stops at the first commit with the status of a copy. An `AGENTS.md` written as a
copy of the `CLAUDE.md` beside it was created by that commit, not by the commit that created the
`CLAUDE.md`. `last_commit_sha` is the last commit under the current name.

The follow query can leave out the earlier history of a path that one commit deleted and a later
commit added again. The commit that adds the path again often gives it text from another file that
the same commit changes, such as text that it moves out of a `CLAUDE.md`. git then lists the path as
a copy, and the follow query stops at that commit. The exact query, the query without `--follow`,
lists the commits of the path before the deletion, and `#commits` and the AI columns count them.
`PathCommits.first` therefore takes the first record of the exact query where the follow query
did not list that commit and its author date is earlier than the date of the first followed record.
`created_at`, `first_commit_sha` and the creation commit of the AI columns then name the commit that
first added the path, and they describe the commits that the counts include. The exact query lists
only the commits of the path under its current name, and the history of another file stays out.
Where the follow query lists the first record of the exact query, as for a renamed file, its own
first record is the older one, whatever the author dates. In angular-eslint/angular-eslint,
`AGENTS.md` was added on 2025-05-24, deleted, and added again on 2026-10-02 in a commit that moved
the text out of `CLAUDE.md`, and its `created_at` is 2025-05-24.

Until 2026-10-03, `#commits` counted the `--all` query alone, under the current name only. A
renamed file then had a creation date from before the rename and a count from after it, and it
could count more AI commits than commits.

`--follow` lists nothing, at exit 0, for a path that a merge added and that neither parent
contained. `created_at` and `first_commit_sha` would then come back blank on a row that the step
counts as measured. For such a path, `PathCommits.first` falls back to the exact query, which
crosses no rename. The NAS run of 2026-09-16 wrote such rows.

The output of `--follow` depends on the git version that runs it. git 2.56 changed how it
follows a path through a non-linear history in which the path was renamed. Over `AGENTS.md` of
`dotnet/sdk` at the collected commit, git 2.56.0 lists fewer commits than git 2.54.0, which also
lists the rename and the history of `.github/copilot-instructions.md` before it.
`created_at`, `#commits` and `first_commit_sha` therefore change with the git version. git 2.55.0,
which `mise.toml` pins for the images and the development machines and which
`deploy/install_tools.sh` installs in the images, lists the same commits there as git 2.54.0 and
as Debian's git 2.47.3, which the images ran before.
`run_logs/machine.json` records the git version of a run. The cloning step records the version in
the `collected_under` of each repository, and a later run under another git version collects the
repository again rather than taking it over. The runner runs the step when the version changed,
as it does for a changed collector.

`clone.git_version` never returns an empty version. `fork()` can fail for a moment on the NAS, with
BlockingIOError, when the container is short of processes or memory. The function therefore runs
`git --version` up to
<!--s:parameters.agentcontext.git.clone.GIT_VERSION_TRIES:words-->five<!--/s--> times,
<!--s:parameters.agentcontext.git.clone.GIT_VERSION_WAIT-->10<!--/s--> s apart, and then raises
`GitVersionUnavailable`. The cloning step then exits 1 before it opens a collection, and the runner
stops the run before it decides whether the step is complete. An empty version would differ from the
version that every line of `finished_repositories.jsonl` records. The step would collect every
repository again, including the repositories of the collection in progress, and record the empty
version in every new line. The next run would then collect every repository again too.

A subsample run compared against a reference has to use the git version of the reference, and
`docs/tools/mirror_run.md` explains how to choose it.

The queries of the history of a path use NUL-delimited fields, and a record or field separator in
a commit message cannot create a false commit or corrupt `path_commits` in the per-repository
commit record. The name-status output follows each complete record, and following renames still
stops at a copy. The parser still reads test fixtures in the earlier RS/US format. The regression
test of the collection checks that control characters in messages are preserved, and it
validates the offline reclassification against the path commits from these queries.

A path below a directory symlink is queried at the committed target path inside the checkout. A
path that ends in a symlink to a file keeps the history of the link itself, and a path with a
target outside the checkout gets no history. The collected path stays the cache key, and the entry
of the path in `path_commits` adds `source_path` when the queried target differs. Querying the
target avoids the empty histories that the expanded directory links produced in the September tree
of `daangn/seed-design`. Tests over local Git repositories verify the creation and the commit count
of a target, the history of a link to a file, and the rejection of targets outside the checkout.

Every query method returns a falsy default when its command raises, and downstream a `git log`
that timed out looks like a path with no history. `_failures` distinguishes the two
cases: <!--s:parameters.agentcontext.git.history._ZERO_FAILURES:words-->two<!--/s--> counters increase when a command raises, and a caller compares them before
and after a measurement.

`measure_repo` clones with <!--s:parameters.agentcontext.git.history.CLONE_RETRIES:words-->three<!--/s--> attempts, <!--s:parameters.agentcontext.git.history.RETRY_BACKOFF-->10<!--/s--> s and <!--s:parameters.agentcontext.git.history.RETRY_BACKOFF.second-->20<!--/s--> s apart. After the last failed attempt, `_clone_outcome`
reads git's message to decide whether the repository is gone. There is no wait after the last
attempt, which would only delay every repository that does not clone. An attempt
that the time bound or the size bound stopped is the last one (`last_clone_bound`), because the
next attempt would run under the same bounds. It would hold the worker for the whole time bound
again, or fetch the same bytes again, on every run of the file history step. A failure of any
other cause is retried.

`history.py` is a module of its own rather than part of the file history step, because the
cloning step and the file history step both clone the same repositories, and the clone is the
expensive part.

Integration tests use local remotes to check that `clone.py` returns a permanent failure for a missing repository without a retry. A test over a parent repository with two submodules verifies that the metadata lookup initializes only the submodule that contains the requested path. Another test fetches a submodule that `.gitmodules` names by an SSH URL of GitHub without running SSH.

## Committed file inventory

`clone.tree_listing(repo_dir)` runs `git -C <repo_dir> ls-tree -r -z HEAD` and
returns its output as bytes. A nonzero exit raises `subprocess.CalledProcessError`
with the command, the exit code and stderr. When the operating system cannot start Git, the
function raises `OSError`. These failures are distinct from a successful empty listing.

When the listing fails, the cloning step writes the error details, with credentials redacted, into
its warning and into the clone log of the repository. It continues to copy the configuration, but
it removes any outdated file list and writes no submodule rows for that repository. The step does
not run the listing for a repository that the snapshot queries showed to be empty. Such a
repository has no HEAD, on which `ls-tree` would fail, and the step writes its file list empty
instead. `submodule_rows` returns an empty list when it runs the listing itself and the listing
fails. [The collection](steps/repository_cloning.md) describes the paths of the file lists and
the handling of an incomplete inventory.

## History-query and checkout warnings

The `history` component logs the Git operations of both the cloning step and the
file history step. After a timeout, the size bound or a failed Git command, the measurement has
failed. It does not show that a path has zero commits. Read the error line that the `history`
component logged before the warning, in the log of the step, `run_logs/<step>.log`, and the summary
at the end of that log. A history failure with the cause `exit`, which the warning gives as "A Git
command failed", means that a Git operation failed. It does not establish a network cause.

A new measurement requires the complete commit history and a verified checkout of the full
collected `head_sha`. When resolving the revision, the checkout or the verification of HEAD
fails, the result is `revision_unavailable`. The step then logs a warning with the repository, the
requested commit and the statement that it made no new measurement of the file history and the AI
authorship. The Git error logged before the warning keeps the command, the exit code and the
redacted stderr. A missing or invalid commit hash gets a warning of its own. The step does not
fall back to dates or branch tips.

When the check for a full history fails, the file history step returns `git_unavailable` before
it measures any path, or `bound_exceeded` when the time bound or the size bound stopped the
history fetch. Only the output `false` of `git rev-parse --is-shallow-repository` shows that a
repository is not shallow, and missing or unexpected output counts as unavailable evidence. A
requested path in a submodule also requires the full history, at the commit that the parent
repository pins.

## Collection snapshot queries

`clone.read_snapshot_metadata` reads the branch, the HEAD hash and the committer date before the
configuration is copied. A failed query on a repository with commits raises an error with the
command, the exit code and the redacted Git error. The cloning step then records
`collector_error` and writes no history measurements, and a resumed run retries the repository.
When the metadata is absent, the function treats the repository as empty only when
`git rev-list --all --count` succeeds and prints zero. A failed count query cannot establish that.

`git log -1 --format=%cI HEAD --` and `git rev-list --max-parents=0 HEAD --` take `HEAD` as a
revision and paths after it, and they end their revisions with `--`. Without `--`, a repository
that tracks a file named `HEAD` at its root makes the command ambiguous between the commit and the
file, and git exits 128. protoman/rockbot is such a repository. `rev-parse` and `ls-tree` read
`HEAD` as a revision also in such a repository, and need no `--`. `tests.test_git_clone.TestEveryRevisionEndsBeforePaths` checks
every such command that the package builds, so that a new command cannot leave out the `--`.
