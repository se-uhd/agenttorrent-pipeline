"""The documentation watch: `python -m agentcontext docs`.

Detect a change of a vendor documentation page that a detection heuristic cites.

Every row of `config_path_patterns.csv` that cites a page cites the page that defines its
mechanism, and every `source=doc` row of `ai_commit_patterns.csv` cites the page that states how
a tool attributes the commits that it makes. Each heuristic is derived from its page, and after
the page changes, the heuristic may no longer match what the tool does.

The command fetches each cited page, hashes its text, and compares the hash with
`doc_hashes.json`. It logs the pages that changed and, with --update, records the new hashes.
The weekly `doc-watch.yml` workflow runs it. Read a changed page again, and if the behavior that
it documents changed, update the heuristic and its `doc_verified` date.

The command reads any heuristics file with a `doc_url` column. `--label-columns` names the
columns that identify a heuristic in the log: `tool,mechanism` for the path patterns and
`tool,scope` for the commit patterns. One `doc_hashes.json` serves every heuristics file,
because the record is keyed by URL. With --statistics, the command reads the statistics of
`docs/statistics.json` with a `vendor` source instead, each a page and the verbatim quote from
which the statistic reads its number, under the key of the statistic.

With --quotes, the command also checks each quotation in the `notes` of a row against the pages
that the row cites: its `doc_url`, the pages that its note links outside its quotations, and the
files that its `evidence` links. A string in double quotes inside a code span of a note is code,
not a quotation, and is not checked. A quotation found in none of the row's pages is listed, even
where another row's page contains it. With --update, the command records each quotation that it
found in `doc_quotes.json`, under the page that contains it, by a digest of the quotation and
with the hash of the page. With --new-only, a page that changed since its hash was recorded keeps
its entry in `doc_quotes.json` as it is. The tests read that record without the network. The
workflow checks the heuristics of the planning documents and the statistics with --quotes.

Inputs:  a heuristics file with a doc_url column or the statistics file, doc_hashes.json, and
         with --quotes the `notes` and `evidence` columns and doc_quotes.json
Outputs: the changed pages and the quotations not found as log messages on stderr, and
         doc_hashes.json, with --quotes also doc_quotes.json, rewritten with --update

Exit codes: 0 when nothing changed, 2 when a page changed or a quotation was not found in the
pages that its row cites, and 1 when an error, such as a network failure, prevented the check.
"""

import argparse
import csv
import gzip
import hashlib
import html
import http.client
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from collections import defaultdict
from pathlib import Path

from agentcontext import caches
from agentcontext import cli
from agentcontext.heuristics import CONFIG_PATH_PATTERNS_PATH, DOC_HASHES_PATH
from agentcontext import logs


# -----------------------------------------------------------------------------
# The watch
# -----------------------------------------------------------------------------

USER_AGENT = "agenttorrent-pipeline doc-watch (+https://github.com/se-uhd/agenttorrent-pipeline)"
TIMEOUT_SECONDS = 30

# The hash is taken over the visible text with runs of space collapsed, because markup and
# whitespace change without a change of the documented behavior.
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
# What a browser does not render: scripts, styles and the content of a template, in which
# Mintlify's sites keep a manifest with the build id of the deployment.
_UNRENDERED = re.compile(r"<(script|style|template)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
# For the hash, the comments are removed before the tags, because a `>` inside a comment, as in
# `(deferred print->all)`, would end a tag there and leave the rest of the comment in the text.
# The quotation check keeps the comments: There a `<` in the prose of a Markdown file, as in
# `<200ms`, would start a tag that the next `>` ends, and a comment between the two stops it.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def page_digest(body: str) -> str:
    """Return a hash of a page's visible text, ignoring markup and spacing.

    Script, style and template bodies are dropped before hashing. They contain per-request nonces
    and build ids on several of these sites, which would otherwise show a change on every run,
    and the reader would learn to ignore the list of changed pages.

    Nothing else is dropped. Filtering "code-like" lines in addition would drop
    documentation samples, and then ordinary sentences containing the English words "let",
    "return" and "location.", and miss real changes of the documented behavior. When many pages
    change at once, read them again rather than build a cleverer hash.
    """
    endpoints = _openrouter_endpoints(body)
    if endpoints is not None:
        text = json.dumps(endpoints, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    text = _UNRENDERED.sub(" ", _COMMENT.sub(" ", body))
    text = _SPACE.sub(" ", _TAG.sub(" ", text)).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# OpenRouter's list of a model's endpoints, `https://openrouter.ai/api/v1/models/<author>/<slug>/
# endpoints`, which the price rows of `2_classification/config/model_prices.csv` cite, is JSON
# with the uptime, the latency and the status of each endpoint over the last minutes, which
# change between two requests. A price row reads only the fields of its endpoint that
# OPENROUTER_ENDPOINT_FIELDS names, and the digest covers those fields of every endpoint of the
# list.
OPENROUTER_ENDPOINT_FIELDS = ("tag", "provider_name", "quantization", "pricing",
                              "supported_parameters", "context_length")


def _openrouter_endpoints(body: str) -> list | None:
    """The fields of OPENROUTER_ENDPOINT_FIELDS of each endpoint, in the order of their tags,
    when `body` is OpenRouter's list of a model's endpoints, and None for any other page."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    listed = data.get("data") if isinstance(data, dict) else None
    endpoints = listed.get("endpoints") if isinstance(listed, dict) else None
    if not isinstance(endpoints, list):
        return None
    kept = [{field: endpoint.get(field) for field in OPENROUTER_ENDPOINT_FIELDS}
            for endpoint in endpoints if isinstance(endpoint, dict)]
    return sorted(kept, key=lambda endpoint: str(endpoint["tag"]))


# A GitHub blob page wraps the file in an HTML payload that contains request-specific fields,
# and hashing it would report a change on every run. The raw host serves the document itself,
# which is what the heuristic was read from.
_GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$")
# A Wayback Machine capture, cited where the vendor's current page no longer states a rule that
# the tool still follows. The plain address shows the capture under a banner that counts the
# captures of the page, which changes whenever the page is archived again. `id_` after the
# timestamp serves the capture alone.
_WAYBACK = re.compile(r"^https://web\.archive\.org/web/(\d+)/(.+)$")


def canonical_url(url: str) -> str:
    """Return the address that serves the document rather than a page about it."""
    m = _GITHUB_BLOB.match(url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = _WAYBACK.match(url)
    if m:
        return f"https://web.archive.org/web/{m.group(1)}id_/{m.group(2)}"
    return url


def fetch(url: str) -> str:
    """Return a page's body, or raise urllib.error.URLError.

    A response that ends before its length, which http.client raises as IncompleteRead, and a
    gzip body that ends early (EOFError) or does not decompress (zlib.error) raise
    URLError too, so that the page is reported as a page that could not be fetched, beside the
    verdict on every other page."""
    request = urllib.request.Request(canonical_url(url),
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
            # The request accepts no encoding, and the Wayback Machine still serves a capture in
            # the encoding that it was recorded in.
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
    except http.client.HTTPException as error:
        raise urllib.error.URLError(f"Could not read the response ({error!r})") from error
    except EOFError as error:
        raise urllib.error.URLError(f"The gzip body ended early ({error})") from error
    except zlib.error as error:
        raise urllib.error.URLError(f"The gzip body does not decompress ({error})") from error
    return body.decode("utf-8", errors="replace")


# How a heuristic is named in the log messages, for the heuristics file that this run reads.
# The configuration heuristics name a tool and a mechanism, and the commit heuristics a tool
# and a scope. Neither is hardcoded, and a third heuristics file with a doc_url needs no change
# here.
DEFAULT_LABEL_COLUMNS = ("tool", "mechanism")


def read_rows(path: Path, label_columns) -> list:
    """Every `(label, row)` of a heuristics file with a `doc_url`, in file order, without the
    comment rows (`read_heuristics`)."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        missing = [c for c in ("doc_url", *label_columns) if c not in fields]
        if missing:
            raise ValueError(f"{path} has no column(s) {missing}. Its header is {fields}")
        first = fields[0]
        return [("/".join(row[c] or "?" for c in label_columns), row)
                for row in reader
                if (row.get("doc_url") or "").strip()
                and not (row.get(first) or "").startswith("#")]


def read_statistics(path: Path) -> list:
    """Every `(key, row)` of a statistics file with a `vendor` source: the page that states the
    number as `doc_url`, and the verbatim quote from which the number is read as the quotation
    of `notes`, so that the watch checks the page and the quote as it checks a heuristic."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return [(key, {"doc_url": entry["from"]["vendor"], "notes": f'"{entry["from"]["quote"]}"'})
            for key, entry in entries.items() if "vendor" in entry.get("from", {})]


def read_heuristics(path: Path, label_columns) -> list:
    """Every (doc_url, label) pair in a heuristics file, in file order.

    Deliberately not `heuristics.load_config_path_patterns`: That function validates the
    columns of the configuration heuristics, and this script needs only a `doc_url` and a label
    for the heuristic. A row with no `doc_url` cites nothing and is skipped.

    A comment row is a row with a first column that starts with `#`, which is the only form
    that both heuristics files share. `heuristics.load_config_path_patterns` recognizes one by
    testing whether `path` starts with a hash, a column that the commit heuristics lack. If the
    file were read generically without this test, a comment row of the configuration
    heuristics would have a `doc_url` of `#`, and the watch would report it on every run as a
    page that it could not fetch.
    """
    return [(row["doc_url"], label) for label, row in read_rows(path, label_columns)]


def urls_by_label(pairs: list) -> dict:
    """Return {doc_url: the sorted labels that cite it}."""
    cited = defaultdict(set)
    for url, label in pairs:
        cited[url].add(label)
    return {url: sorted(labels) for url, labels in sorted(cited.items())}


def load_hashes(path: Path) -> dict:
    """Return the recorded hashes, or an empty mapping on the first run."""
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# The quotations of the notes
# -----------------------------------------------------------------------------

DOC_QUOTES_PATH = DOC_HASHES_PATH.with_name("doc_quotes.json")

# A quotation is the text between a pair of double quotes in a note, the quotes paired from the
# left, so that the text between two quotations is never read as one. The text between a pair
# of backticks outside a quotation is a code span, such as a line of a tool's source, and its
# double quotes enclose strings of that code, not quotations. The note is read from the left,
# and a double quote or a backtick, whichever comes first, opens a quotation or a code span that
# ends at the next mark of the same kind. A quotation therefore keeps the backticks of the
# Markdown that it quotes, and a backtick without a later one is an ordinary character.
_QUOTED_OR_CODE = re.compile(r'"([^"]*)"|`[^`]*`')
# A page that a note names outside its quotations. A link inside a quotation is the quoted
# page's own text.
_LINK = re.compile(r"https?://[^\s\"'`<>()\[\]]+")
# Where a quotation leaves text out. The parts on either side have to be found in order.
_ELLIPSIS = re.compile(r"\[?(?:\.\.\.|…)\]?")
# The quotation check compares the text that a reader sees in the page and in its Markdown
# source alike: A link is its text, and markup, spacing, typographic quotes and dashes, and the
# characters of Markdown's emphasis, headings, block quotes, tables and code spans are ignored.
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*\)")
_LOOSE_TAG = re.compile(r"<[^<>]*>")
_IGNORED = re.compile(r"[\s`*_>#|\\]+")
_TYPOGRAPHIC = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'",
                              " ": " ", "–": "-", "—": "-", "…": "..."})


def comparable(text: str) -> str:
    """A page's text, or a quotation, as the quotation check compares them."""
    text = html.unescape(_SCRIPT_OR_STYLE.sub(" ", text)).translate(_TYPOGRAPHIC)
    text = _LOOSE_TAG.sub(" ", _MARKDOWN_LINK.sub(r"\1", text))
    return _IGNORED.sub("", text)


def quotations(notes: str) -> list:
    """The quotations of a note, in order, without the strings of its code spans and without a
    quotation that has nothing to compare."""
    return [m.group(1) for m in _QUOTED_OR_CODE.finditer(notes or "")
            if m.group(1) is not None and comparable(m.group(1))]


def _without_quotations(notes: str) -> str:
    """A note with a space in place of each quotation and with its code spans kept."""
    return _QUOTED_OR_CODE.sub(lambda m: " " if m.group(1) is not None else m.group(0),
                               notes or "")


def note_links(notes: str) -> list:
    """The pages that a note links outside its quotations, in order, once each."""
    found = []
    for m in _LINK.finditer(_without_quotations(notes)):
        url = m.group(0).rstrip(".,;:")
        if url not in found:
            found.append(url)
    return found


def quote_sources(row: dict) -> list:
    """The pages in which a row's quotations may be found, in the order in which they are
    read: its `doc_url`, the pages that its note links, and the files that its `evidence`
    links."""
    found = [row["doc_url"]]
    for url in [*note_links(row.get("notes")), *(row.get("evidence") or "").split()]:
        if url not in found:
            found.append(url)
    return found


def quoted_in(quotation: str, text: str) -> bool:
    """Whether a quotation is in a page's comparable text: each of its parts, in order."""
    position = 0
    for part in _ELLIPSIS.split(quotation):
        part = comparable(part)
        if not part:
            continue
        found = text.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def check_quotes(rows: list, body_of) -> tuple:
    """`(found, missing, unchecked, unreachable)` for the quotations of `(label, row)` pairs:
    {page: the quotations found there}, `(label, quotation)` for each quotation in none of the
    row's own pages, `(label, quotation, page)` for each quotation in none of the row's pages
    that could be fetched, with `page` the first of its pages that could not be fetched, and
    {page: why it could not be fetched}. `body_of` returns a page's body. A page is fetched when
    a quotation is not in the pages before it."""
    texts, unreachable = {}, {}

    def text_of(url: str):
        if url not in texts and url not in unreachable:
            try:
                texts[url] = comparable(body_of(url))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                unreachable[url] = str(exc)
        return texts.get(url)

    found, missing, unchecked = defaultdict(set), [], []
    for label, row in rows:
        for quotation in quotations(row.get("notes")):
            sources = quote_sources(row)
            for url in sources:
                text = text_of(url)
                if text is not None and quoted_in(quotation, text):
                    found[url].add(quotation)
                    break
            else:
                blocked = next((url for url in sources if url in unreachable), None)
                if blocked is None:
                    missing.append((label, quotation))
                else:
                    unchecked.append((label, quotation, blocked))
    return dict(found), missing, unchecked, unreachable


def quotation_digest(quotation: str) -> str:
    """The key under which the record names a quotation: the start of the SHA-256 of its
    text as the note writes it. A note changes the key when it changes the quotation, and the
    record repeats none of the vendors' words."""
    return hashlib.sha256(quotation.encode("utf-8")).hexdigest()[:16]


def merge_quotes(recorded: dict, found: dict, digests: dict) -> dict:
    """The record of the quotations with what this run found: for each page, the digests of
    the quotations found on it with the page's hash, together with the digests recorded for the
    same hash."""
    merged = dict(recorded)
    for url, quotes in found.items():
        before = recorded.get(url) or {}
        kept = (set(before.get("quotations", ())) if before.get("sha256") == digests[url]
                else set())
        merged[url] = {"sha256": digests[url],
                       "quotations": sorted(kept | {quotation_digest(q) for q in quotes})}
    return dict(sorted(merged.items()))


def changed_since_recorded(found: dict, digests: dict, hashes: dict, recorded: dict) -> set:
    """The pages of `found` with a current hash other than the hash that the records keep for
    them. --new-only leaves the entries of these pages in `recorded`, the record of the
    quotations, as they are. The hash kept for a page is its hash in `hashes`, the record of the
    page hashes that the run writes. For a page without a hash there, such as a page that only a
    note links, it is the hash of the entry of the page in `recorded`."""
    changed = set()
    for url in found:
        kept = hashes.get(url, (recorded.get(url) or {}).get("sha256"))
        if kept is not None and kept != digests[url]:
            changed.add(url)
    return changed


def exit_code(changed: list, new: list, unreachable: list) -> int:
    """The verdict: 0 unchanged, 2 a cited page changed, 1 the watch could not conclude.

    A page with no recorded hash is 1 rather than 0. Nothing was compared for it, and a watch
    that reports agreement after comparing nothing is worse than a watch that reports failure:
    The weekly workflow reads the exit code, and an empty or partial hash record would pass
    every page in the heuristics. ``--update`` records a new page, and it returns before this
    function is reached.
    """
    if unreachable or new:
        return 1
    return 2 if changed else 0


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hashes", default=str(DOC_HASHES_PATH),
                        help=f"Path to the hash record (default: {DOC_HASHES_PATH})")
    parser.add_argument("--heuristics", default=None,
                        help=f"Path to the heuristics file (default: {CONFIG_PATH_PATTERNS_PATH})")
    parser.add_argument("--statistics", default=None,
                        help="Read the vendor sources of a statistics file, such as "
                             "docs/statistics.json, instead of a heuristics file")
    parser.add_argument("--label-columns", default=",".join(DEFAULT_LABEL_COLUMNS),
                        help=f"Comma-separated columns that name a heuristic in the log "
                             f"(default: {','.join(DEFAULT_LABEL_COLUMNS)})")
    parser.add_argument("--update", action="store_true",
                        help="Record the current hash of every page, changed pages included")
    parser.add_argument("--new-only", action="store_true",
                        help="With --update, record only the pages without a hash, and keep the "
                             "previous hash of every changed page and, with --quotes, its "
                             "recorded quotations")
    parser.add_argument("--quotes", action="store_true",
                        help="Also check every quotation of the notes against the pages that "
                             "its row cites, and with --update record the quotations found")
    parser.add_argument("--quote-record", default=str(DOC_QUOTES_PATH),
                        help=f"Path to the record of the quotations (default: {DOC_QUOTES_PATH})")
    args = parser.parse_args(argv)
    if args.new_only and not args.update:
        logs.error("docs", "--new-only records nothing without --update.")
        return 1

    hashes_path = Path(args.hashes)
    recorded = load_hashes(hashes_path)
    label_columns = [c.strip() for c in args.label_columns.split(",") if c.strip()]
    if not label_columns:
        logs.error("docs", "--label-columns named no column.")
        return 1
    # What the run reads: the heuristics of a heuristics file, each a row that cites pages, or
    # with --statistics the statistics of the statistics file with a vendor source, each quoting
    # its page.
    kind = "statistic" if args.statistics else "heuristic"
    citing = "statistics" if args.statistics else "rows"
    try:
        rows = (read_statistics(Path(args.statistics)) if args.statistics else
                read_rows(Path(args.heuristics) if args.heuristics else CONFIG_PATH_PATTERNS_PATH,
                          label_columns))
    except (OSError, ValueError, KeyError) as exc:
        logs.error("docs", f"Cannot read the {kind}s file ({exc}).")
        return 1
    cited = urls_by_label([(row["doc_url"], label) for label, row in rows])
    if not cited:
        # No row cites a page, and this run compared nothing. Reporting that as agreement would
        # pass a file that the run could not read.
        logs.error("docs", f"No {kind} cites a documentation page, and no page was checked.")
        return 1

    def quoted_by(label: str) -> str:
        """Where a quotation is: the notes of a heuristic, or the quote of a statistic."""
        return f"the quote of the statistic {label}" if args.statistics else f"the notes of {label}"

    def not_found(label: str, quotation: str) -> str:
        """The message for a quotation that no page of its heuristic or statistic contains."""
        if args.statistics:
            return (f"The statistic {label} quotes \"{quotation}\", but the page that it cites "
                    f"does not contain it.")
        return (f"The notes of {label} quote \"{quotation}\", but no page that the row cites "
                f"contains it.")

    changed, new, unreachable, current, bodies = [], [], [], {}, {}
    for url, labels in cited.items():
        try:
            bodies[url] = fetch(url)
            digest = page_digest(bodies[url])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            unreachable.append((url, labels, str(exc)))
            # Keep the old hash so that one failed fetch does not look like a change.
            if url in recorded:
                current[url] = recorded[url]
            continue
        current[url] = digest
        if url not in recorded:
            new.append((url, labels))
        elif recorded[url] != digest:
            changed.append((url, labels))

    logs.info("docs", f"Checked {len(cited):,} documentation pages that "
                      f"{sum(len(v) for v in cited.values()):,} {kind}s cite.")
    for url, labels in changed:
        logs.info("docs", f"The page {url} changed (cited by {', '.join(labels)}).")
    for url, labels in new:
        logs.info("docs", f"The page {url} has no recorded hash (cited by {', '.join(labels)}).")
    for url, labels, reason in unreachable:
        logs.info("docs", f"Could not fetch the page {url} cited by {', '.join(labels)} "
                          f"({reason}).")
    if not (changed or new or unreachable):
        logs.info("docs", "No cited page has changed.")

    found, missing, unchecked = {}, [], []
    if args.quotes:
        def body_of(url: str) -> str:
            if url not in bodies:
                bodies[url] = fetch(url)
            return bodies[url]

        found, missing, unchecked, quote_unreachable = check_quotes(rows, body_of)
        logs.info("docs", f"Checked {sum(len(quotations(row.get('notes'))) for _l, row in rows):,} "
                          f"quotations against the pages that their {citing} cite.")
        for label, quotation in missing:
            logs.info("docs", not_found(label, quotation))
        if unchecked:
            # One line per page rather than per quotation: A host that refuses every request,
            # such as raw.githubusercontent.com for every GitHub link, leaves the quotations of
            # every row that cites it unchecked, and a line for each would make the log longer
            # than the weekly workflow can open an issue with.
            by_page = defaultdict(list)
            for label, quotation, url in unchecked:
                by_page[url].append((label, quotation))
            for url, entries in sorted(by_page.items()):
                label, quotation = entries[0]
                logs.info("docs", f"Left {len(entries):,} quotation(s) unchecked, because the "
                                  f"page {url} that their {citing} cite could not be fetched. One "
                                  f"of them is \"{quotation}\" in {quoted_by(label)}.")
        for url, reason in sorted(quote_unreachable.items()):
            if url not in cited:
                logs.info("docs", f"Could not fetch the page {url} that a row links in its notes "
                                  f"or its evidence ({reason}).")

    if args.update:
        # Merge into the record rather than write over it. One doc_hashes.json covers every
        # heuristics file, and a run over one file fetches only that file's pages. Writing
        # `current` alone would drop the hashes of the other files, and the next watch of such a
        # file would report every page as never recorded.
        merged = dict(recorded)
        # A heuristic added while other changed pages are still unread needs its page recorded
        # without them: --update alone would record every changed page as read.
        merged.update({url: digest for url, digest in current.items()
                       if not args.new_only or url not in recorded})
        # Replace the file in one step: An interrupted write here would produce what the merge
        # above prevents, a record without the other files' hashes.
        caches.replace_file(hashes_path,
                            json.dumps(merged, indent=2, sort_keys=True) + "\n")
        # Count the pages fetched, not len(current): An unreachable page keeps its old hash
        # only when it had one and is not always there to subtract, and the count would become
        # negative once the unreachable pages outnumber the fetched pages.
        fetched = len(cited) - len(unreachable)
        if args.new_only:
            recorded_now = sum(1 for url, _labels in new)
            logs.info("docs", f"Recorded the hash of {recorded_now:,} new page(s) in "
                              f"{hashes_path.name}. The {len(changed):,} changed page(s) keep "
                              f"their previous hash.")
        else:
            logs.info("docs", f"Recorded the hash of {fetched:,} fetched page(s) in "
                              f"{hashes_path.name}.")
        if args.quotes:
            quote_path = Path(args.quote_record)
            quote_record = load_hashes(quote_path)
            digests = {url: page_digest(bodies[url]) for url in found}
            # Under --new-only, a changed page keeps its previous hash in doc_hashes.json, and its
            # entry in doc_quotes.json stays as it is: the quotations of every heuristics file
            # that were found in the text with that hash. A new hash in doc_quotes.json would
            # disagree with doc_hashes.json, which tests/test_planning.py checks, and recording
            # only this run's quotations would drop those of the other heuristics file.
            changed_pages = (changed_since_recorded(found, digests, merged, quote_record)
                             if args.new_only else set())
            recording = {url: quotes for url, quotes in found.items()
                         if url not in changed_pages}
            caches.replace_file(quote_path, json.dumps(
                merge_quotes(quote_record, recording, digests),
                indent=2, ensure_ascii=False) + "\n")
            logs.info("docs", f"Recorded {sum(len(q) for q in recording.values()):,} "
                              f"quotation(s) of {len(recording):,} page(s) in "
                              f"{quote_path.name}.")
            if changed_pages:
                logs.info("docs", f"Recorded no quotation of {len(changed_pages):,} changed "
                                  f"page(s), and their entries in {quote_path.name} stay as "
                                  f"they were.")
        if unreachable:
            # Count the two groups of pages separately, because they lose different
            # information. A page that had a recorded hash keeps it and still appears recorded
            # although nothing confirmed it. Reporting success would keep a hash from before the
            # change, and the next scheduled run would report the same change again. A page that
            # had no hash is absent from the record, and the next run that fetches it records
            # its content at that time with nothing to compare it with.
            kept = sum(1 for url, _labels, _reason in unreachable if url in recorded)
            logs.error("docs", f"Could not fetch {len(unreachable):,} page(s), and {kept:,} of them "
                               f"keep their previous hash. Run --update again when the pages "
                               f"are reachable.")
            return 1
        if unchecked:
            return 1
        return 2 if missing else 0

    verdict = exit_code(changed, new, unreachable)
    if verdict == 1 or unchecked:
        return 1
    return 2 if missing else verdict


if __name__ == "__main__":
    raise SystemExit(main())
