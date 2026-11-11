"""The stated statistics: `python -m agentcontext statistics [--check | --collection]`.

Every number that the maintained documentation states comes from one file,
`docs/statistics.json`, and every entry of the file has a source that computes its value from
data. A document marks a stated statistic with the key that names it,
`<!--s:example.repositories-->1,234<!--/s-->`, and this command writes the value of every stated
statistic from the file. An entry has one source of the kinds below, and
`docs/tools/statistics.md` gives the format of every source:

- a file source, `rows`, `size`, `json`, `yaml` or `constant`, which reads a file of the
  checkout
- a `function` source, a computation of `agentcontext/statistics/` over the checkout
- a `collection` source, a computation over the data root of the working copy of a handover,
  which this command reads from the collection statistics file
- a `vendor` source, the number in a verbatim quote of a vendor page

The sources `percent`, `quotient`, `difference`, `product` and `sum` compute a value from other
keys.
`meaning` states what the statistic is. A value is written with thousands separators, or without
them with `plain`, as a port or a year is. `scale` multiplies it first, so that bytes can be
written in megabytes and a fraction as a percent. `decimals` gives the decimals, and `percent`
adds a percent sign. A marker can request a whole number below one hundred as words,
`<!--s:KEY:words-->` for "three repositories" and `:Words` at the start of a sentence, or as an
ordinal, `:ordinal` for "the sixth page" and `:Ordinal`.

Without --check, the computed values are written into the file and every stated statistic into
its document. The exit code is 2 when a difference is left that writing does not fix: a marker
with a key that the file lacks, a marker that does not parse, a statistic that no document
states, a number with a thousands separator or a percent sign outside a marker, or a value of the
collection that the collection statistics file lacks. With --check nothing is written, and the
exit code is 0 when every computed value, every stated statistic and every key agree and no
number is unmarked, and 2 when one differs. In both modes, the exit code is 1 when a value could
not be computed, such as a share with a base of 0, or when the documents could not be listed.

With --collection and --archive, the command computes every `collection` source over the data
root of a working copy and writes the collection statistics file, and nothing else. It exits 0
when it wrote the file, and 1 when a value could not be computed, the data root has no snapshot,
or --archive is missing or names no file.
"""

import argparse
import json
import math
import re
import subprocess
from pathlib import Path

import yaml

from agentcontext import DATA_ROOT, REPO_ROOT, logs
from agentcontext import cli
from agentcontext.statistics import (COLLECTION_STATISTICS, Files, Unavailable, call, call_key,
                                     code_digest, file_digest, lookup)

COMPONENT = "statistics"
STATISTICS_PATH = Path("docs") / "statistics.json"
# The documentation that the statistics are written into: the tracked Markdown files and the
# release notes of the website, the plans among them, because a paper takes its numbers from a
# plan. The history is a dated record of what was measured when it was written, and the rest are
# collected files and data, not documentation. Of the CHANGELOG, only `## [Unreleased]` is
# documentation: A released section is the notes of a published release and keeps the
# statistics of that release.
DOCUMENTS = ("*.md", "_website/index.html")
RELEASE_NOTES = "CHANGELOG.md"
UNRELEASED = re.compile(r"^## \[Unreleased\][^\n]*\n(?:(?!^## \[).*\n?)*", re.M)
NOT_DOCUMENTATION = ("docs/history/", "data/collection/data/", "data/classification/labels/",
                     "data/classification/evaluation/")

KEY = r"[A-Za-z0-9_.]+"
FORMS = ("words", "Words", "ordinal", "Ordinal")
MARKER = re.compile(rf"<!--s:({KEY})(?::({'|'.join(FORMS)}))?-->(.*?)<!--/s-->")
# The two halves of a marker, which outside a marker are a marker that does not parse. An
# opening half ends at its `-->`, or after 40 characters of a line without one.
MARKER_PART = re.compile(r"<!--s:(?:[^\n]*?-->|[^\n]{0,40})|<!--/s-->")
# A number written as a count with a thousands separator or as a share. A smaller integer is
# too often a version, an HTTP status or a step's number to be distinguished from a statistic.
UNMARKED_NUMBER = re.compile(r"(?<![\w.,/#-])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s?%)(?![\w/-])")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
# A code span opens with a run of backticks and closes at the next run of as many, which lets
# `` ` `` contain a backtick. It may continue over a line break, though not over a blank line.
CODE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)*?)(?<!`)\1(?!`)", re.S)
URL = re.compile(r"https?://\S+")
# An HTML tag or comment. A `<` that a letter, `/` or `!` does not follow is a comparison in
# prose, and the text up to the next `>` is prose as well.
TAG = re.compile(r"<!--.*?-->|<[!/]?[A-Za-z][^<>]*>", re.S)
STYLE_OR_SCRIPT = re.compile(r"<(style|script)\b.*?</\1>", re.S | re.I)
# A verbatim quotation of the maintainer: straight double quotes between `<!--maintainer-->` and
# `<!--/maintainer-->`, which may continue over a line break, though not over a blank line. The
# maintainer's words come from no source that computes a number, and a marker would let the
# command rewrite them. A quotation of a vendor page is prose, and a number in it takes a marker
# with a `vendor` source.
MAINTAINER_QUOTE = re.compile(r'<!--maintainer-->"(?:(?!\n[ \t]*\n).)*?"<!--/maintainer-->',
                              re.S)
# The statistics computed from other statistics, by the operation on their values.
DERIVED = {"percent": lambda values: 100 * values[0] / values[1],
           "quotient": lambda values: values[0] / values[1],
           "difference": lambda values: values[0] - values[1],
           "product": math.prod,
           "sum": sum}
# The keys that a source may have.
SOURCE_KEYS = {"rows", "where", "distinct", "size", "json", "pointer", "length", "yaml",
               "constant", "name", "index", "function", "collection", "args", "vendor", "quote",
               "pattern", *DERIVED}
# A number in the quote of a vendor page: digits with thousands separators and decimals.
VENDOR_NUMBER = re.compile(r"(?<![\w.])((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w])")
# The keys of an entry: its value, what it means, its source, and how it is written. A value
# without a source under `from` would state a number that no data computes.
ENTRY_KEYS = {"value", "meaning", "from", "scale", "decimals", "percent", "plain"}


def code_ranges(text: str) -> list[tuple[int, int]]:
    """Where the code blocks and code spans of a document are. A marker there is an example of
    a marker, not a stated statistic."""
    ranges = [match.span() for match in FENCE.finditer(text)]
    outside = FENCE.sub(lambda match: " " * len(match.group(0)), text)
    return ranges + [match.span() for match in CODE.finditer(outside)]


def load(path: Path) -> dict:
    """The statistics, by key, in the file's order."""
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Unavailable(f"Could not read {path} ({error})") from error
    for key, entry in entries.items():
        if not re.fullmatch(KEY, key):
            raise Unavailable(f"The key {key!r} of {path} is not words joined by dots")
        if "from" not in entry or "value" not in entry:
            raise Unavailable(f"The statistic {key} of {path} needs a value and a source under "
                              f"the key from")
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            raise Unavailable(f"The statistic {key} of {path} has the keys {sorted(unknown)}, "
                              f"which no statistic has")
    return entries


WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen", "twenty")
TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy",
        80: "eighty", 90: "ninety"}


def number_word(value: int) -> str | None:
    """A whole number below one hundred in words, as prose writes it: "twenty-three"."""
    if 0 <= value < len(WORDS):
        return WORDS[value]
    if 20 < value < 100:
        tens, units = divmod(value, 10)
        return TENS[tens * 10] + (f"-{WORDS[units]}" if units else "")
    return None
ORDINALS = ("zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
            "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
            "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth", "twentieth")


def render(entry: dict, form: str | None = None) -> str:
    """A value as the documents write it: scaled, with thousands separators and the decimals
    requested, a percent sign for a share, or in one of FORMS, a word or an ordinal for a small
    whole number."""
    value = entry["value"]
    if isinstance(value, str):
        return value
    if "scale" in entry:
        # 0.07 * 100 is 7.000000000000001 in binary floating point.
        value = round(value * entry["scale"], 9)
    decimals = entry.get("decimals")
    if decimals is None and isinstance(value, float) and value.is_integer():
        value = int(value)
    if form and decimals is None and isinstance(value, int):
        if form.lower() == "words" and number_word(value):
            word = number_word(value)
        elif form.lower() == "ordinal" and 0 <= value < len(ORDINALS):
            word = ORDINALS[value]
        else:
            word = f"{value:,}"
        return word.capitalize() if form[0].isupper() else word
    separator = "" if entry.get("plain") else ","
    text = (f"{value:{separator}.{decimals}f}" if decimals is not None
            else f"{value:{separator}}")
    return text + ("%" if entry.get("percent") else "")


def _pointer(document, pointer: str, label: str):
    value = document
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise Unavailable(f"{label} has nothing at {pointer}") from error
    return value


def _count_rows(files: Files, specs: list) -> dict:
    """The counts of every `rows` source over `files`, each CSV read once: {key: count}."""
    by_path: dict = {}
    for key, source in specs:
        by_path.setdefault(source["rows"], []).append((key, source))
    counts = {}
    for relative, sources in by_path.items():
        tallies = {key: (set() if source.get("distinct") else 0) for key, source in sources}
        header = files.header(relative)
        for _key, source in sources:
            wanted = set(source.get("where", {})) | ({source["distinct"]}
                                                     if source.get("distinct") else set())
            missing = wanted - set(header)
            if missing:
                raise Unavailable(f"{relative} has no column {sorted(missing)}")
        try:
            for row in files.rows(relative):
                for key, source in sources:
                    if all(row[column] == value
                           for column, value in source.get("where", {}).items()):
                        if source.get("distinct"):
                            tallies[key].add(row[source["distinct"]])
                        else:
                            tallies[key] += 1
        except OSError as error:
            raise Unavailable(f"Could not read {relative} ({error})") from error
        for key, tally in tallies.items():
            counts[key] = len(tally) if isinstance(tally, set) else tally
    return counts


def vendor_value(source: dict, key: str):
    """The number that a vendor page states, read from the verbatim quote of the page: the
    first group of `pattern` in the quote, or the one number of the quote."""
    quote = source.get("quote", "")
    if "pattern" in source:
        match = re.search(source["pattern"], quote)
        numbers = [match.group(1)] if match else []
    else:
        numbers = VENDOR_NUMBER.findall(quote)
    if len(numbers) != 1:
        raise Unavailable(f"The quote of the statistic {key} states {len(numbers)} numbers, "
                          f"and a pattern has to pick one")
    text = numbers[0].replace(",", "")
    return float(text) if "." in text else int(text)


def collection_values(entries: dict, root: Path) -> tuple[dict, list]:
    """The values of the `collection` sources that the collection statistics file of the
    checkout's data root records, and a difference for each value that the file lacks or that
    another snapshot or other code computed. A value belongs to the snapshot of the collection
    that the checkout tracks and to the current code of its computation."""
    wanted = {key: entry["from"] for key, entry in entries.items()
              if "collection" in entry.get("from", {})}
    if not wanted:
        return {}, []
    statistics_path = root / "data" / COLLECTION_STATISTICS
    snapshot = root / "data" / "collection" / "output" / "snapshot.json"
    remedy = ("Compute the values over the working copy of the handover with python -m "
              "agentcontext statistics --collection --archive HANDOVER and copy "
              "collection/collection_statistics.json into data/collection/.")
    try:
        recorded_file = json.loads(statistics_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, [f"{statistics_path.relative_to(root)} cannot be read, so {len(wanted):,} "
                    f"statistics of the collection have no value. {remedy}"]
    values, differences = {}, []
    recorded = recorded_file.get("derived_from", {}).get("output/snapshot.json")
    current = file_digest(snapshot) if snapshot.is_file() else None
    if recorded != current:
        return {}, [f"{statistics_path.relative_to(root)} was computed over another snapshot than "
                    f"data/collection/output/snapshot.json. {remedy}"]
    stored = recorded_file.get("values", {})
    digests: dict = {}
    for key, source in wanted.items():
        name = source["collection"]
        kind, _function = lookup(name)
        if kind != "collection":
            raise Unavailable(f"The statistic {key} names {name}, which reads the checkout, in "
                              f"a collection source")
        if name not in digests:
            digests[name] = code_digest(name)
        item = stored.get(call_key(name, source.get("args", {})))
        if item is None or item.get("code") != digests[name]:
            differences.append(f"The collection statistics file has no value of the statistic "
                               f"{key} computed by the current code of {name}. {remedy}")
            continue
        values[key] = item["value"]
    return values, differences


def compute(entries: dict, root: Path, differences: list | None = None) -> dict:
    """The value of every statistic, by key. Raises Unavailable when one cannot be computed. A
    value of the collection that the collection statistics file lacks is left out and named in
    `differences`."""
    files = Files(root)
    values = _count_rows(files, [(key, entry["from"]) for key, entry in entries.items()
                                 if "rows" in entry["from"]])
    stored, missing = collection_values(entries, root)
    values.update(stored)
    if differences is not None:
        differences.extend(missing)
    for key, entry in entries.items():
        source = entry["from"]
        unknown = set(source) - SOURCE_KEYS
        if unknown:
            raise Unavailable(f"The statistic {key} has the unknown source keys "
                              f"{sorted(unknown)}")
        if "json" in source:
            document = files.json(source["json"])
            value = _pointer(document, source.get("pointer", ""), source["json"])
            values[key] = len(value) if source.get("length") else value
        elif "yaml" in source:
            document = yaml.safe_load(files.text(source["yaml"]))
            values[key] = _pointer(document, source.get("pointer", ""), source["yaml"])
        elif "constant" in source:
            values[key] = files.constant(source["constant"], source["name"],
                                         source.get("index"), source.get("length", False))
        elif "size" in source:
            values[key] = files.size(source["size"])
        elif "function" in source:
            kind, _function = lookup(source["function"])
            if kind != "checkout":
                raise Unavailable(f"The statistic {key} names {source['function']}, which "
                                  f"reads a data root, in a function source")
            values[key] = call(source["function"], source.get("args", {}), files)
        elif "vendor" in source:
            values[key] = vendor_value(source, key)
        elif not ({"rows", "collection"} | set(DERIVED)) & set(source):
            raise Unavailable(f"The statistic {key} names no known source (its keys are "
                              f"{sorted(source)})")

    def derived(key, seen=()):
        if key in values:
            return values[key]
        if key not in entries:
            raise Unavailable(f"A statistic refers to {key}, which is not in the statistics file")
        if key in seen:
            raise Unavailable(f"The statistic {key} is computed from itself")
        source = entries[key]["from"]
        if "collection" in source:
            raise Unavailable(f"A statistic is computed from {key}, which has no value of the "
                              f"collection")
        name = next(name for name in DERIVED if name in source)
        operands = [derived(other, (*seen, key)) for other in source[name]]
        if name in ("percent", "quotient") and operands[1] == 0:
            relation = "is a share of" if name == "percent" else "divides by"
            raise Unavailable(f"The statistic {key} {relation} {source[name][1]}, which is 0")
        values[key] = DERIVED[name](operands)
        return values[key]

    for key, entry in entries.items():
        if "collection" not in entry["from"] or key in values:
            derived(key)
    return {key: values[key] for key in entries if key in values}


def write_collection(entries: dict, data_root: Path, archive: Path) -> int:
    """Compute every `collection` source over a data root and write the values to its collection
    statistics file, with the SHA-256 of the snapshot of the collection and of the code of each
    computation, and the name, the size and the SHA-256 of `archive`, the handover that the data
    root was extracted from. Returns the number of values written."""
    snapshot = data_root / "collection" / "output" / "snapshot.json"
    if not snapshot.is_file():
        raise Unavailable(f"{data_root} has no collection/output/snapshot.json, so the values "
                          f"would belong to no snapshot")
    # The documents state the handover from this record, and a file without it would trace its
    # values to no archive.
    if not archive.is_file():
        raise Unavailable(f"The handover {archive} is not a file")
    files = Files(data_root)
    values, digests = {}, {}
    for key, entry in entries.items():
        source = entry.get("from", {})
        if "collection" not in source:
            continue
        name, args = source["collection"], source.get("args", {})
        kind, _function = lookup(name)
        if kind != "collection":
            raise Unavailable(f"The statistic {key} names {name}, which reads the checkout, in "
                              f"a collection source")
        if name not in digests:
            digests[name] = code_digest(name)
        item = call_key(name, args)
        if item not in values:
            values[item] = {"value": call(name, args, files), "code": digests[name]}
    derived_from = {"output/snapshot.json": file_digest(snapshot),
                    "handover": {"archive": archive.name, "bytes": archive.stat().st_size,
                                 "sha256": file_digest(archive)}}
    written = {"derived_from": derived_from, "values": dict(sorted(values.items()))}
    target = data_root / COLLECTION_STATISTICS
    target.write_text(json.dumps(written, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(values)


def documents(root: Path) -> list[Path]:
    """The documentation that the statistics are written into: tracked files matching
    DOCUMENTS."""
    proc = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", "-z", "--",
                           *DOCUMENTS], cwd=str(root), capture_output=True, text=True)
    if proc.returncode != 0:
        raise Unavailable(f"git ls-files exited {proc.returncode} ({proc.stderr.strip()})")
    return [root / name for name in sorted(proc.stdout.split("\0"))
            if name and not name.startswith(NOT_DOCUMENTATION)]


def documented(relative: str, text: str) -> tuple[str, str, str]:
    """A document's text as (before, documentation, after): the whole text, or for the
    CHANGELOG its `## [Unreleased]` section, and nothing where it has none."""
    if relative != RELEASE_NOTES:
        return "", text, ""
    match = UNRELEASED.search(text)
    if match is None:
        return text, "", ""
    return text[:match.start()], match.group(0), text[match.end():]


def unmarked(text: str) -> list[str]:
    """The numbers that a document states outside a marker, a code block, a code span, a
    quotation of the maintainer, a style sheet or a script, a URL and an HTML tag. The code spans
    go before the quotations of the maintainer, so that a code span can name the markers of one.
    The quotations go before the URLs, which would otherwise take in the closing quote and
    marker, and before the tags, which would otherwise remove the markers and leave the
    quotation as prose."""
    for pattern in (FENCE, CODE, MAINTAINER_QUOTE, MARKER, STYLE_OR_SCRIPT, URL, TAG):
        text = pattern.sub(" ", text)
    return UNMARKED_NUMBER.findall(text)


def malformed(text: str) -> list[str]:
    """The halves of a marker that belong to no marker, outside a code block, a code span, a
    style sheet and a script: a key with another character than letters, digits, `_` and `.`,
    a form that FORMS lacks, a number over a line break, or one half without the other. Each
    would otherwise be taken for prose and its number left unchecked."""
    for pattern in (FENCE, CODE, STYLE_OR_SCRIPT, MARKER):
        text = pattern.sub(" ", text)
    return [match.group(0) for match in MARKER_PART.finditer(text)]


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="Write nothing, and exit 2 when a value, a stated statistic or a key "
                             "differs")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="The checkout (default: %(default)s)")
    parser.add_argument("--statistics", type=Path, default=None,
                        help=f"The statistics file (default: ROOT/{STATISTICS_PATH})")
    parser.add_argument("--collection", action="store_true",
                        help=f"Compute the statistics of the collection over the data root of "
                             f"the working copy of a handover and write them to "
                             f"DATA_ROOT/{COLLECTION_STATISTICS}, and nothing else")
    parser.add_argument("--archive", type=Path, default=None,
                        help="The handover that the working copy was extracted from, required "
                             "with --collection. The collection statistics file records its "
                             "name, size and SHA-256")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT,
                        help="The data root of the working copy, with --collection (default: "
                             "%(default)s)")
    args = parser.parse_args(argv)
    if args.collection and args.archive is None:
        logs.error(COMPONENT, "--collection needs --archive, the handover that the working copy "
                              "was extracted from.")
        return 1
    if args.archive is not None and not args.collection:
        logs.error(COMPONENT, "--archive applies only with --collection.")
        return 1
    root = args.root.resolve()
    path = args.statistics or root / STATISTICS_PATH
    if args.collection:
        try:
            written = write_collection(load(path), args.data_root.resolve(),
                                       args.archive.resolve())
        except Unavailable as error:
            logs.error(COMPONENT, f"{str(error).rstrip('.')}.")
            return 1
        logs.info(COMPONENT, f"Wrote {written:,} values of the collection to "
                             f"{args.data_root.resolve() / COLLECTION_STATISTICS}.")
        return 0
    # Writing fixes a value or a stated statistic that differs. A key without its statistic, a
    # statistic that no document states, a number without a marker and a value of the collection
    # that the collection statistics file lacks are for a person to fix.
    stale, unresolved = [], []
    try:
        entries = load(path)
        values = compute(entries, root, unresolved)
        files = documents(root)
    except Unavailable as error:
        logs.error(COMPONENT, f"{str(error).rstrip('.')}.")
        return 1
    for key, value in values.items():
        entry = {**entries[key], "value": value}
        if render(entry) != render(entries[key]) or type(entries[key]["value"]) is not type(value):
            stale.append(f"{path.name} records {key} as {render(entries[key])}, but the data "
                         f"gives {render(entry)}")
            entries[key] = entry
    used, rewritten = set(), {}
    for document in files:
        relative = document.relative_to(root).as_posix()
        whole = document.read_text(encoding="utf-8")
        before, text, after = documented(relative, whole)
        code = code_ranges(text)

        def replace(match):
            if any(start <= match.start() < end for start, end in code):
                return match.group(0)
            key, form, written = match.groups()
            used.add(key)
            if key not in entries:
                unresolved.append(f"{relative} marks a number with the key {key}, which is not "
                                  f"in {path.name}")
                return match.group(0)
            value = render(entries[key], form)
            if written != value:
                stale.append(f"{relative} writes {key} as {written}, but {path.name} gives "
                             f"{value}")
            return f"<!--s:{key}{':' + form if form else ''}-->{value}<!--/s-->"
        new = before + MARKER.sub(replace, text) + after
        unresolved += [f"{relative} states the number {number} without a marker"
                       for number in unmarked(text)]
        unresolved += [f"{relative} contains {part}, which does not parse as a marker"
                       for part in malformed(text)]
        if new != whole:
            rewritten[document] = new
    # A statistic that another statistic is computed from is stated through that statistic.
    for entry in entries.values():
        source = entry.get("from", {})
        for name in DERIVED:
            used.update(source.get(name, ()))
    unresolved += [f"No document states the statistic {key}" for key in entries if key not in used]
    for difference in (stale if args.check else []) + unresolved:
        logs.warning(COMPONENT, f"{difference.rstrip('.')}.")
    if args.check:
        logs.info(COMPONENT, f"Checked {len(entries):,} statistics, {len(used):,} of them stated "
                             f"in {len(files):,} documents, and found "
                             f"{len(stale) + len(unresolved):,} differences.")
        return 2 if stale or unresolved else 0
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for document, text in rewritten.items():
        document.write_text(text, encoding="utf-8")
    logs.info(COMPONENT, f"Wrote {len(values):,} computed statistics to {path} and the stated "
                         f"statistics of {len(rewritten):,} documents.")
    logs.info(COMPONENT, f"{len(unresolved):,} differences remain for a person to fix.")
    return 2 if unresolved else 0

if __name__ == "__main__":
    raise SystemExit(main())
