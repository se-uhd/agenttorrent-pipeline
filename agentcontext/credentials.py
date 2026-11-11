"""The GitHub access tokens, OpenAI API keys and OpenRouter API keys with which the pipeline
calls other services, read from the environment and from one file, `credentials.csv`.

The file begins with the header `service,account,credential`, and each row below it holds one
credential:

    service,account,credential
    github,sbaltes,github_pat_...
    openai,se-uhd,sk-svcacct-...
    openrouter,se-uhd,sk-or-v1-...

`service` is `github`, `openai` or `openrouter`. `account` names who owns the credential and may
be empty. GitHub applies its rate limits per account, and two GitHub access tokens of one account
share one rate limit. OpenAI applies its rate limits per organization or project, and OpenRouter
tracks credit per account and per key. A line below the header that starts with `#` is a
comment, such as an example row that is not filled in. When the first line of the file is not
the header, `read` returns a problem and none of the credentials in the file. The file is never
committed (`.gitignore`) and never printed. The message for a problem names its line number and
the reason, never the text of the line or of one of its cells.

The file is at the repository root or at the path that `AGENTCONTEXT_CREDENTIALS` names, which
the container sets to the file mounted from its project folder. A library function reads the
file only at a path that it is given, which keeps a suite that passes no path from reading a
developer's real file. A command line passes `default_path()`. The environment comes first,
`GITHUB_TOKENS` (comma-separated) and `GITHUB_TOKEN` for GitHub, `OPENAI_API_KEY` for OpenAI
and `OPENROUTER_API_KEY` for OpenRouter, and the rows of the file follow without repeats. A value from the environment is
checked against its service's format, as a row of the file is. The message for a value that
fails names its variable and its position.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agentcontext import REPO_ROOT

FILE_NAME = "credentials.csv"
PATH_VARIABLE = "AGENTCONTEXT_CREDENTIALS"
COLUMNS = ("service", "account", "credential")

GITHUB, OPENAI, OPENROUTER = "github", "openai", "openrouter"
# The format of a credential of each service. A GitHub access token consists of letters, digits
# and underscores (`github_pat_...`, `ghp_...`), an OpenAI API key starts with `sk-` and contains
# hyphens, and an OpenRouter API key starts with `sk-or-` (`sk-or-v1-...`). No OpenAI API key
# starts with `sk-or-`, and the format of an OpenAI API key excludes that prefix, so that an
# OpenRouter API key in a row of `openai` is a problem rather than a key that OpenAI rejects at
# the first request.
FORMATS = {GITHUB: re.compile(r"[A-Za-z0-9_]+"),
           OPENAI: re.compile(r"sk-(?!or-)[A-Za-z0-9_\-]+"),
           OPENROUTER: re.compile(r"sk-or-[A-Za-z0-9_\-]+")}
# The name of a credential of each service in a problem or a log message.
NAMES = {GITHUB: "a GitHub access token", OPENAI: "an OpenAI API key",
         OPENROUTER: "an OpenRouter API key"}
# The environment variables from which each service's credentials are read, in order.
# `SERVICE_OF` maps each variable back to its service, for a step that declares a variable that
# it needs.
ENVIRONMENT = {GITHUB: ("GITHUB_TOKENS", "GITHUB_TOKEN"), OPENAI: ("OPENAI_API_KEY",),
               OPENROUTER: ("OPENROUTER_API_KEY",)}
SERVICE_OF = {name: service for service, names in ENVIRONMENT.items() for name in names}


@dataclass(frozen=True)
class Credential:
    """One credential. Its value never appears in a repr, a log line or an error."""

    service: str
    account: str
    value: str = field(repr=False)

    def label(self) -> str:
        """The name of the credential in a log message: the service and the account, never the
        value."""
        return f"{self.service} credential of {self.account or 'an unnamed account'}"


def default_path(environ=None) -> Path:
    """`credentials.csv` at the repository root, or the path that `AGENTCONTEXT_CREDENTIALS`
    names."""
    environ = os.environ if environ is None else environ
    named = environ.get(PATH_VARIABLE)
    return Path(named) if named else REPO_ROOT / FILE_NAME


def read(path: Path | None) -> tuple[list, list]:
    """The credentials in the file at `path` and the problems with the file. Both lists are
    empty when there is no path or no file.

    A byte-order mark is dropped, because an editor on Windows writes one. A path that is a
    directory is a problem, since Docker creates a directory when the file that a mount names is
    missing.
    """
    if path is None:
        return [], []
    path = Path(path)
    if path.is_dir():
        return [], [f"{path} is a directory, not a file. Docker created it because the file did "
                    f"not exist when the container started. Remove the directory and write the "
                    f"file."]
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return [], []
    except OSError as error:
        return [], [f"{path} could not be read ({error})"]
    lines = [(number, raw.replace("﻿", "")) for number, raw in
             enumerate(text.splitlines(), 1) if raw.replace("﻿", "").strip()]
    if not lines:
        return [], []
    header_number, first = lines[0]
    if tuple(cell.strip() for cell in next(csv.reader([first]))) != COLUMNS:
        return [], [f"{path} line {header_number} is not the header {','.join(COLUMNS)}, which "
                    f"has to be the first line of the file"]
    lines = [(number, line) for number, line in lines[1:] if not line.lstrip().startswith("#")]
    credentials, problems = [], []
    rows = list(csv.reader(io.StringIO("\n".join(line for _, line in lines))))
    for (number, _), row in zip(lines, rows):
        cells = [cell.strip() for cell in row]
        if len(cells) != len(COLUMNS):
            problems.append(f"{path} line {number} has {len(cells)} fields, not "
                            f"{len(COLUMNS)}")
            continue
        service, account, value = cells
        if service not in FORMATS:
            # The message leaves out the cell, which is the credential in a row with its columns
            # swapped.
            problems.append(f"{path} line {number} names a service other than "
                            f"{_services()}")
            continue
        if not FORMATS[service].fullmatch(value):
            problems.append(f"{path} line {number} is not {NAMES[service]}, because the value "
                            f"is empty or has a character that {NAMES[service]} cannot "
                            f"contain")
            continue
        credentials.append(Credential(service, account, value))
    return credentials, problems


def _services() -> str:
    """The services of FORMATS as a problem names them: "github, openai or openrouter"."""
    names = sorted(FORMATS)
    return f"{', '.join(names[:-1])} or {names[-1]}"


def _environment_values(service: str, environ) -> list:
    """Every value of the variables of `service` in the environment, in the order of
    ENVIRONMENT, as a tuple of its variable, its position among the variable's comma-separated
    values, the number of these values and the value itself. An empty value is left out."""
    values = []
    for name in ENVIRONMENT[service]:
        parts = str(environ.get(name) or "").split(",")
        values += [(name, position, len(parts), part.strip())
                   for position, part in enumerate(parts, 1) if part.strip()]
    return values


def from_environment(service: str, environ=None) -> list:
    """The credentials of `service` that the environment contains, in the order of
    ENVIRONMENT, without the values that do not have the service's format, which
    `environment_problems` names. Sent to a service, such a value, for example two GitHub
    access tokens separated by a line break, would make requests refuse the header and quote
    the value in its error."""
    environ = os.environ if environ is None else environ
    return [Credential(service, "", value)
            for _, _, _, value in _environment_values(service, environ)
            if FORMATS[service].fullmatch(value)]


def environment_problems(environ=None) -> list:
    """The problems with the credentials of the environment: each value that does not have its
    service's format, named by its variable and its position, never by its text."""
    environ = os.environ if environ is None else environ
    return [f"Value {position} of {count} in {name} does not have the form of "
            f"{NAMES[service]} and is not used. Separate several values with commas."
            for service in ENVIRONMENT
            for name, position, count, value in _environment_values(service, environ)
            if not FORMATS[service].fullmatch(value)]


def of_service(service: str, path: Path | None = None, environ=None) -> list:
    """Every credential of `service`, the environment's credentials first and then the file's,
    without repeats. `read` returns the problems with the file, which a command reports.

    A credential that is in both keeps the account that the file names. The runner's pre-checks
    set `GITHUB_TOKEN`, which every later step inherits, to the first GitHub access token that
    GitHub accepts. Without its account, that GitHub access token would not be paused with the
    other GitHub access tokens of its account. """
    in_file = [credential for credential in read(path)[0] if credential.service == service]
    accounts = {credential.value: credential.account for credential in in_file}
    seen, unique = set(), []
    for credential in from_environment(service, environ) + in_file:
        if credential.value not in seen:
            seen.add(credential.value)
            unique.append(Credential(service, accounts.get(credential.value, credential.account),
                                     credential.value))
    return unique


def github_tokens(path: Path | None = None, environ=None) -> list:
    """The GitHub access tokens, as `Credential`s."""
    return of_service(GITHUB, path, environ)


def openai_keys(path: Path | None = None, environ=None) -> list:
    """The OpenAI API keys, as `Credential`s."""
    return of_service(OPENAI, path, environ)


def openrouter_keys(path: Path | None = None, environ=None) -> list:
    """The OpenRouter API keys, as `Credential`s."""
    return of_service(OPENROUTER, path, environ)
