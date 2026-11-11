"""The git commands that the steps run.

`auth` passes the GitHub access token to git without putting it on the command line. `bounds`
runs one git command under a time bound and a size bound, with maintenance off and, on Linux,
with the process ending when its parent ends. `clone` contains the clone with its retries and
the queries that read a fresh clone. `history` contains the history fetch and the per-file
queries.
"""
