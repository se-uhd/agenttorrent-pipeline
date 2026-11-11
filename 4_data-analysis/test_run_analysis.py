"""
Tests for run_analysis.py

Strategy:
  - run_analysis.py is a list of steps that agentcontext/runner.py runs, and the runner's
    own suite tests the runner. This suite tests the list against the scripts on disk, the
    files that they write, and the check's own list of scripts.
  - One run over stand-ins rooted at a temporary directory.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_run_analysis -v
"""

import concurrent.futures
import contextlib
import fnmatch
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from agentcontext import logs

sys.path.insert(0, str(Path(__file__).resolve().parent))
ra = importlib.import_module("run_analysis")
check = importlib.import_module("check_reproduction")
runner = ra.runner
HERE = Path(__file__).resolve().parent
ANALYSES = [s for s in ra.STEPS if s.key != "reproduction_check"]


def _tracked() -> set:
    """The tracked files of the analysis stage's data directory, relative to it."""
    out = subprocess.run(["git", "ls-files", "--", str(ra.ANALYSIS_DIR)], capture_output=True,
                         text=True, cwd=str(ra.ANALYSIS_DIR), check=True).stdout.split("\n")
    return {line for line in out if line}


class TestStepList(unittest.TestCase):

    def test_every_script_that_the_check_names_here_is_a_step_and_nothing_else(self):
        """The check runs a script or names it in NOT_RUN, and either way the pipeline runs it."""
        known = [rel for rel, _ in check.SCRIPTS] + list(check.NOT_RUN)
        in_check = {rel.split("/", 1)[1] for rel in known if rel.startswith("4_data-analysis/")}
        self.assertEqual({s.args[0] for s in ANALYSES}, in_check)

    def test_every_step_runs_in_the_data_directory_and_its_script_is_here(self):
        for step in ra.STEPS:
            with self.subTest(step=step.key):
                self.assertEqual((step.root, step.code), ("analysis", "4_data-analysis"))
                self.assertEqual(runner.data_dir(step), ra.ANALYSIS_DIR)
                self.assertTrue((runner.code_dir(step) / step.args[0]).is_file())

    def test_a_module_of_the_package_that_a_script_imports_reruns_its_step(self):
        """The table of the imports takes its columns from `imports.STATUSES`. A new image
        that changed only the order of the statuses would rewrite no input of the step. The
        step would then keep its record, and the handover the old table. The test runs over a
        copy of the package in which only that module changes."""
        step = next(s for s in ANALYSES if s.key == "dataset_imports")
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "agentcontext"
            shutil.copytree(runner.PACKAGE_DIR, package,
                            ignore=shutil.ignore_patterns("__pycache__"))
            module = package / "imports.py"
            with unittest.mock.patch.object(runner, "PACKAGE_DIR", package):
                before = runner.script_fingerprint(step)
                module.write_text(module.read_text(encoding="utf-8")
                                  + "\nSTATUSES = tuple(reversed(STATUSES))\n", encoding="utf-8")
                after = runner.script_fingerprint(step)
        self.assertNotEqual(before, after)

    def test_keys_are_the_script_paths_with_the_directory_as_prefix(self):
        for step in ANALYSES:
            directory, name = step.args[0].split("/")
            self.assertEqual(step.key, f"{directory}_{name[:-3]}")

    def test_every_analysis_is_recorded_by_its_inputs(self):
        for step in ANALYSES:
            with self.subTest(step=step.key):
                self.assertEqual(step.completion, runner.COMPLETION_INPUTS)
                self.assertTrue(step.inputs)

    def test_every_output_a_step_declares_is_a_tracked_file(self):
        """The outputs of a script that the check does not run are the exception. No checkout
        writes them, and git ignores them, so that a pipeline run in a checkout leaves
        `git status` clean."""
        tracked = _tracked()
        for step in ANALYSES:
            for rel in step.produces:
                with self.subTest(step=step.key, path=rel):
                    if f"4_data-analysis/{step.args[0]}" in check.NOT_RUN:
                        self.assertNotIn(rel, tracked)
                        ignored = subprocess.run(["git", "check-ignore", "-q", rel],
                                                 cwd=str(ra.ANALYSIS_DIR))
                        self.assertEqual(ignored.returncode, 0, f"{rel} is not ignored")
                    else:
                        self.assertIn(rel, tracked)

    def test_every_tracked_output_is_declared_by_a_step(self):
        """The two lists, what is tracked and what is declared, have to agree."""
        declared = {rel for s in ANALYSES for rel in s.produces}
        outputs = {rel for rel in _tracked()
                   if rel.split("/")[0] in ("rq2", "rq3", "dataset")
                   and rel.endswith((".csv", ".tex", ".pdf"))}
        self.assertEqual(outputs - declared, set())

    def test_the_output_csvs_every_step_reads_are_tracked_or_alternatives(self):
        for step in ANALYSES:
            for rel in step.inputs:
                if rel.startswith(ra.OUT):
                    with self.subTest(step=step.key, path=rel):
                        self.assertIsNotNone(runner.input_path(step, rel))

    def test_the_check_runs_last_and_after_every_analysis(self):
        self.assertEqual(ra.STEPS[-1].key, "reproduction_check")
        self.assertEqual(set(ra.STEPS[-1].consumes), {s.key for s in ANALYSES})
        self.assertEqual(ra.STEPS[-1].args[1:], ["--skip-missing", "--reproduce-only"])


    def test_every_output_csv_a_script_names_is_among_its_inputs(self):
        """A change to an output CSV that a script reads and does not declare would rerun
        nothing. The test reads each script's source for output CSVs named in quotes."""
        tables = {path.name for path in (ra.ANALYSIS_DIR / ra.OUT).glob("*.csv")}
        for step in ANALYSES:
            source = (HERE / step.args[0]).read_text(encoding="utf-8")
            declared = {alt for rel in step.inputs for alt in rel.split("|")}
            for name in sorted(set(re.findall(r'"([\w-]+\.csv)"', source)) & tables):
                with self.subTest(step=step.key, table=name):
                    self.assertIn(f"{ra.OUT}/{name}", declared)



# Run inside the child: record every file under the fixture's output directory that the
# script opens. Python's audit hook receives every `open` event, pandas' included, and every
# path handed to a subprocess, such as the 7zz that extracts the commit archive.
_OPEN_RECORDER = """
import os, runpy, sys
out = os.path.realpath(os.environ["DATA_COLLECTION_OUTPUT"])
opened = set()
def note(name):
    if isinstance(name, (str, bytes, os.PathLike)):
        path = os.path.realpath(os.fsdecode(name))
        if path.startswith(out + os.sep):
            opened.add(os.path.relpath(path, out))
def hook(event, args):
    if event == "open" and args:
        note(args[0])
    elif event == "subprocess.Popen" and len(args) > 1:
        for arg in args[1] or ():
            note(arg)
sys.addaudithook(hook)
script = sys.argv[1]
sys.argv = [script, *sys.argv[2:]]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
try:
    runpy.run_path(script, run_name="__main__")
finally:
    sys.stderr.write("OPENED " + " ".join(sorted(opened)) + "\\n")
"""


class TestEveryOutputCsvAScriptOpensIsDeclared(unittest.TestCase):
    """A script that reads an output CSV through `_shared` quotes no CSV name, and the test
    above looks for quoted names. Each script runs once over the fixture instead, and every
    output CSV that it opens has to be among its step's inputs. A step that reads the classification
    labels, the sampling frame or the validated tree is left out when these inputs are not on
    disk, as in CI."""

    FIXTURE = HERE / "testdata" / "output"
    # The fixture has no intersection as large as the UpSet figure's default minimum. The
    # figure's own suite draws it over the fixture with this argument.
    FIXTURE_ARGS = {"rq2_upset_plot": ["--min-count", "1"]}

    def _runnable(self, step) -> bool:
        if step.reads or f"4_data-analysis/{step.args[0]}" in check.NOT_RUN:
            return False
        # The first alternative of an input outside the output CSVs is the extracted file that
        # the script reads. The archive after it is no input that the script can read instead.
        outside = [rel.split("|")[0] for rel in step.inputs if not rel.startswith(ra.OUT)]
        outside = [rel for rel in outside if rel.endswith((".csv", ".json"))]
        return all((ra.ANALYSIS_DIR / rel).exists() for rel in outside)

    def _opened(self, step, tmp: Path) -> set:
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(self.FIXTURE), ANALYSIS_OUTPUT_DIR=str(tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0")
        result = subprocess.run([sys.executable, "-c", _OPEN_RECORDER, str(HERE / step.args[0]),
                                 *self.FIXTURE_ARGS.get(step.key, [])],
                                cwd=str(HERE), env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        line = [l for l in result.stderr.splitlines() if l.startswith("OPENED ")][-1]
        return set(line[len("OPENED "):].split())

    def test_every_output_csv_a_script_opens_is_an_input_of_its_step(self):
        steps = [s for s in ANALYSES if self._runnable(s)]
        self.assertGreater(len(steps), 10)
        with tempfile.TemporaryDirectory() as root, \
                concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            runs = {s.key: pool.submit(self._opened, s, Path(root) / s.key) for s in steps}
            for step in steps:
                declared = {alt[len(ra.OUT) + 1:] for rel in step.inputs for alt in rel.split("|")
                            if alt.startswith(ra.OUT + "/")}
                opened = {name for name in runs[step.key].result()
                          if name.endswith(".csv") or ".csv.7z" in name}
                with self.subTest(step=step.key):
                    if declared - {"*"}:
                        self.assertTrue(opened, "the script opened no output CSV")
                    for name in sorted(opened):
                        self.assertTrue(any(fnmatch.fnmatch(name, pattern) for pattern in declared),
                                        f"{step.key} opens {name} and does not declare it")


class TestListAndARun(unittest.TestCase):

    def test_list_prints_the_analysis_steps(self):
        """The keys are written out in running order, and the test fails for a deleted step."""
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(ra.main(["--list"]), 0)
        lines = logs.messages(out.getvalue()).splitlines()
        self.assertEqual(lines[0], "The stage analysis runs these steps in order.")
        keys = [line.split()[0] for line in lines[1:]]
        self.assertEqual(keys, [
            "rq2_config_cooccurrence", "rq2_config_mechanism_upset", "rq2_config_tool_heatmap",
            "rq2_content_quality", "rq2_language_ranking_all", "rq2_language_tool_association",
            "rq2_repo_metadata_comparison", "rq2_tool_cooccurrence", "rq2_tool_count_distribution",
            "rq2_upset_plot", "rq2_venn_diagram", "rq3_adoption_timeline",
            "rq3_cooccurrence_analysis", "rq3_count_distribution", "dataset_artifact_distribution",
            "dataset_tool_adoption", "dataset_monthly_ai_commit_volume",
            "dataset_commit_message_first_words", "dataset_filter_steps_table",
            "dataset_planning_documents", "dataset_imports", "reproduction_check"])

    def test_a_changed_csv_re_runs_the_figure_that_reads_it(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "table.csv").write_text("a\n1\n")
        (tmp / "figure.py").write_text("open('figure.txt', 'w').write(open('table.csv').read())\n")
        steps = [ra.Step("figure", sys.executable, ["figure.py"], produces=("figure.txt",),
                           inputs=("table.csv",), completion=ra.COMPLETION_INPUTS, root=str(tmp))]
        saved = (ra.STEPS, ra.ANALYSIS_DIR, ra.MIN_FREE_DISK_GB)
        ra.STEPS, ra.ANALYSIS_DIR, ra.MIN_FREE_DISK_GB = steps, tmp, 0
        try:
            def run() -> str:
                """The runner's log messages, which it writes to stderr. A step writes to stdout."""
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    self.assertEqual(ra.main([]), 0)
                return err.getvalue()
            run()
            self.assertIn("INFO runner: figure skipped, because it is already complete", run())
            (tmp / "table.csv").write_text("a\n2\n")
            out = run()
            self.assertIn("An input of figure (table.csv) or its script has changed", out)
            self.assertEqual((tmp / "figure.txt").read_text(), "a\n2\n")
        finally:
            ra.STEPS, ra.ANALYSIS_DIR, ra.MIN_FREE_DISK_GB = saved


class TestAnalysisInputChanges(unittest.TestCase):
    """Completion follows dataset inputs and imported analysis code."""

    def setUp(self):
        from unittest.mock import patch
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.analysis = self.root / '4_data-analysis'
        self.analysis.mkdir()
        self.heuristics = self.root / 'heuristics'
        self.heuristics.mkdir()
        patcher = patch.object(runner, 'HEURISTICS_DIR', self.heuristics)
        patcher.start()
        self.addCleanup(patcher.stop)
        for source in HERE.rglob('*.py'):
            target = self.analysis / source.relative_to(HERE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        self.data = self.root / 'data' / 'analysis'
        self.data.mkdir(parents=True)
        self.dataset_inputs = [
            '../collection/output/' + name + '.csv'
            for name in ('repos', 'context-files', 'skills', 'commands', 'subagents',
                         'rules', 'settings', 'hooks', 'mcp', 'plugins', 'lsp', 'all_ai_commits',
                         'planning-documents', 'imports')]
        for rel in self.dataset_inputs + ['../sampling/ghs_filtered.json',
                                         '../classification/repos_annotated.csv']:
            path = self.data / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('original input\n')
        (self.heuristics / 'config_path_patterns.csv').write_text('original input\n')

    def step(self, key):
        from dataclasses import replace
        return replace(next(s for s in ra.STEPS if s.key == key), root=str(self.data),
                       code=str(self.analysis))

    def assert_invalidated(self, step, path):
        before = runner.step_fingerprint(step)
        self.assertTrue(before)
        steps = [step]
        steps.append(self.step('reproduction_check'))
        for rel in step.produces:
            output = self.data / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('completed output')
        state = {'completed': {item.key: runner.step_fingerprint(item) for item in steps}}
        self.assertEqual(runner.completed_steps(steps, state), {item.key for item in steps})
        previous = path.read_bytes()
        path.write_bytes(previous + b'changed\n')
        try:
            self.assertNotEqual(runner.step_fingerprint(step), before, str(path))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(runner.completed_steps(steps, state), set())
        finally:
            path.write_bytes(previous)



    def test_every_split_commit_volume_invalidates_its_figures(self):
        csv_path = self.data / '../collection/output/all_ai_commits.csv'
        csv_path.unlink()
        volumes = [Path(str(csv_path) + f'.7z.{i:03}') for i in range(1, 4)]
        for volume in volumes:
            volume.write_bytes(b'archive data')
        for key in ('dataset_monthly_ai_commit_volume', 'dataset_commit_message_first_words'):
            for volume in volumes:
                with self.subTest(step=key, volume=volume.name):
                    self.assert_invalidated(self.step(key), volume)

    def test_every_commit_volume_invalidates_its_figures_beside_the_extracted_csv(self):
        """In a checkout, the extracted CSV stays beside the archive that git tracks, and a merge
        or a pull can replace the archive under it. The loader compares the two only when the
        step runs, so the archive has to run the step again."""
        csv_path = self.data / '../collection/output/all_ai_commits.csv'
        volumes = [Path(str(csv_path) + f'.7z.{i:03}') for i in range(1, 4)]
        for volume in volumes:
            volume.write_bytes(b'archive data')
        for key in ('dataset_monthly_ai_commit_volume', 'dataset_commit_message_first_words'):
            for path in (csv_path, *volumes):
                with self.subTest(step=key, path=path.name):
                    self.assert_invalidated(self.step(key), path)

    def test_the_archives_of_the_labels_and_the_sampling_frame_invalidate_their_readers(self):
        """The archive beside an extracted file counts as the commit archive does, so that a
        new tracked archive runs the steps again, and their scripts then compare the extracted
        file with it."""
        labels = self.data / '../classification/repos_annotated.7z'
        sampling_frame = self.data / '../sampling/ghs_filtered.json.7z'
        for archive in (labels, sampling_frame):
            archive.write_bytes(b'archive data')
        for key in ('rq2_language_ranking_all', 'rq2_repo_metadata_comparison'):
            with self.subTest(step=key, input=labels.name):
                self.assert_invalidated(self.step(key), labels)
        for key in ('rq2_language_ranking_all', 'rq2_repo_metadata_comparison',
                    'rq2_language_tool_association', 'dataset_filter_steps_table'):
            with self.subTest(step=key, input=sampling_frame.name):
                self.assert_invalidated(self.step(key), sampling_frame)

    def test_shared_code_and_context_heuristics_invalidate_every_figure(self):
        """The test covers every step and fails for a step list that declares `_shared.py`
        only for the steps that name no other code input."""
        for step in ANALYSES:
            for path in (self.analysis / '_shared.py', self.heuristics / 'config_path_patterns.csv'):
                with self.subTest(step=step.key, input=path.name):
                    self.assert_invalidated(self.step(step.key), path)

    def test_metadata_helper_invalidates_its_consumers(self):
        for key in ('rq2_language_ranking_all', 'rq2_repo_metadata_comparison'):
            with self.subTest(step=key):
                self.assert_invalidated(self.step(key), self.analysis / 'rq2/_repo_metadata.py')

    def test_the_classification_and_the_sampling_frame_invalidate_their_readers(self):
        """`_repo_metadata.build_repo_metadata` reads both, and the two scripts that import it
        are the only readers of the classification labels. The language association and the
        filter steps table read the sampling frame too."""
        annotated = self.data / '../classification/repos_annotated.csv'
        sampling_frame = self.data / '../sampling/ghs_filtered.json'
        for key in ('rq2_language_ranking_all', 'rq2_repo_metadata_comparison'):
            with self.subTest(step=key, input=annotated.name):
                self.assert_invalidated(self.step(key), annotated)
        for key in ('rq2_language_ranking_all', 'rq2_repo_metadata_comparison',
                    'rq2_language_tool_association', 'dataset_filter_steps_table'):
            with self.subTest(step=key, input=sampling_frame.name):
                self.assert_invalidated(self.step(key), sampling_frame)

    def test_a_listed_file_of_the_validated_tree_invalidates_the_content_quality(self):
        """A listed file of the tree is fingerprinted, and the step runs again when a file that
        the CSVs list changes under the same CSVs. A file of the tree that no CSV lists is not
        read and changes nothing. A listed file that is gone leaves the step without a
        fingerprint, and the step then runs and its script logs the missing file."""
        (self.data / '../collection/output/context-files.csv').write_text(
            'repo_name,context_file\no/r,docs/CLAUDE.md\n')
        repository = self.data / '../collection/data/validated/o§r'
        (repository / 'docs').mkdir(parents=True)
        listed = repository / 'docs' / 'CLAUDE.md'
        listed.write_text('# Instructions\n')
        unlisted = repository / 'notes.md'
        unlisted.write_text('not configuration\n')
        step = self.step('rq2_content_quality')
        self.assert_invalidated(step, listed)
        before = runner.step_fingerprint(step)
        unlisted.write_text('changed\n')
        self.assertEqual(runner.step_fingerprint(step), before)
        listed.unlink()
        self.assertEqual(runner.step_fingerprint(step), '')

    def test_the_file_that_a_listed_marker_names_invalidates_the_content_quality(self):
        """The step counts the lines of a linked rule in the file that its marker names. A
        change of that file runs the step again, although no CSV lists the file."""
        (self.data / '../collection/output/rules.csv').write_text(
            'repo_name,rule\no/r,.agent/rules/x.md\n')
        repository = self.data / '../collection/data/validated/o§r'
        (repository / '.agent' / 'rules').mkdir(parents=True)
        (repository / '.agent' / 'rules' / 'x.md').write_text('../../docs/rules/x.md\n')
        (repository / 'docs' / 'rules').mkdir(parents=True)
        named = repository / 'docs' / 'rules' / 'x.md'
        named.write_text('---\ntrigger: always_on\n---\nUse tabs.\n')
        self.assert_invalidated(self.step('rq2_content_quality'), named)

    def test_a_listed_marker_with_a_target_that_the_tree_lacks_is_fingerprinted(self):
        """`detection.measured_artifact` names no file for such a marker, and the step reads
        the marker alone. The step's fingerprint covers the marker."""
        (self.data / '../collection/output/skills.csv').write_text(
            'repo_name,skills.md\no/r,.claude/skills/create-skill/SKILL.md\n')
        repository = self.data / '../collection/data/validated/o§r'
        (repository / '.claude' / 'skills' / 'create-skill').mkdir(parents=True)
        marker = repository / '.claude' / 'skills' / 'create-skill' / 'SKILL.md'
        marker.write_text('../../../.opencode/skills/create-skill/SKILL.md\n')
        self.assert_invalidated(self.step('rq2_content_quality'), marker)

    def test_a_listed_context_file_that_is_a_link_without_a_target_is_fingerprinted(self):
        """The script reads a context file that the tree holds as a real symlink as the link's
        text, also where the link has no target (`detection.artifact_text`). Such a link is no
        missing input: the step and the reproduction check stay done under an unchanged tree, and
        a link that names another target runs the step again."""
        (self.data / '../collection/output/context-files.csv').write_text(
            'repo_name,context_file\no/r,.agents/CLAUDE.md\n')
        repository = self.data / '../collection/data/validated/o§r'
        (repository / '.agents').mkdir(parents=True)
        link = repository / '.agents' / 'CLAUDE.md'
        os.symlink('notes/AGENTS.md', link)
        step = self.step('rq2_content_quality')
        before = runner.step_fingerprint(step)
        self.assertTrue(before)
        steps = [step, self.step('reproduction_check')]
        for rel in step.produces:
            output = self.data / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('completed output')
        state = {'completed': {item.key: runner.step_fingerprint(item) for item in steps}}
        self.assertEqual(runner.completed_steps(steps, state), {item.key for item in steps})
        link.unlink()
        os.symlink('notes/OTHER.md', link)
        self.assertNotEqual(runner.step_fingerprint(step), before)

    def test_a_file_that_an_agentic_workflow_imports_invalidates_the_content_quality(self):
        """Whether a listed empty file counts with 0 lines or stops the script depends on what
        the repository's GitHub Agentic Workflows import, and an imported file can import
        another file. A file that a workflow imports is therefore fingerprinted, although no
        CSV lists it."""
        (self.data / '../collection/output/settings.csv').write_text(
            'repo_name,settings\no/r,.github/workflows/triage.md\n')
        repository = self.data / '../collection/data/validated/o§r'
        (repository / '.github' / 'workflows' / 'shared').mkdir(parents=True)
        (repository / '.github' / 'workflows' / 'triage.md').write_text(
            '---\non:\n  issues:\nimports:\n  - shared/tools.md\n---\nTriage the issue.\n')
        imported = repository / '.github' / 'workflows' / 'shared' / 'tools.md'
        imported.write_text('---\ntools:\n  github:\n---\n')
        self.assert_invalidated(self.step('rq2_content_quality'), imported)

    def test_the_planning_documents_of_a_listed_anchor_invalidate_the_content_quality(self):
        """A listed empty `.cursor/commands/van.md` counts with 0 lines while
        cursor-memory-bank's tasks are beside it, because the collected file cleanup keeps it as
        their anchor. Without the tasks, the file stops the script, and the tasks are
        fingerprinted although no CSV lists them."""
        (self.data / '../collection/output/commands.csv').write_text(
            'repo_name,command\no/r,.cursor/commands/van.md\n')
        repository = self.data / '../collection/data/validated/o§r'
        (repository / '.cursor' / 'commands').mkdir(parents=True)
        (repository / '.cursor' / 'commands' / 'van.md').write_text('')
        (repository / 'memory-bank').mkdir()
        tasks = repository / 'memory-bank' / 'tasks.md'
        tasks.write_text('# Tasks\n')
        step = self.step('rq2_content_quality')
        before = runner.step_fingerprint(step)
        tasks.unlink()
        after = runner.step_fingerprint(step)
        self.assertTrue(after)
        self.assertNotEqual(after, before)


if __name__ == "__main__":
    unittest.main()
