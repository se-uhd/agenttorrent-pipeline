"""The fixtures that the collection suites share:

- a repository built with `git init`, and the commits in it
- a stand-in for `git upload-pack` that stalls or slows the pack, the git configuration that
  sends a clone through the stand-in, and a time bound that ends a command at the stall
- a shorter sampling interval of the size bound
- the reading of one column of a row of the collected list
- Synology DSM writing into a directory while a step removes it, into a directory that a step has
  just created, and into an `@eaDir` that a step has just listed
- a `git --version` that cannot start
- a sync of a whole file system that does not return
- a repository with files nested too deeply to parse, or with integers too long to convert
- a loopback git server in place of github.com that records the credential of each request
- the outcome counts of a commit reclassification report
- the bash and the environment that run a shell script of the repository
- the latest calendar date in any time zone, which a date written on the day that it names
  never passes

Requirements: Python 3.14, git on PATH (stdlib only)
"""

import base64
import contextlib
import errno
import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentcontext.paths import COLLECTED_CSV_HEADER

# Every outcome of a repository that a commit reclassification report counts, written out here
# rather than read from the step.
REPORT_OUTCOMES = ("current", "failed", "history_incomplete", "history_not_read",
                   "missing_record", "missing_tree", "no_log", "no_path_history", "no_scope",
                   "not_applicable", "reclassified", "unavailable")


# The bash that the tests run the shell scripts of dev/ with. The first `bash` on PATH can be the
# shim of a tool manager that resolves to the shell of another project, in whose environment
# `python3` is an interpreter that does not import agentcontext. The bash of the system runs the
# scripts instead: /bin/bash on Linux, and bash 3.2 on macOS, which the scripts of dev/ support.
BASH = shutil.which("bash", path=os.defpath) or "/bin/bash"


def script_env(base: dict | None = None) -> dict:
    """The environment of a shell script that a test runs: `base`, or the environment of this
    process, with the directory of the interpreter that runs the tests first on PATH, so that the
    `python3` of the script is that interpreter."""
    env = dict(os.environ if base is None else base)
    env["PATH"] = os.pathsep.join(filter(None, [str(Path(sys.executable).parent),
                                                env.get("PATH", "")]))
    return env


# UTC+14, the time zone that begins each day first.
FIRST_ZONE = timezone(timedelta(hours=14))


def latest_date(now: datetime | None = None) -> date:
    """The calendar date in the first time zone to begin each day, at `now` or at the current
    time. A date that a person writes on the day that it names, in any time zone, is never later,
    so that a test that compares such a date with it passes on a machine in any time zone and at
    any hour."""
    return (now or datetime.now(timezone.utc)).astimezone(FIRST_ZONE).date()


def report_counts(**repositories) -> dict:
    """The counts of a commit reclassification report in which the named outcomes have the
    given numbers of repositories and every other outcome has none."""
    return {outcome: repositories.get(outcome, 0) for outcome in REPORT_OUTCOMES}


def _init_git_repo(path: Path) -> None:
    """Create a minimal local git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"],
                   capture_output=True, check=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"],
                   capture_output=True, check=True)


def _add_commit(repo: Path, rel: str, content: str, date: str | None = None) -> None:
    """Write a file into a repo and commit it, optionally at a given committer date."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    env = dict(os.environ)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", f"add {rel}"],
                   capture_output=True, check=True, env=env)


def _add_commit_in_zone(repo: Path, rel: str, content: str, epoch: int, zone: str) -> str:
    """Commit a file with author and committer at `epoch` in the time zone `zone`, such as
    `+2500`, on the branch that HEAD names, and return the commit's hash.

    A repository on GitHub can contain a commit with an offset of 24 hours or more, because
    `git fsck` accepts the offset in a commit. `git commit` refuses such an offset in a date that
    it parses, and the fixture therefore writes the commit object with `hash-object --literally`.
    """
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content)
    run = functools.partial(subprocess.run, capture_output=True, check=True, text=True)
    run(["git", "-C", str(repo), "add", rel])
    tree = run(["git", "-C", str(repo), "write-tree"]).stdout.strip()
    parent = run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"],
                 check=False).stdout.strip()
    ident = f"T <t@t.com> {epoch} {zone}"
    body = (f"tree {tree}\n" + (f"parent {parent}\n" if parent else "")
            + f"author {ident}\ncommitter {ident}\n\nadd {rel}\n")
    sha = run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--literally",
               "--stdin"], input=body).stdout.strip()
    branch = run(["git", "-C", str(repo), "symbolic-ref", "HEAD"]).stdout.strip()
    run(["git", "-C", str(repo), "update-ref", branch, sha])
    return sha


def _add_noise(repo: Path, rel: str, size: int) -> None:
    """Commit `size` random bytes, which no pack compresses, as one file."""
    (repo / rel).write_bytes(os.urandom(size))
    subprocess.run(["git", "-C", str(repo), "add", rel], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", f"add {rel}"],
                   capture_output=True, check=True)


def sampled_every(seconds: float):
    """Every `bounds.run_bounded` in the block samples the size of its directory every `seconds`.

    Otherwise a clone is sampled at the default interval, `bounds.SAMPLE_SECONDS`. That interval
    is longer than any clone that a test makes, and only the check after the clone would find a
    clone over the size bound.
    """
    from unittest import mock
    from agentcontext.git import bounds
    return mock.patch.object(bounds, "run_bounded",
                             functools.partial(bounds.run_bounded, sample_seconds=seconds))


_UPLOAD_PACK = """\
import os, subprocess, sys, threading, time
stall, pause, marker = {stall!r}, {pause!r}, {marker!r}
stall = stall == "always" or (stall == "first" and not os.path.exists(marker))
p = subprocess.Popen(["git", "upload-pack", *sys.argv[1:]],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
def feed():
    while (b := os.read(0, 65536)):
        p.stdin.write(b); p.stdin.flush()
    p.stdin.close()
threading.Thread(target=feed, daemon=True).start()
seen, started = b"", False
while (b := os.read(p.stdout.fileno(), 65536)):
    os.write(1, b)
    started = started or b"PACK" in seen + b
    seen = (seen + b)[-64:]
    if started and stall:
        open(marker, "w").close()
        time.sleep(60)
    if started and pause:
        time.sleep(pause * len(b) / 65536)
"""


def upload_pack(root: Path, stall: str = "never", pause: float = 0.0) -> Path:
    """Write a stand-in for `git upload-pack` under `root` and return its path.

    The stand-in runs the real upload-pack over the repository that it is given and relays the
    output. `stall` is "never", "first" or "always". A run that stalls stops relaying once the
    pack has started and sleeps until the time bound kills it. At that point, the receiving git
    has its lock files and part of the pack on disk. "first" stalls only the first run, and
    `<script>.stalled` exists after a run has stalled. `pause` delays each 64 KiB of the pack by
    that many seconds, which slows the transfer without stopping it. A pack spans many reads
    only when the repository contains several times 64 KiB of data that does not compress.
    """
    script = root / f"upload_pack_{stall}_{pause:g}.py"
    script.write_text(_UPLOAD_PACK.format(stall=stall, pause=pause,
                                          marker=str(script.with_suffix(".stalled"))),
                      encoding="utf-8")
    return script


@contextlib.contextmanager
def time_bound_at_stall(script: Path, hang_seconds: float = 600.0):
    """Inside the block, the time bound ends a git command at the stall of the stand-in `script`
    rather than at a time.

    `bounds.run_bounded` waits for its command with `communicate(timeout=...)`, and when that
    call raises `subprocess.TimeoutExpired`, it kills the process group while the transfer is in
    flight. Inside the block, the first such call of a command that started before
    `<script>.stalled` existed raises the exception once the marker exists and the command still
    runs. A command that exits first returns as usual, and a command that starts after the stall
    waits under the timeout that it was given. The stall, and not the speed of the machine,
    then decides which attempt the time bound ends, and the attempts that follow can run under
    a time bound that only catches a hang. `hang_seconds` bounds the wait for the stall, so
    that a stand-in that never stalls fails the test rather than holding it.
    """
    marker = script.with_suffix(".stalled")

    class EndsAtTheStall(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            # Read before the command starts, which can stall before Popen returns.
            self.ends_at_the_stall = not marker.exists()
            super().__init__(*args, **kwargs)

        def communicate(self, input=None, timeout=None):
            if timeout is None or not self.ends_at_the_stall:
                return super().communicate(input, timeout)
            # `run_bounded` reaps the killed group with further calls, which wait as usual.
            self.ends_at_the_stall = False
            deadline = time.monotonic() + hang_seconds
            while True:
                try:
                    return super().communicate(input, timeout=0.05)
                except subprocess.TimeoutExpired:
                    if marker.exists() or time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(self.args, timeout) from None

    with mock.patch.object(subprocess, "Popen", EndsAtTheStall):
        yield


def ext_redirect(script: Path, remote: Path, url: str) -> dict:
    """The environment's git config that sends a clone of `url` through `script`.

    The ext:: transport runs the script in place of upload-pack, because a clone does not
    read `remote.origin.uploadpack` from the environment's config. The paths must contain no
    space and no percent sign, because they go into the command line unquoted.
    """
    return {"GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": f"url.ext::{sys.executable} {script} {remote}.insteadOf",
            "GIT_CONFIG_VALUE_0": url,
            "GIT_CONFIG_KEY_1": "protocol.ext.allow",
            "GIT_CONFIG_VALUE_1": "always"}


def _row_field(row: str, col: str) -> str:
    """Extract one column value from a row of the collected list by header name."""
    return row.split(",")[COLLECTED_CSV_HEADER.split(",").index(col)]


@contextlib.contextmanager
def dsm_writes_during_removal(trigger: str | None = None, entry: str = "@eaDir/x@SynoEAStream",
                              times: int = 1):
    """Write `entry`, relative to the directory of a file that is being removed, right after
    the file is unlinked, as Synology DSM writes an `@eaDir` into a directory that its indexing
    service visits. `trigger` names the file, or None any file, and `times` sets how many
    removals write the entry. Yields the list of the directories written into.

    The entry makes the removal of the directory fail with ENOTEMPTY, because `shutil.rmtree`
    lists a directory before it removes the directory's entries. `rmtree` unlinks by name
    relative to an open directory where the platform allows it, and by path otherwise, and the
    fixture writes the entry the same way.
    """
    real_unlink = os.unlink
    written = []

    def unlink(path, *, dir_fd=None):
        real_unlink(path, dir_fd=dir_fd)
        name = os.fsdecode(path)
        if len(written) >= times or (trigger is not None and os.path.basename(name) != trigger):
            return
        directory = os.path.dirname(name)
        parts = entry.split("/")
        for depth in range(1, len(parts)):
            with contextlib.suppress(FileExistsError):
                os.mkdir(os.path.join(directory, *parts[:depth]), dir_fd=dir_fd)
        os.close(os.open(os.path.join(directory, *parts), os.O_CREAT | os.O_WRONLY,
                         dir_fd=dir_fd))
        written.append(directory)

    with mock.patch("os.unlink", unlink):
        yield written


@contextlib.contextmanager
def dsm_writes_into_new_directories(name: str, below: Path,
                                    entry: str = "@eaDir/x@SynoEAStream"):
    """Write `entry`, relative to each directory named `name` that is created below `below`,
    right after its creation, as DSM indexes a directory that a step has just written. Yields
    the list of the directories written into.

    The fixture patches `os.mkdir`, which `os.makedirs` also calls, and therefore catches every
    way of creating the directory.
    """
    real_mkdir = os.mkdir
    below = os.path.abspath(below)
    written = []

    def mkdir(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        created = os.path.abspath(os.fsdecode(path))
        if dir_fd is not None or os.path.basename(created) != name \
                or not created.startswith(below + os.sep):
            return
        parts = entry.split("/")
        for depth in range(1, len(parts)):
            with contextlib.suppress(FileExistsError):
                real_mkdir(os.path.join(created, *parts[:depth]))
        Path(created, *parts).write_text("attributes\n")
        written.append(created)

    with mock.patch("os.mkdir", mkdir):
        yield written


class _Listing:
    """The entries of a directory that were listed before DSM changed it, in the form of the
    iterator that `os.scandir` returns."""

    def __init__(self, entries: list):
        self._entries = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._entries = iter(())


@contextlib.contextmanager
def dsm_removes_files_after_listing(below: Path, name: str = "@eaDir"):
    """Remove the files of each directory named `name` below `below` right after a listing of
    that directory, so that a copy that listed the directory finds its files gone. DSM does the
    same when it rewrites what it keeps in an `@eaDir`. Yields the list of the files removed."""
    real_scandir = os.scandir
    below = os.path.abspath(below)
    removed = []

    def scandir(path="."):
        if isinstance(path, int):
            return real_scandir(path)
        listed = os.path.abspath(os.fsdecode(path))
        if os.path.basename(listed) != name or not listed.startswith(below + os.sep):
            return real_scandir(path)
        with real_scandir(path) as entries:
            found = list(entries)
        for entry in found:
            if entry.is_file(follow_symlinks=False):
                os.unlink(entry.path)
                removed.append(entry.path)
        return _Listing(found)

    with mock.patch("os.scandir", scandir):
        yield removed


@contextlib.contextmanager
def git_version_fails(times: int | None = None):
    """Make `git --version` fail to start `times` times, or every time with None, as a fork fails
    on the NAS with BlockingIOError, and skip the wait between two tries. Every other command
    runs. Yields the list of the failures."""
    from agentcontext.git import clone
    real_run = subprocess.run
    failures = []

    def run(args, *rest, **kwargs):
        if list(args) == ["git", "--version"] and (times is None or len(failures) < times):
            failures.append(args)
            raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        return real_run(args, *rest, **kwargs)

    # `create=True` lets the fixture also patch a module without the wait, as when a test runs
    # against the code without its fix to show that it fails.
    with mock.patch("subprocess.run", run), \
            mock.patch.object(clone, "GIT_VERSION_WAIT", 0, create=True):
        yield failures


@contextlib.contextmanager
def file_system_sync_never_returns():
    """Make a sync of a whole file system raise as soon as it starts, `sync(2)` through
    `os.sync` and `syncfs(2)` through the C library that `ctypes` loads. The error stands for
    a sync that waits for every write request pending on the volume and does not return. An
    `fsync(2)` of one file runs."""
    message = "the sync of the file system did not return"

    class Library:
        def __init__(self, *args, **kwargs):
            pass

        def syncfs(self, fd):
            raise TimeoutError(message)

    with mock.patch("os.sync", side_effect=TimeoutError(message)), \
            mock.patch("ctypes.CDLL", Library):
        yield


# Nested deeper than the parsers of the standard library read: `json` raises RecursionError
# when it nears the end of the C stack, and `tomllib` at the interpreter's recursion limit.
DEEP_JSON = "[" * 1_000_000 + "]" * 1_000_000
DEEP_TOML = "a = " + "[" * 3000 + "]" * 3000 + "\n"
# Nested less deeply than `json` reads and more deeply than the interpreter's recursion limit,
# which a Python function that reads the parsed value recursively reaches.
DEEP_SKILLS = '{"name": "p", "skills": ' + "[" * 3000 + "]" * 3000 + "}"


def deeply_nested_files() -> dict:
    """{repository-relative path: content} of a repository in which each file that a step
    parses is nested too deeply to read: the JSON and TOML settings, the Antigravity and Copilot
    hooks, a Copilot plugin and its marketplace, the portable Agent Plugins documents and two
    Claude Code plugins. A CLAUDE.md beside them keeps the repository configured."""
    return {
        "CLAUDE.md": "# Claude\n",
        ".claude/settings.json": DEEP_JSON,
        ".gemini/settings.json": DEEP_JSON,
        ".mcp.json": DEEP_JSON,
        ".codex/config.toml": DEEP_TOML,
        ".agents/hooks.json": DEEP_JSON,
        ".github/plugin/plugin.json": '{"name": "p"}',
        ".github/plugin/marketplace.json": DEEP_JSON,
        "hooks.json": DEEP_JSON,
        "plugin.json": DEEP_JSON,
        "mcp.json": DEEP_JSON,
        ".claude-plugin/plugin.json": DEEP_JSON,
        "skills-plugin/.claude-plugin/plugin.json": DEEP_SKILLS,
    }


# An integer literal with more digits than Python converts to an int by default
# (`sys.get_int_max_str_digits()`). `json` and `tomllib` raise a plain ValueError for it, which
# is neither a JSONDecodeError nor a TOMLDecodeError.
LONG_INTEGER = "7" * (sys.int_info.default_max_str_digits + 1)
LONG_INTEGER_JSON = '{"seed": ' + LONG_INTEGER + "}"
LONG_INTEGER_TOML = '[mcp_servers.x]\ncommand = "npx"\nseed = ' + LONG_INTEGER + "\n"


def files_with_long_integers() -> dict:
    """{repository-relative path: content} of the files of `deeply_nested_files`, with an integer
    literal that Python does not convert in place of each nesting that the parsers cannot read:
    an MCP server table in the TOML settings, and a key of a JSON object elsewhere."""
    replaced = {DEEP_JSON: LONG_INTEGER_JSON, DEEP_SKILLS: LONG_INTEGER_JSON,
                DEEP_TOML: LONG_INTEGER_TOML}
    return {rel: replaced.get(content, content) for rel, content in deeply_nested_files().items()}


class _GitHttp(http.server.BaseHTTPRequestHandler):
    """Serves the repositories under the server's `root` through `git http-backend` and records,
    per request, the (user, password) that the request sent, or None. With the server's `refuse`
    set to `token`, a request that sent a credential gets GitHub's response to a refused GitHub
    access token. With `address`, it gets GitHub's response to an address outside an
    organization's IP allow list."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path, _, query = self.path.partition("?")
        sent = None
        if self.headers.get("Authorization", "").startswith("Basic "):
            user, _, password = base64.b64decode(
                self.headers["Authorization"][len("Basic "):]).decode().partition(":")
            sent = (user, password)
        self.server.requests.append(sent)
        text = {"Content-Type": "text/plain"}
        if sent is None and self.server.refuse == "anonymous and token":
            # GitHub's response to an anonymous clone of a public repository "During periods of
            # unusual traffic", when it requires authentication (changelog of 2026-10-19).
            self._reply(401, b"Authentication required\n",
                        {**text, "WWW-Authenticate": 'Basic realm="GitHub"'})
            return
        if sent and self.server.refuse in ("token", "anonymous and token"):
            self._reply(401, b"Invalid username or token. Password authentication is not "
                             b"supported for Git operations.\n",
                        {**text, "WWW-Authenticate": 'Basic realm="GitHub"'})
            return
        if sent and self.server.refuse == "address":
            self._reply(403, b"Although you appear to have the correct authorization "
                             b"credentials, the `o` organization has an IP allow list enabled, "
                             b"and your IP address is not permitted to access this resource.\n",
                        text)
            return
        env = {"PATH": os.environ["PATH"], "HOME": self.server.root,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_PROJECT_ROOT": self.server.root,
               "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command,
               "PATH_INFO": path, "QUERY_STRING": query, "REMOTE_ADDR": "127.0.0.1",
               "CONTENT_TYPE": self.headers.get("Content-Type", ""),
               "CONTENT_LENGTH": str(len(body)),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", "")}
        output = subprocess.run(["git", "http-backend"], input=body, env=env,
                                capture_output=True).stdout
        head, _, payload = output.partition(b"\r\n\r\n")
        status, headers = 200, {}
        for line in head.decode().splitlines():
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.split()[0])
            else:
                headers[key] = value.strip()
        self._reply(status, payload, headers)

    do_GET = do_POST = _serve


@contextlib.contextmanager
def github_on_loopback(root: Path, refuse: str | None = None):
    """Send the git requests for https://github.com/ to a loopback server of the bare repositories
    under `root`, such as `root/o/a.git` for o/a, and yield the server. Its `requests` lists the
    credential that each request sent. `refuse` makes the server refuse every request with a
    credential ("token"), refuse such a request for an IP allow list ("address"), or also
    refuse every request without a credential, as GitHub may for a public repository
    ("anonymous and token"). The askpass helper of `git.auth` prints the GitHub access token for
    the server's address as it does for github.com. No credential helper of the machine, such as
    osxkeychain on macOS, takes part, so that no test writes a credential into such a helper."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GitHttp)
    server.daemon_threads = True
    server.root, server.refuse, server.requests = str(root), refuse, []
    address = f"127.0.0.1:{server.server_address[1]}"
    home = root / "home"
    home.mkdir(exist_ok=True)
    redirect = {"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"url.http://x-access-token@{address}/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://x-access-token@github.com/",
                "GIT_CONFIG_KEY_1": f"url.http://{address}/.insteadOf",
                "GIT_CONFIG_VALUE_1": "https://github.com/"}
    from agentcontext.git import auth
    apply = auth.apply
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with mock.patch.dict(os.environ, redirect), \
                mock.patch.object(auth, "apply",
                                  lambda env, token, host=address: apply(env, token, host)):
            yield server
    finally:
        server.shutdown()
        server.server_close()
        auth.cleanup()
