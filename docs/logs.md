# Logging

Every program of the pipeline writes its log messages to stderr through `agentcontext.logs`, in
this format:

```text
INFO repository_cloning: 1,200/36,710 (3.3%) done, 1,198 ok and 2 failed this run, 41.0 repositories/min, elapsed 0:29:16, about 14:26:06 left at this rate.
WARNING repository_cloning: The retry of owner/repo ended in outcome clone_failed. Kept the earlier collection.
ERROR pre_checks: '7zz' not found on PATH (required by collected_files_archive and release).
INFO runner: Repository cloning finished in 54210s.
```

The level gives the severity, and the component names the step, the check or the library that
wrote the line. A program logs its summaries, counts and ranked lists at `INFO`. It logs a
condition that the reader has to know about, in a result that remains usable, at `WARNING`, and
a failed operation at `ERROR`. The help of a command goes to stdout, and the data, tables and
figures that a program generates go to files.

## Wording

Start a message with a capital letter unless its first word is an identifier that keeps its own
spelling: a path, an `owner/repo` name, a step key, a command or flag, an environment variable, a
module, function, column or key name, or a number. Write "GitHub access token" in a message or a
help text for the credential that the pipeline sends to GitHub, because the log may later mention
other kinds of tokens. `GITHUB_TOKEN` and `GITHUB_TOKENS` keep their names. `tests/test_logs.py`
checks the first word of every message and the name of the GitHub access token in every message
and help text of `agentcontext/`, `deploy/entrypoint.py` and `run_pipeline.py`.

Write a log message, a help text or an exception text in sentences without semicolons, and put
context in parentheses. End a log message with a period, also after a closing parenthesis or a
value, as in `Downloaded the GHS search result (412 MiB).` and
`Wrote data/collection/output/repos.csv.` A message that a variable or a helper holds takes the
period where the message is assembled. A message that repeats the text of an exception adds the
period after the text and leaves the exception text unchanged, as `f"{str(error).rstrip('.')}."`
does for a text that may end with a period of its own. The row or the header of a table, a line
of a file or of another program that a message repeats, and a traceback below the first line of
a message take no period. Write a count with thousands separators (`{n:,}`). Report one event per
line, and write two lines for a message about two events. For a long transfer, log its start and
its end with the size, and not the amounts received in between. When a command writes a list to
a file, log the number of entries and the destination. A message has no leading or trailing blank
line. `tests/test_glossary.py` checks the log messages, help texts and exception texts of every
tracked Python file for semicolons.

Use no colon as punctuation in a log message or an exception text. The log line already reads
`LEVEL component: message`, and a colon inside the message gives the reader a second one. Put a
name into the sentence ("The retry of owner/repo ended ..."), and a list into parentheses or into
the sentence ("required by collected_files_archive and release"). Turn a label into a sentence
("Calendar months are counted in CEST (UTC+02:00), the time zone of /etc/host-localtime."). A time
such as 0:05:00, a URL, a code span and a colon inside a code value keep their colons, and so does
the text of another program that a message quotes in parentheses. A help text may use a colon
before a list or a default value. `tests/test_logs.py` checks the literal text of every log
message and exception text in the tracked Python files outside the tests. It exempts three error
texts that the classification returns to the model, which keep their wording. The test also
checks that every log message of these files ends with a period. It cannot read the end of a
message that a variable, a helper or a call such as `str(error)` returns, and such a message is
for its writer to check.

Put the fact first: the object, the count or the outcome. Do not narrate the program's reasoning
in a message, as in "This start compares calendar months in the NAS's time zone, which
/etc/host-localtime contains" or "..., so the step probes it again in 5 min". State an action
without a subject ("Recorded ...", "Probing again in 0:05:00.") or a state as a sentence with the
object as its subject. Write a remedy as a sentence of its own in the imperative. Name an internal
code as a value, such as "outcome git_unavailable", and do not set it in parentheses inside a
sentence. The reason for a design belongs in the documents, not in the log.
`tests/test_glossary.py` refuses a printed text that contains "this start", or ", so this" or
", so the" followed by run, step, runner, start, stage or pre-checks. It also refuses "the run" or
"the step" followed by reads, keeps, leaves, takes, decides, probes or measures.

## Components

The progress messages of a step name the step by a noun phrase, such as "Repository cloning
starting" and "Collected file cleanup finished". `logs.step_name` gives all five runners the same
noun phrases. Command-line arguments, the pipeline execution state and the names of the log files
use the step keys. The progress messages call `engineered_project_selection` "Engineered project
selection", and the step logs under its key. Every script of the sampling, classification and
analysis stages logs under its step key as well, such as `github_query`, `repository_labeling` and
`rq2_config_cooccurrence`, and `check_reproduction.py` logs under `reproduction_check`. The
library modules of these stages log under their own names: `ghs_cache`, `readmes`,
`classification_tools`, `shared` and `repo_metadata`. `tests/test_logs.py` checks that every
step script of the four runners logs under its key.

The steps and checks of the collection log under their step keys too, such as `file_history`,
`commit_attribution_validation`, `unmatched_paths_report` and `unmatched_commits_report`.
`unmatched reduce`, which is not a step, logs under `unmatched`. A library that needs a component
of its own uses a name such as `history`, `caches`, `commits` or `repos`. Several modules can log
under one component, as `guards.py`, `paths.py`, `resume.py` and `reuse.py` all log under
`repository_cloning`. One module can also log under several components, as the runner logs under
`runner` and `pre_checks`.

`agentcontext/checks/commits.py` logs under `commit_attribution_validation`, so that its messages
differ from the `commits` messages of the library that reads the commit history. A timestamp that
cannot be parsed is logged as a warning under `repos`.

## Handler and formatting

The module uses the standard library's logging levels and one formatter, `logs.FORMAT`. Its
handler resolves `sys.stderr` at each write, so that tests and callers can redirect the stream.
Component loggers set `propagate=False`, so that a caller that configures the root logger does not
write each message a second time.

`logs.line(level, component, message)` returns a formatted string without writing it.
`Progress.note` calls it for the progress line that it returns to its caller, and `labeled` calls
it to put a step prefix in front of the output of a child process. An unknown logging level raises
`ValueError`.

Write a `SystemExit` message as a plain sentence. The runner catches the `SystemExit` and logs its
message as an `ERROR runner:` line. A message that was formatted before it went into the
`SystemExit` would be formatted a second time.

The tests in `tests/test_logs.py` check the pipeline's logging calls, the formatting of the
components, and direct logging that bypasses the shared module.

## Child processes and progress

The runner combines a step's stdout and stderr and passes each line through `logs.labeled` before
it writes the line to the step log and the console. `logs.labeled` keeps a line's existing log
prefix and discards empty lines. It puts an `INFO <step key>:` prefix in front of unformatted
output from Git, 7-Zip or a traceback.

`logs.Pace` reports a progress message as due at the first check of its caller after
`PROGRESS_SECONDS`, <!--s:parameters.agentcontext.logs.PROGRESS_SECONDS.minutes:words-->ten<!--/s--> minutes, have passed since its last one. `logs.Progress` writes the progress
line of a step that works through a list of repositories. When a repository completes, it writes
a line if <!--s:parameters.agentcontext.guards.PROGRESS_EVERY-->100<!--/s--> repositories have completed or <!--s:parameters.agentcontext.logs.PROGRESS_SECONDS.minutes:words-->ten<!--/s--> minutes have passed since its last line. The
line gives the share done, the repositories that succeeded and failed in this run, the rate, the
elapsed time and the time left at that rate. A step appends its own counts through `details`. The
cloning step appends the number of processes in the container through `guards.Progress`, and the
classification stage appends the requests and the model tokens that it has used. Neither class
starts a timer, and neither writes a line while all work is blocked.

A step that waits for something that a person has to do, such as the labeling while a service
refuses its requests because the credit is spent, states the wait with `logs.step_status`. The
function writes the wait as a JSON object to the file that the runner names in the step's
environment, `AGENTCONTEXT_STEP_STATUS` (`run_logs/<step>.status.json`), and the step passes
`None` to remove the file once the wait ends. The step also logs the beginning and the end of the
wait, and the runner copies the file into the pipeline execution state at the next line of the
step's log ([the runner](runner.md)). Without the variable, as outside a run of the runner,
nothing is written.

## Libraries

A library logs through `logging`'s root logger. Without a handler there, `logging` writes a
warning as the bare message, and the MCP SDK's server configures the root logger with a format of
its own. In both cases, the line reaches the runner without a level, and the runner labels it
`INFO`. `logs.capture_libraries()` puts this module's handler on the root logger at `WARNING`. A
library's warning then becomes a line in this format under the library's logger name, such as
`WARNING mcp.server.lowlevel: ...`. Its `INFO` lines, such as the OpenAI client's line for every
request, are dropped. The classification script, `changed_labels.py` and the MCP server call
`logs.capture_libraries()`. The cloning step imports no library that logs.

## Container entrypoint

`deploy/entrypoint.py` uses standard-library logging directly, because it has to log when the
package is missing or cannot be imported. Its `LOG_FORMAT` matches `logs.FORMAT`, and
`deploy/test_container.py` checks that the two are equal. The sampling, classification, analysis
and website scripts import `agentcontext.logs` and use the same format.

## Failure messages

Name in a failure message the operation, the repository and the input or output path concerned,
and give the exit code and the error text when they are available. State which output is missing
and whether the collection continues or needs a retry. Redact credentials before you write Git's
error text to a log. Do not attribute a failure to the network or to memory when all that you
know is a failed command or a terminated worker.

For example, the message for a failed query of the committed files names `owner/repo`, the
temporary clone path and `git -C <clone> ls-tree -r -z HEAD`. It gives Git's exit code and stderr,
names the file list that was not saved, and states that the submodule metadata is missing while
the copying of the configuration continues. The repository's clone log keeps the same message
under `FILE_LIST_ERROR`.
