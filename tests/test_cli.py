"""Tests for agentcontext.cli: every command names a module with a `main`, the usage lists
every command, and an unknown command is a usage error.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_cli -v
"""

import contextlib
import importlib
import io
import sys
import types
import unittest
from unittest import mock

from agentcontext import cli


class TestTheCommands(unittest.TestCase):

    def test_every_command_names_a_module_with_a_main(self):
        for name, (module, _text) in cli.COMMANDS.items():
            with self.subTest(command=name):
                self.assertTrue(callable(getattr(importlib.import_module(module), "main")))

    def test_every_step_check_and_tool_module_is_a_command(self):
        # A module with a `main` and no command cannot be run through the one command line.
        package = importlib.import_module("agentcontext")
        root = __import__("pathlib").Path(package.__file__).parent
        named = {module for module, _ in cli.COMMANDS.values()}
        for sub in ("steps", "checks", "tools"):
            for path in sorted((root / sub).glob("*.py")):
                if path.name == "__init__.py":
                    continue
                with self.subTest(module=f"{sub}/{path.name}"):
                    self.assertIn(f"agentcontext.{sub}.{path.stem}", named)

    def test_the_usage_lists_every_command(self):
        text = cli.usage()
        for name in cli.COMMANDS:
            self.assertIn(f"  {name}", text)

    def test_no_arguments_is_a_usage_error_and_help_is_not(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([]), 1)
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertEqual(out.getvalue().count("usage:"), 2)

    def test_a_flag_that_no_command_has_is_a_usage_error_and_no_verdict(self):
        """A command with an unknown flag exits 1 (could not check), not argparse's 2. A
        command that reports a verdict through its exit code exits 2 for a difference that it
        found. With 2, `regressions compare --ignor scanned_at` would signal a difference
        between two directories that it never compared. A command with subcommands names the
        flag as an unknown subcommand, and a command with required arguments names those
        arguments first."""
        for name in cli.COMMANDS:
            err = io.StringIO()
            with self.subTest(command=name), contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    code = cli.main([name, "--no-such-flag"])
                except SystemExit as stop:
                    code = stop.code
                self.assertEqual(code, cli.USAGE_ERROR, err.getvalue()[-500:])
                self.assertRegex(err.getvalue(), r"error: |Unknown subcommand '--no-such-flag'")

    def test_an_abbreviated_option_is_a_usage_error(self):
        """By default, argparse accepts an abbreviation of a long option as the option. The
        container's entrypoint recognizes the arguments for one Start in the `command:` of
        compose.yaml by their full names only. If argparse accepted `--fro` as `--from`,
        `run --fro release` would run the release again at every Start, and a `--fre` in the
        cloning step's `--step-arg` value would collect the whole sample again in every monthly
        run. A subparser refuses an abbreviation too."""
        from agentcontext import runner
        from agentcontext.steps import repository_cloning
        for words in (["run", "--fro", "release"], ["run", "--mark", "repository_cloning"],
                      ["run", "--step", "repository_cloning:--fresh"]):
            with self.subTest(words=words):
                err = io.StringIO()
                with mock.patch.object(runner, "run", return_value=0), \
                        contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                    cli.main(words)
                self.assertEqual(stop.exception.code, cli.USAGE_ERROR)
                self.assertIn(f"unrecognized arguments: {' '.join(words[1:])}", err.getvalue())
        parser = cli.ArgumentParser()
        parser.add_argument("--from-name")
        parser.add_subparsers(dest="command").add_parser("collect").add_argument(
            "--fresh", action="store_true")
        for words, parse in ((["--from", "x"], parser.parse_args),
                             (["collect", "--fre"], parser.parse_args),
                             (["--fre"], repository_cloning._build_arg_parser().parse_args)):
            with self.subTest(words=words):
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as stop:
                    parse(words)
                self.assertEqual(stop.exception.code, cli.USAGE_ERROR)

    def test_an_unknown_command_is_a_usage_error_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["no-such-command"]), 1)
        self.assertIn("Unknown command 'no-such-command'", err.getvalue())

    def test_a_step_without_subcommands_is_the_command_its_key_names(self):
        # Command names use the step's noun phrase, as completion keys do.
        from agentcontext import runner
        command_of = {module: name for name, (module, _text) in cli.COMMANDS.items()}
        steps = (runner.CORE_STEPS + [runner.COMMIT_HEURISTICS_STEP]
                  + runner.COMMIT_RECLASSIFICATION_STEPS)
        for step in steps:
            module = runner.step_module(step)
            if hasattr(importlib.import_module(module), "SUBCOMMANDS"):
                continue
            with self.subTest(step=step.key):
                self.assertEqual(command_of[module], step.key.replace("_", "-"))

    def test_retired_step_commands_have_no_aliases(self):
        for command in ("frame", "collect", "validate", "detect", "gitdata",
                        "ai-commits", "archive", "publish", "check-commits",
                        "sampling-frame-filtering", "collection", "validation",
                        "git-metadata-collection", "consistency", "deletions",
                        "archive-creation", "publication"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main([command, "--help"]), 1)

    def test_a_command_gets_the_rest_of_the_arguments(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["verify", "--help"]), 0)
        self.assertIn("The complete verification suite", out.getvalue())

    def test_a_commands_exit_code_is_what_the_command_line_returns(self):
        """The tests above run every command with `--help`, which exits 0. A `main` that
        returned 0 whatever the command returned would pass those tests, and a check that found
        a difference would then read as a pass. A command that returns nothing exits 0."""
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["snapshot"]), 1)
        stand_in = types.ModuleType("stand_in_command")
        stand_in.main = lambda argv: int(argv[0]) if argv else None
        with mock.patch.dict(sys.modules, {"stand_in_command": stand_in}), \
                mock.patch.dict(cli.COMMANDS, {"stand-in": ("stand_in_command", "")}):
            self.assertEqual(cli.main(["stand-in", "3"]), 3)
            self.assertEqual(cli.main(["stand-in"]), 0)


class TestTheSubcommandDispatchers(unittest.TestCase):
    """The modules with subcommands share one dispatcher: no subcommand is a usage error,
    `--help` is not, an unknown subcommand is named on stderr, and a known subcommand reaches its
    own parser with the rest of the arguments."""

    MODULES = {"agentcontext.checks.snapshot": "write", "agentcontext.checks.regressions": "check",
               "agentcontext.checks.unmatched": "paths",
               "agentcontext.tools.measure": "sizes"}

    def test_no_subcommand_is_a_usage_error_and_help_is_not(self):
        for name in self.MODULES:
            module = importlib.import_module(name)
            out = io.StringIO()
            with self.subTest(module=name), contextlib.redirect_stdout(out):
                self.assertEqual(module.main([]), 1)
                self.assertEqual(module.main(["--help"]), 0)
                self.assertEqual(out.getvalue().count("subcommands:"), 2)

    def test_an_unknown_subcommand_is_named_on_stderr(self):
        for name in self.MODULES:
            module = importlib.import_module(name)
            err = io.StringIO()
            with self.subTest(module=name), contextlib.redirect_stderr(err):
                self.assertEqual(module.main(["no-such-subcommand"]), 1)
            self.assertIn("Unknown subcommand 'no-such-subcommand'", err.getvalue())

    def test_a_known_subcommand_reaches_its_own_parser(self):
        for name, sub in self.MODULES.items():
            module = importlib.import_module(name)
            out = io.StringIO()
            with self.subTest(module=name), contextlib.redirect_stdout(out), \
                    self.assertRaises(SystemExit) as stop:
                module.main([sub, "--help"])
            self.assertEqual(stop.exception.code, 0)
            self.assertIn("usage:", out.getvalue())

    def test_every_subcommand_is_named_in_the_line_its_command_prints(self):
        """Each summary names every subcommand of its module, so that a summary copied from
        another command fails."""
        for name in self.MODULES:
            module = importlib.import_module(name)
            with self.subTest(module=name):
                for sub in module.SUBCOMMANDS:
                    self.assertIn(sub, module.SUBCOMMAND_SUMMARY)

    def test_each_command_prints_the_name_the_table_gives_it(self):
        """The test reads the command name that a module passes to the shared dispatcher from
        what the dispatcher prints. Composing the line in the test would repeat the helper and
        would not detect a module that passes the name of another command."""
        command_of = {module: name for name, (module, _text) in cli.COMMANDS.items()}
        for name in self.MODULES:
            module = importlib.import_module(name)
            out = io.StringIO()
            with self.subTest(module=name):
                with contextlib.redirect_stdout(out):
                    module.main(["--help"])
                printed = out.getvalue()
                self.assertIn(f"python -m agentcontext {command_of[name]} <subcommand>",
                              printed)
                for sub in module.SUBCOMMANDS:
                    self.assertIn(sub, printed)


if __name__ == "__main__":
    unittest.main()
