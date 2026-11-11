"""The checks over the outputs of a run, each a subcommand of `python -m agentcontext`.

snapshot     the record of a collection: write, compare and validate output/snapshot.json
regressions  the manifest of the tracked outputs, and the comparison of two runs
unmatched    the paths that no heuristic explains, and the attributions that no commit
             heuristic explains
deletions    the deletion log of the collected file cleanup, checked against the trees and the
             output CSVs
commits      the validation of the commit CSV against the commit patterns
"""
