"""Test repository size queries, nearest-rank percentiles and missing inputs.

Requirements: Python 3.14 (stdlib only).
Run with: python3 -m unittest tests.test_tools_measure -v
"""

import contextlib
import csv
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests import _ambient_env
from tests import _logged
from agentcontext.tools import measure as mcs
from agentcontext import STAGE_DIR

_ENV_STORE: dict = {}


def setUpModule():
    _ambient_env.take(_ENV_STORE)


def tearDownModule():
    _ambient_env.restore(_ENV_STORE)












class TestThePercentile(unittest.TestCase):
    """Nearest rank: the smallest value with at least p percent of the list at or below it.
    `round(x + 0.5)`, which the comment in `percentile` rules out, would return a rank one too
    high wherever p/100 times the count is an odd integer."""

    def test_the_median_of_an_even_list_is_the_lower_middle(self):
        self.assertEqual(mcs.percentile([1, 2, 3, 4, 5, 6], 50), 3)
        self.assertEqual(mcs.percentile([10, 20], 50), 10)

    def test_the_ninetieth_of_ten_is_the_ninth(self):
        self.assertEqual(mcs.percentile(list(range(1, 11)), 90), 9)

    def test_a_fraction_of_a_rank_rounds_up(self):
        self.assertEqual(mcs.percentile(list(range(1, 11)), 99), 10)
        self.assertEqual(mcs.percentile(list(range(1, 11)), 55), 6)

    def test_the_rank_is_exact_where_the_float_is_not(self):
        """In floats, 99.9 / 100 * 1000 is 999.0000000000001, and its ceiling is a rank one too
        high."""
        self.assertEqual(mcs.percentile(list(range(1, 1001)), 99.9), 999)

    def test_the_ends(self):
        self.assertEqual(mcs.percentile([4, 7, 9], 100), 9)
        self.assertEqual(mcs.percentile([4, 7, 9], 0), 4)

    def test_an_empty_list_has_none(self):
        self.assertIsNone(mcs.percentile([], 50))


class _Client:
    """A fake client with a fixed response per repository, in the form that `GitHubClient.get`
    returns."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked = []

    def get(self, path):
        self.asked.append(path)
        return self.answers[path.removeprefix("/repos/")]


class TestFetchSize(unittest.TestCase):

    def test_a_repository_s_size_is_its_payload_s(self):
        client = _Client({"o/r": (200, {"size": 2048, "name": "r"})})
        self.assertEqual(mcs.fetch_size(client, "o/r"), {"repo_name": "o/r", "size_kb": 2048})
        self.assertEqual(client.asked, ["/repos/o/r"])

    def test_anything_but_a_200_with_an_object_is_blank(self):
        # A size in a response other than a 200 is no measurement. A check of the payload alone
        # would accept the 403 response with a size.
        for answer in ((404, {"message": "Not Found"}), (401, None), (200, [1, 2]),
                       (403, {"size": 2048})):
            with self.subTest(answer=answer):
                self.assertEqual(mcs.fetch_size(_Client({"o/r": answer}), "o/r"),
                                 {"repo_name": "o/r", "size_kb": ""})


class TestMainSizes(unittest.TestCase):
    """`measure sizes` over the fake client of this module: ten repositories of 1 to 10 GiB and a
    repository that GitHub no longer serves."""

    GIB_KB = 1024 * 1024

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output = self.tmp / "repo-sizes.csv"
        self.answers = {f"o/r{k:02d}": (200, {"size": k * self.GIB_KB}) for k in range(1, 11)}
        self.answers["o/gone"] = (404, {"message": "Not Found"})

    def _run(self, tokens=("t",)) -> tuple:
        rows = [{"repo_name": name} for name in sorted(self.answers, reverse=True)]
        client = _Client(self.answers)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mcs.snapshot, "collect_tokens", return_value=list(tokens)), \
                mock.patch.object(mcs.snapshot, "load_rows", return_value=rows), \
                mock.patch.object(mcs.snapshot, "in_scope", side_effect=lambda r, _d: r), \
                mock.patch.object(mcs.snapshot, "GitHubClient", return_value=client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mcs.main_sizes(["-o", str(self.output), "-w", "2"])
        return (code, *_logged.split(out.getvalue() + err.getvalue()))

    def test_a_measurement_that_cannot_be_written_leaves_the_previous_measurement(self):
        """A full disk leaves the previous CSV whole rather than truncated."""
        import errno
        self.output.write_text("the previous measurement\n", encoding="utf-8")
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch("agentcontext.caches.os.replace", side_effect=full):
            code, _, _ = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "the previous measurement\n")

    def test_every_size_is_written_in_name_order(self):
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        with open(self.output, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows, [{"repo_name": "o/gone", "size_kb": ""}]
                         + [{"repo_name": f"o/r{k:02d}", "size_kb": str(k * self.GIB_KB)}
                            for k in range(1, 11)])

    def test_the_csv_ends_each_record_with_lf(self):
        """The CSV ends each record with LF, as every CSV of the pipeline does, rather than with
        the CRLF that the csv module writes by default."""
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn(b"\r\n", self.output.read_bytes())
        with open(self.output, newline="", encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), len(self.answers))

    def test_the_summary_is_the_nearest_rank_percentiles_and_the_largest(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Read the sizes of 10 of 11 repositories (1 unavailable)", out)
        for p, gib in ((50, 5), (90, 9), (99, 10), (99.9, 10), (100, 10)):
            with self.subTest(p=p):
                self.assertIn(f"{p:>11}%  {gib:>9.2f} GiB", out)
        largest = [line.split()[-1] for line in out.splitlines() if line.endswith(tuple(
            f"o/r{k:02d}" for k in range(1, 11)))]
        self.assertEqual(largest, [f"o/r{k:02d}" for k in range(10, 0, -1)])
        self.assertIn("6 repositories (60.000%) are larger than 4 GiB", out)

    def test_no_github_access_token_is_exit_1_and_queries_nothing(self):
        code, _, err = self._run(tokens=())
        self.assertEqual(code, 1)
        self.assertIn("No GitHub access token found", err)
        self.assertFalse(self.output.exists())

    def test_no_size_read_is_exit_1_with_the_csv_written(self):
        """With a revoked GitHub access token, GitHub returns 401 for every repository. A summary
        without a size then exits 1 rather than raising a TypeError on the first percentile."""
        self.answers = {name: (401, {"message": "Bad credentials"}) for name in self.answers}
        code, _, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("GitHub returned no size, and no summary was logged", err)
        self.assertEqual(len(self.output.read_text(encoding="utf-8").splitlines()), 12)








if __name__ == "__main__":
    unittest.main()
