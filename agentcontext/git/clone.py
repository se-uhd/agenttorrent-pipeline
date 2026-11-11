"""The cloning step's clone with its retries, and the queries that read a fresh clone.

`clone_with_retry` clones one repository under a time bound and a size bound, retries a
failed attempt after a backoff, and returns one of the outcomes that the cloning step
records: `ok`, `clone_failed`, `size_bound_exceeded`, `time_bound_exceeded`, or the reason for
which GitHub refused the repository, `not_found_or_no_access`, `dmca_takedown` or
`disabled_by_github`. The readers below read the branch, the head commit, its date and the
gitlinks from the clone that the step already has. `write_file_list` writes the clone's file
list beside the collection, xz-compressed, and `read_file_list` reads one back. `remote_head`
queries GitHub for the branch and the commit at a repository's HEAD without a clone.
"""

import lzma
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from agentcontext.git import auth, bounds, history
from datetime import datetime, timezone
from agentcontext import logs


# One `[submodule "..."]` section and its two fields. Each section is read on its own, because
# .gitmodules allows `path` and `url` in either order and with any spacing. A line-based reader
# that remembered the last `path` that it read would give a section that lists `url` first the
# previous section's path.
_SUBMODULE_ENTRY = re.compile(r"\[submodule[^\]]*\](?P<body>(?:[^\[]|\n)*)", re.MULTILINE)
_SUBMODULE_FIELD = re.compile(r"^\s*(path|url)\s*=\s*(\S+)\s*$", re.MULTILINE)
# A .gitmodules url, in any of the forms that git accepts for GitHub. The trailing `.git` is
# optional, and only that suffix is dropped, because excluding the dot from the name would shorten
# `nwjs/chromium.src.git` to `nwjs/chromium`, which is a different repository that exists, and
# `socketio/socket.io` to `socketio/socket`.
_SUBMODULE_URL = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?(?:/|\s|$)")
def tree_listing(repo_dir: Path) -> bytes:
    """List files committed at HEAD, preserving Git's exit status and stderr on failure.

    One `<mode> <type> <object>\t<path>` entry per file, symbolic link and gitlink, each ended
    by a NUL. The listing is bytes, because a path in the tree is bytes, and a Latin-1 name
    decoded strictly would raise an exception that the worker records as a clone failure and
    retries forever.
    In a blobless clone, git lists the files from the trees alone, without fetching a blob.
    """
    result = subprocess.run(["git", "-C", str(repo_dir), "ls-tree", "-r", "-z", "HEAD"],
                            capture_output=True, check=True)
    return result.stdout


# A repository's file list, beside the collection: one `<mode>\t<path>` record per entry of
# `tree_listing`, each ended by a NUL, xz-compressed. The object ids are left out, because
# they make up most of the compressed size, and every use of the list concerns paths, not
# content.
FILE_LIST_SUFFIX = ".ls-tree.xz"


def write_file_list(listing: bytes, path: Path) -> int:
    """Write the file list of `listing` to `path` in one step. Returns the number of entries."""
    records = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        records.append(meta.split(b" ", 1)[0] + b"\t" + name + b"\0")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_bytes(lzma.compress(b"".join(records)))
    os.replace(partial, path)
    return len(records)


def read_file_list(path: Path) -> list[tuple[str, str]]:
    """The (mode, path) entries of a file list, a path decoded as UTF-8 with any other byte
    kept as a surrogate, so that no name is lost or changed."""
    entries = []
    for record in lzma.decompress(path.read_bytes()).split(b"\0"):
        if record:
            mode, _, name = record.partition(b"\t")
            entries.append((mode.decode("ascii"), name.decode("utf-8", "surrogateescape")))
    return entries


def submodule_rows(repo: str, repo_dir: Path, listing: bytes | None = None) -> list[dict]:
    """Return this repository's gitlinks, read from the clone.

    A submodule is a pointer, and the files behind it belong to the repository that the link
    names. No file under a submodule is therefore this repository's configuration. The clone is
    on disk here: `git ls-tree` lists which directories are gitlinks, and the clone's own
    `.gitmodules` records where each points. A query to GitHub would cost a request per
    repository for what the checkout already contains. `listing` is `tree_listing` of the clone
    when the caller already has it.

    The submodules are never checked out, and a later step reads these rows instead.
    """
    if listing is None:
        try:
            listing = tree_listing(repo_dir)
        except (subprocess.CalledProcessError, OSError):
            return []
    paths = []
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if fields and fields[0] == b"160000":
            paths.append(path.decode("utf-8", errors="replace"))
    names = _submodule_urls(repo_dir / ".gitmodules")
    return [{"repo_name": repo, "path": path, "submodule_repo": names.get(path, "")}
            for path in sorted(paths)]


def parse_gitmodules(text: str) -> dict:
    """Map each submodule path to the GitHub repository that its URL names."""
    found = {}
    for entry in _SUBMODULE_ENTRY.finditer(text):
        fields = dict((m.group(1), m.group(2))
                      for m in _SUBMODULE_FIELD.finditer(entry.group("body")))
        path, url = fields.get("path"), fields.get("url", "")
        if not path:
            continue
        repo = _SUBMODULE_URL.search(url)
        found[path.rstrip("/")] = f"{repo.group(1)}/{repo.group(2)}" if repo else ""
    return found


def _submodule_urls(gitmodules: Path) -> dict:
    """`parse_gitmodules` over the file, or an empty map when it is not there."""
    if not gitmodules.is_file():
        return {}
    return parse_gitmodules(gitmodules.read_text(encoding="utf-8", errors="replace"))
_FILTER_UNSUPPORTED_RE = re.compile(
    r"filter-spec|does not support.*filter|unknown option.*filter",
    re.IGNORECASE,
)
# The messages with which GitHub reports a repository that is gone or closed, as distinct from
# a network that is down. A message missing here costs `max_tries` attempts and a retry in the
# step's rounds and on the next run, and counts toward the limit on repositories left to retry.
# `--max-consecutive-failures` such repositories in a row start the connectivity probe, which
# tests the network before the step stops.
_PERMANENT_FAIL_RE = re.compile(
    r"ERROR: Repository not found\.|remote: Repository not found|"
    r"fatal: repository .* not found|not authorized|"
    r"permission denied \(publickey\)|access denied|"
    r"Access to this repository has been disabled|"
    r"Repository unavailable due to|unavailable due to DMCA|"
    # git's own message when the remote is a path with no repository behind it, which the file
    # history step also reads as a repository that is gone (`history._REPOSITORY_IS_GONE`). A
    # local Git mirror reports it for a repository for which GitHub returns Repository not
    # found. Read as a network failure, it would be retried and counted toward the limit.
    r"does not appear to be a git repository|"
    # A repository for which GitHub requests credentials when the run has no GitHub access
    # token. Without one, git has no credential to send and ends with this message. With one,
    # GitHub returns "Repository not found" for a repository that is not public, and a refused
    # GitHub access token is no property of the repository (`_CREDENTIAL_REFUSED_RE`).
    r"could not read Username",
    re.IGNORECASE,
)
# git's messages when GitHub refused the credential that a request sent: GitHub's "Invalid
# username or token", after which git ends with "Authentication failed for". To a request that
# sends a valid GitHub access token, GitHub returns "Repository not found" for a repository that
# is gone or private, and these messages concern the GitHub access token, not the
# repository. GitHub may also require authentication for a public repository: "During periods of
# unusual traffic, GitHub may temporarily require authentication for some clones and fetches of
# public repositories over HTTPS" (changelog of 2026-10-19). A refused GitHub access token then
# fails the clone of every repository.
_CREDENTIAL_REFUSED_RE = re.compile(r"Authentication failed for|Invalid username or token",
                                    re.IGNORECASE)
# git's messages when the clone downloaded the repository and could not write a path of its tree,
# because a name is longer than the file system allows, 255 bytes on ext4 and btrfs. The same
# file system refuses the same name on every attempt.
_FILE_NAME_TOO_LONG_RE = re.compile(r"File name too long", re.IGNORECASE)
_CHECKOUT_FAILED_RE = re.compile(r"Clone succeeded, but checkout failed", re.IGNORECASE)


def build_clone_url(repo: str, use_ssh: bool, token: str | None) -> str:
    """The URL to clone from. With a GitHub access token it names the placeholder user, never
    the GitHub access token.

    A GitHub access token in the URL would be in git's argv, in the clone's `.git/config`, and
    in any message that echoes the command. `git.auth` passes it to git through a credential
    helper instead.
    """
    return auth.clone_url(repo, token or None, use_ssh)


def is_filter_unsupported(log_text: str) -> bool:
    """True if the clone log indicates that the server rejected --filter=blob:none."""
    return bool(_FILTER_UNSUPPORTED_RE.search(log_text))


def is_permanent_failure(log_text: str) -> bool:
    """True if the clone log indicates a permanent failure, a missing repository or no access."""
    return bool(_PERMANENT_FAIL_RE.search(log_text))


# The reasons under which a permanent failure is recorded, read from GitHub's refusal. git's
# message names the reason where GitHub gives one, as in `remote: Repository unavailable due to
# DMCA takedown.` and `remote: Access to this repository has been disabled by GitHub staff.`.
# `Repository not found` gives no reason, because GitHub returns it for a deleted repository and
# a private repository alike, as its API does.
NOT_FOUND_OR_NO_ACCESS = "not_found_or_no_access"
DMCA_TAKEDOWN = "dmca_takedown"
DISABLED_BY_GITHUB = "disabled_by_github"
PERMANENT_OUTCOMES = (NOT_FOUND_OR_NO_ACCESS, DMCA_TAKEDOWN, DISABLED_BY_GITHUB)
_DMCA_RE = re.compile(r"unavailable due to DMCA", re.IGNORECASE)
_DISABLED_RE = re.compile(r"Access to this repository has been disabled|Repository unavailable due to",
                          re.IGNORECASE)


def permanent_failure_reason(log_text: str) -> "str | None":
    """The reason under which a permanent failure is recorded, or None when the log shows no
    permanent failure."""
    if not is_permanent_failure(log_text):
        return None
    if _DMCA_RE.search(log_text):
        return DMCA_TAKEDOWN
    if _DISABLED_RE.search(log_text):
        return DISABLED_BY_GITHUB
    return NOT_FOUND_OR_NO_ACCESS


def redact_token(url: str) -> str:
    """Strip embedded credentials, such as a GITHUB_TOKEN, from an HTTPS URL for safe logging."""
    return re.sub(r"(https://)[^@/]+@", r"\1", url)


GIT_LFS_DISABLE_ARGS = [
    "-c", "filter.lfs.required=false",
    "-c", "filter.lfs.clean=cat",
    "-c", "filter.lfs.smudge=cat",
    # Empty, not `cat`, because the process filter uses a handshake protocol that `cat` echoes back,
    # and git then writes error lines and kills the filter for every LFS file. With no process
    # filter, git uses the two one-shot filters above, and the checkout is byte-identical.
    "-c", "filter.lfs.process=",
]
# The outcomes of a clone attempt besides "ok". `clone_failed` is a failure of this run, which
# the retry loop handles. A permanent reason, `size_bound_exceeded` and `time_bound_exceeded` are
# findings about the repository under the run's bounds and end the loop: git reported that the
# repository is not there, the clone grew past the size bound, or `TIMEOUT_TRIES` attempts did
# not finish within the time bound.
SIZE_BOUND_EXCEEDED = "size_bound_exceeded"
TIME_BOUND_EXCEEDED = "time_bound_exceeded"
# A repository with a path that this machine's file system cannot write. The outcome is a
# property of the repository on this file system rather than of the network. It therefore stays
# recorded for the rest of the collection, and the next collection clones the repository again.
FILE_NAME_TOO_LONG = "file_name_too_long"
# How many attempts the time bound may end before the clone is recorded as
# `time_bound_exceeded`. GROUNDING: a policy, the one of `history.HISTORY_FETCH_TRIES`. An attempt
# that the time bound ends is a transfer that stalled or a repository too large for the bound,
# and one more attempt distinguishes the two at the cost of one more time bound. A stalled
# transfer ends earlier, through `bounds.LOW_SPEED_SECONDS`. Each further attempt under the same
# bound would hold a worker for another time bound, and so would every later run. Only a run
# with a larger `--clone-timeout` therefore clones the repository again
# (`resume.clone_needs_redo`).
TIMEOUT_TRIES = 2


def clone_with_retry(
    repo: str,
    dest: Path,
    log_path: Path,
    *,
    use_ssh: bool = False,
    token: str | None = None,
    max_tries: int = 6,
    base_delay: float = 5.0,
    max_clone_bytes: int = bounds.DEFAULT_MAX_BYTES,
    clone_timeout: float = history.GIT_TIMEOUT_SECONDS,
) -> str:
    """Clone a repository with retries and backoff.

    Returns 'ok', 'clone_failed', 'size_bound_exceeded', 'time_bound_exceeded',
    'file_name_too_long' or one of `PERMANENT_OUTCOMES`. Every attempt runs through
    `bounds.run_bounded` with `dest` as the directory under the size bound and `clone_timeout`
    as the time bound, because a large repository takes gigabytes even when packed. A timeout
    is a failed attempt and is retried like a network failure, until `TIMEOUT_TRIES` attempts
    have ended at the time bound, and the clone is then `time_bound_exceeded`.

    A clone over the size bound is not retried, because the bound is deterministic and a retry
    would download the same bytes again. A clone that finished within one interval of the
    watcher but ended over the size bound counts as a breach too, because the bound applies to
    the working tree that the clone produced. A `max_clone_bytes` of 0 turns the size bound
    off.

    A clone for which GitHub refused the GitHub access token is `clone_failed` after that
    attempt, because GitHub refuses the same GitHub access token again. A clone that could not
    write a name too long for the file system is `file_name_too_long` after that attempt,
    because the file system refuses the same name again.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # This collection's attempts and nothing older, because a log appended across runs grows
    # without limit. The previous run's attempts are in the previous run's handover.
    log_path.write_text("", encoding="utf-8")
    url = build_clone_url(repo, use_ssh, token)

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if use_ssh:
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    auth.apply(env, None if use_ssh else (token or None))

    log_accum: list[str] = []

    def append(text: str) -> None:
        log_accum.append(text)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text)

    def run_clone(use_filter: bool) -> "int | str":
        """The clone's exit code, SIZE_BOUND_EXCEEDED when the size bound stopped it, or
        TIME_BOUND_EXCEEDED when the time bound ended it."""
        cmd = ["git", *GIT_LFS_DISABLE_ARGS, "clone", "--depth", "1", "--single-branch"]
        if use_filter:
            cmd.append("--filter=blob:none")
        cmd += [url, str(dest)]
        try:
            proc = bounds.run_bounded(cmd, cwd=str(dest.parent), env=env,
                                           timeout=clone_timeout, bounded_path=str(dest),
                                           max_bytes=max_clone_bytes)
        except bounds.GitSizeBoundExceeded as e:
            append(f"SIZE_BOUND_EXCEEDED {e}\n")
            return SIZE_BOUND_EXCEEDED
        except subprocess.TimeoutExpired:
            append(f"TIMEOUT after {clone_timeout:g}s\n")
            return TIME_BOUND_EXCEEDED
        except OSError as e:
            # git could not be started, because the machine is out of processes or memory. The
            # failure is transient and counts as an attempt like a timeout, with the backoff.
            append(f"NOT STARTED {e.__class__.__name__} ({e})\n")
            return 1
        append(proc.stdout + proc.stderr)
        if proc.returncode == 0 and max_clone_bytes:
            size = bounds.directory_size(dest)
            if size > max_clone_bytes:
                append(f"SIZE_BOUND_EXCEEDED {dest} is {size / 1024 ** 3:.2f} GiB after the "
                       f"clone, over the {max_clone_bytes / 1024 ** 3:.2f} GiB size bound\n")
                return SIZE_BOUND_EXCEEDED
        return proc.returncode

    delay = base_delay
    timeouts = 0
    for i in range(1, max_tries + 1):
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        append(f"=== {now} attempt {i}/{max_tries} ===\nurl={redact_token(url)}\nuse_ssh={use_ssh}\n")

        outcome = run_clone(use_filter=True)
        if outcome == 0:
            append("SUCCESS (filter=blob:none, lfs disabled)\n")
            return "ok"
        if outcome == SIZE_BOUND_EXCEEDED:
            shutil.rmtree(dest, ignore_errors=True)
            return SIZE_BOUND_EXCEEDED
        timeouts += outcome == TIME_BOUND_EXCEEDED

        if is_filter_unsupported("".join(log_accum)):
            append("--- Retrying without --filter=blob:none in the same attempt ---\n")
            outcome = run_clone(use_filter=False)
            if outcome == 0:
                append("SUCCESS (no filter, lfs disabled)\n")
                return "ok"
            if outcome == SIZE_BOUND_EXCEEDED:
                shutil.rmtree(dest, ignore_errors=True)
                return SIZE_BOUND_EXCEEDED
            timeouts += outcome == TIME_BOUND_EXCEEDED

        if timeouts >= TIMEOUT_TRIES:
            shutil.rmtree(dest, ignore_errors=True)
            append(f"TIME_BOUND_EXCEEDED {timeouts} attempts ended at the time bound of "
                   f"{clone_timeout:g}s\n")
            return TIME_BOUND_EXCEEDED

        tail = "\n".join("".join(log_accum).splitlines()[-80:])
        reason = permanent_failure_reason(tail)
        if reason:
            append(f"PERMANENT_FAIL_{reason.upper()}\n")
            return reason
        if _CHECKOUT_FAILED_RE.search(tail) and _FILE_NAME_TOO_LONG_RE.search(tail):
            shutil.rmtree(dest, ignore_errors=True)
            append(f"{FILE_NAME_TOO_LONG.upper()} a name of the repository's tree is longer "
                   f"than the file system allows\n")
            return FILE_NAME_TOO_LONG
        if _CREDENTIAL_REFUSED_RE.search(tail):
            # Every later clone that GitHub requests credentials for fails the same way, and the
            # step counts the failures in a row and toward --max-clone-failures as it counts any
            # clone_failed.
            append("CREDENTIAL_REFUSED GitHub refused the GitHub access token. Replace it and "
                   "resume the collection\n")
            return "clone_failed"

        if i < max_tries:
            append(f"FAILED (sleep {delay}s)\n")
            time.sleep(delay)
            delay *= 2
        else:
            append("FAILED\n")

    return "clone_failed"


# How many times `git_version` runs `git --version` before it raises, and how long it waits
# before each further run. A fork can fail for a moment on the NAS, as with BlockingIOError when
# the container is short of processes or memory.
GIT_VERSION_TRIES = 5
GIT_VERSION_WAIT = 10


class GitVersionUnavailable(RuntimeError):
    """`git --version` printed no version in any of `GIT_VERSION_TRIES` runs."""


def git_version() -> str:
    """What `git --version` prints, such as `git version 2.55.0`.

    `created_at`, `#commits` and `first_commit_sha` change with the version, because git 2.56
    follows a renamed path through a merged history differently from git 2.54 (`docs/git.md`).
    The images and the development machines run the git that `mise.toml` pins
    (`deploy/install_tools.sh`). A collection is recorded with the git version that collected
    it, so that a new pin, or a machine with another git, collects again rather than keeping the
    columns of the earlier version.

    A run that git cannot start, that times out or that prints no version is run again, and
    after `GIT_VERSION_TRIES` runs the function raises `GitVersionUnavailable` rather than
    returning "". Compared with the version that each line of the list of finished repositories
    records, an empty version would make the cloning step collect every repository again, the
    repositories of the collection in progress included. The step would also record the empty
    version in every new line, and the next run would collect every repository again too.
    """
    failure = ""
    for attempt in range(GIT_VERSION_TRIES):
        if attempt:
            time.sleep(GIT_VERSION_WAIT)
        try:
            result = subprocess.run(["git", "--version"], capture_output=True, text=True,
                                    timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            failure = f"{error.__class__.__name__} ({error})"
            continue
        version = (result.stdout or "").strip()
        if result.returncode == 0 and version:
            return version
        failure = (f"exit status {result.returncode} "
                   f"({(result.stderr or '').strip()[-bounds.STDERR_TAIL:] or 'no message'})")
    raise GitVersionUnavailable(f"git --version printed no version in {GIT_VERSION_TRIES} "
                                f"runs, the last of them with {failure}")


# How long one `git ls-remote` of a repository's HEAD may take: the same bound as the cloning
# step's connectivity probe.
REMOTE_HEAD_TIMEOUT = 60


def remote_head(repo: str, token: str | None = None, use_ssh: bool = False,
                timeout: float = REMOTE_HEAD_TIMEOUT,
                authenticate: bool = False) -> "tuple[str, str] | str | None":
    """The branch that HEAD of `repo` names and the commit that it is at, as
    `read_snapshot_metadata` reads the two from a clone, from `git ls-remote` over the URL and
    the credential helper that a clone uses. Returns ("", "") for a repository without a
    commit, the reason from `PERMANENT_OUTCOMES` when git's message is GitHub's refusal, as a
    clone reads it, and None when git returned no response.

    A refusal and None are kept apart. If None stood for a repository that is gone as well as
    for a network that is down, a network outage would cost a clone of each repository, and
    the line written before the clone would mark the repository for the next run to clone
    again, whatever its head.

    With `authenticate` and a GitHub access token over HTTPS, the query sends the GitHub access
    token with its first request (`auth.PROACTIVE_AUTH`). Without it, git sends the GitHub access
    token only after a 401, which GitHub sends for a repository that is not public, and the query of
    a public repository is anonymous. A refused GitHub access token is None rather than a
    refusal of the repository, whether the query sent it first or after a 401
    (`_CREDENTIAL_REFUSED_RE`), and the caller queries again without `authenticate`.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    auth.apply(env, None if use_ssh else (token or None))
    proactive = authenticate and bool(token) and not use_ssh
    try:
        proc = bounds.run_bounded(["git", *(auth.PROACTIVE_AUTH if proactive else ()),
                                   "ls-remote", "--symref",
                                   auth.clone_url(repo, token, use_ssh), "HEAD"],
                                  cwd=os.getcwd(), env=env, timeout=timeout, max_bytes=0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return permanent_failure_reason(proc.stderr or "")
    branch = sha = ""
    for line in (proc.stdout or "").splitlines():
        ref, _, name = line.partition("\t")
        if name != "HEAD":
            continue
        if ref.startswith("ref: refs/heads/"):
            branch = ref[len("ref: refs/heads/"):]
        elif not ref.startswith("ref: "):
            sha = ref
    return branch, sha


def read_snapshot_metadata(repo_dir: Path) -> tuple[str, str, str]:
    """Read the branch, the committer date and HEAD, and distinguish an empty repository from
    failed queries."""
    # `--` ends the revisions, because a repository that tracks a file named `HEAD` at its root
    # makes `git show HEAD` ambiguous between the commit and the file, and git exits 128, which
    # would leave the repository without a head commit date and fail its collection.
    # The branch is the full name of the ref that HEAD names, without `refs/heads/`. Where a tag
    # has the default branch's name, `rev-parse --abbrev-ref` and `symbolic-ref --short` both
    # print `heads/<name>` to distinguish the two. The step would record that as the branch,
    # build `github_link` with it and never match the branch that GitHub returns, and the
    # repository would then be collected again at every run. A clone's HEAD always names its
    # branch.
    # `log` rather than `show -s`, because `show` compares the commit's tree with its parent's,
    # in a depth-1 clone with the empty tree, and at a gitlink it reads the `.gitmodules` of the
    # working tree. A `.gitmodules` that git cannot parse, such as one with committed
    # merge-conflict markers, would then fail the query, and with it the collection of the
    # repository at every Start.
    queries = {"branch": ["symbolic-ref", "HEAD"],
               "head_commit_date": ["log", "-1", "--format=%cI", "HEAD", "--"],
               "head_sha": ["rev-parse", "HEAD"]}
    values, failures = {}, []
    for name, args in queries.items():
        cmd = ["git", "-C", str(repo_dir), *args]
        result = subprocess.run(cmd, capture_output=True, text=True)
        values[name] = result.stdout.strip() if result.returncode == 0 else ""
        if not values[name]:
            detail = auth.redact((result.stderr or "").strip()[-bounds.STDERR_TAIL:])
            failures.append(f"{' '.join(cmd)} exited {result.returncode} and returned no {name} "
                            f"({detail or 'no error text'})")
    if failures:
        cmd = ["git", "-C", str(repo_dir), "rev-list", "--all", "--count"]
        count = subprocess.run(cmd, capture_output=True, text=True)
        if not values["head_sha"] and count.returncode == 0 and count.stdout.strip() == "0":
            return "", "", ""
        if count.returncode != 0:
            detail = auth.redact((count.stderr or "").strip()[-bounds.STDERR_TAIL:])
            failures.append(f"{' '.join(cmd)} exited {count.returncode} "
                            f"({detail or 'no error text returned'})")
        raise RuntimeError("Could not read the branch, the head commit date and HEAD of the "
                           "clone. " + " ".join(f"{failure}." for failure in failures))
    branch = values["branch"].removeprefix("refs/heads/")
    return branch, values["head_commit_date"], values["head_sha"]


