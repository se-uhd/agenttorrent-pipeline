# First words of AI commit messages

## Context

`commit_message_first_words.py` draws the most frequent first words of the messages of AI commits as a horizontal bar chart of the top `N`, over the commit CSV `data/collection/output/all_ai_commits.csv`. The script lower-cases a message and strips everything but letters, digits and whitespace before it takes the first token, and `Fix:`, `fix` and `FIX` therefore count as one word. The title states the number of commits with a message in the current input.

## Key decisions

- **Language:** Python 3.14, in the single script `commit_message_first_words.py`, with `pandas` through `_shared`, and `matplotlib` and `seaborn` for the figure
- The commit CSV comes through `_shared.read_commits_chunks`, which extracts the tracked archive when the gitignored CSV is absent, as the loader of the volume figure does. It reads the message column alone, one chunk of `_shared.COMMIT_CHUNK_ROWS` rows at a time, and the script counts the first words chunk by chunk, because the whole commit CSV does not fit into the memory of a GitHub runner once pandas holds it. Where git tracks the archive beside the CSV, as in a checkout, a CSV that differs from it stops the script (`_shared.checked_copy`), because the figure would not be that of the commit. The step declares the archive as an alternative of the CSV, and a changed archive runs the step again ([run-analysis-spec.md](../run-analysis-spec.md))
- **Normalization before splitting:** lower case, then `[^a-z0-9\s]` removed, then the first whitespace-separated token. `feat(api):` is `featapi` and `Add-on` is `addon`. The figure counts tokens rather than the words of a language
- A message with no token counts as a commit without a word. The script drops the rows without a message before it takes `n`. A message of punctuation or whitespace stays in `n` but not in the bars
- `--top-n` is clamped to at least <!--s:parameters.4_data_analysis.dataset.commit_message_first_words.min_top_n:words-->one<!--/s-->, and the bars are sorted ascending so that the most frequent word is at the top
- Words with the same count are ranked alphabetically. `value_counts` alone leaves equally frequent words in the order in which the rows first named them. Which of two tied words enters the top `N`, and which is drawn above the other, would then depend on the row order of `all_ai_commits.csv`. The ranking is by count descending, then by word. The current and fixture commit CSVs have no tie among their top <!--s:parameters.4_data_analysis.dataset.commit_message_first_words.top_n:words-->twelve<!--/s-->, and the second key leaves the tracked figure unchanged
- The defaults are bound at import, and `main` takes `argv`, as in the volume figure's script

## Script: `commit_message_first_words.py`

### CLI interface

```
python3 commit_message_first_words.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--data-dir` | `_shared.DATA_DIR` | The directory with `all_ai_commits.csv` or its archive |
| `--output-path` | `<OUTPUT_DIR>/commit_message_first_words.pdf` | Where the figure is written |
| `--top-n` | `12` | How many words to draw |
| `--show` | off | Also display the figure |

### Architecture

`commit_message_first_words` is a function with keyword arguments that does the work. `_parse_args` maps the command line onto those arguments, and `main` calls the function with the parsed arguments.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or `$ANALYSIS_OUTPUT_DIR/dataset/`

Imported from `_shared`: `DATA_DIR`, `configure_plot_style`, `output_dir`, `read_commits_chunks`.

### Function structure

**`commit_message_first_words(data_dir, *, output_path, top_n, show)`**: reads the messages one chunk at a time, drops rows without a message, normalizes each message to its first token, counts the tokens over the chunks, draws the top `top_n` and saves the figure

**`_parse_args(argv)`**: the four flags

**`main(argv)`**: the function over the parsed flags, which returns 0

### Processing pipeline

1. Apply the shared plot style and read the commit CSV
2. Drop the rows with a null `commit_message`. `n` is the number of rows left
3. Lower-case, strip `[^a-z0-9\s]`, split, take the first token, empty when there is none
4. Count the non-empty tokens, rank them by count descending and then by word, keep the first `max(1, top_n)`, and reverse them so that the first is drawn on top
5. Draw one bar per word, save the figure at `output_path`, and show it with `--show`

### Output

`commit_message_first_words.pdf`: horizontal bars, `Occurrences` against `First Word`, titled with `n = <commits> commits`. The script logs `Wrote <path>`.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| Neither the CSV nor the archive in `data_dir` | `SystemExit` from `read_commits_chunks` naming the CSV |
| A CSV that differs from the archive that git tracks beside it | `SystemExit` naming both files and the command that extracts the archive again |
| A null `commit_message` | The row is dropped before `n` |
| A message of whitespace or punctuation only | In `n`, not in the bars |
| Two messages that differ in case or punctuation before the first space | One word |
| `--top-n` of zero or less | <!--s:parameters.4_data_analysis.dataset.commit_message_first_words.min_top_n:Words-->One<!--/s--> word |
| Fewer distinct words than `top_n` | Every word |
| Two words with the same count | Ranked alphabetically, so the word that comes first in the alphabet enters the top `N` first and is drawn above the other word |
| The output directory absent | `FileNotFoundError` from matplotlib, exit 1 |

## Files

| File | Action |
|------|--------|
| `commit_message_first_words.py` | Main script |
| `../../data/collection/output/all_ai_commits.csv`, or the archive beside it (`all_ai_commits.csv.7z.001` and its volumes, or `all_ai_commits.csv.7z`) | Read: `commit_message` |
| `../_shared.py` | Imported: `DATA_DIR`, `configure_plot_style`, `output_dir`, `read_commits_chunks` |
| `commit_message_first_words.pdf` | Written: the figure, tracked |
| `../test_dataset_commit_message_first_words.py` | Tests |

## Usage

```bash
cd 4_data-analysis/dataset
python3 commit_message_first_words.py
python3 commit_message_first_words.py --top-n 20 --output-path /tmp/words.pdf

# Against the fixture, writing under /tmp/figures/dataset/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 commit_message_first_words.py
```

## Tests

See `../test_dataset_commit_message_first_words.py`, a standard library `unittest` suite that needs pandas, matplotlib and seaborn. The suite loads the script by path with the Agg backend. Because the script extracts the words inside the function, the suite runs the function over a commit CSV built in a temporary directory, with pyplot's `subplots` replaced by a recording stand-in, and reads the words, the counts and the title back. The test cases over the fixture read the archive through `read_commits_chunks` and skip when `7zz` is missing.

- **`TestParseArgs`**: the defaults, `top_n` <!--s:parameters.4_data_analysis.dataset.commit_message_first_words.top_n-->12<!--/s--> among them, and every flag, and argparse exits 2 on an abbreviated flag
- **`TestTheWords`**: The first word is lower-cased with punctuation removed. A message with no word counts as a commit and not as a word, and a row without a message is dropped before the count. `top_n` keeps the most frequent words, and `top_n` below <!--s:parameters.4_data_analysis.dataset.commit_message_first_words.min_top_n:words-->one<!--/s--> keeps <!--s:parameters.4_data_analysis.dataset.commit_message_first_words.min_top_n:words-->one<!--/s-->. The bars are in ascending order, and a tie is ranked by the word, whatever the row order. The script holds the message column alone, one chunk of rows at a time, and counts a word across the chunks as over the whole file. The figure is written at the given path
- **`TestOverTheFixture`**: The commits and the top words over the fixture are the ones computed by hand, `fix` the most frequent and `add` the next. `main` passes `--top-n` through, drawing the words `fix`, `add`, `update`, `chore` and `docs` with the counts that the test computes from the fixture, returns 0 and writes the figure. A subprocess with the two environment variables set writes under the temporary root, leaves nothing new beside the script and does not extract the archive beside itself

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_dataset_commit_message_first_words -v
```

## Verification

1. `python3 check_reproduction.py --only dataset/commit_message_first_words` from `4_data-analysis/` exits 0
2. The `n` in the title equals the number of rows of `all_ai_commits.csv` with a non-empty `commit_message`

## Known limitations

- The tokens are ASCII letters and digits. A message in another writing system normalizes to an empty token and counts as a commit without a word
- A conventional-commit prefix keeps its scope, which makes `feat(api):` and `feat:` two words
- No function returns the word frequencies. A function `first_words(series)` would let a suite assert them without a stand-in for pyplot
