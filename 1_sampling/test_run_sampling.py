"""
Tests for run_sampling.py

Strategy:
  - The runner is a list of steps for agentcontext/runner.py, which has its own suite. This
    suite tests the list and what it declares about the scripts, and runs the list over steps
    that stand in for the scripts.
  - The stand-ins are short Python snippets that are written as scripts into a temporary
    directory and run with the real list's columns. No test reads the real cache or the real
    configs or reaches the network.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_run_sampling -v
"""

import contextlib
import dataclasses
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
from agentcontext import REPO_ROOT, logs

sys.path.insert(0, str(Path(__file__).resolve().parent))
rs = importlib.import_module("run_sampling")
ghs_cache = importlib.import_module("ghs_cache")
runner = rs.runner
HERE = Path(__file__).resolve().parent
KEYS = ["ghs_download", "github_query", "retrieval_comparison", "repository_filtering"]


def snippet(code: str) -> list:
    return ["-c", code]


class TestStepList(unittest.TestCase):

    def test_four_steps_in_pipeline_order_with_their_scripts_here_and_their_data_apart(self):
        self.assertEqual([s.key for s in rs.STEPS], KEYS)
        self.assertTrue(all(s.root == "sampling" and s.code == "1_sampling" for s in rs.STEPS))
        self.assertEqual({s.stage for s in rs.STEPS}, {"sampling"})
        for step in rs.STEPS:
            with self.subTest(step=step.key):
                self.assertEqual(runner.code_dir(step), HERE)
                self.assertEqual(runner.data_dir(step), rs.SAMPLING_DIR)
                self.assertTrue((runner.code_dir(step) / step.args[0]).is_file())
                self.assertIn(step.key, logs.STEP_NAMES)

    def test_the_download_is_irreproducible_recorded_by_the_pointer_and_reports(self):
        download = rs.STEPS[0]
        self.assertTrue(download.irreproducible)
        self.assertEqual(download.completion, runner.COMPLETION_OUTPUT)
        self.assertEqual(download.produces, (rs.POINTER,))
        self.assertEqual(download.report, "ghs_retrieval_report.json")

    def test_no_step_takes_an_argument(self):
        """A step runs in its data directory without a path on its command line, because each
        script defaults to its configuration beside it and to its data under the data root."""
        for step in rs.STEPS:
            self.assertEqual(step.args[1:], [])

    def test_the_steps_after_the_download_are_recorded_by_the_retrieval_they_read(self):
        """A fallback writes the same bytes into the pointer again, and no step after the
        download runs again. A new retrieval changes the bytes, and every step runs again. Each
        step is also recorded by the rows and GitHub's responses that it reads, and the
        comparison by the retrieval before it, rather than by an edge to the query. A query that
        finds its responses in place therefore redoes no step after it."""
        labels = {"github_query": [rs.POINTED_ROWS],
                  "retrieval_comparison": [rs.POINTED_ROWS, rs.PREVIOUS_ROWS],
                  "repository_filtering": [rs.POINTED_ROWS]}
        for step in rs.STEPS[1:]:
            with self.subTest(step=step.key):
                self.assertEqual(step.completion, runner.COMPLETION_INPUTS)
                self.assertEqual(step.inputs[:1], (rs.POINTER,))
                self.assertEqual(step.code_inputs[:1], (rs.CACHE_MODULE,))
                self.assertEqual(step.consumes, ())
                self.assertEqual([label for label, _ in step.inputs_of(step)], labels[step.key])

    def test_the_github_query_needs_one_of_the_two_github_access_token_variables(self):
        self.assertEqual(rs.STEPS[1].env, ("GITHUB_TOKENS|GITHUB_TOKEN",))

    def test_what_each_step_reads_is_on_disk_or_written_before_it(self):
        for step in rs.STEPS:
            for rel in step.reads:
                with self.subTest(step=step.key, path=rel):
                    self.assertTrue(rel == rs.POINTER or (runner.data_dir(step) / rel).is_file())


class TestList(unittest.TestCase):

    def test_list_prints_the_four_steps_under_the_stage_and_their_root(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(rs.main(["--list"]), 0)
        lines = logs.messages(out.getvalue()).splitlines()
        self.assertEqual(lines[0], "The stage sampling runs these steps in order.")
        self.assertEqual([l.split()[0] for l in lines[1:]], KEYS)
        self.assertTrue(all(" 1_sampling/" in l and l.endswith("(data directory sampling).")
                            for l in lines[1:]))

    def test_the_runner_lists_under_a_bare_interpreter(self):
        """The runner imports nothing outside the standard library and agentcontext, so it lists
        its steps where no stage could run: under an interpreter that imports no site packages
        (`-S`), with the checkout of this suite as the only entry of PYTHONPATH. The same child
        runs in CI, before the install of the dependency lock, and in a .venv that holds it."""
        result = subprocess.run([sys.executable, "-S", str(HERE / "run_sampling.py"), "--list"],
                                capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  ghs_download runs ", logs.messages(result.stderr))


# The download stand-in writes the pointer with the retrieval id in RETRIEVAL, and a report.
DOWNLOAD = ("import json, os; rid = os.environ.get('RETRIEVAL', 'r1'); "
            "open('ghs_retrieval.json', 'w').write(json.dumps({'retrieval_id': rid})); "
            "json.dump({'outcome': os.environ.get('OUTCOME', 'retrieved'), 'retrieval_id': rid}, "
            "open('ghs_retrieval_report.json', 'w')); "
            "open('downloads.log', 'a').write(rid + '\\n')")


def _counting(name, output=None):
    """A stand-in that appends its name to runs.log and writes `output` when given."""
    code = f"open('runs.log', 'a').write('{name}\\n')"
    if output:
        code += f"; open('{output}', 'w').write(open('ghs_retrieval.json').read())"
    return snippet(code)


class TestARun(unittest.TestCase):
    """Stand-ins with the real list's columns, in a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "ghs_cache.py").write_text("# the cache module\n")
        root = str(self.tmp)
        real = {step.key: step for step in rs.STEPS}

        def stand_in(key, args, **changes):
            # A script file rather than `-c`, because the fingerprint of a step recorded by its
            # inputs includes the hash of its script. With `-c`, there is no script, and the step
            # would never count as complete. The stand-ins write no cache of retrievals, and no
            # file in the cache records them. `TestWhatTheStepsAfterTheDownloadRead` writes a
            # cache.
            script = self.tmp / f"{key}.py"
            script.write_text(args[1] + "\n")
            changes.setdefault("inputs_of", None)
            return dataclasses.replace(real[key], interpreter=sys.executable, args=[script.name],
                                       root=root, code=root, imports=(), env=(), reads=(),
                                       **changes)

        self.steps = [
            stand_in("ghs_download", snippet(DOWNLOAD)),
            stand_in("github_query", _counting("query")),
            stand_in("retrieval_comparison", _counting("comparison", "ghs_changes.json"),
                     produces=("ghs_changes.json",)),
            stand_in("repository_filtering", _counting("filtering", "ghs_filtered.json"),
                     produces=("ghs_filtered.json",), inputs=(rs.POINTER,),
                     code_inputs=(rs.CACHE_MODULE,)),
        ]
        self.saved = (rs.STEPS, rs.SAMPLING_DIR, rs.MIN_FREE_DISK_GB, dict(os.environ))
        rs.STEPS, rs.SAMPLING_DIR, rs.MIN_FREE_DISK_GB = self.steps, self.tmp, 0

    def tearDown(self):
        rs.STEPS, rs.SAMPLING_DIR, rs.MIN_FREE_DISK_GB, environ = self.saved
        os.environ.clear()
        os.environ.update(environ)

    def _run(self, *argv) -> tuple:
        """Run the sampling runner with `argv` and return the exit code and the runner's log
        messages on stderr."""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = rs.main(list(argv))
        return code, err.getvalue()

    def _state(self) -> dict:
        return json.loads((self.tmp / "run_logs" / "state.json").read_text())

    def _runs(self) -> list:
        path = self.tmp / "runs.log"
        return path.read_text().split() if path.exists() else []

    def test_a_run_records_every_step_and_the_downloads_report(self):
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertEqual(set(self._state()["completed"]), set(KEYS))
        download = self._state()["steps"][0]
        self.assertEqual(download["report"], {"outcome": "retrieved", "retrieval_id": "r1"})

    def test_a_retrieval_already_named_is_adopted_rather_than_taken(self):
        (self.tmp / "ghs_retrieval.json").write_text('{"retrieval_id": "r0"}')
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("INFO runner: ghs_download is irreproducible, and its output is "
                      "on disk", out)
        self.assertFalse((self.tmp / "downloads.log").exists())

    def test_a_new_retrieval_redoes_every_step_after_the_download(self):
        self._run()
        os.environ["RETRIEVAL"] = "r2"
        code, out = self._run("--from", "ghs_download")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._runs(), ["query", "comparison", "filtering"] * 2)

    def test_a_fallback_to_the_same_retrieval_redoes_nothing_after_the_download(self):
        self._run()
        os.environ["OUTCOME"] = "fallback"
        code, out = self._run("--from", "ghs_download")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._runs(), ["query", "comparison", "filtering"])
        self.assertEqual(self._state()["steps"][0]["report"]["outcome"], "fallback")

    def test_a_second_run_skips_what_is_recorded(self):
        self._run()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("skipped, because it is already complete"), 4)

    def test_mark_complete_refuses_a_step_argument_that_it_would_ignore(self):
        """The step arguments are merged into the steps before `run`, and `run` cannot see them.
        Without the rejection, `--mark-complete` with `--step-arg` would record the download
        from the pointer on disk and exit 0."""
        (self.tmp / "ghs_retrieval.json").write_text('{"retrieval_id": "r0"}')
        code, out = self._run("--mark-complete", "ghs_download",
                              "--step-arg", "ghs_download:--end-date 2026-03-16")
        self.assertEqual(code, 1, out)
        self.assertIn("cannot be combined with --step-arg", out)
        self.assertFalse((self.tmp / "run_logs" / "state.json").exists())
        code, out = self._run("--mark-complete", "ghs_download")
        self.assertEqual(code, 0, out)
        self.assertIn("ghs_download", self._state()["completed"])

    def test_a_step_argument_reaches_the_step(self):
        (self.tmp / "repository_filtering.py").write_text(
            "import sys; open('ghs_filtered.json', 'w').write(sys.argv[-1])\n")
        code, _ = self._run("--step-arg", "repository_filtering:--top-languages 5")
        self.assertEqual(code, 0)
        self.assertEqual((self.tmp / "ghs_filtered.json").read_text(), "5")

    def test_the_filters_top_languages_reaches_the_download_too(self):
        """Without a list of the languages that the previous sampling frame kept, the download
        decides by the languages that the filter keeps. The filter's `--top-languages`
        therefore reaches the download in each form that the filter's parser reads, and the
        last value counts. A value that the filter cannot parse does not reach the download,
        and neither does an abbreviation, which the filter refuses as a usage error, or another
        argument of the filter."""
        (self.tmp / "ghs_download.py").write_text(
            f"import sys\n{DOWNLOAD}\n"
            f"open('download_args.json', 'w').write(json.dumps(sys.argv[1:]))\n")
        for words, passed in (("--top-languages 5", ["--top-languages", "5"]),
                              ("--top-languages 4 --commits-min 500 --top-languages=7",
                               ["--top-languages", "7"]),
                              ("--top 3", []),
                              ("--commits-min 500", []),
                              ("--top-languages many", [])):
            with self.subTest(words=words):
                code, out = self._run("--from", "ghs_download",
                                      "--step-arg", f"repository_filtering:{words}",
                                      "--step-arg", "ghs_download:--accept-smaller")
                self.assertEqual(code, 0, out)
                self.assertEqual(json.loads((self.tmp / "download_args.json").read_text()),
                                 ["--accept-smaller", *passed])


RID, OLDER = "2026-10-05T120000Z", "2026-03-16T180701Z"


def _retrieval(cache: Path, rid: str, names: list) -> None:
    """Write a retrieval into the cache: GHS's rows and their index row."""
    _, sha = ghs_cache.write_rows(cache, rid, "ghs", [{"id": i, "name": n}
                                                      for i, n in enumerate(names)])
    ghs_cache.put_index_row({"retrieval_id": rid, "outcome": ghs_cache.RETRIEVED,
                             "ghs_file_sha256": sha}, cache)


def _responses(cache: Path, rid: str, status: str) -> None:
    """Write GitHub's responses for a retrieval as the query writes them: the file, and its
    digest in the retrieval's index row."""
    _, sha = ghs_cache.write_rows(cache, rid, "github", [{"ghsName": "o/r", "status": status}])
    ghs_cache.put_index_row({"retrieval_id": rid, "github_file_sha256": sha}, cache)


class TestWhatTheStepsAfterTheDownloadRead(unittest.TestCase):
    """The query writes its responses into the cache and leaves the pointer as it was. If the
    steps were recorded by the pointer alone, the runner would skip the comparison and the
    filter after the query wrote new responses. It would also skip the comparison after a new
    earlier retrieval, and the query when its responses were gone. The tests use a real cache
    of retrievals under a temporary data root, with `sampling/` beside `collection/`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sampling = self.tmp / "sampling"
        self.cache = self.sampling / rs.CACHE_DIR
        _retrieval(self.cache, RID, ["o/r"])
        _responses(self.cache, RID, "found")
        (self.sampling / rs.POINTER).write_text(json.dumps({"retrieval_id": RID}))
        self.steps = {step.key: dataclasses.replace(step, root=str(self.sampling))
                      for step in rs.STEPS}

    def _fingerprints(self) -> dict:
        return {key: runner.step_fingerprint(self.steps[key])
                for key in ("github_query", "retrieval_comparison", "repository_filtering")}

    def test_responses_requested_again_redo_the_comparison_and_the_filter(self):
        before = self._fingerprints()
        self.assertTrue(all(before.values()), before)
        _responses(self.cache, RID, "not_found")
        after = self._fingerprints()
        for key in before:
            with self.subTest(step=key):
                self.assertNotEqual(after[key], before[key])

    def test_missing_responses_run_the_query_again(self):
        self.assertTrue(runner.step_fingerprint(self.steps["github_query"]))
        ghs_cache.rows_path(self.cache, RID, "github").unlink()
        self.assertEqual(runner.step_fingerprint(self.steps["github_query"]), "")

    def test_an_earlier_retrieval_redoes_the_comparison_alone_and_a_collection_none(self):
        """The comparison compares each new retrieval with the collection before it. A new
        collection leaves the comparison's fingerprint as it was, because the cloning step
        writes the collected list after the comparison ran."""
        before = self._fingerprints()
        collected = self.tmp / "collection" / "data" / "collected" / "repos.csv"
        collected.parent.mkdir(parents=True)
        collected.write_text("repo_name,branch,head_sha\no/r,main,abc\n")
        self.assertEqual(self._fingerprints(), before)
        collected.write_text("repo_name,branch,head_sha\no/r,main,def\n")
        self.assertEqual(self._fingerprints(), before)
        _retrieval(self.cache, OLDER, ["o/r", "o/s"])
        after_older = self._fingerprints()
        self.assertNotEqual(after_older["retrieval_comparison"], before["retrieval_comparison"])
        for key in ("github_query", "repository_filtering"):
            with self.subTest(step=key):
                self.assertEqual(after_older[key], before[key])

    def test_a_query_that_runs_again_redoes_the_comparison_and_the_filter_in_the_same_run(self):
        """Through the runner, the query runs again with `--from` and writes other responses.
        The comparison and the filter, recorded before, run again in the same run. A query that
        writes the same responses redoes no step after it."""
        log = self.sampling / "runs.log"

        def stand_in(key, code, **changes):
            script = self.sampling / f"{key}.py"
            script.write_text(f"import sys; sys.path.insert(0, {str(HERE)!r})\n{code}\n"
                              f"open('runs.log', 'a').write('{key}\\n')\n")
            return dataclasses.replace(self.steps[key], interpreter=sys.executable,
                                       args=[script.name], code=str(self.sampling), imports=(),
                                       env=(), reads=(), **changes)

        query = ("import os, ghs_cache\n"
                 "_, sha = ghs_cache.write_rows(ghs_cache.Path('ghs_retrievals'), "
                 f"{RID!r}, 'github', [{{'ghsName': 'o/r', 'status': os.environ['RESPONSE_STATUS']}}])\n"
                 f"ghs_cache.put_index_row({{'retrieval_id': {RID!r}, "
                 "'github_file_sha256': sha}, ghs_cache.Path('ghs_retrievals'))")
        steps = [stand_in("github_query", query, code_inputs=()),
                 stand_in("retrieval_comparison", "open('ghs_changes.json', 'w').write('{}')",
                          produces=("ghs_changes.json",), code_inputs=()),
                 stand_in("repository_filtering", "open('ghs_filtered.json', 'w').write('{}')",
                          produces=("ghs_filtered.json",), code_inputs=())]
        saved = (rs.STEPS, rs.SAMPLING_DIR, rs.MIN_FREE_DISK_GB, dict(os.environ))

        def restore():
            rs.STEPS, rs.SAMPLING_DIR, rs.MIN_FREE_DISK_GB, environ = saved
            os.environ.clear()
            os.environ.update(environ)

        self.addCleanup(restore)
        rs.STEPS, rs.SAMPLING_DIR, rs.MIN_FREE_DISK_GB = steps, self.sampling, 0
        os.environ["GITHUB_TOKEN"] = "unused"

        def run(*argv) -> list:
            log.unlink(missing_ok=True)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                self.assertEqual(rs.main(list(argv)), 0, err.getvalue())
            return log.read_text().split() if log.exists() else []

        os.environ["RESPONSE_STATUS"] = "found"
        self.assertEqual(run(), ["github_query", "retrieval_comparison", "repository_filtering"])
        self.assertEqual(run("--from", "github_query"), ["github_query"])
        os.environ["RESPONSE_STATUS"] = "not_found"
        self.assertEqual(run("--from", "github_query"),
                         ["github_query", "retrieval_comparison", "repository_filtering"])


if __name__ == "__main__":
    unittest.main()
