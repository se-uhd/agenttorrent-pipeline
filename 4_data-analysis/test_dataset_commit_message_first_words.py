"""
Tests for dataset/commit_message_first_words.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. _parse_args is
    tested on its own. The word extraction is inline in the function, and the tests run the
    function over a commit CSV built in a temporary directory. A recording wrapper around
    `pandas.read_csv` shows how many rows and which columns the script holds at a time. Pyplot's `subplots` is
    replaced by a recording stand-in from which the words, their counts and the title can be
    read back. The real pyplot is used once to check the PDF written at the given path.
  - The fixture's archive under testdata/output is read through _shared.read_commits_chunks,
    which needs 7zz. The cases over the fixture skip without 7zz, and their top words are
    computed from the fixture by hand.

Requirements: Python 3.14 (pandas, matplotlib and seaborn, and 7zz on PATH for the fixture cases)
Run with:  python3 -m unittest test_dataset_commit_message_first_words -v
"""

import contextlib
import csv
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentcontext import SEVEN_ZIP, logs

HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE / "dataset"
SCRIPT = SCRIPT_DIR / "commit_message_first_words.py"
FIXTURE = HERE / "testdata" / "output"
for _p in (HERE, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
os.environ.setdefault("MPLBACKEND", "Agg")


def _has(*names) -> bool:
    for name in names:
        if importlib.util.find_spec(name) is None:
            return False
    return True


DEPS = _has("pandas", "matplotlib", "seaborn")
HAS_7Z = shutil.which(SEVEN_ZIP) is not None


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("commit_message_first_words_under_test", SCRIPT) if DEPS else None

COLUMNS = ["repo_name", "commit_timestamp", "branch", "commit_sha", "github_link",
           "commit_message", "ai_tool", "matched_heuristics", "attributions"]


def _write_commits(path: Path, messages: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for message in messages:
            writer.writerow(["o/a", "2025-01-01T00:00:00Z", "main", "abc", "https://x", message, "Claude", "r"])


class _Recording:

    def __enter__(self):
        self.fig, self.ax = mock.MagicMock(name="fig"), mock.MagicMock(name="ax")
        self.patchers = [
            mock.patch("matplotlib.pyplot.subplots", return_value=(self.fig, self.ax)),
            mock.patch("matplotlib.pyplot.close"),
        ]
        for p in self.patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patchers:
            p.stop()
        return False

    @property
    def title(self) -> str:
        return self.ax.set_title.call_args.args[0]

    @property
    def bars(self) -> dict:
        words, counts = self.ax.barh.call_args.args[:2]
        return {str(w): int(c) for w, c in zip(words, counts)}


@unittest.skipUnless(DEPS, "pandas, matplotlib and seaborn are needed")
class TestParseArgs(unittest.TestCase):

    def test_the_defaults(self):
        args = mod._parse_args([])
        self.assertEqual(args.data_dir, str(mod.DATA_DIR))
        self.assertEqual(args.output_path, str(mod.OUTPUT_DIR / "commit_message_first_words.pdf"))
        self.assertEqual(args.top_n, 12)
        self.assertFalse(args.show)

    def test_every_flag(self):
        args = mod._parse_args(["--data-dir", "/d", "--output-path", "/o/x.pdf", "--top-n", "3", "--show"])
        self.assertEqual((args.data_dir, args.output_path, args.top_n, args.show), ("/d", "/o/x.pdf", 3, True))

    def test_an_abbreviated_flag_is_refused(self):
        """The script takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with self.assertRaises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()):
            mod._parse_args(["--top", "3"])
        self.assertEqual(stop.exception.code, 2)


@unittest.skipUnless(DEPS, "pandas, matplotlib and seaborn are needed")
class TestTheWords(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run(self, messages: list, **kwargs) -> _Recording:
        _write_commits(self.tmp / "all_ai_commits.csv", messages)
        with _Recording() as rec, contextlib.redirect_stdout(io.StringIO()):
            mod.commit_message_first_words(self.tmp, output_path=self.tmp / "x.pdf", **kwargs)
        return rec

    def test_the_first_word_lower_cased_with_punctuation_removed(self):
        rec = self._run(["Fix: the bug", "fix typo", "WIP wip", "wip x", "Add-on support", "feat(api): route"])
        self.assertEqual(rec.bars, {"fix": 2, "wip": 2, "addon": 1, "featapi": 1})
        self.assertIn("(n = 6 commits)", rec.title)

    def test_a_message_with_no_word_counts_as_a_commit_and_not_as_a_word(self):
        rec = self._run(["fix a", "   ", "...", "fix b"])
        self.assertEqual(rec.bars, {"fix": 2})
        self.assertIn("(n = 4 commits)", rec.title)

    def test_a_row_without_a_message_is_dropped_before_the_count(self):
        rec = self._run(["fix a", "", "fix b"])
        self.assertIn("(n = 2 commits)", rec.title)

    def test_top_n_keeps_the_most_frequent_words(self):
        rec = self._run(["a x", "a y", "b x", "c x", "c y", "c z"], top_n=2)
        self.assertEqual(rec.bars, {"c": 3, "a": 2})

    def test_a_tie_is_ranked_by_the_word_whatever_the_row_order(self):
        """Tied words are ranked by the word, and the row order of the commit CSV does not
        decide which of two equally frequent words is in the top N."""
        messages = ["beta x", "beta y", "alpha x", "alpha y", "gamma", "gamma", "gamma"]
        self.assertEqual(self._run(messages, top_n=2).bars, {"gamma": 3, "alpha": 2})
        self.assertEqual(self._run(list(reversed(messages)), top_n=2).bars, {"gamma": 3, "alpha": 2})
        # Bars are drawn bottom to top, and `beta`, the lower-ranked word of the tied pair, is
        # below `alpha`.
        self.assertEqual(list(self._run(messages, top_n=3).bars), ["beta", "alpha", "gamma"])

    def test_top_n_below_one_keeps_one_word(self):
        rec = self._run(["a x", "b x"], top_n=0)
        self.assertEqual(len(rec.bars), 1)

    def test_the_bars_are_in_ascending_order(self):
        rec = self._run(["a x", "a y", "b x", "c x", "c y", "c z"])
        self.assertEqual(list(rec.bars.values()), sorted(rec.bars.values()))

    def test_the_messages_are_held_one_chunk_at_a_time(self):
        """The whole commit CSV does not fit into the memory of a GitHub runner once pandas
        holds it. The script reads the message column alone, one chunk of rows at a time, and
        counts a word across the chunks as over the whole file."""
        import pandas as pd
        import _shared
        held = []
        read_csv = pd.read_csv

        def recording(*args, **kwargs):
            result = read_csv(*args, **kwargs)
            if isinstance(result, pd.DataFrame):
                held.append(result.shape + (list(result.columns),))
                return result

            def chunks():
                for chunk in result:
                    held.append(chunk.shape + (list(chunk.columns),))
                    yield chunk
            return chunks()

        with mock.patch.object(_shared, "COMMIT_CHUNK_ROWS", 2), \
                mock.patch("pandas.read_csv", side_effect=recording):
            rec = self._run(["fix a", "add b", "fix c", "add d", "fix e", ""])
        self.assertEqual(rec.bars, {"fix": 3, "add": 2})
        self.assertIn("(n = 5 commits)", rec.title)
        self.assertTrue(held)
        self.assertEqual({tuple(columns) for _, _, columns in held}, {("commit_message",)})
        self.assertLessEqual(max(rows for rows, _, _ in held), 2)

    def test_the_figure_is_written_at_the_given_path(self):
        _write_commits(self.tmp / "all_ai_commits.csv", ["fix a", "add b"])
        target = self.tmp / "figures" / "words.pdf"
        target.parent.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            mod.commit_message_first_words(self.tmp, output_path=target)
        self.assertTrue(target.read_bytes().startswith(b"%PDF"))


@unittest.skipUnless(DEPS and HAS_7Z, "pandas, matplotlib, seaborn and 7zz are needed")
class TestOverTheFixture(unittest.TestCase):
    """The fixture's all_ai_commits.csv, counted by hand, has 6,897 rows, each with a message.
    `fix` is the first word of 706 of them, and `add` of 520."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.before = {p.name for p in SCRIPT_DIR.iterdir()}

    def test_the_top_words(self):
        with _Recording() as rec, contextlib.redirect_stdout(io.StringIO()):
            mod.commit_message_first_words(FIXTURE, output_path=self.tmp / "x.pdf")
        self.assertIn("(n = 6,897 commits)", rec.title)
        self.assertEqual(len(rec.bars), 12)
        self.assertEqual(rec.bars["fix"], 706)
        self.assertEqual(rec.bars["add"], 520)
        self.assertEqual(max(rec.bars, key=rec.bars.get), "fix")

    def test_main_returns_0_and_writes_the_figure(self):
        """main honors --top-n, beyond returning 0 and writing a PDF. Over the fixture, the
        fifth word is `docs`, the first word of 118 commits, and the sixth `feat` of 112."""
        argv = ["--data-dir", str(FIXTURE), "--output-path", str(self.tmp / "unused.pdf"),
                "--top-n", "5"]
        with _Recording() as rec, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(argv), 0)
        self.assertEqual(list(rec.bars.items()),
                         [("docs", 118), ("chore", 207), ("update", 219), ("add", 520), ("fix", 706)])
        target = self.tmp / "words.pdf"
        with contextlib.redirect_stdout(io.StringIO()):
            code = mod.main(["--data-dir", str(FIXTURE), "--output-path", str(target), "--top-n", "5"])
        self.assertEqual(code, 0)
        self.assertTrue(target.read_bytes().startswith(b"%PDF"))

    def test_the_environment_variables_route_a_run(self):
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(self.tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(SCRIPT_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        pdf = self.tmp / "dataset" / "commit_message_first_words.pdf"
        self.assertIn(f"Wrote {pdf}", logs.messages(result.stderr))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)
        self.assertFalse((FIXTURE / "all_ai_commits.csv").exists(), "the archive was extracted beside itself")


if __name__ == "__main__":
    unittest.main()
