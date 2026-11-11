"""Name repositories as directories, and render their timestamps in UTC.

# The directory name

A repository is stored under a directory named after it with the slash replaced, such as
``owner§repo`` for ``owner/repo``. This module defines the naming, and every step imports it
rather than spelling out the character.

``os.path.isdir`` tests whether an entry is a directory, not whether it is a repository. A
collected tree contains one directory per repository and the list of the step that wrote the
tree, and nothing else. Any other entry got there by accident, such as a ``.DS_Store``, a
staging directory from an interrupted run or an ``input.csv`` from an archive extracted in
place. A step that copied whatever it found would publish such an entry with the dataset.

A caller names the list that it expects in ``expected``, so that the file that it wrote a moment
earlier is not reported as a stray on every run. Naming the list there rather than filtering
afterwards keeps that decision in one place.

GitHub allows letters, digits, hyphen, underscore and dot in an owner or repository name. The
separator can therefore occur in neither half, and a name that contains it is unambiguous.

# The timestamps

``steps.detection`` normalizes `head_commit_date` and `created_at` in every CSV that it writes
and `commit_timestamp` in the commit CSVs. ``git.history`` renders `created_at` per
configuration artifact, and ``steps.file_history`` normalizes `head_commit_date` and
`created_at` in the rows that it fills. All of them convert through one function,
``utc_text``, so that a value read from one published CSV compares equal with the same value
read from another.

The output format is ``YYYY-MM-DDTHH:MM:SSZ``. A published CSV leaves blank a value that does
not parse (``to_utc_or_blank``), and the step logs the count and the repositories. git prints a
commit of the year 10000 as such a value, and a damaged repository can carry other dates that
do not parse. Refusing them would stop every run at the same repository, and a value kept in its
raw form would be the one value in another format in a column of UTC values. The file history
cache keeps git's text instead (``parse_and_convert_to_utc``), because a blank there records a
history that was not read, and the file history step reads such a history again.

git stores the time of a commit as seconds since the epoch and the committer's offset, and
``%cI`` prints those seconds shifted by the offset. Subtracting the offset gives the epoch
seconds back for any offset that git prints. That includes an offset of 24 hours or more, which
``datetime`` refuses as a time zone but a commit can record, because ``git fsck`` checks only
that the offset has four digits.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from agentcontext import logs

# The mapping to a directory name and back is exact, because the separator is neither a slash
# nor a character that GitHub allows in an owner or repository name.
SEPARATOR = "§"


# The characters besides the unreserved ones that RFC 3986 allows in a path segment. A link keeps
# them as they are, because `@`, `(`, `)`, `*` and `+` are valid there and encoding them would
# change links that work. A space, `#`, `?`, `%` and every character outside ASCII are encoded.
URL_PATH_SAFE = "/!$&'()*+,;=:@"


def url_path(text: str) -> str:
    """A branch or a path as it goes into a GitHub link, `/` kept as the separator."""
    return quote(text, safe=URL_PATH_SAFE)


def repo_name_to_dir(repo_name: str) -> str:
    """Convert 'owner/repo' to 'owner§repo' for use as a directory name."""
    return repo_name.replace("/", SEPARATOR)


def dir_name_to_repo(dir_name: str) -> str:
    """Convert 'owner§repo' back to 'owner/repo'."""
    return dir_name.replace(SEPARATOR, "/")


def is_repo_dir_name(name: str) -> bool:
    """Whether a directory entry name is the name of a repository directory."""
    return SEPARATOR in name


def split(root: str | os.PathLike,
          expected: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """Split the entries directly under `root` into repository directories and strays.

    Both lists are sorted. A stray is anything else at that level, whether a file or a
    directory, except a name in `expected`, which is in neither list. A symlink is a stray
    whatever its name. ``os.path.isdir`` follows a symlink, and a step that scanned through one
    would delete inside a tree that it was not given, such as the collected tree, which the
    cleanup never modifies.

    Callers report strays rather than dropping them silently, because a stray is either an
    accident to remove or a naming change that nobody documented.
    """
    repos, strays = [], []
    for name in sorted(os.listdir(root)):
        if name in expected:
            continue
        path = os.path.join(root, name)
        target = repos if (is_repo_dir_name(name) and os.path.isdir(path)
                           and not os.path.islink(path)) else strays
        target.append(name)
    return repos, strays


def _iso_for_fromisoformat(value: str) -> str:
    """Rewrite the ISO 8601 forms that datetime.fromisoformat rejects before Python 3.11.

    The rewrite changes no result on Python 3.14, which the pipeline runs on, because Python
    3.11 and later accept the whole grammar. The archived rows contain these forms, such as a
    trailing `Z`, and under Python 3.10 a `+0100` offset would be returned unconverted.
    """
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    # +HHMM or -HHMM at the very end, which 3.10 requires as +HH:MM.
    if len(value) > 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = value[:-2] + ":" + value[-2:]
    return value


# A timestamp with a numeric offset, as git's `%cI` prints it. The hours and the minutes of the
# offset may be any two digits, because git prints the offset that the commit stores.
_OFFSET_TIMESTAMP = re.compile(r"(?P<local>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
                               r"(?P<sign>[+-])(?P<hours>\d{2}):?(?P<minutes>\d{2})")


def utc_instant(value: str) -> datetime:
    """An ISO 8601 timestamp as an aware datetime in UTC. A value without an offset is read as
    UTC. Raises ValueError when the value does not parse or names an instant outside the years
    1 to 9999 that `datetime` holds.

    The function subtracts a numeric offset from the local time rather than giving it to
    `datetime` as a time zone. An offset of 24 hours or more, which git prints for a commit that
    records one, then gives the commit's epoch seconds as any other offset does. git prints a
    commit of the year 10000 in the offset -0100 as `9999-12-31T23:30:00-01:00`, and the same
    commit in the offset +0100 with the year 10000, which does not parse. Both raise ValueError,
    so that a caller handles the first as it handles the second.
    """
    value = value.strip()
    found = _OFFSET_TIMESTAMP.fullmatch(value)
    try:
        if found:
            offset = timedelta(hours=int(found["hours"]), minutes=int(found["minutes"]))
            local = datetime.fromisoformat(found["local"]).replace(tzinfo=timezone.utc)
            return local - offset if found["sign"] == "+" else local + offset
        dt = datetime.fromisoformat(_iso_for_fromisoformat(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError(f"{value!r} names an instant outside the years 1 to 9999") from error


def utc_text(value: str) -> str:
    """An ISO 8601 timestamp as `YYYY-MM-DDTHH:MM:SSZ`, and a blank value as the empty string.
    Raises ValueError where `utc_instant` does."""
    value = value.strip()
    return utc_instant(value).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def parse_and_convert_to_utc(value: str) -> str:
    """An ISO 8601 timestamp in UTC, or the value unchanged, with a warning, where it does not
    parse."""
    try:
        return utc_text(value)
    except (ValueError, TypeError):
        logs.warning("repos", f"Could not parse the timestamp {value!r}.")
        return value.strip()


def to_utc_or_blank(rows, columns: tuple) -> list:
    """Convert the values of `columns` in every row to UTC in place, and leave blank a value
    that does not parse. A row without one of the columns does not gain it.

    Returns `(row, column, value)` for each value left blank, with the value as it was read.
    """
    blanked = []
    for row in rows:
        for column in columns:
            if column not in row:
                continue
            value = (row[column] or "").strip()
            try:
                row[column] = utc_text(value)
            except (ValueError, TypeError):
                row[column] = ""
                blanked.append((row, column, value))
    return blanked


def blanked_dates_message(where: str, blanked: list) -> str:
    """The warning about the values that `to_utc_or_blank` left blank in `where`: their count,
    their columns, one of the values and every repository."""
    names = sorted({row.get("repo_name") or "" for row, _, _ in blanked})
    columns = " and ".join(sorted({column for _, column, _ in blanked}))
    return (f"{where} had {len(blanked):,} {columns} value(s) that are not ISO 8601 timestamps "
            f"in the years 1 to 9999, such as {blanked[0][2]!r}. Left them blank. The values "
            f"are in {len(names):,} repositories ({', '.join(names)}).")
