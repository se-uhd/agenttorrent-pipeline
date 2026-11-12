"""Tests for agentcontext.tools.verify. The check reads the module lists and the interpreter options
of the workflow as the workflow runs them, and every step that it names is in the workflow. It
runs every suite and check with its own interpreter and without PYTHONPATH, the suite whose step
sets PYTHONPATH with the checkout as its only entry, and the suite that CI runs before its install
with the interpreter of an empty virtual environment, without the bin directory of its own
environment on PATH. Its report names what failed and no directory of the machine. It refuses
to start under another release of Python than the pin, with a `python3` on PATH of another
environment, in an environment that differs from the development lock, without the editable
install of the checkout, and with a file in site-packages that differs from the RECORD of its
distribution or that no RECORD lists. For such a file, it names how to create .venv of the
checkout again, or the environment that runs it when that is another one. An `*.egg-info` folder
in the working directory is no installed distribution.

Requirements: Python 3.14 and packaging, a dependency of matplotlib that locks/development.txt
lists, for the environment markers of a dependency lock
Run with:  python3 -m unittest tests.test_tools_verify -v
"""

import base64
import contextlib
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentcontext import REPO_ROOT, SEVEN_ZIP
from agentcontext import cli
from agentcontext.tools import verify
from tests._fixtures import BASH, script_env


# The `direct_url.json` of the editable install of this checkout, which CI and dev/setup.sh make.
EDITABLE = {"url": REPO_ROOT.as_uri(), "dir_info": {"editable": True}}
# The environment of CI for the tests of `verify.main` that are about the suites, the checks and the
# report, so that they test the same in any environment that runs them.
IN_THE_ENVIRONMENT_OF_THE_DEVELOPMENT_LOCK = mock.patch.object(verify, "environment_problems",
                                                               new=lambda: [])
# The bin directory of the environment that runs the tests, with the console scripts of the
# development lock.
OWN_BIN = os.path.realpath(Path(sys.prefix) / "bin")
# The environment of the tests without AGENTCONTEXT_DATA_ROOT, which a shell that processes the
# working copy of a handover exports. `verify.main` refuses to run while the variable names a data
# root, and the suites, checks and programs that the tests start would read that data root. The
# test of the refusal sets the variable itself.
WITHOUT_A_DATA_ROOT = mock.patch.dict(os.environ)


def setUpModule():
    WITHOUT_A_DATA_ROOT.start()
    os.environ.pop("AGENTCONTEXT_DATA_ROOT", None)


def tearDownModule():
    WITHOUT_A_DATA_ROOT.stop()


def remedy(venv: Path) -> str:
    """The line that follows the files and directories that no RECORD lists in `venv`, the .venv
    of the checkout. dev/setup.sh leaves them, and a .venv that mise creates again holds none."""
    return (f"dev/setup.sh does not remove a file or a directory that no RECORD lists. Remove "
            f"{venv} and run mise exec -- bash dev/setup.sh, which creates it again and installs "
            f"the locked distributions into it.")


def console_script_of_the_lock() -> str | None:
    """A console script that a distribution of the development lock installs into `OWN_BIN`, such
    as coverage, or None. The virtual environment of the development container has no such
    script, because it sees the distributions of the image through the system's site packages."""
    locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
    return next((entry.name for entry in sorted(
                     importlib.metadata.entry_points(group="console_scripts"),
                     key=lambda entry: entry.name)
                 if getattr(entry, "dist", None) is not None
                 and verify.normalized(entry.dist.metadata["Name"] or "") in locked
                 and (Path(OWN_BIN) / entry.name).is_file()), None)


def in_own_bin(found: str | None) -> bool:
    """Whether `found`, a program that `shutil.which` found or None, is in `OWN_BIN`."""
    return found is not None and os.path.realpath(os.path.dirname(found)) == OWN_BIN


@IN_THE_ENVIRONMENT_OF_THE_DEVELOPMENT_LOCK
class TestTheWorkflowIsRead(unittest.TestCase):

    def test_every_suite_step_that_the_check_runs_is_a_step_of_the_workflow(self):
        text = verify.WORKFLOW.read_text(encoding="utf-8")
        # The test counts the steps rather than only iterating over them. An empty SUITES would
        # pass a loop, and the check would then run no suite and still exit 0.
        self.assertEqual(len(verify.SUITES),
                         len(re.findall(r"python3(?:\s+-[A-Za-z]+)*\s+-m unittest", text)))
        for step, directory in verify.SUITES:
            with self.subTest(step=step):
                self.assertIn(f"- name: {step}", text)
                self.assertTrue(directory.is_dir(), directory)

    def test_every_check_that_the_workflow_runs_is_a_check_of_the_verification(self):
        """CI runs the checks that need no extracted data. The verification has to run each of
        these checks in the same directory, with the same command or a stricter command. CI's
        reproduction check, for example, adds `--skip-missing --reproduce-only` to the command of
        the verification."""
        text = verify.WORKFLOW.read_text(encoding="utf-8")
        steps = re.findall(r"- name: [^\n]*\n(?:\s+#[^\n]*\n)*?\s+working-directory: (\S+)\n"
                           r"\s+run: (python3 (?!-m unittest)[^\n|]+)\n", text)
        self.assertGreaterEqual(len(steps), 3, steps)
        for directory, command in steps:
            ci = command.split()[1:]
            with self.subTest(command=command):
                self.assertTrue(any(cwd.resolve() == (REPO_ROOT / directory).resolve()
                                    and ci[:len(argv)] == argv
                                    and all(flag.startswith("--") for flag in ci[len(argv):])
                                    for _, argv, cwd in verify.CHECKS), command)

    def test_the_checks_over_the_local_data_are_run(self):
        """The verification runs the checks that CI cannot run because they need the extracted
        collection: the regression manifest, the derived sections of the snapshot, the cross
        check of the output CSVs and the stated statistics. A check dropped from the verification
        would run nowhere."""
        commands = {tuple(command) for _, command, _ in verify.CHECKS}
        for required in (("-m", "agentcontext", "regressions", "check"),
                         ("-m", "agentcontext", "snapshot", "write", "--check", "--outputs-only"),
                         ("-m", "agentcontext", "output-cross-check"),
                         ("-m", "agentcontext", "statistics", "--check")):
            with self.subTest(check=" ".join(required)):
                self.assertIn(required, commands)

    def test_every_check_that_the_verification_runs_is_a_command_of_the_package(self):
        for label, command, cwd in verify.CHECKS:
            with self.subTest(check=label):
                self.assertTrue(cwd.is_dir(), cwd)
                if command[0:1] == ["-m"]:
                    self.assertIn(command[2], cli.COMMANDS, label)
                    # The subcommand is checked too, where there is one, because `cli.COMMANDS`
                    # contains no subcommands and `snapshot writ --check` would pass the check
                    # of the name.
                    module = importlib.import_module(cli.COMMANDS[command[2]][0])
                    if hasattr(module, "SUBCOMMANDS") and len(command) > 3:
                        self.assertIn(command[3], module.SUBCOMMANDS, label)
                else:
                    self.assertTrue((cwd / command[0]).is_file(), command)

    def test_the_workflow_rejects_unmatched_paths_in_three_repositories(self):
        text = verify.WORKFLOW.read_text(encoding="utf-8")
        step = text.split("- name: Check the path globs that no heuristic explains", 1)[1].split("- name:", 1)[0]
        self.assertIn("run: python3 -m agentcontext unmatched paths --fail-above 2", step)

    def test_the_collection_step_lists_every_module_under_tests(self):
        listed = set(verify.modules("Run collection suites"))
        on_disk = {f"tests.{p.stem}" for p in (REPO_ROOT / "tests").glob("test_*.py")}
        self.assertEqual(listed, on_disk)

    def test_a_module_is_read_in_either_form_and_under_its_own_step_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = Path(tmp) / "workflow.yml"
            workflow.write_text(
                "      - name: Run the first suites\n        run: |\n"
                "          python3 -m unittest \\\n            tests.test_a \\\n"
                "            test_b\n\n      - name: Run the second suites\n"
                "        run: |\n          python3 -m unittest \\\n            test_c\n")
            with mock.patch.object(verify, "WORKFLOW", workflow):
                self.assertEqual(verify.modules("Run the first suites"), ["tests.test_a", "test_b"])
                self.assertEqual(verify.modules("Run the second suites"), ["test_c"])

    def test_the_verification_runs_every_test_module_in_the_repository(self):
        """The verification runs every `test_*.py` that git tracks, each by its step from its
        directory. A step that ran only the first module of its list would still report the
        length of the list."""
        ran = []

        def recording(label, argv, cwd, results, failures, env):
            if "unittest" in argv:
                ran.extend((Path(cwd).resolve(), name)
                           for name in argv[argv.index("unittest") + 1:])

        with (mock.patch.object(verify, "run", side_effect=recording),
              mock.patch.object(verify, "CHECKS", ()),
              mock.patch.object(verify.subprocess, "run",
                                return_value=mock.Mock(stdout="", stderr="", returncode=0)),
              contextlib.redirect_stderr(io.StringIO())):
            verify.main([])
        tracked = subprocess.run(["git", "ls-files", "*test_*.py"], cwd=REPO_ROOT,
                                 capture_output=True, text=True, check=True).stdout.split()
        expected = set()
        for relative in tracked:
            path = Path(relative)
            if path.parts[0] == "tests":
                expected.add((REPO_ROOT.resolve(), f"tests.{path.stem}"))
            else:
                expected.add(((REPO_ROOT / path.parent).resolve(), path.stem))
        self.assertGreater(len(expected), 80)
        self.assertEqual(set(ran), expected)

    def test_a_module_named_in_a_comment_is_not_run(self):
        # The list has one module per line, and a comment that names a module is prose.
        listed = verify.modules("Run collection suites")
        self.assertTrue(all(re.fullmatch(r"tests\.test_\w+", m) for m in listed), listed)

    def test_the_sampling_runner_suite_runs_without_site_packages_as_in_ci(self):
        """CI runs the suite with `python3 -S`, which imports no site packages, before it installs
        anything, and the verification gives the suite the same option. Every other suite runs
        with the site packages."""
        self.assertEqual(verify.unittest_command("Run the sampling runner suite"),
                         [sys.executable, "-S", "-m", "unittest", "test_run_sampling"])
        for step, _ in verify.SUITES:
            if step != "Run the sampling runner suite":
                with self.subTest(step=step):
                    self.assertEqual(verify.interpreter_options(step), [])

    def test_every_suite_and_check_runs_with_the_interpreter_of_the_verification(self):
        """The comparison with the development lock reads the environment of the interpreter that
        runs the verification, and a `python3` from PATH could be the interpreter of another
        environment, such as mise's shared installation of Python. The suite that CI runs before
        its install is the exception, with the interpreter of an empty virtual environment."""
        ran = []
        with (mock.patch.object(verify, "run",
                                side_effect=lambda label, argv, *rest: ran.append(argv)),
              mock.patch.object(verify.subprocess, "run",
                                return_value=mock.Mock(stdout="", stderr="", returncode=0)),
              contextlib.redirect_stderr(io.StringIO())):
            verify.main([])
        self.assertEqual(len(ran), len(verify.SUITES) + len(verify.CHECKS))
        before = [argv for argv in ran if argv[-1] == "test_run_sampling"]
        self.assertEqual(len(before), 1, ran)
        self.assertEqual({argv[0] for argv in ran if argv not in before}, {sys.executable})
        self.assertNotEqual(before[0][0], sys.executable)

    def test_only_the_sampling_runner_suite_runs_before_the_install_in_ci(self):
        for step, _ in verify.SUITES:
            with self.subTest(step=step):
                self.assertEqual(verify.runs_before_the_install(step),
                                 step == "Run the sampling runner suite")

    def test_a_comment_that_names_an_install_is_no_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = Path(tmp) / "workflow.yml"
            workflow.write_text("      # The suites run after `uv pip install`.\n"
                                "      - name: Run the first suites\n        run: python3 -m x\n"
                                "      - name: Install\n        run: uv pip install -r a.txt\n"
                                "      - name: Run the second suites\n        run: python3 -m y\n")
            with mock.patch.object(verify, "WORKFLOW", workflow):
                self.assertTrue(verify.runs_before_the_install("Run the first suites"))
                self.assertFalse(verify.runs_before_the_install("Run the second suites"))

    def test_the_suite_before_the_install_runs_in_an_empty_virtual_environment(self):
        """In CI, the sampling runner suite runs in the .venv that mise created, before anything
        is installed into it, and a child that it starts with `sys.executable` or as `python3`
        finds no distribution in its site-packages. The verification runs the suite with the
        interpreter of an empty virtual environment of its own release, activated in place of its
        own, so that the children see the same and find none of the console scripts in the bin
        directory of the environment of the verification, and it removes the environment
        afterwards. A child of every other suite finds the distributions of the development lock
        and their console scripts in that bin directory. Each child runs in the directory of its
        suite, and the probe reads its site-packages only, because the checkout on the
        PYTHONPATH of the sampling runner suite can hold an `*.egg-info` folder, which is no
        installed distribution."""
        script = console_script_of_the_lock()
        probe = ("import importlib.metadata, json, platform, shutil, site, sys\n"
                 "sites = site.getsitepackages() + ([site.getusersitepackages()]\n"
                 "                                  if site.ENABLE_USER_SITE else [])\n"
                 "print(json.dumps([platform.python_version(), sys.prefix != sys.base_prefix,\n"
                 "    sorted(d.metadata['Name'] for d in importlib.metadata.distributions(\n"
                 "        path=sites)),\n"
                 "    shutil.which('python3') == sys.executable,\n"
                 f"    shutil.which({script!r}) if {script!r} else None]))\n")
        seen = {}

        def probing(label, argv, cwd, results, failures, env):
            child = subprocess.run([argv[0], "-c", probe], cwd=cwd, capture_output=True,
                                   text=True, env=env)
            self.assertEqual(child.returncode, 0, child.stderr)
            seen[argv[-1]] = (argv[0], json.loads(child.stdout))

        with (mock.patch.object(verify, "run", side_effect=probing),
              mock.patch.object(verify, "CHECKS", ()),
              contextlib.redirect_stderr(io.StringIO())):
            verify.main([])
        interpreter, printed = seen["test_run_sampling"]
        self.assertEqual(printed[:4], [platform.python_version(), True, [], True])
        self.assertFalse(in_own_bin(printed[4]), printed[4])
        self.assertFalse(Path(interpreter).exists(), interpreter)
        other, printed = seen["test_filter_ghs"]
        self.assertEqual(other, sys.executable)
        self.assertIn("PyYAML", printed[2])
        if script is not None:
            self.assertTrue(in_own_bin(printed[4]), (script, printed[4]))

    def test_the_empty_environment_takes_the_place_of_the_running_one_on_path(self):
        """`deactivate` takes the bin directory of the running environment off PATH, and
        `activate` puts the one of the empty environment first. The console scripts of the
        development lock, such as coverage, are in the first and not in the .venv of CI before
        the install. Without an empty environment, PATH stays as it is."""
        own = str(Path(sys.prefix) / "bin")
        path = os.pathsep.join([own, "/usr/bin", own, "/bin"])
        with mock.patch.dict(os.environ, {"PATH": path}):
            activated = verify.child_environment("Run the sampling runner suite",
                                                 Path("/scratch/venv"))
            unchanged = verify.child_environment("Run the sampling script suites")
        self.assertEqual(activated["PATH"], os.pathsep.join(["/scratch/venv/bin", "/usr/bin",
                                                             "/bin"]))
        self.assertEqual(activated["VIRTUAL_ENV"], "/scratch/venv")
        self.assertEqual(unchanged["PATH"], path)

    def test_the_variables_of_a_step_are_read_from_its_env_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = Path(tmp) / "workflow.yml"
            workflow.write_text(
                "      - name: Run the first suites\n        working-directory: a\n"
                "        env:\n          PYTHONPATH: ${{ github.workspace }}\n"
                "          # The checkout.\n          OTHER: '1'\n"
                "        run: |\n          python3 -m unittest \\\n            test_a\n\n"
                "      - name: Run the second suites\n        run: python3 -m unittest test_b\n")
            with mock.patch.object(verify, "WORKFLOW", workflow):
                self.assertEqual(verify.step_variables("Run the first suites"),
                                 {"PYTHONPATH": "${{ github.workspace }}", "OTHER": "'1'"})
                self.assertEqual(verify.step_variables("Run the second suites"), {})

    def test_every_variable_of_a_suite_step_is_one_that_the_verification_sets(self):
        """The verification sets PYTHONPATH to this checkout for a step that sets it to the
        checkout of CI, `${{ github.workspace }}`, and sets no other variable of a step. Another
        variable or value would run the suite in CI in an environment that the verification
        does not give it."""
        for step, _ in verify.SUITES:
            with self.subTest(step=step):
                self.assertLessEqual(verify.step_variables(step).items(),
                                     {("PYTHONPATH", "${{ github.workspace }}")})
        self.assertEqual(verify.step_variables("Run the sampling runner suite"),
                         {"PYTHONPATH": "${{ github.workspace }}"})

    def test_only_a_suite_whose_step_sets_pythonpath_runs_with_the_checkout_on_it(self):
        """CI sets PYTHONPATH to the checkout for the sampling runner suite, whose `env:` names
        it, and runs every other suite and every check without it. A suite that imported a
        module of the checkout through PYTHONPATH, such as run_pipeline from a stage directory,
        would pass here and fail in CI. An entry that the shell sets reaches no suite."""
        seen = {}

        def recording(label, argv, cwd, results, failures, env):
            seen[label] = env.get("PYTHONPATH")

        with (mock.patch.dict(os.environ, {"PYTHONPATH": "/elsewhere"}),
              mock.patch.object(verify, "run", side_effect=recording),
              mock.patch.object(verify.subprocess, "run",
                                return_value=mock.Mock(stdout="", stderr="", returncode=0)),
              contextlib.redirect_stderr(io.StringIO())):
            verify.main([])
        self.assertEqual(len(seen), len(verify.SUITES) + len(verify.CHECKS))
        self.assertEqual({label: path for label, path in seen.items() if path is not None},
                         {"the sampling runner suite with 1 module": str(REPO_ROOT)})

    def test_the_verification_refuses_to_run_while_another_data_root_is_named(self):
        """AGENTCONTEXT_DATA_ROOT names the data root that every suite and check reads
        (`agentcontext.DATA_ROOT`), such as the working copy of a handover, while `git status`
        inspects this checkout, and CI never sets it. The verification exits 1 with a line that
        names the variable and runs no suite and no check. An empty value names no data root,
        as for `agentcontext.DATA_ROOT`."""
        ran = []

        def recording(label, argv, cwd, results, failures, env):
            ran.append(label)

        for value in ("/work/handover/data", ""):
            ran.clear()
            out = io.StringIO()
            with (self.subTest(value=value),
                  mock.patch.dict(os.environ, {"AGENTCONTEXT_DATA_ROOT": value}),
                  mock.patch.object(verify, "run", side_effect=recording),
                  mock.patch.object(verify.subprocess, "run",
                                    return_value=mock.Mock(stdout="", stderr="", returncode=0)),
                  contextlib.redirect_stderr(out)):
                code = verify.main([])
                if value:
                    self.assertEqual((code, ran), (1, []))
                    self.assertIn("AGENTCONTEXT_DATA_ROOT is set to /work/handover/data",
                                  out.getvalue())
                else:
                    self.assertEqual((code, len(ran)),
                                     (0, len(verify.SUITES) + len(verify.CHECKS)))


@IN_THE_ENVIRONMENT_OF_THE_DEVELOPMENT_LOCK
class TestTheReport(unittest.TestCase):

    def _check_over(self, check: str, git_status: str = "", cwd: Path = REPO_ROOT) -> tuple:
        """`verify.main` over one real check running `check` in `cwd`, with git reporting that
        the working tree's status is `git_status`. Returns the exit code and the report."""
        real_run = subprocess.run

        def answering(argv, *args, **kwargs):
            if argv[0] == "git":
                return mock.Mock(stdout=git_status if argv[1] == "status" else "abc1234\n",
                                 stderr="", returncode=0)
            return real_run(argv, *args, **kwargs)

        out = io.StringIO()
        with (mock.patch.object(verify, "SUITES", ()),
              mock.patch.object(verify, "CHECKS", (("a check", ["-c", check], cwd),)),
              mock.patch.object(verify.subprocess, "run", side_effect=answering),
              contextlib.redirect_stderr(out)):
            code = verify.main([])
        return code, out.getvalue()

    def test_a_check_runs_with_this_interpreter_and_not_with_the_python3_on_path(self):
        """A `python3` first on PATH that is not this interpreter, which the comparison of the
        environments would refuse, does not run the check."""
        with tempfile.TemporaryDirectory() as tmp:
            python3 = Path(tmp) / "python3"
            python3.write_text("#!/bin/sh\necho 'the python3 on PATH'\nexit 3\n")
            python3.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"}):
                code, report = self._check_over(
                    f"import sys; print('ran by this interpreter:', "
                    f"sys.executable == {sys.executable!r})")
        self.assertEqual(code, 0, report)
        self.assertIn("ran by this interpreter: True", report)

    def test_a_failing_command_fails_the_check_and_is_quoted(self):
        """Testing `run` alone, which collects the failure, would not check that `main` turns
        it into the exit code, and a `main` that checked only the working tree would pass."""
        code, report = self._check_over("import sys; print('boom'); sys.exit(2)")
        self.assertEqual(code, 2)
        self.assertIn("FAIL exit=2", report)
        self.assertIn("===== a check =====\nboom", report)

    def test_a_path_that_the_shell_adds_does_not_reach_a_check(self):
        """CI runs a check without PYTHONPATH. A directory of modules that the shell adds to
        PYTHONPATH would let a check import modules that CI does not have, such as yaml without
        its distribution, which no comparison of the installed distributions sees."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "planted_module.py").write_text("PLANTED = True\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": tmp}):
                code, report = self._check_over(
                    "import importlib.util, os; "
                    "print('planted:', importlib.util.find_spec('planted_module') is not None, "
                    "'path:', os.environ.get('PYTHONPATH'))")
        self.assertEqual(code, 0, report)
        self.assertIn("planted: False path: None", report)

    def test_a_check_imports_the_package_of_the_checkout_that_it_verifies(self):
        """An editable install binds `import agentcontext` to the checkout that it was made
        from, and the verification refuses to start without the editable install of this
        checkout (`direct_install_differences`). A check that starts in a stage directory, where
        the directory of the check comes first on `sys.path`, then imports the package of this
        checkout without PYTHONPATH, as in CI, and `check_reproduction.py` writes its outputs
        here rather than into another checkout."""
        if verify.direct_install_differences(verify.direct_installs(), "lock.txt"):
            self.skipTest("the environment holds no editable install of this checkout, without "
                          "which the verification refuses to start")
        code, report = self._check_over(
            "import agentcontext, os; "
            "print('package of the checkout:', os.path.realpath(os.path.dirname(os.path.dirname("
            f"agentcontext.__file__))) == {str(REPO_ROOT.resolve())!r}, "
            "'path:', os.environ.get('PYTHONPATH'))",
            cwd=REPO_ROOT / "4_data-analysis")
        self.assertEqual(code, 0, report)
        self.assertIn("package of the checkout: True path: None", report)

    def test_the_report_names_no_directory_of_the_machine(self):
        """docs/validation/ keeps reports of the verification, and a check that names a file by
        its absolute path, such as the report of the output cross check, would put the
        directories of the machine that ran it into the repository."""
        home = "/nonexistent-home/somebody"
        check = (f"print('Wrote ' + {str(REPO_ROOT / 'data' / 'report.csv')!r} + ' in ' + "
                 f"{str(REPO_ROOT)!r} + '. Read ' + {home + '/.cache/tool/log'!r} + ' of ' + "
                 f"{home!r} + ', and ' + {str(REPO_ROOT) + '-elsewhere/x'!r}); "
                 "import sys; sys.exit(2)")
        with mock.patch.dict(os.environ, {"HOME": home}):
            code, report = self._check_over(check)
        self.assertEqual(code, 2, report)
        expected = ("Wrote data/report.csv in .. Read ~/.cache/tool/log of ~, and "
                    f"{REPO_ROOT}-elsewhere/x")
        # Once in the summary of the check and once in its failure.
        self.assertEqual(report.count(expected), 2, report)
        self.assertNotIn(home, report)

    def test_a_path_is_replaced_only_where_it_begins_with_the_checkout_or_the_home_directory(self):
        """/code, the checkout of the development container, and /home/dev also occur inside
        a longer path, inside a URL of a host and in an HTML tag, which name no directory of the
        machine and which a replacement there would make wrong, such as https://example.comx.
        A file URL and a short option, such as -o of 7zz, name the directory of the path that
        follows them."""
        with (mock.patch.object(verify, "REPO_ROOT", Path("/code")),
              mock.patch.dict(os.environ, {"HOME": "/home/dev"})):
            for text, expected in [
                    ("see https://example.com/code/x", "see https://example.com/code/x"),
                    ("see https://host/home/dev/y.", "see https://host/home/dev/y."),
                    ("see file:///code/x, file:///code and file:///home/dev/y.",
                     "see x, . and ~/y."),
                    ("see file:///opt/code/x", "see file:///opt/code/x"),
                    ("/opt/code/bin/tool failed", "/opt/code/bin/tool failed"),
                    ("/var/home/dev/x and ~/code/y", "/var/home/dev/x and ~/code/y"),
                    ("/code-elsewhere/x and /codex", "/code-elsewhere/x and /codex"),
                    ("<code>x</code>", "<code>x</code>"),
                    ("Wrote /code/data/x.csv in '/code' (/code/).",
                     "Wrote data/x.csv in '.' (.)."),
                    ("[/code] and [/code/], in /code? Yes, in /code!",
                     "[.] and [.], in .? Yes, in .!"),
                    ("PATH=/code/bin:/home/dev/.local/bin, then /home/dev.",
                     "PATH=bin:~/.local/bin, then ~."),
                    ("7zz x /code/data/a.7z -o/code/data -y and cc -I/home/dev/include",
                     "7zz x data/a.7z -odata -y and cc -I~/include"),
                    ("--output=/code/x and -o/code", "--output=x and -o."),
                    ("x-o/code/y and --o/code/y", "x-o/code/y and --o/code/y")]:
                with self.subTest(text=text):
                    self.assertEqual(verify.without_local_paths(text), expected)

    def test_the_command_that_extracts_a_differing_file_again_names_no_directory(self):
        """An analysis script stops on an input that differs from its archive with the message
        of `CopyDiffers`, and the reproduction check quotes it in the report. Its command gives
        the directory to 7zz after -o without a space."""
        from agentcontext import archives
        copy = REPO_ROOT / "data" / "sampling" / "ghs_filtered.json"
        message = str(archives.CopyDiffers(copy, copy.with_name(copy.name + ".7z")))
        masked = verify.without_local_paths(message)
        self.assertIn("`7zz x data/sampling/ghs_filtered.json.7z -odata/sampling -y`", masked)
        self.assertNotIn(str(REPO_ROOT), masked)

    def test_the_message_of_a_git_status_that_failed_names_no_directory_of_the_machine(self):
        """Git names the checkout that it refuses to inspect, as in a checkout with dubious
        ownership under a bind mount, and the report quotes the message of git."""
        home = "/nonexistent-home/somebody"
        refusal = (f"fatal: detected dubious ownership in repository at '{REPO_ROOT}'\n"
                   f"To add an exception for this directory, call:\n\n"
                   f"\tgit config --global --add safe.directory {REPO_ROOT}\n")

        def answering(argv, *args, **kwargs):
            if argv[:2] == ["git", "status"]:
                return mock.Mock(stdout="", stderr=refusal, returncode=128)
            return mock.Mock(stdout="abc1234\n", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.txt"
            out = io.StringIO()
            with (mock.patch.object(verify, "SUITES", ()), mock.patch.object(verify, "CHECKS", ()),
                  mock.patch.object(verify.subprocess, "run", side_effect=answering),
                  mock.patch.dict(os.environ, {"HOME": home}),
                  contextlib.redirect_stderr(out)):
                self.assertEqual(verify.main([str(report)]), 1)
            written = report.read_text(encoding="utf-8")
        self.assertIn("git status exited 128 (fatal: detected dubious ownership in repository "
                      "at '.'", written)
        self.assertIn("safe.directory .).", written)
        for text in (written, out.getvalue()):
            self.assertNotIn(str(REPO_ROOT), text)

    def test_every_check_passing_over_a_clean_tree_passes_the_check(self):
        """A `main` that always returned 1 would pass the other tests, because they expect a
        failure."""
        code, report = self._check_over("print('fine')")
        self.assertEqual(code, 0)
        self.assertIn("PASS exit=0", report)
        self.assertIn("The working tree is clean after the checks.", report)

    def test_a_dirty_tree_fails_the_check(self):
        code, report = self._check_over("print('fine')", git_status=" M a file\n")
        self.assertEqual(code, 2)
        self.assertIn(" M a file", report)

    def test_a_git_status_that_exits_nonzero_fails_the_check(self):
        """When git refuses, it prints nothing on stdout. A test of the output alone for
        emptiness would report a tree that the check never inspected as clean and exit 0. Git
        refuses in a checkout with dubious ownership under a bind mount and in an unpacked
        archive with no `.git`."""
        import contextlib, io
        out = io.StringIO()
        with (mock.patch.object(verify, "SUITES", ()), mock.patch.object(verify, "CHECKS", ()),
              mock.patch.object(verify.subprocess, "run") as run,
              contextlib.redirect_stderr(out)):
            run.return_value = mock.Mock(stdout="", stderr="fatal: not a git repository\n",
                                         returncode=128)
            self.assertEqual(verify.main([]), 1)
        self.assertIn("git status exited 128", out.getvalue())

    def test_analysis_outputs_can_be_reproduced_outside_the_checkout(self):
        real_run = subprocess.run

        def answering(argv, *args, **kwargs):
            if argv[0] == "git":
                return mock.Mock(stdout="" if argv[1] == "status" else "abc1234\n",
                                 stderr="", returncode=0)
            return real_run(argv, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checker = root / "reproduce.py"
            checker.write_text(
                "import argparse\nfrom pathlib import Path\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--output-dir', required=True)\n"
                "out = Path(parser.parse_args().output_dir)\n"
                "out.mkdir(parents=True)\n"
                "(out / 'result.txt').write_text('reproduced')\n")
            report, output = root / "report.txt", root / "analysis"
            with (mock.patch.object(verify, "SUITES", ()),
                  mock.patch.object(verify, "CHECKS", (("check_reproduction.py",
                       [str(checker)], root),)),
                  mock.patch.object(verify.subprocess, "run", side_effect=answering),
                  contextlib.chdir(root), contextlib.redirect_stderr(io.StringIO())):
                code = verify.main(["--analysis-output-dir", str(output), str(report)])
            self.assertEqual(code, 0)
            self.assertEqual((output / "result.txt").read_text(), "reproduced")
            self.assertIn("PASS exit=0", report.read_text())

    def test_help_prints_the_docstring(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(verify.main(["--help"]), 0)
        self.assertIn("The complete verification suite", out.getvalue())


class TestTheEnvironmentCheck(unittest.TestCase):
    """The verification runs in the environment of CI, the release of Python that mise.toml pins
    with the distributions of locks/development.txt, and refuses to start in another one. A suite
    that passes with a distribution that no dependency lock lists, such as httpx in mise's shared
    installation of Python, or under another release fails in CI and in the images, which run the
    pinned release with the dependency locks only."""

    HASH = "    --hash=sha256:{}\n"
    LOCK = ("certifi==2025.10.5 \\\n" + HASH.format("a" * 64) + "    # via requests\n"
            "httpx2==2.13.1 \\\n" + HASH.format("b" * 64) +
            "pywin32==312 ; sys_platform == 'win32' \\\n" + HASH.format("c" * 64) +
            "Typing_Extensions==4.15.0 ; sys_platform != 'win32' \\\n" + HASH.format("d" * 64))
    LOCKED = {"certifi": "2025.10.5", "httpx2": "2.13.1", "typing-extensions": "4.15.0"}

    def test_a_dependency_lock_lists_the_entries_of_this_platform_by_normalized_name(self):
        self.assertEqual(verify.locked_versions(self.LOCK), self.LOCKED)

    def test_an_installed_distribution_that_is_not_locked_is_a_difference(self):
        installed = {**self.LOCKED, "httpx": "0.28.1"}
        self.assertEqual(verify.environment_differences(self.LOCKED, installed, "lock.txt"),
                         ["lock.txt does not list httpx 0.28.1, which is installed."])

    def test_a_missing_distribution_and_another_version_are_differences(self):
        installed = {"certifi": "2025.10.5", "httpx2": "2.16.0"}
        self.assertEqual(verify.environment_differences(self.LOCKED, installed, "lock.txt"),
                         ["lock.txt lists typing-extensions 4.15.0, which is not installed.",
                          "lock.txt lists httpx2 2.13.1, and 2.16.0 is installed."])

    def test_the_editable_package_and_pip_are_no_differences(self):
        installed = {**self.LOCKED, "agentcontext": "1.0.0", "pip": "26.3"}
        self.assertEqual(verify.environment_differences(self.LOCKED, installed, "lock.txt"), [])

    def test_the_first_distribution_of_a_name_on_the_path_is_the_installed_one(self):
        """pip is in a virtual environment and in the system's site packages that the
        environment includes, and the one that an import finds comes first."""
        found = [mock.Mock(metadata={"Name": "typing_extensions"}, version="4.15.0"),
                 mock.Mock(metadata={"Name": "pip"}, version="26.3"),
                 mock.Mock(metadata={"Name": "pip"}, version="25.2")]
        with mock.patch.object(verify.importlib.metadata, "distributions", return_value=found):
            self.assertEqual(verify.installed_versions(),
                             {"typing-extensions": "4.15.0", "pip": "26.3"})

    def test_another_release_of_python_is_a_difference(self):
        self.assertEqual(verify.interpreter_differences("3.14.10", "3.14.10"), [])
        lines = verify.interpreter_differences("3.14.0", "3.14.10")
        self.assertEqual(len(lines), 1)
        self.assertIn("This is Python 3.14.0, and mise.toml pins 3.14.10.", lines[0])
        self.assertIn(f"remove {REPO_ROOT / '.venv'} and run mise exec -- bash dev/setup.sh",
                      lines[0])

    def _path_with_python3(self, tmp: str, prefix: str) -> str:
        """A PATH with one `python3`, which reports `prefix` as its environment."""
        python3 = Path(tmp) / "python3"
        python3.write_text(f"#!/bin/sh\necho '{prefix}'\n")
        python3.chmod(0o755)
        return tmp

    def test_a_python3_on_path_of_another_environment_is_a_difference(self):
        """The suites start `python3` by name. A `python3` of mise's shared installation of
        Python first on PATH would run them with distributions that the comparison with the
        development lock did not see."""
        with tempfile.TemporaryDirectory() as tmp:
            lines = verify.path_interpreter_differences(self._path_with_python3(tmp, "/elsewhere"))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"is the interpreter of /elsewhere, and this one is the interpreter of "
                      f"{sys.prefix}", lines[0])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                verify.path_interpreter_differences(self._path_with_python3(tmp, sys.prefix)), [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(verify.path_interpreter_differences(tmp),
                             ["No python3 is on PATH, and the suites start python3 by name."])

    def test_the_python3_of_mise_exec_is_the_interpreter_of_the_verification(self):
        """In the environment that runs this suite under `mise exec` or in CI, the `python3` on PATH
        is this interpreter, the one of .venv."""
        if os.environ.get("VIRTUAL_ENV") is None:
            self.skipTest("no virtual environment is active")
        self.assertEqual(verify.path_interpreter_differences(), [])

    def test_a_distribution_from_a_url_or_a_local_path_is_a_difference(self):
        """A distribution built from a local directory or a fork can have the locked version and
        other files. The editable install of this checkout is the one that the environment
        holds, and pip, which is not locked, is left out."""
        direct = {"agentcontext": EDITABLE,
                  "pip": {"url": "file:///tmp/pip-26.2.2-py3-none-any.whl", "archive_info": {}},
                  "idna": {"url": "file:///tmp/idna", "dir_info": {}}}
        self.assertEqual(verify.direct_install_differences(direct, "lock.txt"),
                         ["idna is installed from file:///tmp/idna rather than from the files "
                          "that lock.txt lists."])

    def test_the_package_of_the_checkout_is_an_editable_install_of_the_checkout(self):
        """CI and dev/setup.sh install agentcontext editable from the checkout. A package from
        another checkout, a package that is not editable and a missing package are differences."""
        for record, line in (
                (None, f"agentcontext is not installed as an editable install of {REPO_ROOT}, "
                       f"as CI and dev/setup.sh install it."),
                ({"url": "file:///elsewhere/ai-config-pipeline", "dir_info": {"editable": True}},
                 f"agentcontext is installed from file:///elsewhere/ai-config-pipeline, and CI "
                 f"and dev/setup.sh install it as an editable install of {REPO_ROOT}."),
                ({"url": REPO_ROOT.as_uri(), "dir_info": {}},
                 f"agentcontext is installed from {REPO_ROOT.as_uri()}, and CI and dev/setup.sh "
                 f"install it as an editable install of {REPO_ROOT}.")):
            with self.subTest(record=record):
                direct = {} if record is None else {"agentcontext": record}
                self.assertEqual(verify.direct_install_differences(direct, "lock.txt"), [line])

    def test_the_record_of_a_direct_install_is_read_from_the_metadata(self):
        """The first distribution of a name on the path is the one that an import finds."""
        def distribution(name: str, record) -> mock.Mock:
            found = mock.Mock(metadata={"Name": name})
            found.read_text.side_effect = lambda file: record if file == "direct_url.json" else None
            return found

        local = json.dumps({"url": "file:///tmp/idna", "dir_info": {}})
        found = [distribution("idna", local), distribution("idna", None),
                 distribution("certifi", None), distribution("broken", "{")]
        with mock.patch.object(verify.importlib.metadata, "distributions", return_value=found):
            self.assertEqual(verify.direct_installs(),
                             {"idna": {"url": "file:///tmp/idna", "dir_info": {}}, "broken": {}})

    def test_the_editable_install_of_this_checkout_is_its_only_direct_install(self):
        """The environment that runs this suite, .venv under `mise exec` or in CI, holds no
        distribution from a URL or a local path but the editable install of this checkout."""
        if os.environ.get("VIRTUAL_ENV") is None:
            self.skipTest("no virtual environment is active")
        self.assertEqual(verify.direct_install_differences(verify.direct_installs(), "lock.txt"),
                         [])

    @staticmethod
    def _egg_info_folders(directory: Path) -> None:
        """Write `agentcontext.egg-info` and `agentconfigs.egg-info` into `directory`, as a build
        of the package with setuptools leaves them in the root of a checkout, which .gitignore
        hides."""
        for name in ("agentcontext", "agentconfigs"):
            folder = directory / f"{name}.egg-info"
            folder.mkdir(parents=True)
            (folder / "PKG-INFO").write_text(f"Metadata-Version: 2.4\nName: {name}\n"
                                             f"Version: 1.0.0\n", encoding="utf-8")

    def test_an_egg_info_folder_in_the_working_directory_is_no_installed_distribution(self):
        """`python -m agentcontext verify` puts the working directory first on `sys.path`. An
        `agentcontext.egg-info` there does not hide the metadata of the editable install in
        site-packages, which holds its `direct_url.json`. Neither it nor `agentconfigs.egg-info`
        counts as a distribution that the dependency lock does not list."""
        with tempfile.TemporaryDirectory() as tmp:
            site, checkout = Path(tmp) / "site-packages", Path(tmp) / "checkout"
            write_distribution(site, "agentcontext", "1.0.0",
                               {"__editable__.agentcontext-1.0.0.pth": "import finder\n"})
            (site / "agentcontext-1.0.0.dist-info" / "direct_url.json").write_text(
                json.dumps(EDITABLE), encoding="utf-8")
            self._egg_info_folders(checkout)
            with (contextlib.chdir(checkout),
                  mock.patch.object(sys, "path", [str(checkout), *sys.path]),
                  mock.patch.object(verify, "site_directories", return_value=[site])):
                installed, direct = verify.installed_versions(), verify.direct_installs()
        self.assertEqual(installed, {"agentcontext": "1.0.0"})
        self.assertEqual(direct, {"agentcontext": EDITABLE})
        self.assertEqual(verify.direct_install_differences(direct, "lock.txt"), [])

    def test_the_editable_install_is_found_from_a_directory_with_an_egg_info_folder(self):
        """The environment that runs this suite, .venv under `mise exec` or in CI, compared from a
        working directory with `agentcontext.egg-info` and `agentconfigs.egg-info`, holds the
        editable install of this checkout and no distribution that the folders name."""
        if os.environ.get("VIRTUAL_ENV") is None:
            self.skipTest("no virtual environment is active")
        probe = ("import json\nfrom agentcontext.tools import verify\n"
                 "print(json.dumps([sorted(verify.installed_versions()),\n"
                 "    verify.direct_install_differences(verify.direct_installs(), 'lock.txt')]))\n")
        with tempfile.TemporaryDirectory() as tmp:
            self._egg_info_folders(Path(tmp))
            child = subprocess.run([sys.executable, "-c", probe], cwd=tmp, capture_output=True,
                                   text=True, env={key: value for key, value in os.environ.items()
                                                   if key != "PYTHONPATH"})
        self.assertEqual(child.returncode, 0, child.stderr)
        names, differences = json.loads(child.stdout)
        self.assertIn("agentcontext", names)
        self.assertNotIn("agentconfigs", names)
        self.assertEqual(differences, [])

    def _main_over(self, installed: dict, direct: dict | None = None,
                   running: str | None = None, sites: list | None = None) -> tuple:
        """`verify.main` with `installed` as the distributions of the running interpreter, `direct`
        as those installed from a URL or a local path, by default the editable install of this
        checkout, `running` as its release of Python, by default the pinned one, `sites` as its
        site-packages, by default none, and the `python3` on PATH as this interpreter. Returns
        the exit code, the suites and checks that ran, and the log."""
        ran, out = [], io.StringIO()
        with (mock.patch.object(verify, "installed_versions", return_value=installed),
              mock.patch.object(verify, "direct_installs",
                                return_value={"agentcontext": EDITABLE} if direct is None
                                else direct),
              mock.patch.object(verify, "site_directories", return_value=sites or []),
              mock.patch.object(verify.platform, "python_version",
                                return_value=running or verify.pinned_python()),
              mock.patch.object(verify, "path_interpreter_differences", return_value=[]),
              mock.patch.object(verify, "run", side_effect=lambda label, *rest: ran.append(label)),
              mock.patch.object(verify.subprocess, "run",
                                return_value=mock.Mock(stdout="", stderr="", returncode=0)),
              contextlib.redirect_stderr(out)):
            code = verify.main([])
        return code, ran, out.getvalue()

    def test_the_verification_refuses_to_start_under_another_release_of_python(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        code, ran, log = self._main_over({**locked, "pip": "26.3"}, running="3.14.0")
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn(f"This is Python 3.14.0, and mise.toml pins {verify.pinned_python()}.", log)

    def test_the_verification_refuses_to_start_with_a_python3_on_path_of_another_environment(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        ran, out = [], io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = self._path_with_python3(tmp, "/elsewhere")
            with (mock.patch.dict(os.environ, {"PATH": path}),
                  mock.patch.object(verify, "installed_versions", return_value=locked),
                  mock.patch.object(verify, "direct_installs",
                                    return_value={"agentcontext": EDITABLE}),
                  mock.patch.object(verify, "site_directories", return_value=[]),
                  mock.patch.object(verify.platform, "python_version",
                                    return_value=verify.pinned_python()),
                  mock.patch.object(verify, "run",
                                    side_effect=lambda label, *rest: ran.append(label)),
                  contextlib.redirect_stderr(out)):
                code = verify.main([])
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn("is the interpreter of /elsewhere", out.getvalue())

    def test_the_verification_refuses_a_distribution_installed_from_a_local_directory(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        code, ran, log = self._main_over(
            locked, direct={"idna": {"url": "file:///tmp/idna", "dir_info": {}}})
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn("idna is installed from file:///tmp/idna rather than from the files that "
                      "locks/development.txt lists.", log)

    def test_the_verification_refuses_to_start_without_the_editable_install_of_the_checkout(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        code, ran, log = self._main_over(locked, direct={})
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn(f"agentcontext is not installed as an editable install of {REPO_ROOT}", log)

    def test_the_verification_refuses_to_start_with_a_file_that_differs_from_its_record(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_distribution(site, "idna", "3.20", {"idna/__init__.py": "core = True\n"})
            (site / "idna" / "__init__.py").write_text("core = True\nprint('fork')\n")
            code, ran, log = self._main_over(locked, sites=[site])
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn(f"{site / 'idna' / '__init__.py'} has another sha256 hash than the RECORD "
                      f"of idna lists.", log)
        # dev/setup.sh installs an edited file again from the locked file.
        self.assertNotIn("dev/setup.sh", log)

    def test_the_verification_refuses_to_start_with_a_file_that_no_record_lists(self):
        """The log names the file and, once, the environment that runs the verification, which
        does not hold the site-packages that the test writes."""
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            write_distribution(site, "idna", "3.20", {"idna/__init__.py": "core = True\n"})
            (site / "stray_module.py").write_text("import os\n")
            code, ran, log = self._main_over(locked, sites=[site])
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        self.assertIn(f"{site / 'stray_module.py'} is in site-packages, and no RECORD of a "
                      f"distribution lists it.", log)
        self.assertEqual(log.count(f"The verification runs in the environment {sys.prefix}, "), 1,
                         log)

    def test_the_verification_refuses_to_start_in_another_environment(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        installed = {**locked, "httpx": "0.28.1", "agentcontext": "1.0.0"}
        installed["pandas"] = "0.0.1"
        del installed["numpy"]
        code, ran, log = self._main_over(installed)
        self.assertEqual(code, 1)
        self.assertEqual(ran, [])
        for line in ("locks/development.txt does not list httpx 0.28.1, which is installed.",
                     f"locks/development.txt lists numpy {locked['numpy']}, which is not "
                     f"installed.",
                     f"locks/development.txt lists pandas {locked['pandas']}, and 0.0.1 is "
                     f"installed."):
            with self.subTest(line=line):
                self.assertEqual(log.count(line), 1, log)
        self.assertEqual(len(log.strip().splitlines()), 4, log)

    def test_the_verification_starts_in_the_environment_of_the_development_lock(self):
        locked = verify.locked_versions(verify.LOCK.read_text(encoding="utf-8"))
        code, ran, log = self._main_over({**locked, "agentcontext": "1.0.0", "pip": "26.3"})
        self.assertGreater(len(ran), len(verify.SUITES), log)
        self.assertEqual(code, 0, log)

    def test_every_entry_of_the_development_lock_is_read(self):
        """The pattern of an entry reads every entry that uv writes, the entries with an
        environment marker among them."""
        text = verify.LOCK.read_text(encoding="utf-8")
        entries = [line for line in text.splitlines() if re.match(r"[A-Za-z0-9]", line)]
        self.assertGreater(len(entries), 50)
        self.assertEqual([line for line in entries if not verify._LOCK_ENTRY.match(line)], [])


def write_distribution(site: Path, name: str, version: str, files: dict) -> None:
    """Write `files`, {path: text}, into `site` as the files of the distribution `name`, with a
    dist-info directory whose RECORD lists each file and the METADATA with its SHA-256, as pip and
    uv write it, and the RECORD itself without a hash."""
    info = f"{name}-{version}.dist-info"
    files = {**files, f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\n"
                                          f"Version: {version}\n"}
    rows = []
    for path, text in files.items():
        (site / path).parent.mkdir(parents=True, exist_ok=True)
        (site / path).write_text(text, encoding="utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        rows.append(f"{path},sha256={digest.decode()},{len(text.encode())}")
    rows.append(f"{info}/RECORD,,")
    (site / info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")


class TestTheSitePackagesCheck(unittest.TestCase):
    """The comparison of versions does not see a file of site-packages that an installer did not
    write, such as a module put there by hand or a locked distribution edited in place. The check
    compares site-packages with the RECORD files of the distributions, over site-packages that
    the tests write into the .venv of a checkout. The verification runs in that .venv unless a
    test says otherwise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.venv = self.tmp / "checkout" / ".venv"
        self.site = self.venv / "lib" / "python3.14" / "site-packages"
        self.site.mkdir(parents=True)
        venv = mock.patch.object(verify, "VENV", self.venv)
        venv.start()
        self.addCleanup(venv.stop)
        write_distribution(self.site, "idna", "3.20",
                           {"idna/__init__.py": "core = True\n", "idna/codec.py": "x = 1\n"})
        write_distribution(self.site, "six", "1.17.0", {"six.py": "PY3 = True\n"})

    def _differences(self, directories: list) -> list:
        """The differences of `directories` from their RECORD files, in the .venv of the
        checkout."""
        return verify.site_packages_differences(directories, str(self.venv))

    def test_site_packages_that_match_their_records_have_no_difference(self):
        """The `__pycache__` directories that Python writes on import, and the README.txt that
        CPython puts into the site-packages of its installation, belong to no distribution."""
        for pycache in (self.site / "__pycache__", self.site / "idna" / "__pycache__"):
            pycache.mkdir()
            (pycache / "x.cpython-314.pyc").write_bytes(b"compiled")
        (self.site / "README.txt").write_text("This directory exists so that 3rd party "
                                              "packages can be installed here.\n")
        self.assertEqual(self._differences([self.site]), [])

    def test_a_module_that_no_record_lists_is_a_difference(self):
        (self.site / "stray_module.py").write_text("import os\n")
        (self.site / "idna" / "extra.py").write_text("import os\n")
        self.assertEqual(self._differences([self.site]), [
            f"{self.site / 'idna' / 'extra.py'} is in site-packages, and no RECORD of a "
            f"distribution lists it.",
            f"{self.site / 'stray_module.py'} is in site-packages, and no RECORD of a "
            f"distribution lists it.", remedy(self.venv)])

    def test_an_edited_and_a_missing_file_are_differences(self):
        (self.site / "idna" / "__init__.py").write_text("core = True\nprint('fork')\n")
        (self.site / "six.py").unlink()
        self.assertEqual(self._differences([self.site]), [
            f"{self.site / 'idna' / '__init__.py'} has another sha256 hash than the RECORD of "
            f"idna lists.",
            f"{self.site / 'six.py'} is missing, and the RECORD of six lists it."])

    def test_a_distribution_without_a_record_owns_no_file(self):
        (self.site / "six-1.17.0.dist-info" / "RECORD").unlink()
        self.assertEqual(self._differences([self.site]), [
            f"{self.site / 'six-1.17.0.dist-info'} is in site-packages, and no RECORD of a "
            f"distribution lists it.",
            f"{self.site / 'six.py'} is in site-packages, and no RECORD of a distribution "
            f"lists it.", remedy(self.venv)])

    def test_a_distribution_that_an_earlier_one_of_its_name_hides_is_not_compared(self):
        """In the development container, the pip of its virtual environment comes first, and the
        pip of the system's site packages, whose tests the base image deleted, is never
        imported. Its files still belong to it."""
        system = self.tmp / "system-site-packages"
        system.mkdir()
        write_distribution(system, "idna", "3.20", {"idna/__init__.py": "core = True\n",
                                                    "idna/tests/test_core.py": "pass\n"})
        (system / "idna" / "tests" / "test_core.py").unlink()
        (system / "idna" / "__init__.py").write_text("hidden = True\n")
        self.assertEqual(self._differences([self.site, system]), [])
        self.assertEqual(len(self._differences([system, self.site])), 2)

    def test_more_differences_than_are_shown_are_counted(self):
        for number in range(verify.SHOWN + 3):
            (self.site / f"stray_{number:02}.py").write_text("pass\n")
        lines = self._differences([self.site])
        self.assertEqual(len(lines), verify.SHOWN + 2)
        self.assertEqual(lines[-2:], ["3 more files and directories in site-packages that no "
                                      "RECORD lists.", remedy(self.venv)])

    def _elsewhere(self, prefix: Path, venv: Path) -> str:
        """The line that follows the files and directories that no RECORD lists when they are
        not all in `venv`, the .venv of the checkout, or the verification runs in `prefix`, another
        environment."""
        return (f"The verification runs in the environment {prefix}, and dev/setup.sh installs "
                f"into {venv} only. Remove each file and directory that no RECORD lists, or create "
                f"the environment that holds it again with the distributions of "
                f"locks/development.txt only.")

    def test_in_another_environment_the_line_names_that_environment(self):
        """The development container runs the verification in a virtual environment of its own
        and has no .venv and no mise. dev/setup.sh does not install into that environment, and
        the line does not say to create .venv again."""
        (self.site / "stray_module.py").write_text("import os\n")
        of_the_checkout = self.tmp / "workspace" / ".venv"
        with mock.patch.object(verify, "VENV", of_the_checkout):
            lines = self._differences([self.site])
        self.assertEqual(lines, [f"{self.site / 'stray_module.py'} is in site-packages, and no "
                                 f"RECORD of a distribution lists it.",
                                 self._elsewhere(self.venv, of_the_checkout)])

    def test_a_file_outside_the_venv_is_not_removed_by_creating_it_again(self):
        """A .venv that includes the system's site packages imports them, and creating .venv
        again leaves a file there that no RECORD lists."""
        system = self.tmp / "system-site-packages"
        system.mkdir()
        (system / "stray_module.py").write_text("import os\n")
        self.assertEqual(self._differences([self.site, system]), [
            f"{system / 'stray_module.py'} is in site-packages, and no RECORD of a distribution "
            f"lists it.", self._elsewhere(self.venv, self.venv)])

    def test_the_site_packages_of_this_environment_match_their_records(self):
        """The environment that runs this suite, .venv under `mise exec` or in CI, was installed
        by uv, which lists every file that it writes in a RECORD."""
        if os.environ.get("VIRTUAL_ENV") is None:
            self.skipTest("no virtual environment is active")
        directories = verify.site_directories()
        self.assertGreater(len(directories), 0)
        self.assertEqual(verify.site_packages_differences(directories, sys.prefix), [])


class TestVerificationWrapper(unittest.TestCase):

    def _checkout(self, root: Path, python: str) -> dict:
        """A new repository at `root` with one commit of `4_data-analysis/notes.md`, edited after
        the commit, and the environment of a run of dev/verify.sh in it, with a `python3` first
        on PATH that runs the shell commands `python`."""
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        tracked = root / "4_data-analysis" / "notes.md"
        tracked.parent.mkdir()
        tracked.write_text("committed\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                        "commit", "-qm", "fixture"], cwd=root, check=True)
        tracked.write_text("local edit\n")
        bin_dir = root / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python3").write_text(f"#!/bin/sh\n{python}")
        (bin_dir / "python3").chmod(0o755)
        return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def _wrapper(self, root: Path, env: dict) -> subprocess.CompletedProcess:
        """Run dev/verify.sh in the repository at `root` with the report directory
        `root`/reports."""
        return subprocess.run([BASH, str(REPO_ROOT / "dev/verify.sh"), str(root / "reports")],
                              cwd=root, env=env, capture_output=True, text=True)

    def test_exit_status_and_local_edits_survive_the_wrapper(self):
        """A failed check must fail its wrapper, and the wrapper must leave edits alone. The
        wrapper runs the `python3` that comes first on PATH, which `mise exec` makes the one of
        the virtual environment of the checkout."""
        for code in (0, 2):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self._checkout(root, f'printf "check result\\n" > "$4"\nexit {code}\n')
                result = self._wrapper(root, env)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual((root / "4_data-analysis" / "notes.md").read_text(),
                                 "local edit\n")
                self.assertEqual(len(list((root / "reports").glob("verify-*.txt"))), 1)

    def test_a_run_without_a_report_shows_its_log_and_not_an_earlier_report(self):
        """A verification that exits before it writes its report, such as after a refusal of
        the environment, finds the report of an earlier verification of the same commit in the
        report directory. The wrapper removes that report before the run and prints the log,
        with the line that explains the exit, and not the lines of the earlier report."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self._checkout(root, 'printf "ERROR verify: the environment differs\\n"\n'
                                       'exit 1\n')
            commit = subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], cwd=root,
                                    capture_output=True, text=True, check=True).stdout.strip()
            earlier = root / "reports" / f"verify-{commit}.txt"
            earlier.parent.mkdir()
            earlier.write_text(f"VERIFICATION at {commit}\n"
                               f"PASS exit=0  700.0s  the suites of an earlier verification\n")
            result = self._wrapper(root, env)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("ERROR verify: the environment differs", result.stdout)
            self.assertNotIn("the suites of an earlier verification", result.stdout)
            self.assertFalse(earlier.exists())


class TestCurrentDatasetRestore(unittest.TestCase):

    def test_setup_extracts_the_current_commits_and_leaves_other_archives_alone(self):
        import shutil
        seven_zip = shutil.which(SEVEN_ZIP)
        if not seven_zip:
            self.skipTest('7zz is required for archive restoration')
        setup = REPO_ROOT / 'dev/setup.sh'
        for split in (False, True):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stage = root / 'data' / 'collection'
                (stage / 'output').mkdir(parents=True)
                (stage / 'data').mkdir()
                # An archive that setup does not name. Reading it would fail, and setup must
                # leave it alone.
                (stage / 'data/obsolete.7z').write_bytes(b'obsolete archive')
                source = root / 'source'
                source.mkdir()
                commits = source / 'all_ai_commits.csv'
                commits.write_text('repo_name,commit_sha\no/current,abc\n')
                archive = stage / 'output/all_ai_commits.csv.7z'
                subprocess.run([seven_zip, 'a', str(archive), commits.name],
                               cwd=source, check=True, capture_output=True)
                if split:
                    content = archive.read_bytes()
                    half = len(content) // 2
                    Path(str(archive) + '.001').write_bytes(content[:half])
                    Path(str(archive) + '.002').write_bytes(content[half:])
                    archive.unlink()
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                result = subprocess.run([BASH, str(setup), '--data-only'], env=script_env(),
                                        cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                restored = stage / 'output/all_ai_commits.csv'
                self.assertEqual(restored.read_text(), commits.read_text())
                self.assertEqual((stage / 'data/obsolete.7z').read_bytes(), b'obsolete archive')
                # A CSV that matches its archive is kept as it is.
                os.utime(restored, ns=(10**18, 10**18))
                subprocess.run([BASH, str(setup), '--data-only'], cwd=root, env=script_env(),
                               check=True, capture_output=True)
                self.assertEqual(restored.stat().st_mtime_ns, 10**18)
                # A CSV extracted from an earlier archive, as a pull leaves it, is extracted
                # again, because the analysis stops on it.
                restored.write_text('repo_name,commit_sha\no/earlier,abc\n')
                again = subprocess.run([BASH, str(setup), '--data-only'], cwd=root,
                                       env=script_env(), capture_output=True, text=True)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                self.assertEqual(restored.read_text(), commits.read_text())
                self.assertIn('output/all_ai_commits.csv differs from', again.stderr)
                if split:
                    Path(str(archive) + '.002').unlink()
                    restored.write_text('repo_name,commit_sha\no/earlier,abc\n')
                    unreadable = subprocess.run([BASH, str(setup), '--data-only'],
                                                env=script_env(), cwd=root,
                                                capture_output=True, text=True)
                    self.assertEqual(unreadable.returncode, 1)
                    self.assertEqual(restored.read_text(), 'repo_name,commit_sha\no/earlier,abc\n')
                    restored.unlink()
                    failed = subprocess.run([BASH, str(setup), '--data-only'],
                                            env=script_env(), cwd=root,
                                            capture_output=True, text=True)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertFalse(restored.exists())
                    self.assertEqual(list((stage / 'output').glob('.all_ai_commits.*')), [])

    def test_setup_extracts_the_missing_sampling_frame_and_labels_and_keeps_a_differing_file(self):
        """Analysis scripts read the sampling frame and the labels beside their archives, and
        the check of the step that derives the sample reads the list of the sample. The filter
        and the labeling write newer files there in a checkout, after hours of queries to GitHub
        or a paid run of a model, which setup must not overwrite."""
        import csv
        import shutil
        seven_zip = shutil.which(SEVEN_ZIP)
        if not seven_zip:
            self.skipTest('7zz is required for archive restoration')
        setup = REPO_ROOT / 'dev/setup.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data/collection/output').mkdir(parents=True)
            sampling, classification = root / 'data/sampling', root / 'data/classification'
            source = root / 'source'
            contents = {
                'ghs_filtered.json': '{"items": [{"name": "o/a"}, {"name": "o/b"}]}\n',
                'repos_annotated.csv': ('repo_name,github_id,engineered_project\n'
                                        'o/a,1,true\no/b,2,false\n'),
                'repos_annotated.json': '{"model": "a model"}\n',
                'repos_excluded.csv': 'repo_name\no/c\n',
            }
            source.mkdir()
            for name, text in contents.items():
                (source / name).write_text(text)
            archives = {sampling / 'ghs_filtered.json.7z': ['ghs_filtered.json'],
                        classification / 'repos_annotated.7z': ['repos_annotated.csv',
                                                                'repos_annotated.json',
                                                                'repos_excluded.csv']}
            for archive, names in archives.items():
                archive.parent.mkdir(parents=True)
                subprocess.run([seven_zip, 'a', str(archive), *names], cwd=source, check=True,
                               capture_output=True)
            extracted = {name: archive.parent / name
                         for archive, names in archives.items() for name in names}
            subprocess.run(['git', 'init', '-q', str(root)], check=True)

            def setup_run() -> subprocess.CompletedProcess:
                return subprocess.run([BASH, str(setup), '--data-only'], cwd=root,
                                      env=script_env(), capture_output=True, text=True)

            result = setup_run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name, path in extracted.items():
                self.assertEqual(path.read_text(), contents[name], name)
            sample = classification / 'repos_annotated_included.csv'
            with open(sample, newline='') as f:
                self.assertEqual([row['repo_name'] for row in csv.DictReader(f)], ['o/a'])
            self.assertEqual(list(classification.glob('.extract.*')), [])
            # Files that match their archive, and the list derived from them, are kept.
            for path in [*extracted.values(), sample]:
                os.utime(path, ns=(10**18, 10**18))
            again = setup_run()
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            for path in [*extracted.values(), sample]:
                self.assertEqual(path.stat().st_mtime_ns, 10**18, path.name)
            # Newer labels beside the archive are kept, and no other file of the archive is
            # extracted beside them, nor is the list of the sample derived from them.
            newer = 'repo_name,github_id,engineered_project\no/a,1,true\no/b,2,true\n'
            extracted['repos_annotated.csv'].write_text(newer)
            extracted['repos_annotated.json'].unlink()
            kept = setup_run()
            self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
            self.assertEqual(extracted['repos_annotated.csv'].read_text(), newer)
            self.assertFalse(extracted['repos_annotated.json'].exists())
            self.assertEqual(sample.stat().st_mtime_ns, 10**18)
            self.assertIn('repos_annotated.csv differs from', kept.stderr)

    def test_setup_installs_no_file_of_an_extraction_that_fails(self):
        """The listing of an archive reads only its header, and the extraction reads its data.
        7zz writes a file even when its data fails the CRC and exits 2, and it extracts no file for
        a name that the archive stores in a directory. Setup extracts into a directory beside the
        archive and removes it, also when a signal terminates setup during the extraction,
        because an untracked directory fails the verification."""
        import shutil
        seven_zip = shutil.which(SEVEN_ZIP)
        if not seven_zip:
            self.skipTest('7zz is required for archive restoration')
        setup = REPO_ROOT / 'dev/setup.sh'
        frame = '{"items": [{"name": "o/a"}, {"name": "o/b"}]}\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'data/collection/output').mkdir(parents=True)
            sampling = root / 'data/sampling'
            sampling.mkdir(parents=True)
            source = root / 'source'
            (source / 'nested').mkdir(parents=True)
            (source / 'ghs_filtered.json').write_text(frame)
            (source / 'nested/ghs_filtered.json').write_text(frame)
            fakes = root / 'bin'
            fakes.mkdir()
            # A 7zz that passes the listing to 7zz, and that writes the file of an extraction
            # and then sends the termination signal to setup.
            (fakes / '7zz').write_text(
                '#!/bin/bash\n'
                'if [ "$1" = x ]; then\n'
                '    for argument in "$@"; do\n'
                '        case "$argument" in -o*) out=${argument#-o} ;; esac\n'
                '    done\n'
                '    echo partial > "$out/ghs_filtered.json"\n'
                '    kill -TERM "$PPID"\n'
                '    exit 0\n'
                'fi\n'
                f'exec "{seven_zip}" "$@"\n')
            (fakes / '7zz').chmod(0o755)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            archive = sampling / 'ghs_filtered.json.7z'
            # Stored without compression, so that the bytes of the sampling frame are the packed
            # data, and the header after them still lists the file with their CRC.
            stored = root / 'stored.7z'
            subprocess.run([seven_zip, 'a', '-mx0', str(stored), 'ghs_filtered.json'],
                           cwd=source, check=True, capture_output=True)
            data = stored.read_bytes()
            corrupt = data.replace(b'"items"', b'"XXXXX"')
            self.assertNotEqual(corrupt, data)
            nested = root / 'nested.7z'
            subprocess.run([seven_zip, 'a', str(nested), 'nested/ghs_filtered.json'],
                           cwd=source, check=True, capture_output=True)
            for case, content, search_path, expected in [
                    ('corrupt data', corrupt, os.environ['PATH'], 'Could not extract'),
                    ('a file in a directory', nested.read_bytes(), os.environ['PATH'],
                     'does not contain ghs_filtered.json at its top level'),
                    ('a termination signal', data, f'{fakes}{os.pathsep}{os.environ["PATH"]}',
                     None)]:
                with self.subTest(case=case):
                    archive.write_bytes(content)
                    listed = subprocess.run([seven_zip, 'l', str(archive)], capture_output=True)
                    self.assertEqual(listed.returncode, 0, case)
                    result = subprocess.run([BASH, str(setup), '--data-only'], cwd=root,
                                            capture_output=True, text=True,
                                            env=script_env({**os.environ, 'PATH': search_path}))
                    if expected is None:
                        self.assertEqual(result.returncode, -signal.SIGTERM,
                                         result.stdout + result.stderr)
                    else:
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assertIn(expected, result.stderr)
                    self.assertEqual(sorted(path.name for path in sampling.iterdir()),
                                     ['ghs_filtered.json.7z'])


if __name__ == "__main__":
    unittest.main()
