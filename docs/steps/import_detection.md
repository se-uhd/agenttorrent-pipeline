# Import detection: `output/imports.csv`

## Context

An import names a file that a tool loads into context automatically with the configuration artifact
that contains it, such as `@docs/style.md` in a CLAUDE.md. The imported file can import further
files, and the tool reads the chain up to a depth of its own. `heuristics/import_patterns.csv`
names, for each tool, the files in which it reads imports, the syntax, what a relative path resolves
against, the depth and the limits, each with the page or the posts that state it
([heuristics](../heuristics.md#imports)).

The cloning step reads the importing files in the clone and follows every chain as the tool does. It
copies each file inside the repository that an import reaches to the imported tree,
`data/imported/<owner§repo>/<path>` ([repository cloning](repository_cloning.md)). This step parses
the importing files again and writes `output/imports.csv`, with one row per tool and import and the
target that each import reaches. It runs after the file history step, so that it reads
`output/repos.csv` and the mechanism CSVs in the form in which they are published.

## Key decisions

- **One parser for the copy and the rows:** this step and the cloning step call the same functions
  of `agentcontext/imports.py`, `importing_files` and `chain`, and the files in the imported tree
  and the targets of `imports.csv` therefore cannot disagree. `tests/test_steps_import_detection.py`
  runs both steps over one clone and compares their results
- **Read as the tool read it:** where the cloning step wrote an importing file into the imported
  tree as a link, as for a CLAUDE.md that is a link in the clone, the step reads the file through
  that link. Otherwise it reads the file from the validated tree and follows a symlink marker to the
  file that the marker names (`imports.CollectedTrees`). The step reads no text from the marker of a
  link with a target that neither tree holds, such as a target outside the repository or a file in
  which the cloning step found no import (`paths.link_marker`). The one line of such a marker, such
  as `../packages/@acme/docs/copilot.md`, is not the text that the tool read. A file with one import
  or reference as its whole text is no marker, and the step reads that text, such as `@./AGENTS.md`
  saved by an editor with a byte order mark, `(@docs/notes.md)` or `[notes](docs/notes.md)`. The
  collected file cleanup keeps an empty CLAUDE file that determines whether Claude Code reads an
  AGENTS.md (`imports.deciding_claude_files`). This step therefore tests the condition
  `no_claude_md` over the same files as the cloning step. The step reads the file of the imported
  tree at the path of an importing file only where the validated tree lacks the path. The file in
  the imported tree can be a stub, as for an AGENTS.md that starts a chain of Claude Code and that
  Copilot CLI does not read as the target of an import. A stub has no text, and reading it would
  lose the imports of the file for every other chain. Where the byte limit of a row counts the
  importing file, as Claude Code Review counts the root REVIEW.md, the step counts the bytes of the
  file from which it reads the text, because the imported tree does not hold REVIEW.md unless an
  import reaches it
- **A target is looked up in the imported tree:** the step finds a target in the imported tree only,
  also through the links that the cloning step wrote there. For a file that no chain reads because
  of its extension, its content, its size or a link on its path, the cloning step writes a stub.
  The stub keeps the extension, the size up to one byte more than the largest limit of a row, and
  whether the text is valid UTF-8. A link on the path of the file in the clone stays a link in the
  imported tree. This step then gives the file `not_read` or `over_limit`, as the chain did in the
  clone, and not `missing`. The release publishes only the found targets, with the bytes of the
  clone
- **A stub is never the file:** when a retry fails in every round, or as long as the time bound that
  stopped a retry is unchanged, the cloning step keeps the earlier collection of the repository with
  its imported tree, whatever import heuristics wrote that tree. The current import heuristics can
  read a target that the earlier heuristics did not read, such as through an added extension or a
  raised limit. Without a record of the stubs, this step would then take the stub for the file and
  mark it `found`, with NUL bytes as its text and its lines. The cloning step therefore names each
  stub in a list beside the repository's directory, `data/imported/<owner§repo>.stubs.json`
  (`imports.stub_list_path`). A stub on the list that the tool would read is `not_copied`. The chain
  counts it toward its limits with the size of the stub and reads no import in it, and the row has
  no archive and no line count. The step logs the repositories with such a row, and a new collection
  of them copies the targets. A directory without a list holds no stub, because the cloning step
  writes the list wherever it writes a stub. Leaving out every row of a repository with such a stub
  would also publish no false row. The step cannot tell from the trees which import heuristics wrote
  them, however, and the status `not_copied` keeps every other row of the repository
- **An extension beside the configuration artifacts:** a row sets no tool flag, adds no repository
  to `output/repos.csv` and changes no row of a mechanism CSV. The step reads those CSVs and writes
  only `output/imports.csv`
- **Every import has a row:** an import keeps its row when its target is missing, outside the
  repository, not read by the tool or beyond its limits, with the reason in `target_status`. The CSV
  shows what the importing files name as well as what the tool reads
- **One row per tool and chain:** a file that Claude Code and Copilot CLI both read starts a chain
  of each tool, with its own rows, because the tools resolve, limit and skip differently. Copilot
  CLI reads an AGENTS.md and a CLAUDE.md with the same content in one directory as one source, and
  their imports once. The step still writes the chain of each file
- **The category of a target is the category of the file in the dataset:** the mechanism of a
  mechanism CSV that lists it, `skills` for a file in the folder of a skill, `planning_document` for
  a file of `output/planning-documents.csv` where the detection step wrote that CSV, `other` for a
  file of `other.csv`, and `imported` for a file that only the imports reach
- **The archive that contains a target:** `repos_data.7z` where the validated tree has the bytes
  that the tool read at the path and the archive step keeps the path, and `imported_files.7z`
  otherwise. The release step packs `imported_files.7z` from the imported tree with these targets
  ([release](release.md))

## `agentcontext/steps/import_detection.py`

### Architecture

`main` checks that `output/repos.csv`, `data/validated/` and `data/imported/` exist, loads the
import heuristics, and reads the mechanism CSVs with `categories` into one entry per repository. For
each repository of `output/repos.csv` with a directory in the validated tree, it reads the list of
the stubs (`imports.read_stubs`), and `repository_rows` lists the importing files with
`imports.importing_files` over the validated tree. `repository_rows` follows each chain with
`imports.chain` and builds a row per record. `location` compares the bytes of a found target in both
trees, and `github_link` links the line of the import in the importing file at the collected branch.
`write_csv` replaces `output/imports.csv` in one operation.

### Output

`output/imports.csv` has one row per tool and import, and an import written twice on one line of a
file is one import (`ROW_KEY`):

| Column | Meaning |
|---|---|
| `repo_name`, `scanned_at`, `branch`, `head_sha`, `head_commit_date` | The repository and its collection, as in the mechanism CSVs |
| `importer` | The file that contains the import |
| `importer_category` | The category of the file at the start of the chain, or `imported` for a file that the chain imported |
| `root` | The importing file at the start of the chain |
| `tool` | `claude` (Claude Code and Claude Code Review), `copilot`, `gemini` (Gemini CLI and Antigravity) or `cursor` (Bugbot) |
| `heuristic` | The `id` of the row of `import_patterns.csv` |
| `import` | The import as written |
| `line` | The line of `importer` on which the import starts |
| `target` | The repository-relative path that the import resolves to, or the path as it resolves where it leaves the repository. A chain of Claude Code or Gemini CLI from a root file in another case spells each name of a target as the directory does (`imports.ignores_case`) |
| `target_location` | `repos_data.7z` or `imported_files.7z` for a found target, blank otherwise |
| `target_status` | `found`, `missing`, `outside`, `not_read`, `over_limit`, `pruned` or `not_copied` (`imports.STATUSES`) |
| `target_category` | The category of a found target, blank otherwise |
| `depth` | The hop of the import, 1 for an import written in `root` |
| `target_line_count` | The lines of a found target, read and counted as the detection step reads and counts the lines of a configuration artifact (`detection.read_artifact`, `detection.split_lines`), each ending at LF, CRLF or CR and not at a form feed or U+2028 inside a line, and blank for a target that is no text (`detection.no_text`), such as a file that git-crypt encrypted |
| `github_link` | The line of the import on GitHub, blank without a branch |
| `dataset_version` | As in `output/repos.csv` |

The step logs the number of rows of each status. It exits 0 when it wrote the CSV, and 1 when an
input is missing or cannot be read.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `data/imported/` is missing | `ERROR` naming the cloning step, returns `1`, writes nothing |
| `output/repos.csv` or a mechanism CSV is missing | `ERROR`, returns `1` |
| A repository of `output/repos.csv` has no directory in the validated tree | No rows, and one `WARNING` with the count |
| An importing file imports nothing | No rows |
| An import of a file that the chain has read, also through a link to it | A row with `found`, and the file is not read again |
| Gemini CLI or Antigravity reads a file again for each import that reaches it | The imports of each file have rows once per chain, at the smallest depth at which the tool reads the file. For files that import each other, the number of rows grows with the files and not with the branches |
| An import of a path that no file can have, longer than `imports.PATH_MAX` bytes or with a name longer than `imports.NAME_MAX` bytes, such as a word of minified CSS | `missing`, without a lookup, with `import` and `target` cut to `imports.SHOWN` characters. Copilot CLI's imports of one such word are one row |
| A path that the archive step prunes, such as a session transcript or a log (`prune.is_prunable`) | `pruned`, whatever is there, and the cloning step copies nothing for it |
| A path with a name that is not valid UTF-8 after its links | `missing`, because no CSV can hold the name, and the imported tree holds no such name |
| A target that only a link reaches | `found`, and `target` is the path as written, which the imported tree holds as a link |
| An import that names a file in another case than the entries of its directories, such as an include of Claude Code Review | `missing`, also on a file system that ignores case, where the chain compares each name with the entries of its directory |
| An import of a chain of Claude Code or Gemini CLI from a root file in another case, such as `@AGENTS.MD` in a root `claude.md`, that names a file in another case | `found`, with `target` spelled as the directory spells the file, such as `AGENTS.md`, because the tools read such a root file only on a file system that ignores case. A name that two entries of its directory match ignoring case is `missing` |
| A root agents.md in another case beside a root CLAUDE file in any case, such as a `claude.md` | No chain of Claude Code, because the file system that ignores case, on which Claude Code reads the agents.md, opens the `claude.md` as CLAUDE.md |
| A line of the root REVIEW.md with `@` and a path into a subdirectory, or with other text beside them | No row, because Claude Code Review includes no such line |
| `output/planning-documents.csv` is missing | No target is `planning_document` |
| A target that the tool reads, with a stub in the imported tree that the list of the stubs names, written under import heuristics that did not read the target | `not_copied`, and one `WARNING` with the number of repositories and one of them |
| A list of the stubs that is not a JSON array of paths | `ERROR` naming the repository, returns `1`, writes nothing |
| A field of a mechanism CSV longer than the default limit of the csv module, such as a `referenced_file` with thousands of references | Read whole, because the module sets `csv.field_size_limit(sys.maxsize)` at import |
