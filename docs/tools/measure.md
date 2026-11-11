# Repository size measurements

`python -m agentcontext measure sizes` queries GitHub for the packed size of the repositories in
a list of the collection, `data/collected/repos.csv` unless `--input` names another list. It
queries each repository that `snapshot.in_scope` selects: every repository with a directory in
the collected tree or a flag that is TRUE, and every row without a `head_sha`. It writes
`output/repo-sizes.csv`, with `repo_name` and `size_kb`, and logs nearest-rank percentiles. The
subsample selection uses these sizes to limit the size of the local Git copies. The command exits
1 when it finds no GitHub access token or reads no size. It replaces the CSV in one step, so that
the previous CSV stays when writing fails, and the command then exits 1.

```shell
python -m agentcontext measure sizes
```

The command rotates its API requests through the GitHub access tokens of `GITHUB_TOKENS`,
`GITHUB_TOKEN` and the credentials file that `--credentials` names. GitHub's packed size of the full
history differs from the size of a shallow clone with its checkout. For the actual duration of the
collection, the bytes of each clone, the cost of fetching the history and the failure outcomes, use
the measurements in `finished_repositories.jsonl`. The clone duration includes retries and backoff,
and the clone bytes include the checkout. These measurements do not give the bytes transferred over
the network or the timing of a single attempt, and they are no bootstrap estimate for a future run.
Check the collection date, the source commit, the machine and the network conditions before you use
them to plan capacity.

The command does not clone repositories or extrapolate from a sample. An earlier probe,
`docs/history/clone-probe.csv`, is the evidence for the size and time bounds of a git command in
`agentcontext/git/bounds.py`, and neither the collection nor the subsample selection reads it.
