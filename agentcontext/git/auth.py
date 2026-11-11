#!/usr/bin/env python3
"""Pass a GitHub access token to git without putting it on the command line.

A clone URL of the form `https://<token>@github.com/owner/repo.git` would put the secret in the
process's own argv, where `ps` shows it, and in any message that echoes the command.
`_git_failed` in `git.history` logs the exception that it caught, and the text of a
`subprocess.TimeoutExpired` is the whole command line. A clone slower than the timeout would
therefore write the GitHub access token to stderr, which the runner copies into `run_logs/`.

Git runs a helper program to obtain credentials when a URL has a username and no password. The
URL therefore names a placeholder user, and the helper prints the password from an environment
variable. The helper is a shell script that prints the variable, written once per process
with mode 0700 and removed at exit. Nothing secret is ever an argument.

The authors' mining project jaws26-loop-engineering uses the same scheme.

Inputs and outputs: none. This module is a library for the steps that clone repositories.
"""

from __future__ import annotations

import atexit
import os
import tempfile
import threading

# Git runs a credential helper for the password only when the URL has a username. GitHub does
# not check the value when a GitHub access token authenticates the request, and `x-access-token`
# is the name that GitHub's own documentation uses.
PLACEHOLDER_USER = "x-access-token"

TOKEN_ENV_VAR = "GIT_ASKPASS_TOKEN"
HOST_ENV_VAR = "GIT_ASKPASS_HOST"
USER_ENV_VAR = "GIT_ASKPASS_USER"

# The only host that the GitHub access token is for.
TOKEN_HOST = "github.com"

# The git options with which a command sends the GitHub access token with its first request. git
# sends a credential only after the server responds with 401, which GitHub does not do for a public
# repository. Without these options, a request for a public repository is anonymous whether or not a
# GitHub access token is set. `http.proactiveAuth` (git 2.46 and later) makes git run the askpass
# script for the password before the first request. `credential.helper=` empties the list of
# credential helpers, so that the askpass script is the only source of the password and no helper,
# such as osxkeychain or store, keeps the GitHub access token that git approves after each request
# that succeeds. The setting applies to every URL that the command reaches. For a URL on another
# host, the script prints an empty password, which git would then send to that host with its first
# request. Only a command that names one URL on github.com and fetches no submodule, such as
# `git ls-remote`, takes these options. They contain no secret.
PROACTIVE_AUTH = ("-c", "credential.helper=", "-c", "http.proactiveAuth=basic")

# The script prints the GitHub access token only for the host that it belongs to, and only at
# the password prompt. For a submodule on any other host it prints an empty line, which fails the
# fetch rather than sending a GitHub access token to that host. The repository being measured
# writes its own submodule URLs, and the .gitmodules of a large project can point at
# googlesource.com, a GitLab instance or a self-hosted server. GIT_ASKPASS applies to every
# prompt that git issues.
#
# The prompt matters as much as the host. git prompts in two forms:
#
#   Username for 'https://github.com'
#   Password for 'https://x-access-token@github.com'
#
# A submodule URL in the ordinary form has no username, and git prompts for one first. A GitHub
# access token printed at that prompt would become the username. git would build the next prompt
# from it and pass that prompt to this script as an argument, and the secret would appear in a
# process's argv, where ps shows it. The username prompt therefore gets the placeholder, and only
# the password prompt gets the GitHub access token.
#
# The host is compared exactly rather than matched as a substring, because a prompt can contain
# more than one @: 'https://a@github.com@evil.example' names evil.example, not github.com.
_ASKPASS_SCRIPT = """#!/bin/sh
# The quoted URL, then its host: everything after the last '@', or after '://' when there is no
# '@'. Comparing that to the expected host is exact where a substring test is not. The quote is
# held in a variable because a backslash escape here does not survive the Python string.
q="'"
url=${1#*$q}
url=${url%%$q*}
host=${url##*@}
host=${host##*://}
if [ "$host" != "$GIT_ASKPASS_HOST" ]; then
  echo ""
elif [ "${1#Username}" != "$1" ]; then
  echo "$GIT_ASKPASS_USER"
elif [ "${1#Password}" != "$1" ]; then
  echo "$GIT_ASKPASS_TOKEN"
else
  echo ""
fi
"""

_lock = threading.Lock()
_askpass_path: str | None = None


def clone_url(repo_name: str, token: str | None = None, use_ssh: bool = False) -> str:
    """Return the URL to clone from. It never contains the GitHub access token."""
    if use_ssh:
        return f"git@github.com:{repo_name}.git"
    if token:
        return f"https://{PLACEHOLDER_USER}@github.com/{repo_name}.git"
    return f"https://github.com/{repo_name}.git"


def askpass_script() -> str:
    """Return the path to this process's askpass helper, creating it on first use."""
    global _askpass_path
    with _lock:
        if _askpass_path is not None and os.path.exists(_askpass_path):
            return _askpass_path
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", prefix="git_askpass_", delete=False)
        handle.write(_ASKPASS_SCRIPT)
        handle.close()
        try:
            os.chmod(handle.name, 0o700)
        except OSError:
            os.unlink(handle.name)
            raise
        _askpass_path = handle.name
        atexit.register(cleanup)
        return _askpass_path


def cleanup() -> None:
    """Remove the helper script. Safe to call more than once."""
    global _askpass_path
    with _lock:
        if _askpass_path:
            try:
                os.unlink(_askpass_path)
            except OSError:
                pass
            _askpass_path = None


def discard() -> None:
    """Remove the helper script from a signal handler that ends the process with `os._exit`.

    `os._exit` runs no `atexit` handler, and without this call a stopped collection worker would
    leave its helper in the temporary directory. The function takes no lock, because a signal
    handler must not wait on one. The handler can interrupt `askpass_script` while that function
    holds the lock.
    """
    path = _askpass_path
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def apply(env: dict, token: str | None, host: str = TOKEN_HOST) -> dict:
    """Add the credential-helper variables to an environment. Returns the same dict.

    `host` is the only host for which the helper prints the GitHub access token. For every other
    host it prints an empty line.
    """
    if token:
        env["GIT_ASKPASS"] = askpass_script()
        env[TOKEN_ENV_VAR] = token
        env[HOST_ENV_VAR] = host
        env[USER_ENV_VAR] = PLACEHOLDER_USER
        # git translates its prompts, and the helper reads the first word to distinguish a
        # username prompt from a password prompt. Under another locale neither branch matches
        # and the helper prints an empty line, which fails the fetch without leaking the GitHub
        # access token. The C locale keeps the prompts in English, and the fetch succeeds.
        env.setdefault("LC_ALL", "C")
    return env


def redact(text: str) -> str:
    """Strip embedded credentials from a URL in text, such as git's error output."""
    import re
    return re.sub(r"(https://)[^@/\s]+@", r"\1", text)
