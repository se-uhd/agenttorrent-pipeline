"""
Tests for run_pipeline.py

Strategy:
  - The composed list against the four lists that it joins: every key once, every step's
    directory and script on disk, every dependency on an earlier step, and the edges that
    run_pipeline.py adds where a step reads from another stage's directory.
  - --list in a subprocess, from the repository root and from another directory, to show
    that every step resolves from a clean checkout.
  - One run of main over a stand-in step, to check where main keeps the pipeline execution
    state.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_run_pipeline -v
"""

import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentcontext import logs

sys.path.insert(0, str(Path(__file__).resolve().parent))
rp = importlib.import_module("run_pipeline")
runner = rp.runner
ROOT = Path(__file__).resolve().parent


class TestComposition(unittest.TestCase):

    def setUp(self):
        self.steps = rp.compose()
        self.by_key = {s.key: s for s in self.steps}

    def test_the_four_lists_in_order_with_every_key_once(self):
        keys = [s.key for s in self.steps]
        self.assertEqual(len(keys), len(set(keys)))
        # The engineered project selection appears once, as the last step of the
        # classification, and not again in the list of the collection.
        expected = ([s.key for s in rp.run_sampling.STEPS]
                    + [s.key for s in rp.run_classification.STEPS]
                    + [s.key for s in runner.CORE_STEPS if s.key != 'engineered_project_selection']
                    + [s.key for s in rp.run_analysis.STEPS])
        self.assertEqual(keys, expected)

    def test_the_checks_over_the_commit_history_join_when_requested(self):
        keys = [s.key for s in rp.compose(with_commit_reclassification=True)]
        self.assertIn('commit_attribution_validation', keys)
        self.assertLess(keys.index('commit_attribution_validation'), keys.index('collected_files_archive'))
        self.assertLess(keys.index('unmatched_commits_report'), keys.index('collected_files_archive'))

    def test_five_stages_in_order_each_one_run_of_steps(self):
        """The pipeline has five stages, each a contiguous sequence of steps. A stage with steps
        of another stage between its own steps would appear twice in --list."""
        for reclassify in (False, True):
            with self.subTest(with_commit_reclassification=reclassify):
                groups = [s.stage for s in rp.compose(with_commit_reclassification=reclassify)]
                self.assertNotIn("", groups)
                runs = [g for i, g in enumerate(groups) if i == 0 or groups[i - 1] != g]
                self.assertEqual(runs, ["sampling", "classification", "collection", "publication",
                                        "analysis"])

    def test_commit_reclassification_runs_before_detection_and_invalidates_its_readers(self):
        steps = rp.compose(with_commit_reclassification=True)
        keys = [s.key for s in steps]
        self.assertEqual(keys[keys.index('repository_cloning') + 1], 'commit_reclassification')
        self.assertLess(keys.index('commit_reclassification'), keys.index('detection'))
        reached = runner.invalidated_by(steps, 'commit_reclassification')
        for key in ('detection', 'file_history', 'release', 'unmatched_commits_report',
                    "reproduction_check"):
            self.assertIn(key, reached)
        self.assertNotIn('repository_cloning', reached)
        self.assertNotIn('commit_reclassification', [s.key for s in rp.compose()])

    def test_every_step_has_a_data_directory_and_a_script_on_disk(self):
        """Each step has its data directory under the data root and, unless it runs a module
        of the package, a script under its code directory in the repository."""
        for step in self.steps:
            with self.subTest(step=step.key):
                self.assertTrue(runner.data_dir(step).is_relative_to(runner.DATA_ROOT))
                if not step.args[0].startswith("-"):
                    self.assertTrue(runner.code_dir(step).is_relative_to(runner.REPO_ROOT))
                    self.assertTrue((runner.code_dir(step) / step.args[0]).is_file())

    def test_over_an_empty_data_root_every_step_finds_what_it_reads(self):
        """The container's first Start runs over an empty data root. A file that a step reads but no
        step writes, and that only a checkout tracks, would make the pre-checks refuse every
        first Start. The filter downloads the SPDX license list when the list is absent."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runner, "DATA_ROOT", Path(tmp)), \
                mock.patch.object(runner, "STAGE_DIR", Path(tmp) / "collection"):
            steps = rp.compose(with_commit_reclassification=True)
            self.assertEqual(runner.missing_reads(steps, {s.key for s in steps}), [])

    def test_every_step_has_an_entry_in_step_names(self):
        """Every step has an entry in `logs.STEP_NAMES`, so that its progress messages name it by
        its noun phrase rather than by the completion key that `logs.step_name` falls back to."""
        for step in rp.compose(with_commit_reclassification=True):
            with self.subTest(step=step.key):
                self.assertIn(step.key, logs.STEP_NAMES)

    def test_every_dependency_names_an_earlier_step(self):
        seen = set()
        for step in self.steps:
            for key in step.consumes:
                with self.subTest(step=step.key, consumes=key):
                    self.assertIn(key, seen)
            seen.add(step.key)

    def test_the_edges_between_the_lists(self):
        self.assertEqual(self.by_key["repository_labeling"].consumes, ())
        self.assertEqual(self.by_key['engineered_project_selection'].consumes, ("repository_labeling",))
        self.assertEqual(self.by_key['repository_cloning'].consumes, ())
        for key in ("rq2_config_cooccurrence", "rq3_adoption_timeline", "rq2_tool_cooccurrence"):
            self.assertIn('file_history', self.by_key[key].consumes)
        self.assertIn("rq3_adoption_timeline", self.by_key["reproduction_check"].consumes)
        self.assertIn("repository_labeling", self.by_key["rq2_language_ranking_all"].consumes)
        self.assertIn("repository_filtering", self.by_key["rq2_language_tool_association"].consumes)
        self.assertEqual(self.by_key["rq2_content_quality"].consumes, ('file_history',))

    def test_the_content_quality_step_reads_the_tree_that_the_file_history_step_leaves(self):
        """The content quality step reads the files of the validated tree that the mechanism
        CSVs of the file history step list. It therefore reads the tree after the file history
        step has removed the directories of the repositories that git reports as gone. With an
        edge to the collected file cleanup, the content quality step would read directly what
        the file history step modifies. The check for modified inputs would then rebuild the
        validated tree and run every later step again, at a Start after a run that stopped in
        the analysis stage or after an image that changed only the content quality script."""
        for reclassify in (False, True):
            steps = rp.compose(with_commit_reclassification=reclassify)
            keys = [s.key for s in steps]
            recorded = set(keys)
            stopped = set(keys[keys.index("rq2_content_quality"):])
            edited = set(runner.invalidated_by(steps, "rq2_content_quality"))
            for name, pending in (("stopped", stopped), ("edited", edited)):
                with self.subTest(with_commit_reclassification=reclassify, run=name):
                    self.assertEqual(runner.modified_input_problems(steps, pending, recorded), [])
            with self.subTest(with_commit_reclassification=reclassify):
                # A rebuilt validated tree still reaches the content quality step, through the
                # file history step.
                self.assertIn("rq2_content_quality",
                              runner.invalidated_by(steps, 'collected_file_cleanup'))
                # The check for modified inputs still reports detection over the tree that
                # the file history step pruned.
                self.assertNotEqual(runner.modified_input_problems(
                    steps, set(runner.invalidated_by(steps, 'detection')), recorded), [])

    def test_a_start_after_a_stop_between_the_file_history_step_and_the_archive_resumes(self):
        """A failed check, or a Stop while 7zz packs the archive, leaves every step up to the file
        history step recorded. The archive reads the tree that the file history step pruned, as
        in an uninterrupted run. The next Start therefore resumes at the step that did not
        finish, and it does not rebuild the tree and run every later step again."""
        for reclassify in (False, True):
            steps = rp.compose(with_commit_reclassification=reclassify)
            keys = [s.key for s in steps]
            self.assertIn('collected_file_cleanup',
                          self.by_key['collected_files_archive'].consumes)
            recorded = set(keys[:keys.index('file_history') + 1])
            for stopped_in in ('output_cross_check', 'collected_files_archive'):
                pending = set(keys[keys.index(stopped_in):])
                with self.subTest(with_commit_reclassification=reclassify, stopped_in=stopped_in):
                    self.assertIn('collected_files_archive', pending)
                    self.assertEqual(runner.modified_input_problems(steps, pending, recorded), [])
            with self.subTest(with_commit_reclassification=reclassify, run="--from detection"):
                # The check still reports detection over the pruned tree, and also the archive
                # when the file history step runs again before the archive.
                pending = set(runner.invalidated_by(steps, 'detection')) | {
                    'collected_files_archive'}
                problems = runner.modified_input_problems(steps, pending, set(keys))
                self.assertIn(('detection', 'collected_file_cleanup', 'file_history'), problems)
                self.assertIn(('collected_files_archive', 'collected_file_cleanup',
                               'file_history'), problems)

    def test_the_pre_checks_cover_what_a_monthly_run_reaches_after_the_download(self):
        """A monthly run repeats the download and the cloning step. The query, the filter and the
        labeling read what the download names instead of consuming the download step. They become
        pending only once the run reaches them, and the pre-checks verify them by the
        paths that they read. A new label reaches the cloning step through the list of repositories
        that the engineered project selection writes."""
        for reclassify in (False, True):
            steps = rp.compose(with_commit_reclassification=reclassify)
            with self.subTest(with_commit_reclassification=reclassify):
                monthly = runner.steps_to_redo(steps, ["ghs_download"])
                reached = runner.may_become_pending(steps, monthly)
                for key in ("github_query", "repository_filtering", "repository_labeling",
                            'engineered_project_selection', 'repository_cloning', "release"):
                    self.assertIn(key, reached)
                labels = set(runner.invalidated_by(steps, "repository_labeling"))
                self.assertIn('repository_cloning', runner.may_become_pending(steps, labels))
                self.assertEqual(runner.may_become_pending(
                    steps, set(runner.invalidated_by(steps, 'detection'))) & {
                        'repository_cloning', "repository_labeling", "ghs_download"}, set())

    def test_the_pre_checks_find_the_model_that_the_labeling_step_requests(self):
        """The pre-checks send their request to OpenAI with this model. Without a model, the
        pre-checks could check only that a key is configured."""
        self.assertIn('repository_labeling', runner.LABELING_STEPS)
        self.assertTrue(runner.labeling_model(self.by_key['repository_labeling']))

    def test_editing_the_filter_reaches_the_language_figures_and_not_the_repository_cloning(self):
        """The labeling is recorded by the sampling frame's contents rather than reached
        through an edge (`TestTheLabelingRecord`)."""
        reached = runner.invalidated_by(self.steps, "repository_filtering")
        self.assertIn("rq2_language_tool_association", reached)
        self.assertNotIn("repository_labeling", reached)
        self.assertNotIn('repository_cloning', reached)

    def test_a_new_sampling_frame_or_new_labels_publish_again(self):
        """The release writes a row for each repository in the sampling frame, with its labels,
        and the record of the sampling frame. If the release did not consume the filter and the
        labeling, a changed filter or prompt that kept the sample list would run the language
        figures again and publish nothing new."""
        for producer in ("repository_filtering", "repository_labeling"):
            with self.subTest(producer=producer):
                self.assertIn("release", runner.invalidated_by(self.steps, producer))

    def test_the_release_declares_the_sampling_frame_and_the_labels_it_reads(self):
        from agentcontext.steps import release
        step = self.by_key["release"]
        # Every alternative of every declared input, present or not, because CI has no
        # extracted sampling frame or labels, and `runner.input_path` names only a file that
        # exists.
        declared = {(runner.data_dir(step) / alternative).resolve()
                    for path in step.inputs for alternative in path.split("|")}
        for read in (release.DEFAULT_GHS_JSON, release.DEFAULT_RETRIEVAL_REPORT,
                     release.DEFAULT_ANNOTATED_CSV,
                     release.DEFAULT_ANNOTATED_CSV.with_suffix(".json")):
            with self.subTest(path=read.name):
                self.assertIn(read.resolve(), declared)

    def test_the_own_lists_are_left_as_they_are(self):
        self.assertEqual(rp.run_classification.STEPS[0].consumes, ())
        self.assertEqual(next(s for s in runner.CORE_STEPS if s.key == 'engineered_project_selection').consumes, ())

    def test_a_shared_key_is_refused(self):
        saved = rp.run_sampling.STEPS
        rp.run_sampling.STEPS = saved + [saved[0]]
        try:
            with self.assertRaises(ValueError):
                rp.compose()
        finally:
            rp.run_sampling.STEPS = saved


class TestTheLabelingRecord(unittest.TestCase):
    """The labeling is recorded by the repositories in the sampling frame and by the values of
    its `_metadata` that the labeling reads. The filter runs again after a change to its code,
    its config, the reviewed licenses or the SPDX list. A filter run that writes the same
    repositories must leave the labeling complete, because a new labeling requests the README of
    every repository in the sampling frame again."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, value in (("DATA_ROOT", tmp), ("STAGE_DIR", tmp / "collection")):
            patcher = mock.patch.object(runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.steps = rp.compose()
        self.labeling = next(s for s in self.steps if s.key == "repository_labeling")
        self.frame = tmp / "sampling" / "ghs_filtered.json"
        from _script_loader import load_script
        self.filter = load_script("4_filter_ghs.py")
        for rel in self.labeling.produces:
            path = tmp / "classification" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("written by the labeling\n")

    def write_frame(self, filtered_at, items, config=None, licenses=None, commits_min=355.0):
        """Write a sampling frame as the filter writes it, at the time `filtered_at`, with the
        filter's config, the values of its license step and the commit minimum of its activity
        step."""
        from datetime import datetime
        row = {"retrieval_id": "2026-10-05T120000Z", "started_at": "2026-10-05T12:00:00Z",
               "end_date": "2026-09-22", "end_date_source": "crawl_date:Python",
               "created_max": "2025-03-22", "committed_min": "2026-03-22",
               "query": '{"contributorsMin": 2}', "ghs_version": "1.17.1",
               "github_started_at": "2026-10-05T12:10:00Z",
               "github_finished_at": "2026-10-05T13:00:00Z", "github_found": "2",
               "github_not_found": "0", "github_unresolved": "0"}
        step = self.filter.FilterStep
        results = [step(5, "licenses", "not one of 1 allowed licenses", 0, len(items),
                        licenses or {"allowed": ["MIT License"], "unreviewed": [],
                                     "spdx_list_version": "3.29.0", "review_sha256": "a" * 64}),
                   step(8, "activity", "commits below 355", 0, len(items),
                        {"commits_min": commits_min, "watchers_min": 6.5})]
        with mock.patch.object(self.filter.ghs_cache, "utc_now",
                               return_value=datetime.fromisoformat(filtered_at)), \
                contextlib.redirect_stderr(io.StringIO()):
            metadata = self.filter.sampling_frame_metadata(
                row, config or self.filter.FilterConfig(), results, len(items), len(items))
            self.filter.save_output(self.frame, metadata, items)
        return runner.step_fingerprint(self.labeling)

    def test_a_filter_run_that_writes_the_same_repositories_keeps_the_labeling_complete(self):
        items = [{"id": 1, "name": "a/b"}, {"id": 2, "name": "c/d"}]
        recorded = self.write_frame("2026-10-05T14:00:00+00:00", items)
        self.assertTrue(recorded)
        self.assertEqual(self.write_frame("2026-10-18T09:30:00+00:00", items), recorded)
        # The filter ran again, and its record no longer matches. The labeling still counts as
        # done.
        state = {"completed": {"repository_filtering": "inputs:an earlier run",
                               "repository_labeling": recorded}}
        with contextlib.redirect_stderr(io.StringIO()):
            done = runner.completed_steps(self.steps, state)
        self.assertNotIn("repository_filtering", done)
        self.assertIn("repository_labeling", done)

    def test_another_config_or_license_review_keeps_the_labeling_complete(self):
        """The filter records its config and the license step's review in `_metadata`, and the
        labeling reads neither of them. A filter run after an edit of the config, a review of a
        license or a new SPDX list leaves the labeling complete when it keeps the same
        repositories."""
        items = [{"id": 1, "name": "a/b"}, {"id": 2, "name": "c/d"}]
        recorded = self.write_frame("2026-10-05T14:00:00+00:00", items)
        config = self.filter.FilterConfig(top_n_languages=12, educational_name_regex="tutorial",
                                          license_report_path="licenses.csv")
        licenses = {"allowed": ["MIT License"], "unreviewed": ["MIT License"],
                    "spdx_list_version": "3.30.0", "review_sha256": "b" * 64}
        self.assertEqual(self.write_frame("2026-10-18T09:30:00+00:00", items, config=config,
                                          licenses=licenses), recorded)

    def test_another_commit_minimum_keeps_the_labeling_complete(self):
        """The labeling reads no commit minimum, because the fork heuristic decides from a fork's
        own authors and its commits in the sampling window whether the fork is independent.
        Another commit minimum of the activity step over the same repositories leaves the labeling
        complete."""
        items = [{"id": 1, "name": "a/b"}]
        recorded = self.write_frame("2026-10-05T14:00:00+00:00", items, commits_min=355.0)
        self.assertEqual(self.write_frame("2026-10-05T14:00:00+00:00", items,
                                          commits_min=412.0), recorded)

    def test_another_sampling_frame_runs_the_labeling_again(self):
        recorded = self.write_frame("2026-10-05T14:00:00+00:00", [{"id": 1, "name": "a/b"}])
        changed = self.write_frame("2026-10-05T14:00:00+00:00", [{"id": 1, "name": "a/B"}])
        self.assertNotEqual(changed, recorded)
        self.frame.unlink()
        self.assertEqual(runner.step_fingerprint(self.labeling), "")


class TestTheComparisonRecord(unittest.TestCase):
    """The comparison of retrievals compares each new retrieval with the collection before it.
    The collected list holds that collection when the comparison runs, and the cloning step
    rewrites the list later in the same run. If the comparison were recorded by the list, it would
    run again at the next Start after every completed run. Because that Start would run a step,
    the entrypoint would then pack another handover of the run."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, value in (("DATA_ROOT", tmp), ("STAGE_DIR", tmp / "collection")):
            patcher = mock.patch.object(runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ghs_cache = rp.run_sampling.ghs_cache
        self.cache = tmp / "sampling" / "ghs_retrievals"
        self.rid = "2026-10-05T120000Z"
        _, ghs_sha = ghs_cache.write_rows(self.cache, self.rid, "ghs", [{"id": 1, "name": "o/r"}])
        _, github_sha = ghs_cache.write_rows(self.cache, self.rid, "github",
                                             [{"ghsName": "o/r", "status": "found"}])
        ghs_cache.put_index_row({"retrieval_id": self.rid, "outcome": ghs_cache.RETRIEVED,
                                 "ghs_file_sha256": ghs_sha, "github_file_sha256": github_sha},
                                self.cache)
        ghs_cache.write_pointer(self.rid, tmp / "sampling" / "ghs_retrieval.json")
        self.steps = rp.compose(with_commit_reclassification=True)
        self.by_key = {s.key: s for s in self.steps}
        self.comparison = self.by_key["retrieval_comparison"]
        for rel in self.comparison.produces:
            (runner.data_dir(self.comparison) / rel).write_text("written by the comparison\n")
        # The list of the collection before this run, where the cloning step writes it.
        self.collected = (runner.data_dir(self.by_key["repository_cloning"])
                          / "data" / "collected" / "repos.csv")
        self.collected.parent.mkdir(parents=True)
        self.collected.write_text("repo_name,branch,head_sha\no/r,main,111\n")

    def _done(self, recorded: str) -> set:
        with contextlib.redirect_stderr(io.StringIO()):
            return runner.completed_steps(self.steps,
                                          {"completed": {"retrieval_comparison": recorded}})

    def test_the_collected_list_that_the_cloning_step_rewrites_keeps_the_comparison_complete(self):
        recorded = runner.step_fingerprint(self.comparison)
        self.assertTrue(recorded)
        self.collected.write_text("repo_name,branch,head_sha\no/r,main,222\n")
        self.assertIn("retrieval_comparison", self._done(recorded))

    def test_a_new_retrieval_runs_the_comparison_again(self):
        """A monthly run takes a new retrieval. The comparison then runs before the cloning step
        and compares the new retrieval with the previous collection."""
        recorded = runner.step_fingerprint(self.comparison)
        ghs_cache = rp.run_sampling.ghs_cache
        rid = "2026-11-02T120000Z"
        _, ghs_sha = ghs_cache.write_rows(self.cache, rid, "ghs", [{"id": 1, "name": "o/r"}])
        _, github_sha = ghs_cache.write_rows(self.cache, rid, "github",
                                             [{"ghsName": "o/r", "status": "found"}])
        ghs_cache.put_index_row({"retrieval_id": rid, "outcome": ghs_cache.RETRIEVED,
                                 "ghs_file_sha256": ghs_sha, "github_file_sha256": github_sha},
                                self.cache)
        ghs_cache.write_pointer(rid, runner.data_dir(self.comparison) / "ghs_retrieval.json")
        self.assertNotIn("retrieval_comparison", self._done(recorded))


class TestList(unittest.TestCase):

    def test_list_names_every_stage_and_step_with_its_directory(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(rp.main(["--list"]), 0)
        lines = logs.messages(out.getvalue()).splitlines()
        self.assertEqual([l for l in lines if not l.startswith(" ")],
                         [f"The stage {stage} runs these steps in order." for stage in
                          ("sampling", "classification", "collection", "publication",
                           "analysis")])
        self.assertEqual([l.split()[0] for l in lines if l.startswith(" ")],
                         [s.key for s in rp.compose()])
        self.assertTrue(any(" 1_sampling/" in l and l.endswith("(data directory sampling).")
                            for l in lines))
        self.assertTrue(any(" 4_data-analysis/" in l and l.endswith("(data directory analysis).")
                            for l in lines))

    def test_the_list_resolves_from_the_root_and_from_elsewhere(self):
        for cwd in (ROOT, Path(tempfile.gettempdir())):
            with self.subTest(cwd=str(cwd)):
                result = subprocess.run([sys.executable, str(ROOT / "run_pipeline.py"), "--list"],
                                        capture_output=True, text=True, cwd=str(cwd),
                                        env={**os.environ, "PYTHONPATH": ""})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("  ghs_download runs ", logs.messages(result.stderr))
                self.assertIn("  reproduction_check runs ", logs.messages(result.stderr))


class TestARun(unittest.TestCase):

    def test_an_abbreviated_option_is_a_usage_error(self):
        """The container's entrypoint recognizes `--from`, `--mark-complete` and `--step-arg` in
        the `command:` of compose.yaml by their full names. It passes an argument for one Start
        until a run has used the argument. If argparse accepted `--fro release` as
        `--from release`, the entrypoint would not recognize the abbreviation as an argument
        for one Start, and the release would run again at every Start.
        `--step repository_cloning:--fresh` would likewise collect the whole sample again in
        every monthly run. The runner of each stage refuses abbreviated options too."""
        import run_analysis
        import run_classification
        import run_sampling
        for words in (["--fro", "release"], ["--mark", "repository_cloning"],
                      ["--step", "repository_cloning:--fresh"]):
            with self.subTest(words=words):
                err = io.StringIO()
                with mock.patch.object(runner, "run", return_value=0), \
                        contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
                    rp.main(words)
                self.assertEqual(stop.exception.code, rp.cli.USAGE_ERROR)
                self.assertIn(f"unrecognized arguments: {' '.join(words)}", err.getvalue())
        for build in (run_sampling.build_parser, run_classification.build_parser,
                      run_analysis.build_parser):
            with self.subTest(runner=build.__module__):
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as stop:
                    build().parse_args(["--fro", "release"])
                self.assertEqual(stop.exception.code, rp.cli.USAGE_ERROR)

    def test_the_free_space_override_reaches_the_cloning_step_and_the_pre_checks(self):
        with mock.patch.object(runner, "run", return_value=0) as run:
            self.assertEqual(rp.main(["--min-free-gb", "12"]), 0)
        self.assertEqual(run.call_args.kwargs["min_free_gb"], 12)
        collect = next(s for s in run.call_args.args[0] if s.key == 'repository_cloning')
        pos = collect.args.index("--min-free-gb")
        self.assertEqual(collect.args[pos + 1], "12")

    def test_the_filters_top_languages_reaches_the_download_too(self):
        """The download decides from the rows of the GHS search result whether the filter would
        keep a language that the end date left out. The filter's number of top languages therefore
        reaches the download, as it does under run_sampling.py."""
        with mock.patch.object(runner, "run", return_value=0) as run:
            self.assertEqual(rp.main(["--step-arg", "repository_filtering:--top-languages 5"]),
                             0)
        steps = {s.key: s for s in run.call_args.args[0]}
        self.assertEqual(steps["ghs_download"].args, ["1_download_ghs.py", "--top-languages", "5"])
        self.assertEqual(steps["repository_filtering"].args,
                         ["4_filter_ghs.py", "--top-languages", "5"])

    def test_mark_complete_refuses_the_flags_that_it_would_ignore(self):
        """`main` applies `--repo-csv` and `--step-arg` to the steps before it calls `run`, and
        `run` cannot detect them. `--mark-complete` beside either flag would therefore record
        the step and exit 0. The test marks a stand-in step that is recorded by its output, with
        that output on disk."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = runner.LOG_DIR
        self.addCleanup(runner.use_log_dir, saved)
        runner.use_log_dir(tmp / "elsewhere")
        (tmp / "snapshot.txt").write_text("taken\n")
        # The list that the step collects. It names no repository, and a collection therefore
        # cannot have left a repository of the list unreached.
        (tmp / "list.csv").write_text("repo_name\n")
        step = runner.Step("repository_cloning", sys.executable,
                           ["-c", "pass", "--repo-csv", "list.csv"], root=str(tmp),
                           produces=("snapshot.txt",), completion=runner.COMPLETION_OUTPUT)
        record = tmp / "run_logs" / "state.json"
        for extra in (["--step-arg", "repository_cloning:--fresh"], ["--repo-csv", "other.csv"]):
            err = io.StringIO()
            with self.subTest(flag=extra[0]), \
                    mock.patch.object(rp, "DATA_ROOT", tmp), \
                    mock.patch.object(rp, "compose",
                                      lambda with_commit_reclassification=False: [step]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(rp.main(["--mark-complete", "repository_cloning", *extra]), 1)
                self.assertFalse(record.exists(), "the step was recorded")
                self.assertIn(f"cannot be combined with {extra[0]}", err.getvalue())
        # The same stand-in is recorded when nothing else is on the command line.
        with mock.patch.object(rp, "DATA_ROOT", tmp), \
                mock.patch.object(rp, "compose", lambda with_commit_reclassification=False: [step]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rp.main(["--mark-complete", "repository_cloning"]), 0)
        self.assertIn("repository_cloning", json.loads(record.read_text())["completed"])

    def test_the_record_is_kept_at_the_data_root(self):
        """run_pipeline.py keeps its pipeline execution state in the data root's `run_logs/`.
        The collection stage's `run_logs/` holds the lock and the state file of that stage's own
        runner. `--list` returns before anything is recorded, and the test therefore runs one
        stand-in step, with `DATA_ROOT` pointed at a temporary directory."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = (runner.LOG_DIR, runner.MIN_FREE_DISK_GB)

        def restore():
            runner.use_log_dir(saved[0])
            runner.MIN_FREE_DISK_GB = saved[1]

        self.addCleanup(restore)
        # Point the runner's log directory away from the checkout before the run. A `main` that
        # does not move the directory then writes there and not into the checkout.
        runner.use_log_dir(tmp / "elsewhere")
        runner.MIN_FREE_DISK_GB = 0
        step = runner.Step("one", sys.executable, ["-c", "pass"], root=str(tmp))
        with (mock.patch.object(rp, "DATA_ROOT", tmp),
              mock.patch.object(rp, "compose", lambda with_commit_reclassification=False: [step]),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(rp.main([]), 0)
        record = tmp / "run_logs" / "state.json"
        self.assertTrue(record.is_file(), "the run was recorded somewhere else")
        state = json.loads(record.read_text())
        self.assertEqual(state["outcome"], "success")
        self.assertEqual(list(state["completed"]), ["one"])


if __name__ == "__main__":
    unittest.main()
