"""One line format for every log message that the pipeline writes, built on `logging`.

The format is `FORMAT` below, a level, a component and a message, written to stderr:

    ERROR pre_checks: '7zz' not found on PATH (required by collected_files_archive and release).
    WARNING repository_cloning: 3 clone(s) failed and are retried by the next run.
    INFO runner: Repository cloning finished in 54210s.

A reader filters on the level. The component names the module that wrote the line, and the
message states the event as a sentence with a period at its end rather than a tag. In a
container log, where the output of every step is interleaved with the runner's, the component
identifies the part of the run that wrote a line. The levels are `logging`'s own, with its
names and its numbers.

Each component is a `logging` logger of that name: `%(name)s` is the component, and `logging`
handles the levels, the filtering and the writing. The package and the entrypoint share the
format, the one constant `FORMAT`. The handler resolves `sys.stderr` at every write
(`StderrHandler`), and each component logger keeps its lines off the root logger (`logger`).

Every line that a program of the pipeline writes, including its counts, its tables and the names
of the files that it wrote, goes to stderr through this module. No program prints anything except
the help of the command line. In a run, the runner passes the lines of the programs that a step
starts, such as git or 7zz, through `labeled`, which gives each line without a level the level
INFO and the step as its component.
"""

from __future__ import annotations

import logging
import re
import sys
import time

# `deploy/entrypoint.py` repeats this string rather than importing this module, because the
# entrypoint also runs when the package cannot be imported and has to log that.
# `deploy/test_container.py` checks that the two strings are equal.
FORMAT = "%(levelname)s %(name)s: %(message)s"

# This module adds no level of its own. A reader who knows what WARNING means in Python knows what
# it means here, and `logging` filters on a level's number rather than on a word in a string. The
# pipeline writes INFO, WARNING and ERROR. It writes no CRITICAL, which adds nothing to ERROR when
# the process exits anyway, and no DEBUG, which is for lines that nobody would read on a long run.


STEP_NAMES = {
    'engineered_project_selection': "Engineered project selection",
    'repository_cloning': "Repository cloning",
    'commit_reclassification': "Commit reclassification",
    'collected_file_cleanup': "Collected file cleanup",
    'detection': "Detection",
    'file_history': "File history",
    'import_detection': "Import detection",
    "output_cross_check": "Output cross check",
    'unmatched_paths_report': "Unmatched paths report",
    "deleted_files_report": "Deleted files report",
    "collection_summary": "Collection summary",
    'collected_files_archive': "Collected files archive",
    'release': "Release",
    'commit_attribution_validation': "Commit attribution validation",
    'unmatched_commits_report': "Unmatched commits report",
    "ghs_download": "GHS download",
    "github_query": "GitHub query",
    "retrieval_comparison": "Retrieval comparison",
    "repository_filtering": "Repository filtering",
    "repository_labeling": "Repository labeling",
    "reproduction_check": "Analysis reproduction check",
    "rq2_config_cooccurrence": "Configuration co-occurrence analysis",
    "rq2_config_mechanism_upset": "Configuration mechanism intersection plot generation",
    "rq2_config_tool_heatmap": "Configuration tool heatmap generation",
    "rq2_content_quality": "Configuration content analysis",
    "rq2_language_ranking_all": "Repository language ranking",
    "rq2_language_tool_association": "Language and tool association analysis",
    "rq2_repo_metadata_comparison": "Repository metadata comparison",
    "rq2_tool_cooccurrence": "Tool co-occurrence analysis",
    "rq2_tool_count_distribution": "Tool count distribution analysis",
    "rq2_upset_plot": "Tool intersection plot generation",
    "rq3_adoption_timeline": "Configuration adoption timeline generation",
    "rq3_cooccurrence_analysis": "Configuration creation order analysis",
    "rq3_count_distribution": "Configuration count distribution analysis",
    'rq2_venn_diagram': 'Tool Venn diagram generation',
    'dataset_artifact_distribution': 'Configuration artifact distribution analysis',
    'dataset_tool_adoption': 'Tool adoption analysis',
    'dataset_monthly_ai_commit_volume': 'Monthly AI commit volume analysis',
    'dataset_commit_message_first_words': 'Commit message opening analysis',
    'dataset_filter_steps_table': 'Filter steps table generation',
    'dataset_planning_documents': 'Planning document table generation',
    'dataset_imports': 'Import table generation',
}


def step_name(key: str) -> str:
    """The noun phrase used in step progress messages."""
    return STEP_NAMES.get(key, key)


class StderrHandler(logging.StreamHandler):
    """A stderr handler that follows a redirect of `sys.stderr`.

    `logging.StreamHandler()` stores the `sys.stderr` of the moment at which it is built, and a
    handler built for the first log line of a process keeps the real stderr for the rest of the
    process. The suites read a log message by running a step under
    `contextlib.redirect_stderr`, and the workers of the cloning step are spawned processes with
    a stderr of their own. In both cases the handler has to write to the stream that is current
    at the write, not to the stream that was current when the handler was built.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


_HANDLER = StderrHandler()


def logger(component: str) -> logging.Logger:
    """The logger for one component, configured on first use.

    The logger sets `propagate = False`. Otherwise a program that imports the package and calls
    `logging.basicConfig()` for its own code would write every log message of the pipeline
    twice, once in this format and once in its own.
    """
    log = logging.getLogger(component)
    if _HANDLER not in log.handlers:
        log.addHandler(_HANDLER)
        log.propagate = False
        log.setLevel(logging.INFO)
    return log


def line(level: str, component: str, message: str) -> str:
    """The formatted line, without the newline, for a caller that needs the string.

    `Progress.note` returns the formatted progress line, and `labeled` formats the lines of a
    child process that lack a prefix. Both use the formatter of the handler.

    `level` is a level name of `logging`. An unknown level name raises `ValueError` rather than
    giving a line that reads `Level 25 collect:`.
    """
    levels = logging.getLevelNamesMapping()
    if level not in levels:
        raise ValueError(f"level is {level!r}, not one of {sorted(levels)}")
    record = logging.LogRecord(component, levels[level], __file__, 0, message, None, None)
    return _HANDLER.format(record)


# The level and the component at the head of a formatted line, which `labeled` uses to
# distinguish a log message from a line that a program printed.
_FORMATTED = re.compile(r"^(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) [A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*: ")


def labeled(text: str, component: str) -> str | None:
    """A line that a step wrote, as a line of the run's log: unchanged when it is a log message
    already, with `INFO <component>:` in front of it when it has no level, and None when it is
    empty.

    The runner passes every line of a step through this function. A line without a level comes
    from a program that a step starts, such as git or 7zz, or is a line after the first of a
    message that quotes a traceback.
    """
    text = text.rstrip("\r\n")
    if not text.strip():
        return None
    return text if _FORMATTED.match(text) else line("INFO", component, text)


def messages(text: str) -> str:
    """The messages of a log, one per line, without their level and component, for a test that
    compares a program's counts and tables."""
    lines = [_FORMATTED.sub("", line) for line in text.splitlines()]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


# The minimum interval, in seconds, between two progress lines of a long step.
# `guards.PROGRESS_SECONDS`, the interval of the cloning step, takes its value from here. A step
# that ends within ten minutes writes no progress line, because the runner's `starting` and
# `finished` lines already mark its start and its end.
PROGRESS_SECONDS = 600.0


class Pace:
    """Whether a progress line is due: once `seconds` have passed since the last one, or since
    the step began."""

    def __init__(self, seconds: float = PROGRESS_SECONDS, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.last = clock()

    def due(self) -> bool:
        now = self.clock()
        if now - self.last < self.seconds:
            return False
        self.last = now
        return True


def hms(seconds: float) -> str:
    """Seconds as h:mm:ss."""
    whole = int(round(seconds))
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


class Progress:
    """The progress line of a step that works through a list of repositories: the share done,
    the rate and the time left.

    The class writes the line after every `every` finished repositories, and on a slow run after
    `seconds` without a line, so that the console gets a line at least that often while
    repositories finish. The rate is the number of repositories that this run finished divided
    by its own wall clock time, and the time left is the pending count at that rate. A resumed
    run counts the repositories that it had finished before as done, but not in the rate. The
    cloning step writes the line through `guards.Progress`, and the classification stage through
    this class. A step appends its own counts to the line through `details`.
    """

    def __init__(self, component: str, total: int, already_done: int, every: int,
                 seconds: float = PROGRESS_SECONDS, clock=time.monotonic):
        self.component = component
        self.total, self.already_done = total, already_done
        self.every, self.seconds = max(1, every), seconds
        self.clock = clock
        self.started = self.last = clock()
        self.finished = self.failed = 0

    def note(self, failed: bool) -> "str | None":
        """Count one finished repository, and log and return the line when one is due.

        The line is returned as well as logged, so that a caller such as a test does not have to
        read the stream back.
        """
        self.finished += 1
        self.failed += 1 if failed else 0
        now = self.clock()
        if self.finished % self.every and now - self.last < self.seconds:
            return None
        self.last = now
        # The message is computed once, because `details` may read /proc, and a second call could
        # make the logged line and the returned line differ.
        message = self.message(now)
        info(self.component, message)
        return line("INFO", self.component, message)

    def message(self, now: float) -> str:
        """The share done, the rate and the time left, and the counts that `details` appends,
        as a sentence without the level and the component."""
        done = self.already_done + self.finished
        elapsed = max(now - self.started, 1e-9)
        rate = self.finished / elapsed
        pending = max(0, self.total - done)
        left = hms(pending / rate) if rate > 0 else "unknown"
        share = done / self.total if self.total else 1.0
        return (f"{done:,}/{self.total:,} ({share:.1%}) done, "
                f"{self.finished - self.failed:,} ok and {self.failed:,} failed this run, "
                f"{rate * 60:.1f} repositories/min, elapsed {hms(elapsed)}, "
                f"about {left} left at this rate{self.details()}.")

    def details(self) -> str:
        """The counts that a step appends to the line, starting with ", ". This class appends
        nothing."""
        return ""


def capture_libraries(level: int = logging.WARNING) -> None:
    """Write the lines that the imported libraries log at `level` or above in this format, under
    their own logger names, and drop the rest.

    A library logs through `logging`'s root logger. With no handler there, `logging` writes a
    warning as the bare message, and a library that configures the root itself, as the MCP
    SDK's server does with `basicConfig`, writes its own format. Either way the line reaches the
    runner without a level, and the runner labels it `INFO`, even a warning. This function leaves
    the lines of the pipeline's own loggers unchanged, because they do not propagate to the root.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(_HANDLER)
    root.setLevel(level)


def error(component: str, message: str) -> None:
    """A run, a step or a check cannot complete its operation."""
    logger(component).error(message)


def warning(component: str, message: str) -> None:
    """A condition that a reader has to know about, in a result that remains usable."""
    logger(component).warning(message)


def info(component: str, message: str) -> None:
    """An event of the run, for its log."""
    logger(component).info(message)


# The environment variable with which the runner names the file in which a running step states
# what it waits for, `run_logs/<step>.status.json` (`runner.run_step`). The runner copies the file
# into the pipeline execution state at the next line of the step's log, which the step writes
# when it begins or ends the wait.
STATUS_VARIABLE = "AGENTCONTEXT_STEP_STATUS"


def step_status(status: dict | None) -> None:
    """State `status`, what the running step waits for, such as credit, in the file that the
    runner names in STATUS_VARIABLE, or remove the file when `status` is None. Without the
    variable, as outside a run of the runner, nothing is written. A file that cannot be written
    leaves a WARNING, and the step goes on, because the log states the wait too."""
    import json
    import os
    named = os.environ.get(STATUS_VARIABLE)
    if not named:
        return
    try:
        if status is None:
            if os.path.exists(named):
                os.unlink(named)
            return
        temporary = f"{named}.tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            f.write(json.dumps(status, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, named)
    except OSError as failure:
        warning("logs", f"Could not write the status of the step to {named} ({failure}).")
