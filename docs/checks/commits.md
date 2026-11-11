# Validate stored commit classifications

`python -m agentcontext commit-attribution-validation` compares the recorded commit
classifications with the current message heuristics. The check reads the commit CSV and needs no
network. It does not reconstruct the author or committer identities, which the CSV does not store.

The current input is the handover that [dataset facts](../dataset.md) names. It contains the
commit logs beside the per-repository commit records. With these logs,
[commit reclassification](../steps/commit_reclassification.md) reclassifies every repository with a
commit history that was read, and this check then matches the message heuristics against the
resulting CSV again.
The attribution totals of earlier collections are not test expectations for a new collection or a
changed heuristic.

## Validation behavior

For a row with `matched_heuristics`, the recorded heuristic ids must exist and name the tools in
`ai_tool`. The recorded message matches must be the heuristics that match the stored message now.
The check takes the identity matches as recorded, because the CSV lacks the identity fields that a
new match would need. `attributions` must be what the heuristics give: the attributions that
`commits.author_attributions` gives for the recorded identity matches, and the attributions that
`commits.attributions_in_fields` gives over the stored message. For each tool that a recorded
identity match names, `commits.author_attributions` gives `account` when one of the recorded
identity heuristics of that tool has `account`, and `convention` otherwise.

`--other-input` names the CSV of the tools outside the six, `output/other_ai_commits.csv`. The
check compares that CSV with `other_ai_commit_patterns.csv` in the same way, with the attributions
that `commits.other_attributions_in_fields` gives, and writes its classification beside it.
Against the heuristics of the six tools, every row of that CSV would name heuristics that they do
not have. The exit code is 1 when either CSV cannot be read, and otherwise 2 when either contains
a difference.

When the cloning step finds another tool, also in the author's identity, it leaves out the
heuristics and the attribution of the label `tool not named`. The CSV does not store the author's
identity, and the check therefore expects that label's heuristics and attribution only where a
row's `ai_tool` names it. If the check decided from the message alone, a commit of Devin's bot
with `Co-authored-by: AI` would be a difference, and its exit code 2 once stopped a run before the
release.

For a row without `matched_heuristics`, the check compares the tools that its message matches with
the recorded tools. A message that matches a tool that the row does not record is a difference.
The absence of a message match cannot show that an identity match was correct, especially when the
heuristics have changed since the collection.

The check keeps the input columns and appends these columns:

| Column | Meaning |
|---|---|
| `detection_source` | `message` if a message heuristic matches a recorded tool, and otherwise `author_only`, or blank if the tool column is empty |
| `matched_scope` | `trailer`, `full_body`, `trailer+full_body`, or blank |
| `matched_pattern` | The first matching regular expression, or blank |

`author_only` labels a row without a current message match. It does not verify the original
author identity. For a row that credits several tools, a message match for any recorded tool makes
the row's source `message`, and the summary counts that source for every credited tool. To learn
whether each tool has a message match of its own, call `classify_detection_source` of
`agentcontext/checks/commits.py` for each tool of the row.

The check matches each stored message once, with the functions and the trailer parser of
`agentcontext.commits` that the cloning step uses. Both the classification and the comparison come
from that match. For each tool of a row, `matched_pattern` is the pattern of the tool's first
matching trailer heuristic, in the order of the heuristics file. When no trailer heuristic
matches, it is the pattern of the tool's first matching full-body heuristic. The check joins the
subject and the body with the same blank-line boundary as the cloning step, and the match
therefore also covers disclosures at the start of the body. The CSV stores each message verbatim.
A row of the 1.0.0 release has no `matched_heuristics` column and stores a newline as `\n`, and
the check decodes it before classification. The check raises the CSV field size limit to accept
large messages.

The check reads the CSV a row at a time. It classifies a row, writes it to the output and compares
it with the heuristics before it reads the next row. Of the results, it keeps only the summary
counts and the first ten differences. Its memory use depends on the longest row rather than on the
size of the CSV, which grows with every collection. A first pass over the CSV counts its rows, so
that the progress messages can state the total.

## Commands and exit codes

```shell
python -m agentcontext commit-attribution-validation -i output/all_ai_commits.csv --other-input output/other_ai_commits.csv
python -m agentcontext commit-attribution-validation -i input.csv -o classified.csv
python -m agentcontext commit-attribution-validation -i input.csv --summary-only
```

`-i` is required. The first command runs from `data/collection/`, as the runner's
`commit_attribution_validation` step does. The default output name appends `-validated` to the
input filename. The check writes the classification even when it finds differences, unless you
pass `--summary-only`. It writes the rows to a temporary file beside the output file and renames
the temporary file once every row is written. When writing fails, the previous file stays and the
check exits 1. Logs and summaries go to stderr.

| Exit | Meaning |
|---|---|
| 0 | Every comparison that the stored CSV supports agrees |
| 2 | Recorded classifications differ from current heuristic matches |
| 1 | The input cannot be checked |

A passing check of the CSV does not show that the identity matches are current. The separate
`commit-reclassification` step reclassifies the commits from the commit logs and records its
coverage in `output/commit-reclassification.json`.

## Tests

`tests/test_checks_commits.py` tests the matching, the messages that a CSV stores, the
classification columns and the exit codes with explicit messages, recorded heuristic ids,
attributions and malformed inputs. The match outcomes are checked with known examples rather than
with an aggregate partition pinned to an earlier dataset. The suite compares the output CSVs, the
log messages and the exit code of the check, byte for byte, with those of a check that reads every
row into memory first and matches the compiled patterns of each tool on their own. Over a
generated CSV, it holds the memory that the check allocates below a fixed bound smaller than the
CSV. A further test covers the row that the cloning step writes for a commit of a tool's bot with a
trailer that credits AI and names no tool. Before you change a heuristic because of a difference
in the current data, read the commit that the warning `The row of commit <sha> of <repository>
differs` gives, because the difference is evidence for review.
