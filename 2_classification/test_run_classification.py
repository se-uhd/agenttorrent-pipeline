"""
Tests for run_classification.py

Strategy:
  - The runner is a list of steps that agentcontext/runner.py runs. The suite of
    agentcontext/runner.py tests the runner itself, and this suite tests the list. It checks
    the two scripts, the commands that 0_README.md documents for them and what they declare. It
    also checks that a change to anything that decides a label runs the classification again
    and that new labels run the comparison again.
  - One case runs the stage over a stand-in step rooted at a temporary directory, and nothing
    reads a README or calls a model.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_run_classification -v
"""

import contextlib
import dataclasses
import importlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from agentcontext import logs

sys.path.insert(0, str(Path(__file__).resolve().parent))
rc = importlib.import_module("run_classification")
runner = rc.runner
HERE = Path(__file__).resolve().parent


class TestStepList(unittest.TestCase):

    def test_the_stage_labels_compares_and_selects(self):
        """The comparison with the previous run ends a run over the sampling frame
        (changed_labels.py) rather than being a step of its own. The selection of the sample is
        the last step."""
        self.assertEqual([(s.key, s.args) for s in rc.STEPS],
                         [("repository_labeling", ["1_classify_repos.py"]),
                          ("engineered_project_selection",
                           ["-m", "agentcontext.steps.engineered_project_selection"])])
        self.assertEqual({s.stage for s in rc.STEPS}, {"classification"})
        self.assertIn("label_changes.csv", rc.STEPS[0].produces)
        self.assertEqual((rc.STEPS[0].root, rc.STEPS[0].code), ("classification", "2_classification"))
        self.assertEqual(runner.code_dir(rc.STEPS[0]), HERE)
        self.assertEqual(runner.data_dir(rc.STEPS[0]), rc.CLASSIFICATION_DIR)
        self.assertTrue((runner.code_dir(rc.STEPS[0]) / rc.STEPS[0].args[0]).is_file())

    def test_the_selection_is_the_packages_step_and_follows_the_labeling(self):
        """The step has one definition. The collection's list, which the container runs alone,
        has the same step, and this list adds only the edge to the labeling."""
        package = next(s for s in runner.CORE_STEPS if s.key == "engineered_project_selection")
        self.assertEqual(rc.STEPS[1], dataclasses.replace(package, consumes=("repository_labeling",)))
        self.assertEqual(runner.invalidated_by(rc.STEPS, "repository_labeling"),
                         ["repository_labeling", "engineered_project_selection"])

    def test_the_commands_are_the_ones_the_readme_documents(self):
        readme = (HERE / "0_README.md").read_text(encoding="utf-8")
        for step in rc.STEPS[:1]:
            with self.subTest(step=step.key):
                documented = re.search(rf"^python3 ({re.escape(step.args[0])})(.*)$", readme,
                                       re.MULTILINE)
                self.assertIsNotNone(documented)
                self.assertEqual(step.args, [documented.group(1), *documented.group(2).split()])

    def test_what_decides_a_label_runs_the_step_again(self):
        """What decides a label is the sampling frame and, in the code, the two prompts and
        every module that the script imports. Nothing else needs to run the step again. Over an
        unchanged sampling frame, the step that runs again completes the finished run when the
        current rules derive each of its decisions unchanged."""
        step = rc.STEPS[0]
        self.assertEqual(step.completion, runner.COMPLETION_INPUTS)
        self.assertFalse(step.irreproducible)
        local = {path.name for path in HERE.glob("*.py")}
        imported = set(re.findall(r"^(?:import|from) (\w+)",
                                  (HERE / "1_classify_repos.py").read_text(), re.MULTILINE))
        source = (HERE / "1_classify_repos.py").read_text()
        imported |= set(re.findall(r"^\s+from (\w+) import", source, re.MULTILINE))
        imported |= set(re.findall(r"^\s+import (\w+)$", source, re.MULTILINE))
        modules = {f"{name}.py" for name in imported} & local
        modules |= {"classification_tools.py"}  # the bridge starts it as a subprocess
        self.assertEqual(step.inputs, (rc.SAMPLING_FRAME,))
        self.assertEqual(set(step.code_inputs) - {rc.PROMPT, rc.SUMMARY_PROMPT}, modules)
        for path in step.code_inputs:
            with self.subTest(path=path):
                self.assertTrue((runner.code_dir(step) / path).is_file())

    def test_the_sampling_frame_is_read_where_the_sampling_stage_writes_it(self):
        step = rc.STEPS[0]
        self.assertIn(rc.SAMPLING_FRAME, step.reads)
        self.assertEqual((runner.data_dir(step) / rc.SAMPLING_FRAME).resolve(),
                         (rc.CLASSIFICATION_DIR.parent / "sampling" / "ghs_filtered.json").resolve())

    def test_the_credentials_and_packages_are_checked_before_it_runs(self):
        step = rc.STEPS[0]
        # The default model, DeepSeek V4.1 Flash, labels and summarizes through OpenRouter.
        self.assertEqual(step.env, ("OPENROUTER_API_KEY", "GITHUB_TOKENS|GITHUB_TOKEN"))
        requirements = (HERE / "requirements.txt").read_text()
        for package in step.imports:
            name = {"lingua": "lingua-language-detector"}.get(package, package)
            with self.subTest(package=package):
                self.assertRegex(requirements, rf"(?m)^{re.escape(name)}==")

    def test_no_binary_is_needed(self):
        """GitHub's API returns the README and the outline of the files, and neither git nor
        Linguist runs (rule 11 of the section "The reading rules" of 0_README.md)."""
        self.assertEqual({step.binaries for step in rc.STEPS}, {()})
        script = (HERE / "classification_tools.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", script)

    def test_the_included_list_is_declared_by_the_selection_alone(self):
        """`agentcontext/steps/engineered_project_selection.py` derives the included list, and
        no other step declares it."""
        owners = [s.key for s in rc.STEPS
                  if any(p.endswith("repos_annotated_included.csv") for p in s.produces)]
        self.assertEqual(owners, ["engineered_project_selection"])
        self.assertEqual((runner.data_dir(rc.STEPS[1]) / rc.STEPS[1].produces[0]).resolve(),
                         (rc.CLASSIFICATION_DIR / "repos_annotated_included.csv").resolve())


class TestListAndARun(unittest.TestCase):

    def test_list_prints_the_stage_and_its_two_steps(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(rc.main(["--list"]), 0)
        lines = logs.messages(out.getvalue()).splitlines()
        self.assertEqual(lines[0], "The stage classification runs these steps in order.")
        self.assertEqual([l.split()[0] for l in lines[1:]],
                         ["repository_labeling", "engineered_project_selection"])
        self.assertTrue(lines[1].endswith("(data directory classification)."), lines[1])
        self.assertIn(" 2_classification/1_classify_repos.py", lines[1])

    def test_a_run_over_a_stand_in_records_it_here(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "frame.json").write_text("{}")
        steps = [rc.Step("repository_labeling", sys.executable,
                           ["-c", "open('repos_annotated.csv', 'w').write('x')"],
                           produces=("repos_annotated.csv",), inputs=("frame.json",),
                           completion=runner.COMPLETION_INPUTS, root=str(tmp))]
        saved = (rc.STEPS, rc.CLASSIFICATION_DIR, rc.MIN_FREE_DISK_GB)
        rc.STEPS, rc.CLASSIFICATION_DIR, rc.MIN_FREE_DISK_GB = steps, tmp, 0
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rc.main([]), 0)
        finally:
            rc.STEPS, rc.CLASSIFICATION_DIR, rc.MIN_FREE_DISK_GB = saved
        state = json.loads((tmp / "run_logs" / "state.json").read_text())
        self.assertEqual(set(state["completed"]), {"repository_labeling"})

    def test_mark_complete_refuses_a_step_argument_that_it_would_ignore(self):
        """The step arguments are added to the steps before `run` receives them, and `run`
        would therefore ignore the flag that the refusal names."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = (rc.CLASSIFICATION_DIR, rc.MIN_FREE_DISK_GB)
        rc.CLASSIFICATION_DIR, rc.MIN_FREE_DISK_GB = tmp, 0
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(rc.main(["--mark-complete", "repository_labeling",
                                          "--step-arg", "repository_labeling:--workers 4"]), 1)
        finally:
            rc.CLASSIFICATION_DIR, rc.MIN_FREE_DISK_GB = saved
        self.assertIn("cannot be combined with --step-arg", err.getvalue())


if __name__ == "__main__":
    unittest.main()
