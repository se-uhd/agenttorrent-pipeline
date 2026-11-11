"""Computations over the data root of the working copy of a handover: the trees, the file lists,
the workflow files and the commit logs of the collection, which the checkout does not contain.
`python -m agentcontext statistics --collection` computes them during the import of a handover
and writes their values to the collection statistics file (`docs/tools/statistics.md`).

A file list, `collection/data/file_lists/<owner§repo>.ls-tree.xz`, has one `<mode>\\t<path>`
record per entry of the tree at the collected commit, each ended by a NUL
(`agentcontext.git.clone.write_file_list`), and shows the paths that no heuristic matches. A
commit log, `collection/collect_commits/<owner§repo>.log.xz`, has every commit of every ref of a
repository with its identities and its whole message, as `git log` wrote them while the cloning
step read the commit history.

A value of the collection statistics file is current only while the code that computed it is
unchanged, which includes the modules of the package that this module imports, such as the
reader of the commit logs in `agentcontext/commits.py` (`agentcontext.statistics.code_digest`).
"""

import json
import lzma
import posixpath
import re

from agentcontext import commits as commit_records
from agentcontext.repos import repo_name_to_dir
from agentcontext.statistics import Files, Unavailable, collection

LOGS = "collection/collect_commits"
# A source of a catalog entry outside the repository: a URL or an address of git.
REMOTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|git@)", re.IGNORECASE)


@collection
def files(files: Files, pattern: str, contains: str | None = None) -> int:
    """The files of the data root that match a glob pattern, such as
    `collection/data/workflows/*/*.yml`, and with `contains` the files with text that the
    regular expression matches on some line."""
    # A file list ends each record with a NUL, so `(?:\A|\x00)[0-7]+\tCONTEXT\.md\x00` matches
    # the file lists with an entry `CONTEXT.md` at the root.
    found = files.glob(pattern)
    if contains is None:
        return len(found)
    line = re.compile(contains, re.MULTILINE)
    return sum(1 for relative in found
               if line.search(files.bytes(relative).decode("utf-8", errors="replace")))


@collection
def size(files: Files, path: str) -> int:
    """The size in bytes of a file of the data root, such as a CSV that the checkout does not
    track."""
    return files.size(path)


def _inside(source: object) -> bool:
    """Whether a source of a catalog entry is a path inside the repository: a relative path,
    such as `"./plugins/review"`, or an object with the source `local` and a `path`, as Codex
    writes it."""
    if isinstance(source, dict) and source.get("source") == "local":
        source = source.get("path")
    if not isinstance(source, str) or not source or REMOTE.match(source):
        return False
    path = posixpath.normpath(source)
    return not (path == ".." or path.startswith("../") or path.startswith("/"))


@collection
def catalogs(files: Files, plugins: str = "collection/output/plugins.csv",
             tree: str = "collection/data/validated", name: str = "marketplace.json") -> int:
    """The marketplace catalogs named `name` that the plugins CSV lists with a plugin entry that
    names a source inside the repository, read from the validated tree."""
    # A catalog lists plugins and the source that each is fetched from. A URL, an address of git
    # and an object that names another repository, a package or a URL are sources outside it,
    # and a catalog that is not a JSON object with a list of plugins names no source.
    found = 0
    for row in files.rows(plugins):
        if posixpath.basename(row["plugin"]) != name:
            continue
        text = files.bytes(f"{tree}/{repo_name_to_dir(row['repo_name'])}/{row['plugin']}")
        try:
            catalog = json.loads(text.decode("utf-8-sig"))
        except ValueError:
            continue
        entries = catalog.get("plugins") if isinstance(catalog, dict) else None
        if isinstance(entries, list) and any(
                isinstance(entry, dict) and _inside(entry.get("source")) for entry in entries):
            found += 1
    return found


@collection
def log_commits(files: Files, message: str, heuristic: str | None = None,
                table: str = "collection/output/other_ai_commits.csv", logs: str = LOGS) -> int:
    """The commits of the commit logs with a whole message that the regular expression
    `message` matches, in every repository or, with `heuristic`, in the repositories with a
    commit that the heuristic credits in the commit CSV `table`."""
    # The checkout keeps only the commits that a heuristic credits, and a count of the other
    # commits, such as the merge commits of a branch, needs the logs.
    if heuristic is None:
        names = files.glob(f"{logs}/*{commit_records.LOG_SUFFIX}")
    else:
        repositories = {row["repo_name"] for row in files.rows(table)
                        if heuristic in row["matched_heuristics"].split(";")}
        names = [f"{logs}/{repo_name_to_dir(repo)}{commit_records.LOG_SUFFIX}"
                 for repo in sorted(repositories)]
    pattern = re.compile(message)
    found = 0
    for name in names:
        if not files.exists(name):
            # A repository with a credited commit had its commit history read, and its log is
            # the evidence of the count. Without it, the count would leave its commits out.
            raise Unavailable(f"The data root has no commit log {name}")
        with lzma.open(files.path(name), "rb") as stream:
            for record in commit_records._records_from(stream, "nul-v1"):
                if pattern.search(record["message"]):
                    found += 1
    return found
