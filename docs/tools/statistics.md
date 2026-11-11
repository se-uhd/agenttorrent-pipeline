# Stated statistics

Every number that the maintained documentation states comes from one file,
[`docs/statistics.json`](../statistics.json). A document marks the number with the key that
names it:

```markdown
The sample contains <!--s:example.repositories-->1,234<!--/s--> repositories.
```

`python -m agentcontext statistics` writes the value of every stated statistic from the file into
the documents. A rendered page shows only the number, because the markers are HTML comments. To
change a stated statistic, edit its entry in `docs/statistics.json`, never the document, and run
the command.

## Which numbers the documents state

Every stated statistic is traceable: Its entry names the data and the code that compute it, and
the checkout that tracks both reproduces it. The exception is a value of an earlier state, which
`docs/history/values-at-commits.json` records with the commit of the private repository that it
was computed from. A maintained document states a number only when a reader needs it, and each
number belongs to one of these kinds:

| Kind | Example | Source | Captured |
|---|---|---|---|
| A value that the code or a configuration file uses, where the text explains the behavior that depends on it | the retries of a request, the end date of the sampling window | `constant`, `yaml`, `code.literal` | At every check, from the current code |
| A count of the collection that the checkout tracks, where the text describes the dataset, an evidence limit or what a step produces | the configured repositories, the commits that one heuristic alone credits | `rows`, `json`, `function` over the tracked outputs, `collection` over the handover | At the import of each handover, when the regenerated outputs and the collection statistics file are committed |
| A result of a tracked evaluation or measurement, where a decision rests on it | the agreement of two models in a label evaluation | `json`, `rows`, `function` over the dated directory of the evaluation | When the evaluation runs. Its directory keeps its results and is rewritten only to take out what names a person |
| A limit of a vendor that the pipeline depends on | the results that GitHub's code search returns | `vendor` | When the quote is recorded, and checked every week |
| An earlier state, where the comparison is the point | the sizes of the retrievals behind a threshold of the GHS download | a `json` source over [`docs/history/values-at-commits.json`](../history/values-at-commits.json) | When the value was moved there, from the files of the commit that the file names |

The documents state no other number. The effect of a past change, such as the commits that a
heuristic newly credited, describes the change and not the dataset: the document states the
current quantity, such as the commits that the heuristic alone credits, or no number. A count of
the work of a session, a count of code structures that the text does not need, a count of a test
fixture that the test asserts, a timing of one development session, and a count of an earlier
collection or release that the text does not compare are not stated.

A count of the collection traces back to the handover. The tracked outputs come from the
regeneration of the working copy, and the collection statistics file records the SHA-256 of the
snapshot that they belong to and the name, the size and the SHA-256 of the handover that the
working copy was extracted from (`--archive`). The commit that imports the handover pins the
outputs, the collection statistics file and the documents together, and the release pins the counts that it states.

## The file

The file has one entry per quantity, under a key of words joined by dots with the topic first
(e.g., `collection.rows.skills`, `parameters.2_classification.1_classify_repos.REQUEST_TOKENS`).
A quantity stated in several documents has one key. An entry has its `value`, a `meaning`, and
exactly one source, which computes the value from the data:

| Source | Value |
|---|---|
| `{"rows": PATH}` | The data rows of a CSV, of a `.csv.xz`, or of the one CSV that a `.7z` archive holds, which `7zz` extracts to its standard output. An archive in numbered volumes is named by its first volume, `.7z.001`. With `"where": {COLUMN: VALUE}`, the rows with columns that contain the values, and with `"distinct": COLUMN`, the distinct values of the column |
| `{"size": PATH}` | The size in bytes of a file, such as a tracked archive |
| `{"json": PATH, "pointer": "/a/0"}` | A value of a JSON file, a `.json.xz` or the one JSON file of a `.7z` archive, by JSON pointer. With `"length": true`, the length of the list or object there |
| `{"yaml": PATH, "pointer": "/a/0"}` | A value of a YAML file, such as a setting of `1_sampling/config/`, by JSON pointer |
| `{"constant": PATH, "name": NAME}` | A top-level constant of a Python file, read without importing it. With `"index": N`, the item N of a tuple, and with `"length": true`, the length of a tuple, a list, a set or a dict |
| `{"function": NAME, "args": {...}}` | A computation of `agentcontext/statistics/` over the files of the checkout, such as `tables.cell` or `commits.count`, with its arguments |
| `{"collection": NAME, "args": {...}}` | A computation of `agentcontext/statistics/` over the data root of the working copy of the latest handover, read from the collection statistics file ([The statistics of a collection](#the-statistics-of-a-collection)) |
| `{"vendor": URL, "quote": TEXT}` | The number in a verbatim quote of the vendor page at URL, such as a limit of GitHub's API. With `"pattern": REGEX`, the first group of the pattern in the quote, for a quote with several numbers. The documentation watch checks the quote on the page every week (`python -m agentcontext docs --statistics docs/statistics.json --quotes`) |
| `{"percent": [A, B]}`, `{"quotient": [A, B]}`, `{"difference": [A, B]}`, `{"product": [A, ...]}`, `{"sum": [A, ...]}` | Computed from other keys |

A statistic of an earlier state, such as the size of a retired file or a count of the sample of
March 2026, was computed from the files of a commit of the private repository
`se-uhd/agenttorrent-pipeline-internal`, which holds the history of this tree. This repository starts from one
commit and contains neither those commits nor those files. [`docs/history/values-at-commits.json`](../history/values-at-commits.json)
keeps each such value under the key of the statistic, with `commit`, the commit of the private
repository that the value came from, and `source`, what computed it from the files of that commit.
Its entry reads the value through `{"json": "docs/history/values-at-commits.json", "pointer":
"/values/KEY/value"}`. The file is a record of what was measured, and no check resolves a commit
that it names. A new statistic of a past state states a quantity of the current data instead.

A commit named elsewhere in the file of the statistics, such as a value that a `where` selects, is
a plain value that names a commit of the private repository, and no check resolves it either.

A computed value follows the data: After a new collection, the command writes the new counts
into every document that states them. The command refuses an entry without a source under
`from`, because its value would be a number that no data computes.

## The computations

A computation is a function of a module of `agentcontext/statistics/`, registered with
`checkout` for the files of the checkout or with `collection` for the data root of a handover,
under the name `MODULE.FUNCTION`. It takes a `Files` and the arguments of its source and returns
a number. `Files` reads a CSV, a JSON file, a JSON Lines file, a text and a Python constant,
whole or compressed, under a root. Its `memo` keeps a parsed file for as long as the `Files`
exists, and the command keeps one `Files`, so that one run of the command parses each large file
once, such as a commit CSV, the decisions of a classification run or the rows of a GHS retrieval. Each module is named for the files that it reads, and it has one
function for each kind of question, with arguments for the variants:

- `tables` reads a CSV, a JSON file or a JSON Lines file, whose records are the rows of the CSV,
  the items at a JSON pointer or the lines. `rows` counts the records that conditions select, by
  the value of a column, a regular expression, a cell that is not empty, a comparison with a
  number, another column or a constant of the code, and the presence or absence of the key of a
  record in other CSVs, or it counts the distinct values of a column in them. `columns` counts
  the columns of a header, `cell` reads the number in one record, `aggregate` computes the sum,
  mean, median, minimum or maximum of a column, and `shared_keys` counts the keys that two CSVs
  share.
- `commits` reads the commit CSVs as one set of commits, in which a commit that credits one of
  the six tools and another tool has the credits of its rows in both CSVs. `count` counts the
  commits or their repositories by tool, repository, matched heuristics, the heuristics that
  alone credit a tool, attributions, message, absence from one CSV and a label of a label CSV,
  and `longest_message` measures the longest commit message.
- `unmatched` reads the tracked unmatched attributions, the one archive that its glob pattern
  matches, whatever day names it. `count` counts the attributions that no negative explains, and
  `size` gives the size of the archive.
- `labels` reads the runs of the classification and the label evaluations. `decisions` counts
  the decisions of a run by outcome, mirror description, upstream, limit reached and fork
  result, and `calls` counts their tool calls by tool, output, README path and a link of the
  repository's own file at another ref. `calls_per_decision`, `usage`, `bytes_per_decision` and
  `fork_value` read one quantity of the decisions of a run, and `agreement` counts the repositories
  that several runs or the models of an evaluation label alike or differently.
- `retrievals` reads the cache of GHS retrievals. `minutes`, `end_date_days`, `languages` and
  `rows_per_file` read what its index records, and `created_at_groups`,
  `created_at_head_pairs`, `timestamp_hour` and `created_after_window` count what the rows of a
  retrieval and GitHub's responses show. `language_rows`, `language_loss`, `share_kept` and
  `language_lead` count the languages of the rows, and `crawl_cost` computes what the requests of
  the GHS download for crawl dates cost.
- `code` reads the code of the checkout. `literal` reads the number that a regular expression
  finds once in a file. It serves a file in which moving the number into a top-level constant,
  which the `constant` source reads, would itself cost something. A change of
  `agentcontext/commits.py` or `agentcontext/heuristics.py` changes the digest of the commit
  classification. A change of the collector's code needs a version of the collector, and a
  change of a text that a model reads labels the sample again. `files` counts the files that
  contain a text, `calls` counts the calls in a top-level tuple by their arguments,
  `whole_repository_suites` counts the suites that test no module, and `interpreter_default`
  reads a default of the standard library of the Python that runs the pipeline.
- `handover` reads the data root of a handover, and its computations are `collection`
  computations. `files` counts the files that match a glob pattern and contain a line that a
  regular expression matches, such as the workflow files that run an action or the file lists
  with an entry at a path, and `size` reads the size of a file. `catalogs` counts the
  marketplace catalogs with a plugin source inside the repository, and `log_commits` counts the
  commits of the commit logs by their message. `log_commits` reads the logs with the reader of
  `agentcontext/commits.py`, which the code of these computations therefore includes ([The statistics of a collection](#the-statistics-of-a-collection)).

## The statistics of a collection

The trees, the file lists, the workflow files and the commit logs of a collection are in the
handover, which the checkout does not contain. A statistic of them has a `collection` source.
During the import of a handover, after `snapshot write`, `python -m agentcontext statistics
--collection --archive HANDOVER` computes every such source over the data root of the working
copy, which `AGENTCONTEXT_DATA_ROOT` or `--data-root` names. It writes the values to the
collection statistics file of the data root, `collection/collection_statistics.json`, with the
SHA-256 of the snapshot of the collection, `collection/output/snapshot.json`, and of the code of
each computation. It also records the name, the size in bytes and the SHA-256 of `HANDOVER`, the
downloaded handover that the working copy was extracted from. `--archive` is required, because
[the dataset](../dataset.md) states the name, the size and the SHA-256 of the handover from this
record through `json` sources. The import copies the file into the checkout as
`data/collection/collection_statistics.json`.

The code of a computation is the source of its module and of every module of the package that
the module imports, directly or through another module, `agentcontext/statistics/__init__.py`
among them (`code_files`). A change of any of these modules can change the value, so the
SHA-256 covers them all. For a `handover` computation, the modules include
`agentcontext/commits.py` and every module that it imports, and a change of one of them needs the
values computed again over the working copy.

The command in the checkout states a value of the collection statistics file only while it
belongs to the collection that the checkout tracks: the file has to record the SHA-256 of
`data/collection/output/snapshot.json` and the SHA-256 of the current code of the computation.
Every regeneration of the working copy rewrites the snapshot, and a value computed before it
would state the collection as it was. Otherwise the value is a difference that writing does not
fix, and its message gives the command that computes the value again over the working copy.

`scale` multiplies a value before it is written, so that bytes can be written in megabytes or a
fraction as a percent. `decimals` sets the number of decimals, `percent` adds a percent sign, and
`plain` leaves out the thousands separator, as for a port. A quantity written in two units has a
key for each unit. The marker determines whether a small whole number is written as a word.
`<!--s:KEY:words-->three<!--/s-->` writes a whole number below one hundred as words, `:Words` does
the same capitalized at the start of a sentence, and `:ordinal` and `:Ordinal` write an ordinal,
as in "the sixth page".

## The documents covered

The command covers the tracked Markdown files and the release notes of the website. It leaves
out:

- `docs/history/`, a dated record of what was measured when it was written
- the released sections of `CHANGELOG.md`, which are the notes of a published release, while its
  `## [Unreleased]` section counts as documentation
- the collected files and the data under `data/collection/data/`,
  `data/classification/labels/` and `data/classification/evaluation/`

Code blocks, code spans, URLs, HTML tags and HTML comments are not prose, and the command does not
check a number in them. A `<` that a letter, `/` or `!` does not follow is a comparison, and the
text after it stays prose.

A number in a quotation of a vendor page is prose and takes a marker with a `vendor` source
([The file](#the-file)), as in the quotations of `docs/heuristics.md`. The command does not check
a number in a verbatim quotation of the maintainer, written as
`<!--maintainer-->"..."<!--/maintainer-->`, which may continue over a line break, though not over
a blank line. The maintainer's words come from no source that computes a number, and a marker in
them would let the command rewrite them.

## Exit codes

`python -m agentcontext statistics --check` writes nothing. It exits 0 when all of these conditions
hold:

- every computed value matches its data, in its type as well as its rendering
- every value of the collection is in the collection statistics file, computed over the tracked
  snapshot by the current code
- every stated statistic matches the file
- every marker names a key of the file
- every key is stated by some document or used to compute a key that is
- no document states a number with a thousands separator or a percent sign outside a marker, or
  half of a marker outside a whole marker

It exits 2 when one of these conditions fails, and 1 when a value cannot be computed, such as a
share of a base that is 0, or when the documents cannot be listed. Without `--check`, the command
writes the computed values and the stated statistics. It exits 2 when a difference remains that
writing does not fix: a marker without its key, a marker that does not parse, a key without a
document, an unmarked number, or a value of the collection that the collection statistics file
lacks. `python -m agentcontext statistics --collection --archive HANDOVER` exits 0 when it wrote
the file, and 1 when a value cannot be computed, the data root has no snapshot, or `--archive` is
missing or names no file.

A marker that does not parse is half of a marker, outside a code block or a code span, that no
whole marker contains. It can have a key with a character other than letters, digits, `_` and
`.`, a form other than `words`, `Words`, `ordinal` and `Ordinal`, or a number that runs over a
line break, or it can be one half without the other. Such a marker would be read as prose, and
its number would never be checked.

The check compares the type of a computed value because a share of whole counts is a float. A
value stored as the integer that it renders as would pass a check of the rendering alone, and the
command would still rewrite it.

`python -m agentcontext verify` runs the check. `tests/test_tools_statistics.py` checks every
source, the rendering, the markers and the differences over a checkout in a temporary
directory.
