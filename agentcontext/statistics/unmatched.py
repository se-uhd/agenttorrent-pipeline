"""Computations over the tracked unmatched attributions: the archive
`output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z` that `unmatched reduce` writes for the
latest collection, with one row per attribution that no heuristic explains and that names a bot
or a tool and no person, its `kind`, its `token` and the `commits` and `repos` that it is in.

The repository tracks one archive at a time, named for the day on which its collection started,
so a computation reads it by its glob pattern and not by its day.
"""

from agentcontext.statistics import Files, Unavailable, checkout

UNMATCHED = "data/collection/output/all_ai_commits-unmatched-*.csv.7z"
NEGATIVES = "heuristics/not_ai_commits.csv"


def _rows(files: Files, pattern: str) -> list:
    """The rows of the one tracked archive of unmatched attributions that `pattern` names."""
    found = files.glob(pattern)
    if len(found) != 1:
        raise Unavailable(f"{len(found)} files match {pattern}, not one")
    return files.memo(("unmatched", found[0]), lambda: list(files.rows(found[0])))


@checkout
def count(files: Files, negatives: str = NEGATIVES, pattern: str = UNMATCHED) -> int:
    """The unmatched attributions that no negative explains."""
    # A row of the negatives explains an attribution when its pattern is part of the token, as
    # `unmatched commits` reads it (`checks.unmatched.explains`).
    explaining = [row["pattern"] for row in files.rows(negatives)
                  if row["pattern"] and not row["pattern"].startswith("#")]
    return sum(1 for row in _rows(files, pattern)
               if not any(key in row["token"] for key in explaining))


@checkout
def size(files: Files, pattern: str = UNMATCHED) -> int:
    """The size in bytes of the one tracked archive of unmatched attributions."""
    found = files.glob(pattern)
    if len(found) != 1:
        raise Unavailable(f"{len(found)} files match {pattern}, not one")
    return files.size(found[0])
