# Engineered project selection

`python3 -m agentcontext engineered-project-selection` is the last step of the classification
stage. It reads the labels and writes the list of repositories that the collection clones: the
rows with `engineered_project` exactly equal to `true`, with their columns and in their input
order. The input labels determine the sample size, and the step requires no row count from an
earlier release.

## Inputs and commands

The default input is `data/classification/repos_annotated.csv`, and the default output is
`data/classification/repos_annotated_included.csv`. Both paths are under
`agentcontext.CLASSIFICATION_DIR`, the data root's `classification/`, whatever the working
directory. On a clean checkout, run the classification stage first, or extract the tracked
labels, those of the development dataset, from the repository root with
`7zz x data/classification/repos_annotated.7z -odata/classification`. The step itself extracts no
file that another step writes.

```shell
python3 -m agentcontext engineered-project-selection
python3 -m agentcontext engineered-project-selection --check
python3 -m agentcontext engineered-project-selection --annotated-csv /work/labels.csv -o /work/sample.csv
```

`--expect-rows N` compares the row count with a count that you supply. Use it when independent
evidence establishes the expected size of an input. On a mismatch, the step exits 1 and leaves the
existing output unchanged. Without the option, the step accepts a sample size that differs from
the previous one.

## Behavior and errors

The step reads the labels with Python's standard-library `csv` module, requires the
`repo_name`, `github_id` and `engineered_project` columns, and keeps every other column. At
import, it raises the limit of the module on the length of a field. Nothing bounds the length of
the model's justification, and a justification over the default limit would make the step fail at
every Start.

Two rows of the sample with one GitHub id are one repository under an old and a new name. The
step then exits 1 and logs the first five pairs of names with their id. A row with a blank id
takes part in no pair, because a blank id names no repository.

The step leaves out labels such as `false`, `unclear`, `TRUE` and ` true`. An input without a
`true` row produces a sample that contains only the header, unless `--expect-rows` rejects the
input. The step writes a missing trailing value as an empty field, and an extra value without a
header raises `ValueError` before anything is written.

The writer creates a temporary file beside the output, sets its mode to `0644` and replaces the
output with `os.replace`. When writing fails, the writer removes the temporary file and the
previous sample stays in place. The output uses LF line endings.

`--check` derives the sample without writing it. The step then exits 0 if the output agrees, 2 if
the output is absent or differs, and 1 if the input cannot be read, fails validation or fails the
count check of `--expect-rows`. The comparison reads the bytes of the file, treats CRLF as LF and
compares a carriage return inside a quoted field, such as a justification, as written. Reading the
file as text with universal newlines would turn that carriage return into a line feed, and the
check would find a difference in a sample that the step had just written.

## Runner integration

The progress messages of the step use the name "Engineered project selection". Its log messages
state the classification input, the `engineered_project=true` selection and the output that the
collection reads. The function is `derive_sample`, the command is `engineered-project-selection`,
and the completion key is `engineered_project_selection`. The output is the sample, which the
glossary defines apart from the sampling frame.

The step belongs to the classification stage (`stage="classification"`). Its module is in the
package beside the modules of the collection, because the list of steps of the collection, which
the container runs, starts with this step. `run_pipeline.py` runs it after `repository_labeling`,
which it consumes in the list of steps of `2_classification/run_classification.py`. The runner
records its completion by script hash.
`repository_cloning` declares no `consumes` dependency on it, because a change to the
implementation of the engineered project selection must not invalidate a completed collection.
A custom `--repo-csv` affects only the collection. The engineered project selection still
derives its default output, which that run of the collection does not read.

## Tests

`tests/test_steps_engineered_project_selection.py` checks over temporary CSVs that the step
filters the rows, keeps their columns and order, and replaces the output atomically. It also
checks the input errors and the exit codes of `--check`. It reads a justification longer than
the default limit of the csv module in a process of its own, as the runner runs the step.
Without `--expect-rows`, the tests expect the step to accept a changed or an empty population.
With the option, they cover agreement, refusal and the preservation of an existing output. No
test requires a count from a published dataset.
