"""Computations over a CSV, a JSON file or a JSON Lines file of the checkout that the `rows` and
`json` sources cannot express: rows selected by conditions, the columns of a header, the number
in one cell, an aggregate of a column, and the keys that two CSVs share.

A record is a row of a CSV, an item of the list or a value of the object at a JSON pointer of a
JSON file, or a line of a JSON Lines file, and its columns are the columns of the row or the
fields of the item. The conditions that select records are the same in every computation:

- `where`: every column equal to its value
- `match`: every column matched by its regular expression
- `nonempty`: every column not empty
- `compare`: every column compared with an operand by `==`, `!=`, `<`, `<=`, `>` or `>=`, as
  `{"size_kb": [">", 4194304]}`. The operand is a number or a text, `{"column": NAME}` for the
  value of another column of the record, or `{"constant": PATH, "name": NAME}` for a top-level
  constant of a Python file. A text that is a number compares as the number, and an ordering
  comparison with an empty value fails. With `per`, the value of each column of `compare` is
  divided by the value of the column `per` first, as for the characters per token of a README.
- `absent_from` and `present_in`: the key of the record, the value of the column `key` or the
  values of the columns of a list `key`, in no CSV of `absent_from` and in some CSV of
  `present_in`
- `alone`: the key in no record of the same file that fails the conditions above, such as the
  repositories that one kind of row alone puts into a mechanism CSV

The records of a file are read once for a `Files`, so that the computations over one evaluation
or one output CSV share one pass.
"""

import operator
import re
import statistics

from agentcontext.statistics import Files, Unavailable, checkout

AGGREGATES = {"sum": sum, "mean": statistics.fmean, "median": statistics.median,
              "min": min, "max": max}
OPERATORS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
             ">": operator.gt, ">=": operator.ge}
JSON = (".json", ".json.xz", ".json.7z")
JSON_LINES = (".jsonl", ".jsonl.xz")


def records(files: Files, path: str, pointer: str = "") -> list:
    """The records of a file: the rows of a CSV, the dict items or values at a JSON pointer of a
    JSON file, or the lines of a JSON Lines file."""
    def read() -> list:
        if path.endswith(JSON):
            value = files.json(path)
            for part in pointer.split("/")[1:]:
                part = part.replace("~1", "/").replace("~0", "~")
                value = value[int(part)] if isinstance(value, list) else value[part]
            items = value.values() if isinstance(value, dict) else value
            return [item for item in items if isinstance(item, dict)]
        if path.endswith(JSON_LINES):
            return list(files.jsonl(path))
        return list(files.rows(path))
    return files.memo(("records", path, pointer), read)


def _number(value: object, label: str) -> object:
    """A number of a record: a JSON number as it is, a text read as an int or a float."""
    if not isinstance(value, str):
        return value
    try:
        return float(value) if any(sign in value for sign in ".eE") else int(value)
    except ValueError as error:
        raise Unavailable(f"{label} holds {value!r}, which is not a number") from error


def _comparable(value: object) -> object:
    """A value as `compare` compares it: a text that is a number as the number, an empty text
    as None."""
    if value == "":
        return None
    if isinstance(value, str):
        try:
            return _number(value, "")
        except Unavailable:
            return value
    return value


def holds(value: object, name: str, wanted: object) -> bool:
    """Whether `value` stands in the relation `name`, such as `<`, to `wanted`. An ordering
    comparison with a missing value fails."""
    if name not in OPERATORS:
        raise Unavailable(f"A condition compares with one of {sorted(OPERATORS)}, not {name!r}")
    if name not in ("==", "!=") and (value is None or wanted is None):
        return False
    return OPERATORS[name](value, wanted)


def _operand(files: Files, record: dict, operand: object) -> object:
    """The value of an operand of `compare` for a record."""
    if isinstance(operand, dict) and "column" in operand:
        return _comparable(record.get(operand["column"]))
    if isinstance(operand, dict) and "constant" in operand:
        return files.constant(operand["constant"], operand["name"])
    return _comparable(operand)


def selected(files: Files, rows: list, where: dict | None = None, match: dict | None = None,
             nonempty: list | None = None, compare: dict | None = None,
             per: str | None = None) -> list:
    """The records that fit `where`, `match`, `nonempty` and `compare`."""
    patterns = {column: re.compile(pattern) for column, pattern in (match or {}).items()}
    found = []
    for row in rows:
        if not all(row.get(column) == value for column, value in (where or {}).items()):
            continue
        if not all(pattern.search(str(row.get(column, ""))) for column, pattern in
                   patterns.items()):
            continue
        if not all(row.get(column) for column in nonempty or ()):
            continue
        if compare and not all(_compared(files, row, column, condition, per)
                               for column, condition in compare.items()):
            continue
        found.append(row)
    return found


def _compared(files: Files, row: dict, column: str, condition: list, per: str | None) -> bool:
    """Whether a record meets one condition of `compare`."""
    name, operand = condition
    value = _comparable(row.get(column))
    if per is not None and value is not None:
        value = value / _number(row[per], f"The column {per}")
    return holds(value, name, _operand(files, row, operand))


def _key(row: dict, key: str | list) -> tuple:
    """The key of a record: the values of its columns `key`."""
    return tuple(row[column] for column in ([key] if isinstance(key, str) else key))


def _keys(files: Files, paths: list, key: str | list) -> set:
    """The keys of the records of the files at `paths`."""
    return {_key(row, key) for path in paths for row in records(files, path)}


def _check_columns(files: Files, path: str | None, rows: list, *groups) -> None:
    """Raise Unavailable for a column of a condition that the header of a CSV lacks, which would
    otherwise select nothing without notice."""
    if path is None or path.endswith(JSON + JSON_LINES):
        return
    header = set(rows[0]) if rows else set(files.header(path))
    wanted = set()
    for group in groups:
        if isinstance(group, str):
            wanted.add(group)
        elif group:
            wanted.update(group)
    missing = wanted - header
    if missing:
        raise Unavailable(f"{path} has no column {sorted(missing)}")


@checkout
def columns(files: Files, path: str) -> int:
    """The columns of the header of a CSV."""
    return len(files.header(path))


@checkout
def rows(files: Files, path: str | None = None, pattern: str | None = None, pointer: str = "",
         where: dict | None = None, match: dict | None = None, nonempty: list | None = None,
         compare: dict | None = None, per: str | None = None, absent_from: list | None = None,
         present_in: list | None = None, key: str | list = "repo_name", alone: bool = False,
         distinct: str | None = None) -> int:
    """The records of a file, or of every CSV that the glob pattern `pattern` matches, that the
    conditions select, or the distinct values of the column `distinct` in them."""
    if (path is None) == (pattern is None):
        raise ValueError("rows needs either a path or a glob pattern")
    paths = [path] if path is not None else files.glob(pattern)
    if not paths:
        raise Unavailable(f"No file matches {pattern}")
    every = [row for relative in paths for row in records(files, relative, pointer)]
    _check_columns(files, path, every, where, match, nonempty, compare, per, distinct,
                   [] if absent_from is None and present_in is None and not alone else key)
    found = selected(files, every, where, match, nonempty, compare, per)
    if alone:
        chosen = {id(row) for row in found}
        others = {_key(row, key) for row in every if id(row) not in chosen}
        found = [row for row in found if _key(row, key) not in others]
    if absent_from is not None:
        excluded = _keys(files, absent_from, key)
        found = [row for row in found if _key(row, key) not in excluded]
    if present_in is not None:
        included = _keys(files, present_in, key)
        found = [row for row in found if _key(row, key) in included]
    if distinct is None:
        return len(found)
    return len({row[distinct] for row in found})


@checkout
def cell(files: Files, path: str, column: str, where: dict | None = None,
         match: dict | None = None) -> int | float:
    """The number in `column` of the one record of a file that `where` and `match` select."""
    every = records(files, path)
    _check_columns(files, path, every, column, where, match)
    found = selected(files, every, where, match)
    if len(found) != 1:
        raise Unavailable(f"{len(found)} rows of {path} fit {where or {}} {match or {}}, not one")
    return _number(found[0][column], f"The column {column} of {path}")


@checkout
def aggregate(files: Files, path: str, column: str, how: str, pointer: str = "",
              where: dict | None = None, match: dict | None = None) -> int | float:
    """The sum, mean, median, minimum or maximum of a numeric column over the records of a file
    that `where` and `match` select, leaving out the records without a value."""
    if how not in AGGREGATES:
        raise Unavailable(f"how is one of {sorted(AGGREGATES)}, not {how!r}")
    every = records(files, path, pointer)
    _check_columns(files, path, every, column, where, match)
    numbers = [_number(row[column], f"The column {column} of {path}")
               for row in selected(files, every, where, match)
               if row.get(column) not in ("", None)]
    if not numbers:
        raise Unavailable(f"No row of {path} fits {where or {}} {match or {}} with a value of "
                          f"{column}")
    return AGGREGATES[how](numbers)


@checkout
def shared_keys(files: Files, path: str, other: str, key: list,
                nonempty: list | None = None) -> int:
    """The keys, the values of the columns `key`, that records of both CSVs have with every
    column of `nonempty` filled, such as the files that two scripts both measured."""
    def keys(relative: str) -> set:
        """The keys of the records of one CSV with every column of `nonempty` filled."""
        return {_key(row, key) for row in selected(files, records(files, relative),
                                                   nonempty=nonempty)}
    return len(keys(path) & keys(other))
