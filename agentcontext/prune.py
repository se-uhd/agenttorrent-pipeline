"""The paths that the collected_files_archive step removes from the published collected files.

The archive step removes these paths from its copy of the validated tree, and the cloning step
copies no import target among them to the imported tree. The import detection step marks such a
target `pruned`, and the release packs none of them into the archive of the imported files.

From the package, the module imports only `paths`, which holds the collector's test for the
places of the tools' local state. `reuse.import_files` records the code of the imports with the
import heuristics, and with this one import that code contains the test but not the archive step
or the command line that the step imports.
"""

from fnmatch import fnmatch
from pathlib import PurePosixPath

from agentcontext import paths

# Paths removed before publication, matched against the repository-relative
# path, beside the places of the tools' local state (`paths.local_state`). These
# are the files inside a configuration directory that configure nothing:
# version-control and editor housekeeping, dependency locks and logs. Everything
# else is published, including configuration artifacts that no detector classifies.
PRUNE_GLOBS = [
    "**/.gitignore",
    "**/.gitattributes",
    "**/.gitmodules",
    "**/.DS_Store",
    "**/*.lock",
    "**/*.log",
    "**/node_modules/**",
    "**/__pycache__/**",
]


def is_prunable(rel_path):
    """Report whether a repository-relative path is removed before publication: a place of a
    tool's local state (`heuristics/local_state_paths.csv`), such as a session transcript of
    Claude Code, or a path that `PRUNE_GLOBS` matches."""
    posix = PurePosixPath(rel_path).as_posix()
    if paths.local_state(posix) is not None:
        return True

    for pattern in PRUNE_GLOBS:
        if fnmatch(posix, pattern):
            return True
        # In fnmatch, "**/x" requires a slash before the "x" and misses a bare "x" at
        # the root, which this second test matches.
        if pattern.startswith("**/") and fnmatch(posix, pattern[3:]):
            return True
    return False
