"""Tests for agentcontext.logs: the format in which every log message is written, the stderr
handler that follows a redirect, and the rule that the package writes no other format.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_logs -v
"""

import ast
import contextlib
import io
import logging
import re
import unittest
from pathlib import Path

from agentcontext import logs as _mod

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "agentcontext"
# Every program of the pipeline, not only the package. The scripts of the other stages write to
# the same terminal and, through `run_pipeline.py`, to the same log. The website builder is
# included because it reads the output CSVs of the collection and logs its errors in the same
# way. `data/` is excluded because it contains data rather than code and because its collected
# tree contains repository directories named `*.py`.
PROGRAMS = ("agentcontext", "1_sampling", "2_classification", "4_data-analysis", "_website")
# `run_pipeline.py` at the root runs the stages as one list. It is a program of the pipeline
# too, and the tests read it with the directories above.
ROOT_SCRIPTS = ("run_pipeline.py",)
# The programs that the wording checks read: the collection stage's package, the container's
# entrypoint and the pipeline runner. The checks read the first word of each message, the colons
# in each message and exception text, and the kind that each token is named as.
MESSAGE_SOURCES = ("agentcontext", "deploy/entrypoint.py", "run_pipeline.py")
# The site that `_website/build_site.py` writes, a gitignored copy of the site with the usage
# example, which is no program of the pipeline.
BUILD_OUTPUT = ("_website/_site/",)


def program_files() -> list:
    """The modules of `ROOT_SCRIPTS` and of `PROGRAMS`, without the build output."""
    paths = [ROOT / name for name in ROOT_SCRIPTS]
    for program in PROGRAMS:
        paths.extend(sorted((ROOT / program).rglob("*.py")))
    return [path for path in paths
            if not path.relative_to(ROOT).as_posix().startswith(BUILD_OUTPUT)]


def message_sources():
    """Yield (relative path, parsed module) for every module of `MESSAGE_SOURCES` except the
    tests."""
    for name in MESSAGE_SOURCES:
        path = ROOT / name
        for module in [path] if path.is_file() else sorted(path.rglob("*.py")):
            if not module.name.startswith("test_"):
                yield (module.relative_to(ROOT).as_posix(),
                       ast.parse(module.read_text(encoding="utf-8")))


def log_messages(tree) -> list:
    """(line, message expression) for every log message that a module writes.

    The message is the last argument of `logs.info`, `logs.warning`, `logs.error` and
    `logs.line`. `log`, `log_warning` and `log_error` name the entrypoint's logger and the
    logger that `subsample.select` receives, and in a call to one of them the message is the
    first argument. A `SystemExit` message counts too, because the runner logs it when it
    catches the exception.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "logs"
                and func.attr in ("info", "warning", "error", "line")):
            found.append((node.lineno, node.args[-1]))
        elif isinstance(func, ast.Name) and func.id in ("log", "log_warning", "log_error",
                                                        "SystemExit"):
            found.append((node.lineno, node.args[0]))
    return found


def help_texts(tree) -> list:
    """(line, text expression) for every `help=` of the module, which `--help` prints."""
    return [(node.lineno, keyword.value) for node in ast.walk(tree) if isinstance(node, ast.Call)
            for keyword in node.keywords if keyword.arg == "help"]


def leading_texts(node) -> list:
    """The literal text that a message can start with, one entry per wording of the message.

    A message that starts with an expression gives nothing to check, because its first word is
    known only when the line is written. Inside braces, both wordings of an `IfExp` are read. A
    string constant padded inside the braces, such as `{'mechanism':14}`, is left out, because it
    is the header of a table column and names the column.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant):
            return [first.value]
        if isinstance(first, ast.FormattedValue) and isinstance(first.value, ast.IfExp):
            return leading_texts(first.value)
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return leading_texts(node.left)
    if isinstance(node, ast.IfExp):
        return leading_texts(node.body) + leading_texts(node.orelse)
    return []


def literal_text(node) -> str:
    """The literal parts of a message, without the expressions in its braces, except the two
    wordings of an `IfExp`. A padded constant in braces is a column header and is left out."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant)
                       else literal_text(part.value) if isinstance(part.value, ast.IfExp)
                       else " " for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_text(node.left) + " " + literal_text(node.right)
    if isinstance(node, ast.IfExp):
        return literal_text(node.body) + " " + literal_text(node.orelse)
    return ""


def exception_texts(tree) -> list:
    """(line, text expression) for the text of every exception that a module raises, the first
    argument of `raise Name(...)` or `raise module.Name(...)`. A caller logs the text or prints
    it, as the runner logs a `SystemExit`."""
    return [(node.lineno, node.exc.args[0]) for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args]


def template(node) -> str:
    """The text of a message with `{}` in place of each expression. Both wordings of an `IfExp`
    are kept, joined by `|`, also inside braces, so that a colon in either wording is seen."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant)
                       else f"{{{template(part.value)}}}" if isinstance(part.value, ast.IfExp)
                       else "{}" for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return template(node.left) + template(node.right)
    if isinstance(node, ast.IfExp):
        return template(node.body) + "|" + template(node.orelse)
    return "{}"


# What a message may contain with a colon: a URL, a code span such as `init: true`, and a time
# such as 0:05:00 or an offset such as UTC+02:00. A colon inside an identifier or a format, such
# as `repository_cloning:"--fresh"` or `NAME:"args"`, and a colon between two expressions have
# no space after them and are not punctuation. Text that the program quotes from another
# program, such as git's stderr, is an expression of the message.
ALLOWED_COLONS = re.compile(r"https?://\S+|`[^`]*`|\d+:\d\d(?::\d\d)?")
# A colon as punctuation: one that a space follows or that ends the text.
PUNCTUATION_COLON = re.compile(r":(?=\s|$)")


def punctuation_colons(tree) -> list:
    """(line, text) for every log message and exception text of `tree` with a colon as
    punctuation."""
    found = []
    for line, node in log_messages(tree) + exception_texts(tree):
        text = template(node)
        if PUNCTUATION_COLON.search(ALLOWED_COLONS.sub("", text)):
            found.append((line, text))
    return sorted(set(found))


# The first word of a message ends at a space or punctuation, and a prose word consists of
# lowercase letters alone. A word with `_`, `/`, `.`, `-`, `=`, a digit or a backtick is an
# identifier that keeps its own spelling, such as a path, a step key, a flag, a column name, a
# command quoted as `git rev-parse` or a number.
FIRST_WORD = re.compile(r"[^\s:;,()'\"]+")
PROSE_WORD = re.compile(r"[a-z]+")


def lowercase_starts(tree) -> list:
    """(line, word) for every log message of `tree` that starts with a lowercase prose word."""
    found = []
    for line, message in log_messages(tree):
        for text in leading_texts(message):
            word = FIRST_WORD.match(text.lstrip())
            if word and PROSE_WORD.fullmatch(word.group()):
                found.append((line, word.group()))
    return found


# The kinds of token that a message may name, each written before the word "token". A token
# can be a credential, a unit of model input or a piece of a parsed line, and a message
# therefore names the kind. The pipeline calls GitHub with GitHub access tokens. A message about
# another kind of token names that kind, and the kind is added to this tuple.
TOKEN_KINDS = ("GitHub access",)
TOKEN_WORD = re.compile(r"(?<![\w-])tokens?(?![\w-])", re.I)


def unnamed_tokens(text: str) -> list:
    """Every `token` or `tokens` in `text` that no kind of `TOKEN_KINDS` comes before.
    `GITHUB_TOKEN`, `--token-file` and `x-access-token` are identifiers and are not counted."""
    return [match.group() for match in TOKEN_WORD.finditer(text)
            if not any(text[:match.start()].endswith(f"{kind} ") for kind in TOKEN_KINDS)]


class TestTheFormat(unittest.TestCase):
    """`LEVEL component: message`, and the level names are `logging`'s."""

    def _captured(self, call):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            call()
        return err.getvalue()

    def test_a_library_warning_is_a_formatted_line(self):
        from agentcontext import repos
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            repos.parse_and_convert_to_utc("not a date")
        self.assertRegex(err.getvalue(), r"^WARNING repos: Could not parse the timestamp")

    def test_each_level_writes_its_own_name(self):
        self.assertEqual(self._captured(lambda: _mod.info("runner", "Collection finished")),
                         "INFO runner: Collection finished\n")
        self.assertEqual(self._captured(lambda: _mod.warning('repository_cloning', "3 clones failed")),
                         "WARNING repository_cloning: 3 clones failed\n")
        self.assertEqual(self._captured(lambda: _mod.error('detection', "no head_sha")),
                         "ERROR detection: no head_sha\n")

    def test_line_returns_what_a_logged_line_would_say(self):
        self.assertEqual(_mod.line("ERROR", "runner", "state.json is unreadable"),
                         "ERROR runner: state.json is unreadable")
        self.assertEqual(
            _mod.line("INFO", 'repository_cloning', "1/2 done"),
            self._captured(lambda: _mod.info('repository_cloning', "1/2 done")).rstrip("\n"))

    def test_a_level_logging_does_not_have_is_refused(self):
        """A level that is not one of `logging`'s, such as `NOTE`, would appear as
        `Level 25 collection:` in the log."""
        for level in ("NOTE", "TRACE", "warning"):
            with self.subTest(level=level), self.assertRaises(ValueError):
                _mod.line(level, 'repository_cloning', "x")
        for level in sorted(logging.getLevelNamesMapping()):
            with self.subTest(level=level):
                self.assertTrue(_mod.line(level, 'repository_cloning', "x").endswith("repository_cloning: x"))


class TestTheStream(unittest.TestCase):
    """Every line goes to `sys.stderr` as it is at the moment of writing."""

    def test_a_redirect_after_the_first_line_is_still_seen(self):
        """`logging.StreamHandler()` keeps the stream that it is built with. A handler built at
        a process's first log line would keep the real stderr for the rest of the process, and
        every suite that reads a log message through `redirect_stderr` would read nothing."""
        first = io.StringIO()
        with contextlib.redirect_stderr(first):
            _mod.info('repository_cloning', "one")
        second = io.StringIO()
        with contextlib.redirect_stderr(second):
            _mod.info('repository_cloning', "two")
        self.assertEqual(first.getvalue(), "INFO repository_cloning: one\n")
        self.assertEqual(second.getvalue(), "INFO repository_cloning: two\n")

    def test_a_program_with_its_own_root_handler_gets_one_line(self):
        """The package's logger sets `propagate = False`. Without it, a program that imports
        the package and calls `logging.basicConfig()` for its own code would get every line
        twice, once in this format and once in its own."""
        root, theirs = logging.getLogger(), io.StringIO()
        handler = logging.StreamHandler(theirs)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _mod.info('detection', "one")
        self.assertEqual(err.getvalue(), "INFO detection: one\n")
        self.assertEqual(theirs.getvalue(), "")

    def test_nothing_is_written_to_stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            _mod.info('repository_cloning', "one")
            _mod.error('repository_cloning', "two")
        self.assertEqual(out.getvalue(), "")


class TestTheFormatIsTheOnlyOne(unittest.TestCase):
    """No program of the pipeline writes to stderr except through `agentcontext.logs`.

    The tests below refuse, in every step, a tag that mixes a level, an event and a component in
    one format, and an `ERROR:` without a component. They also check that only
    `agentcontext.logs` writes to stderr and that a `SystemExit` message is a plain sentence that
    the interpreter prints.
    """

    EXCEPT = ()

    # A tag at the start of a printed line. `[Errno 28]` inside a message and a `[1]` of a
    # shell example are not tags. The pattern therefore matches only at the start of the
    # printed text, and only after `print` itself, not after a name that ends in `print`, such
    # as `code_fingerprint([...])`.
    TAG = re.compile(r'''\bprint\((?:\s|f|["'])*\\?n?\[[A-Za-z0-9]''')

    def _sources(self):
        paths = program_files()
        for path in paths:
            rel = path.relative_to(ROOT).as_posix()
            if not path.is_file() or path.name.startswith("test_") or rel in self.EXCEPT:
                continue
            yield rel, path.read_text(encoding="utf-8")

    def test_the_pattern_finds_a_printed_tag_and_no_call_that_ends_in_print(self):
        self.assertTrue(self.TAG.search('print("[cloning] done")'))
        self.assertTrue(self.TAG.search("print(f'\\n[step] {name}')"))
        self.assertIsNone(self.TAG.search("code_fingerprint([Path(__file__)])"))

    def test_no_module_prints_a_bracketed_tag(self):
        offenders = [rel for rel, text in self._sources() if self.TAG.search(text)]
        self.assertEqual(offenders, [])

    def test_nothing_but_this_module_writes_to_stderr(self):
        """The rule that the other tests of this class check in detail.

        Every log message goes through `agentcontext.logs`. A `print(..., file=sys.stderr)`
        anywhere else writes a line without a level and a component.
        """
        offenders = []
        for rel, text in self._sources():
            if rel == "agentcontext/logs.py":
                continue
            offenders.extend(f"{rel}:{line}" for line in self._stderr_writes(ast.parse(text)))
        self.assertEqual(offenders, [])

    @staticmethod
    def _stderr_writes(tree) -> list:
        """The lines of `tree` that write to stderr other than through `agentcontext.logs`.

        The forms are a `print` with a `file=`, a `write` or `writelines` on `sys.stderr`, a use
        of `sys.stderr.buffer`, `stderr` imported from `sys`, and a direct use of `logging` or of
        `warnings.warn`.
        """
        def is_stderr(node):
            return (isinstance(node, ast.Attribute) and node.attr in ("stderr", "__stderr__")
                    and isinstance(node.value, ast.Name) and node.value.id == "sys")

        lines = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                    and any(k.arg == "file" for k in node.keywords)):
                lines.append(node.lineno)
            elif (isinstance(node, ast.Attribute) and is_stderr(node.value)
                  and node.attr in ("write", "writelines", "buffer")):
                lines.append(node.lineno)
            elif (isinstance(node, ast.ImportFrom) and node.module == "sys"
                  and any(a.name in ("stderr", "__stderr__") for a in node.names)):
                lines.append(node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.module == "logging":
                lines.append(node.lineno)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and ((node.func.value.id == "logging" and node.func.attr in (
                      "getLogger", "basicConfig", "debug", "info", "warning", "warn",
                      "error", "exception", "critical", "log"))
                       or (node.func.value.id == "warnings" and node.func.attr == "warn"))):
                lines.append(node.lineno)
        return sorted(lines)

    def test_every_way_of_writing_to_stderr_is_seen(self):
        """The check above, run over code that contains each form and the one use of stderr
        that is not a write: the `sys.stderr.flush()` of `steps/file_history.py`."""
        tree = ast.parse("import sys\n"
                         "print('x', file=sys.stderr)\n"
                         "sys.stderr.write('x')\n"
                         "sys.stderr.buffer.write(b'x')\n"
                         "sys.__stderr__.writelines(['x'])\n"
                         "from sys import stderr\n"
                         "sys.stderr.flush()\n"
                         "print('x')\n"
                         "import logging\n"
                         "log = logging.getLogger(__name__)\n"
                         "logging.warning('x')\n"
                         "import warnings\n"
                         "warnings.warn('x')\n"
                         "warnings.filterwarnings('ignore')\n"
                         "from logging import warning\n")
        self.assertEqual(self._stderr_writes(tree), [2, 3, 4, 5, 6, 10, 11, 13, 15])

    def test_no_module_writes_a_level_inside_the_text(self):
        """The test looks for a level word anywhere in a printed string, not only at its start.
        It finds a line such as `print(f"    WARNING: ...")`, indented under the lines about one
        repository, which a pattern anchored at the first character would miss."""
        pattern = re.compile(r"\b(?:ERROR|WARNING|WARN|NOTE)\b\s*:")
        offenders = []
        for rel, text in self._sources():
            for node in ast.walk(ast.parse(text)):
                if not (isinstance(node, ast.Call) and node.args and (
                        (isinstance(node.func, ast.Name) and node.func.id == "print")
                        or (isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "logs"
                            and node.func.attr in ("info", "warning", "error")
                            and len(node.args) == 2))):
                    continue
                first = node.args[-1]
                parts = first.values if isinstance(first, ast.JoinedStr) else [first]
                printed = "".join(v.value for v in parts if isinstance(v, ast.Constant)
                                  and isinstance(v.value, str))
                if pattern.search(printed):
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_a_system_exit_message_is_a_plain_sentence(self):
        """A `SystemExit` message is the one message that does not go through
        `agentcontext.logs`. Python prints the message itself, and a log line formatted into it
        would appear inside the interpreter's own format. The runner catches the messages that
        its guards raise and logs them under its own component. A guard therefore does not need
        to know the component of the line."""
        offenders = []
        for rel, text in self._sources():
            for node in ast.walk(ast.parse(text)):
                if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                        and isinstance(node.exc.func, ast.Name)
                        and node.exc.func.id == "SystemExit" and node.exc.args):
                    continue
                inner = node.exc.args[0]
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "line"):
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_the_format_is_defined_only_in_the_package(self):
        """Nothing outside `agentcontext/logs.py` builds the line itself, and a change of the
        format remains one edit. `deploy/entrypoint.py` is the documented exception, and
        `deploy/test_container.py` checks the entrypoint's copy of the format against `FORMAT`."""
        offenders = [rel for rel, text in self._sources()
                     if "%(levelname)s" in text and rel != "agentcontext/logs.py"]
        self.assertEqual(offenders, [])

    def test_every_logs_call_names_a_component_and_a_message(self):
        """Each call passes two positional arguments, three for `logs.line`, and no `file=` or
        `flush=` as `print` takes them. `logs.info(..., file=sys.stderr)` raises a TypeError
        only when its branch runs."""
        wrong = []
        for rel, text in self._sources():
            for node in ast.walk(ast.parse(text)):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "logs"
                        and node.func.attr in ("info", "warning", "error", "line")):
                    continue
                expected = 3 if node.func.attr == "line" else 2
                if len(node.args) != expected or node.keywords:
                    wrong.append(f"{rel}:{node.lineno}")
        self.assertEqual(wrong, [])


class TestTheLabelOfAStepLine(unittest.TestCase):
    """`labeled`, through which the runner passes every line of a step."""

    def test_a_printed_line_gets_the_step_as_its_component(self):
        self.assertEqual(_mod.labeled("Wrote 8275 rows to data/validated/repos.csv\n", 'collected_file_cleanup'),
                         "INFO collected_file_cleanup: Wrote 8275 rows to data/validated/repos.csv")

    def test_a_log_message_is_kept_as_it_is(self):
        for text in ("INFO caches: Git facts: 3 measured", "WARNING repository_cloning: o/r: no file list",
                     "ERROR file_history: output directory not found: x"):
            self.assertEqual(_mod.labeled(text + "\n", 'detection'), text)

    def test_engineered_project_selection_component_is_preserved_and_its_message_can_be_read(self):
        text = "INFO engineered_project_selection: selected 5 repositories"
        self.assertEqual(_mod.labeled(text, 'engineered_project_selection'), text)
        self.assertEqual(_mod.messages(text), "selected 5 repositories")

    def test_an_empty_line_is_none(self):
        for text in ("\n", "   \n", "\r\n", ""):
            self.assertIsNone(_mod.labeled(text, 'detection'))

    def test_indentation_is_kept(self):
        self.assertEqual(_mod.labeled("  settings.csv: 2600 rows", 'detection'),
                         "INFO detection:   settings.csv: 2600 rows")


class TestAStepLogsUnderItsKey(unittest.TestCase):
    """The runner labels a step's printed lines with the step key. The step's own log messages
    use the key too, because otherwise one step would write under two names."""

    UNMATCHED_FUNCTIONS = {"_table": {'unmatched_paths_report'},
                           "main_paths": {'unmatched_paths_report'},
                           "main_commits": {'unmatched_commits_report'},
                           "main_reduce": {"unmatched"}}

    @staticmethod
    def wrong_components(tree, step_keys, functions=None):
        constants = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     and isinstance(n.value, ast.Constant) for t in n.targets
                     if isinstance(t, ast.Name)}
        wrong = []
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            allowed = functions.get(function.name, set()) if functions is not None else step_keys
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "logs"
                        and node.func.attr in ("info", "warning", "error") and node.args):
                    continue
                first = node.args[0]
                name = (first.value if isinstance(first, ast.Constant)
                        else constants.get(first.id) if isinstance(first, ast.Name) else None)
                if name not in allowed:
                    wrong.append((node.lineno, name))
        return wrong

    def test_every_component_a_step_module_names_is_its_step_key(self):
        from agentcontext import runner
        keys: dict = {}
        for step in runner.steps_for(True):
            module = step.args[step.args.index("-m") + 1]
            keys.setdefault(module.replace(".", "/") + ".py", set()).add(step.key)
        self.assertIn('commit_reclassification', keys["agentcontext/steps/commit_reclassification.py"])
        wrong = []
        for rel, step_keys in sorted(keys.items()):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            functions = self.UNMATCHED_FUNCTIONS if rel == "agentcontext/checks/unmatched.py" else None
            wrong.extend(f"{rel}:{line} {name!r}"
                         for line, name in self.wrong_components(tree, step_keys, functions))
        self.assertEqual(wrong, [])

    def test_the_scripts_of_the_other_runners_log_under_their_step_keys(self):
        """The steps of the sampling, classification and analysis stages are scripts. Each
        script logs under its step key, as the collection's steps do, and not under the
        script's name, such as `query_github` for `github_query`. The library modules of these
        stages keep their own components: `ghs_cache.py`, `readmes.py`,
        `classification_tools.py`, `_shared.py` and `rq2/_repo_metadata.py`."""
        import importlib.util
        wrong, scripts = [], 0
        for directory, name in (("1_sampling", "run_sampling"),
                                ("2_classification", "run_classification"),
                                ("4_data-analysis", "run_analysis")):
            spec = importlib.util.spec_from_file_location(f"_runner_{name}",
                                                          ROOT / directory / f"{name}.py")
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            for step in runner.STEPS:
                if step.args[0] == "-m":
                    continue  # a step of the package, which the suites of its modules check
                rel = f"{step.code}/{step.args[0]}"
                tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
                scripts += 1
                wrong.extend(f"{rel}:{line} {name!r} is not {step.key!r}"
                             for line, name in self.wrong_components(tree, {step.key}))
        self.assertGreaterEqual(scripts, 25)
        self.assertEqual(wrong, [])

    def test_reduce_component_is_rejected_in_the_paths_subcommand(self):
        tree = ast.parse('def main_paths():\n    logs.info("unmatched", "message")\n')
        self.assertEqual(self.wrong_components(tree, {'unmatched_paths_report', 'unmatched_commits_report'},
                                              self.UNMATCHED_FUNCTIONS), [(2, "unmatched")])


class TestTheMessages(unittest.TestCase):

    def test_each_line_loses_its_level_and_component_and_nothing_else(self):
        text = ("INFO count_distribution: Skills (n=17):\n"
                "INFO count_distribution:   min=1, max=24\n"
                "WARNING detect: 2 rows: blank\n"
                "a line git wrote\n")
        self.assertEqual(_mod.messages(text),
                         "Skills (n=17):\n  min=1, max=24\n2 rows: blank\na line git wrote\n")


class TestThePace(unittest.TestCase):
    """A progress line at most once per `PROGRESS_SECONDS`, the cloning step's pace."""

    def test_the_first_line_waits_a_whole_interval(self):
        now = [0.0]
        pace = _mod.Pace(600, clock=lambda: now[0])
        self.assertFalse(pace.due())
        now[0] = 599.9
        self.assertFalse(pace.due())
        now[0] = 600.0
        self.assertTrue(pace.due())
        self.assertFalse(pace.due(), "and the next one waits another")
        now[0] = 1200.0
        self.assertTrue(pace.due())

    def test_the_cloning_step_keeps_the_same_pace(self):
        from agentcontext import guards
        self.assertEqual(guards.PROGRESS_SECONDS, _mod.PROGRESS_SECONDS)


class TestTheProgressLine(unittest.TestCase):
    """The progress line of a step that works through repositories, under the step's
    component, with the counts that the step appends. The cloning step's own tests are in
    tests.test_guards."""

    def test_the_line_names_the_component_and_ends_with_the_details_and_a_period(self):
        class Counting(_mod.Progress):
            def details(self):
                return ", 7 requests"
        now = [0.0]
        progress = Counting("repository_labeling", 10, 4, 2, clock=lambda: now[0])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(progress.note(False))
            now[0] = 60.0
            line = progress.note(True)
        self.assertEqual(line, "INFO repository_labeling: 6/10 (60.0%) done, 1 ok and 1 "
                               "failed this run, 2.0 repositories/min, elapsed 0:01:00, about "
                               "0:02:00 left at this rate, 7 requests.")
        self.assertEqual(err.getvalue(), line + "\n")


class TestTheLibrariesLog(unittest.TestCase):
    """`capture_libraries`: What a library logs through the root logger appears in this format
    from WARNING up, and nothing below WARNING appears."""

    def setUp(self):
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)
        theirs = logging.StreamHandler(io.StringIO())
        root.addHandler(theirs)

        def restore():
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in saved[0]:
                root.addHandler(handler)
            root.setLevel(saved[1])
        self.addCleanup(restore)
        self.theirs = theirs

    def test_a_library_warning_is_a_formatted_line_and_its_info_is_dropped(self):
        _mod.capture_libraries()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            logging.getLogger("httpx").info("HTTP Request: POST https://api.openai.com/v1")
            logging.getLogger("mcp.server").warning("Tool failed")
            _mod.info('repository_cloning', "one")
        self.assertEqual(err.getvalue(), "WARNING mcp.server: Tool failed\n"
                                         "INFO repository_cloning: one\n")
        self.assertNotIn(self.theirs, logging.getLogger().handlers)


class TestEveryLineGoesThroughTheLogger(unittest.TestCase):
    """No program of the pipeline calls `print`, except to show its help.

    A count, a table or a `Wrote` line printed for a person to read would be a line of the
    container's log without a level and a component. The help of the command line and of
    `verify` is what `--help` prints. Argparse prints the help to stdout for every command, and
    the help is not a line of any run. The usage example of the website is no program of the
    pipeline: A dataset user runs it in the directory of the downloaded release, without the
    package and its logger, and it prints its results for that user.
    """

    HELP = {("agentcontext/cli.py", 2), ("agentcontext/tools/verify.py", 1)}
    NOT_THE_PIPELINE = {"_website/usage_example.py"}

    def test_no_program_calls_print(self):
        found = []
        paths = program_files()
        counts = {}
        for path in paths:
            if not path.is_file() or path.name.startswith("test_"):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in self.NOT_THE_PIPELINE:
                continue
            n = sum(1 for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print")
            if n:
                counts[rel] = n
        self.assertEqual(set(counts.items()), self.HELP)

    def test_the_help_is_all_that_is_printed(self):
        """The two exceptions print the usage text and nothing else."""
        for rel, _n in self.HELP:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "print":
                    printed = ast.unparse(node.args[0])
                    with self.subTest(rel=rel, printed=printed):
                        self.assertRegex(printed, r"usage|__doc__")


class TestNoProgramPrintsAnEmptyLine(unittest.TestCase):
    """No `print` and no log message writes an empty line.

    In the container's log, every line is a row with a date and a stream, and an empty line is a
    row without content. A newline at the edge of a printed string writes an empty line, and so
    does a `print()` that separates counts, tables and summaries at a terminal. The lines that a
    header introduces therefore follow it directly.
    """

    @staticmethod
    def _edges(node) -> tuple:
        """The literal text at the start and at the end of a printed expression, or None where
        an expression is there instead."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, node.value
        if isinstance(node, ast.JoinedStr) and node.values:
            first, last = node.values[0], node.values[-1]
            return (first.value if isinstance(first, ast.Constant) else None,
                    last.value if isinstance(last, ast.Constant) else None)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return (TestNoProgramPrintsAnEmptyLine._edges(node.left)[0],
                    TestNoProgramPrintsAnEmptyLine._edges(node.right)[1])
        return None, None

    @classmethod
    def _empty_lines(cls, tree) -> list:
        """The lines of `tree` that print or log an empty line: a `print()`, a newline at either
        edge of the printed text, or two consecutive newlines inside it."""
        lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                printed = node.args
                if not printed:
                    lines.append(node.lineno)
                    continue
            elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "logs" and len(node.args) >= 2):
                printed = node.args[-1:]
            else:
                continue
            start, end = cls._edges(printed[0])[0], cls._edges(printed[-1])[1]
            inside = [v.value for arg in printed
                      for v in ast.walk(arg) if isinstance(v, ast.Constant)
                      and isinstance(v.value, str)]
            if ((start or "").startswith("\n") or (end or "").endswith("\n")
                    or any("\n\n" in text for text in inside)):
                lines.append(node.lineno)
        return sorted(lines)

    def test_no_program_prints_an_empty_line(self):
        offenders = []
        paths = program_files()
        for path in paths:
            if not path.is_file() or path.name.startswith("test_"):
                continue
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders.extend(f"{rel}:{line}" for line in self._empty_lines(tree))
        self.assertEqual(offenders, [])

    def test_every_way_of_printing_an_empty_line_is_seen(self):
        """The check above, run over code that contains each form and the forms that print no
        empty line: a newline between two expressions, and a newline inside a line."""
        tree = ast.parse("print()\n"
                         "print('\\nheader')\n"
                         "print(f'{n} rows\\n')\n"
                         "print('\\n' + '=' * 70)\n"
                         "print(f'a\\n\\nb')\n"
                         "logs.info('collect', f'\\n{n} rows')\n"
                         "print(f'{a}\\n{b}')\n"
                         "print(f'API URL:\\n{url}')\n"
                         "print('=' * 70)\n"
                         "logs.info('collect', f'{n} rows')\n")
        self.assertEqual(self._empty_lines(tree), [1, 2, 3, 4, 5, 6])


class TestAMessageStartsWithACapital(unittest.TestCase):
    """A log message starts with a capital letter unless its first word is an identifier.

    `docs/logs.md` states the rule, so that a reader does not see
    `INFO runner: Collection finished` beside `ERROR runner: could not write run_logs/state.json`.
    """

    def test_no_message_starts_with_a_lowercase_word(self):
        offenders = [f"{rel}:{line} {word!r}" for rel, tree in message_sources()
                     for line, word in lowercase_starts(tree)]
        self.assertEqual(offenders, [])

    def test_every_form_of_a_lowercase_start_is_seen(self):
        """The check above, run over code that contains each form and the starts that it
        allows: an expression, a flag, a quoted command, a path, a column name, a table header,
        a number and a capital."""
        tree = ast.parse("logs.error('x', f'could not write {path}')\n"
                         "logs.info('x', '  none')\n"
                         "logs.error('x', f\"{'neither' if a else 'one'} root holds it\")\n"
                         "raise SystemExit(f'step {key} is unreadable')\n"
                         "log(f'extracted {path}')\n"
                         "logs.line('INFO', 'x', 'stopping')\n"
                         "logs.info('x', 'wrote ' + name)\n"
                         "logs.info('x', f'{repo} failed to clone again')\n"
                         "logs.info('x', '--fresh removed nothing')\n"
                         "logs.warning('x', '`git rev-parse` returned maybe')\n"
                         "logs.error('x', 'data/validated/repos.csv is missing')\n"
                         "logs.error('x', 'head_sha is blank')\n"
                         "logs.info('x', f\"{'mechanism':14} {'rows':>6}\")\n"
                         "logs.error('x', '7zz exited 2')\n"
                         "logs.info('x', 'Wrote ' + name)\n")
        self.assertEqual(lowercase_starts(tree),
                         [(1, "could"), (2, "none"), (3, "neither"), (3, "one"), (4, "step"),
                          (5, "extracted"), (6, "stopping"), (7, "wrote")])


# Exception texts that the classification returns to the model as the result of a tool call
# with invalid arguments. The model reads them, and they keep their wording.
TEXTS_THAT_A_MODEL_READS = {
    "2_classification/classification_tools.py": ("repo_name must be owner/repo: ",
                                                 "revision must be a commit id of 40 hex digits",
                                                 "max_chars must be 0 or more: "),
}


class TestAMessageHasNoColon(unittest.TestCase):
    """A log message and an exception text contain no colon as punctuation.

    A log line reads `LEVEL component: message`, and a colon inside the message gives the reader
    a second colon, as in `WARNING repository_cloning: owner/repo: The retry failed`. A name goes
    into the sentence instead, a label becomes a sentence, and a list goes into parentheses or
    into the sentence, as `docs/logs.md` describes. The check reads the literal text of each
    message. A text that a helper returns or that a variable holds is for the writer to check.
    """

    def test_no_message_has_a_colon(self):
        """Every tracked Python file outside the tests, the stage scripts included."""
        import subprocess
        listed = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                                text=True)
        if listed.returncode != 0:
            self.skipTest("not inside a git checkout")
        offenders = []
        for rel in listed.stdout.split():
            if rel.startswith("tests/") or Path(rel).name.startswith("test_"):
                continue
            try:
                tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            except (SyntaxError, FileNotFoundError):
                continue
            offenders += [f"{rel}:{line} {text!r}" for line, text in punctuation_colons(tree)
                          if not text.startswith(TEXTS_THAT_A_MODEL_READS.get(rel, ()))]
        self.assertEqual(offenders, [])

    def test_every_form_of_a_colon_is_seen(self):
        """The check above, run over code that contains each form of a colon as punctuation and
        each colon that a message may contain."""
        tree = ast.parse("logs.warning('x', f'{repo}: The retry failed')\n"
                         "logs.info('x', 'Time zone of the calendar months: UTC')\n"
                         "logs.error('x', f'Unknown steps: {names}')\n"
                         "raise ValueError(f'{path}: {error}')\n"
                         "raise SystemExit('Stopped: no step ran')\n"
                         "log(f\"{'Mode: on' if a else 'off'}\")\n"
                         "logs.line('INFO', 'x', 'sampling:')\n"
                         "log_error('7zz packing ' + name + ': ' + line)\n"
                         "logs.info('x', f'Checking again in {wait} s, at 0:05:00')\n"
                         "logs.info('x', 'Calendar months are counted in CEST (UTC+02:00)')\n"
                         "logs.info('x', 'Fetched https://api.github.com/rate_limit')\n"
                         "logs.error('x', f'{repo} could not be cloned ({stderr})')\n"
                         "logs.error('x', '--step-arg must be NAME:\"args\"')\n"
                         "logs.warning('x', 'Set `init: true` in compose.yaml')\n"
                         "logs.info('x', f'{hours}:{minutes:02d} left')\n"
                         "raise RuntimeError(f'7zz exited {code} ({output})')\n")
        self.assertEqual([line for line, _text in punctuation_colons(tree)],
                         [1, 2, 3, 4, 5, 6, 7, 8])


def written_messages(tree, own: bool = False) -> list:
    """(line, message expression) for every message that a module writes to the log: the last
    argument of `logs.info`, `logs.warning`, `logs.error` and `logs.debug`, and the first of
    `log`, `log_warning` and `log_error`, as `log_messages` reads them. In `agentcontext/logs.py`
    itself, `own`, the module calls its functions by their bare names, and the last argument of
    `info`, `warning`, `error` and `debug` counts too. `logs.line` writes nothing, and a
    `SystemExit` text is an exception text, which keeps its wording."""
    found = []
    levels = ("info", "warning", "error", "debug")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "logs" and func.attr in levels):
            found.append((node.lineno, node.args[-1]))
        elif own and isinstance(func, ast.Name) and func.id in levels and len(node.args) >= 2:
            found.append((node.lineno, node.args[-1]))
        elif isinstance(func, ast.Name) and func.id in ("log", "log_warning", "log_error"):
            found.append((node.lineno, node.args[0]))
    return found


# The end of a message that the check cannot read: an expression outside an f-string, such as a
# variable, `str(error)` or a `join`. The writer puts the period where such a message is
# assembled, or where its text is written, for an exception text that the message repeats.
COMPUTED = object()
# The end of an f-string whose last part is an expression in braces, such as `{path}`, which
# takes the period after it.
A_VALUE = object()


def endings(node) -> list:
    """Every way in which a message can end: its last literal character, `A_VALUE` or
    `COMPUTED`. Only the text before the first line break counts, because what follows it, such
    as a traceback, is quoted, and the sentence before it ends with the period. Both wordings of
    an `IfExp` count, also inside braces, and an empty wording ends where the text before it
    ends."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.split("\n", 1)[0][-1:]]
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant) and "\n" in part.value:
                parts.append(ast.Constant(part.value.split("\n", 1)[0]))
                break
            parts.append(part)
        return _ends_of_parts(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        right = endings(node.right)
        return ([end for end in right if end != ""] + endings(node.left) if "" in right
                else right)
    if isinstance(node, ast.IfExp):
        return endings(node.body) + endings(node.orelse)
    return [COMPUTED]


def _ends_of_parts(parts: list) -> list:
    """`endings` of the parts of an f-string, read from the last part backwards."""
    found = []
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        if isinstance(part, ast.Constant):
            if part.value:
                return found + [part.value[-1:]]
            continue
        if not isinstance(part.value, ast.IfExp):
            return found + [A_VALUE]
        wordings = endings(part.value)
        found += [end for end in wordings if end != ""]
        if "" not in wordings:
            return found
    return found + [""]


def is_table_row(node) -> bool:
    """Whether a message is a row or the header of a table: a field padded to a width or
    aligned, such as `{count:>7,}` or `{'rows':>6}`. A zero-padded number such as `{minutes:02d}`
    is part of a time and makes no row."""
    for part in ast.walk(node):
        if isinstance(part, ast.FormattedValue) and part.format_spec is not None:
            spec = "".join(value.value if isinstance(value, ast.Constant) else "{}"
                           for value in part.format_spec.values)
            if re.match(r"(.?[<>^=])|[1-9]", spec):
                return True
    return False


def is_quoted_line(node) -> bool:
    """Whether a message repeats a line of a file or of another program: an f-string whose
    literal text is only its indentation, such as `f"  {line}"`. Wordings of an `IfExp` in
    braces are literal text of the message."""
    return (isinstance(node, ast.JoinedStr)
            and all(not part.value.strip() if isinstance(part, ast.Constant)
                    else not isinstance(part.value, ast.IfExp) for part in node.values))


def unfinished_messages(tree, own: bool = False) -> list:
    """(line, message source) for every message of `tree` that can end without a period, with
    the bare calls of `agentcontext/logs.py` when `own` is set (`written_messages`)."""
    found = []
    for line, node in written_messages(tree, own):
        if is_table_row(node) or is_quoted_line(node):
            continue
        if any(end is not COMPUTED and end != "." for end in endings(node)):
            found.append((line, ast.unparse(node)))
    return sorted(found)


class TestAMessageEndsWithAPeriod(unittest.TestCase):
    """A log message is a sentence and ends with a period, also after a closing parenthesis or
    a value, as in `Wrote data/collection/output/repos.csv.`

    The check reads the literal text at the end of each message. A message that ends with an
    expression outside an f-string, such as a variable, `str(error)` or a `join`, is computed,
    and its writer puts the period where it assembles the text, as `f"{note}."` or
    `f"{str(error).rstrip('.')}."` does for an exception text that may end with a period of its
    own. Two kinds of message are no sentence and take no period: the row or the header of a
    table, which pads a field to a width, and a line of a file or of another program that a
    message repeats with its indentation alone, such as `f"  {line}"`. A message with a line
    break, such as a sentence followed by a traceback, ends its first line with the period.
    """

    def test_every_message_ends_with_a_period(self):
        """Every tracked Python file outside the tests, the stage scripts included."""
        import subprocess
        listed = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                                text=True)
        if listed.returncode != 0:
            self.skipTest("not inside a git checkout")
        offenders = []
        for rel in listed.stdout.split():
            if rel.startswith(("tests/", "data/")) or Path(rel).name.startswith("test_"):
                continue
            try:
                tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            except (SyntaxError, FileNotFoundError):
                continue
            offenders += [f"{rel}:{line} {text}" for line, text
                          in unfinished_messages(tree, own=rel == "agentcontext/logs.py")]
        self.assertEqual(offenders, [])

    def test_every_form_of_a_missing_period_is_seen(self):
        """The check above, run over code that contains each end without a period and each end
        that it accepts."""
        tree = ast.parse("logs.info('x', 'Collection finished')\n"
                         "logs.info('x', f'Wrote {path}')\n"
                         "logs.warning('x', f'Downloaded the GHS search result ({size} MiB)')\n"
                         "logs.error('x', 'Stopped. ' 'No step ran')\n"
                         "log(f\"{'Ready' if a else 'Waiting.'}\")\n"
                         "log_warning(f'Failed ({error}){\". Retrying\" if retry else \"\"}')\n"
                         "logs.info('x', f'Took {n}' + (f' ({k} again)' if k else ''))\n"
                         "logs.debug('x', 'Reading ' + name)\n"
                         "log_error(f'{repo} failed\\n{trace}')\n"
                         "logs.info('x', 'Collection finished.')\n"
                         "logs.info('x', f'Wrote {path}.')\n"
                         "logs.info('x', f'Took {n}' + (f' ({k} again)' if k else '') + '.')\n"
                         "log_warning(f'Failed ({error}){\". Retrying\" if retry else \"\"}.')\n"
                         "logs.info('x', f'Exited {code}.{\"\" if ok else \" See the log.\"}')\n"
                         "logs.error('x', f'{repo} failed.\\n{trace}')\n"
                         "logs.error('x', str(error))\n"
                         "logs.warning('x', message)\n"
                         "logs.error('x', f\"{str(e).rstrip('.')}.\")\n"
                         "logs.info('x', f\"{'mechanism':14} {'rows':>6}\")\n"
                         "logs.info('x', f'  {kind:<8} {count:>6,}')\n"
                         "logs.info('x', f'  {line}')\n"
                         "logs.info('x', f'{hours}:{minutes:02d} left')\n"
                         "logs.line('INFO', 'x', 'sampling')\n"
                         "raise SystemExit('No step ran')\n")
        self.assertEqual([line for line, _text in unfinished_messages(tree)],
                         [1, 2, 3, 4, 5, 6, 7, 9, 22])
        own = ast.parse("warning('logs', f'Could not write {named} ({failure})')\n"
                        "error('logs', 'Stopped.')\n"
                        "info(record)\n")
        self.assertEqual([line for line, _text in unfinished_messages(own, own=True)], [1])
        self.assertEqual(unfinished_messages(own), [])


class TestATokenNamesItsKind(unittest.TestCase):
    """A log message or a help text that mentions a token names its kind.

    The credentials with which the pipeline calls GitHub are GitHub access tokens, and a token
    of another kind, such as a unit of a model's input, can enter the same log. `GITHUB_TOKEN`
    and `--token-file` are names and keep their spelling.
    """

    def test_every_token_is_named_as_a_github_access_token(self):
        offenders = []
        for rel, tree in message_sources():
            for line, node in log_messages(tree) + help_texts(tree):
                offenders.extend(f"{rel}:{line} {word!r}"
                                 for word in unnamed_tokens(literal_text(node)))
        self.assertEqual(offenders, [])

    def test_a_bare_token_is_seen_and_a_name_is_not(self):
        tree = ast.parse("logs.warning('pre_checks', f'Token {i} failed validation')\n"
                         "logs.info('collection_summary', f'{n} token(s), {w} worker(s)')\n"
                         "parser.add_argument('--token-file', help='One GitHub token per line')\n"
                         "logs.error('p', f\"{'no token' if a else 'none'} found\")\n"
                         "logs.info('collection_summary', f'{n} GitHub access token(s), {w} worker(s)')\n"
                         "logs.error('pre_checks', "
                         "'GITHUB_TOKEN and GITHUB_TOKENS are unset')\n"
                         "logs.info('unmatched', f\"  {'kind':<8} {'token':<40}\")\n")
        found = [(line, word) for line, node in log_messages(tree) + help_texts(tree)
                 for word in unnamed_tokens(literal_text(node))]
        self.assertEqual(sorted(found), [(1, "Token"), (2, "token"), (3, "token"), (4, "token")])


class TestTheStatusOfAStep(unittest.TestCase):
    """A step that waits, such as for credit, writes what it waits for to the file that the runner
    names in the step's environment, and the runner copies the file into the pipeline execution
    state."""

    def test_the_status_is_written_to_the_file_that_the_runner_names_and_removed_with_none(self):
        import json
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repository_labeling.status.json"
            with mock.patch.dict(os.environ, {_mod.STATUS_VARIABLE: str(path)}):
                _mod.step_status({"waiting_for": "credit", "service": "OpenRouter"})
                self.assertEqual(json.loads(path.read_text()),
                                 {"waiting_for": "credit", "service": "OpenRouter"})
                _mod.step_status(None)
                self.assertFalse(path.exists())
                _mod.step_status(None)
            self.assertEqual(os.listdir(directory), [])

    def test_outside_a_run_of_the_runner_nothing_is_written(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_mod.STATUS_VARIABLE, None)
            with mock.patch("builtins.open") as opened:
                _mod.step_status({"waiting_for": "credit"})
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
