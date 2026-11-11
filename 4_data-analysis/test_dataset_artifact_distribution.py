"""
Tests for dataset/artifact_distribution.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. load_artifact_stats
    is tested over small CSVs in a temporary directory with DATA_DIR replaced, and over the
    fixture under testdata/output. Over the fixture, the configuration artifact and
    repository counts are computed by hand and compared with the flag columns of the
    fixture's repos.csv.
  - main() runs in-process, with the module's pyplot replaced by a recording stand-in from
    which the bar labels and the title can be read. It also runs as a subprocess over the
    fixture with ANALYSIS_OUTPUT_DIR set, which puts the PDF under a temporary root.

Requirements: Python 3.14 (numpy, matplotlib, seaborn)
Run with:  python3 -m unittest test_dataset_artifact_distribution -v
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
from agentcontext import logs

HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE / "dataset"
SCRIPT = SCRIPT_DIR / "artifact_distribution.py"
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


DEPS = _has("numpy", "matplotlib", "seaborn")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("artifact_distribution_under_test", SCRIPT) if DEPS else None

TABLES = ["context-files.csv", "skills.csv", "commands.csv", "subagents.csv", "rules.csv",
          "settings.csv", "hooks.csv", "mcp.csv", "plugins.csv", "lsp.csv"]


def _write_csv(path: Path, header: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class _Bar:
    """A bar as `barh` places it by default: centered on its y, `height` tall."""

    def __init__(self, width, centre, height):
        self.width, self.centre, self.height = width, centre, height

    def get_width(self):
        return self.width

    def get_y(self):
        return self.centre - self.height / 2

    def get_height(self):
        return self.height


def _recording_pyplot():
    """A stand-in for the module's `plt`: `subplots` gives a figure and an axis that record
    every call, and `barh` returns bars of the widths and positions that it was given."""
    plt = mock.MagicMock(name="plt")
    fig, ax = mock.MagicMock(name="fig"), mock.MagicMock(name="ax")
    ax.barh.side_effect = lambda y, widths, height, **kw: [
        _Bar(w, centre, height) for centre, w in zip(y, widths)]
    plt.subplots.return_value = (fig, ax)
    return plt, fig, ax


def _drawn(ax) -> tuple:
    """What the recorded axis shows: {(tick label, series): bar width} for every bar, paired
    by the nearest tick, and {(tick label, series): text} for every count label, paired with
    the bar that has its center at the label's height."""
    ticks = dict(zip(ax.set_yticks.call_args.args[0], ax.set_yticklabels.call_args.args[0]))
    bars = {}
    for call in ax.barh.call_args_list:
        for centre, width in zip(call.args[0], call.args[1]):
            tick = min(ticks, key=lambda t: abs(t - centre))
            bars[(ticks[tick], call.kwargs["label"])] = (centre, int(width))
    texts = {}
    for call in ax.text.call_args_list:
        at = [key for key, (centre, _) in bars.items() if abs(centre - call.args[1]) < 1e-9]
        texts[at[0] if len(at) == 1 else ("unplaced", call.args[1])] = call.args[2]
    return {key: width for key, (_, width) in bars.items()}, texts


@unittest.skipUnless(DEPS, "numpy, matplotlib and seaborn are needed")
class _Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        for name, value in (("DATA_DIR", self.data), ("OUTPUT_DIR", self.out)):
            patcher = mock.patch.object(mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_all(self, rows_by_table: dict) -> None:
        for name in TABLES:
            _write_csv(self.data / name, ["repo_name", "path"], rows_by_table.get(name, []))


class TestLoadArtifactStats(_Tmp):

    def test_configuration_artifacts_are_rows_and_repositories_are_distinct_names(self):
        self.write_all({"context-files.csv": [("o/a", "CLAUDE.md"), ("o/a", "AGENTS.md"), ("o/b", "CLAUDE.md")],
                        "skills.csv": [("o/b", "x"), ("o/c", "y")]})
        stats, n_repos = mod.load_artifact_stats()
        self.assertEqual(stats["Context Files"], {"configuration_artifacts": 3, "repos": 2})
        self.assertEqual(stats["Skills"], {"configuration_artifacts": 2, "repos": 2})
        self.assertEqual(stats["Hooks"], {"configuration_artifacts": 0, "repos": 0})
        self.assertEqual(n_repos, 3)
        self.assertEqual(list(stats), list(mod.MECHANISM_CSVS))

    def test_a_missing_mechanism_csv_is_refused_rather_than_counted_as_zero(self):
        """Every analysis script refuses a missing mechanism CSV, because an all-zero figure
        is byte-reproducible and would pass the reproduction check."""
        self.write_all({"skills.csv": [("o/a", "x")]})
        (self.data / "mcp.csv").unlink()
        with self.assertRaises(SystemExit) as caught:
            mod.load_artifact_stats()
        self.assertIn("mcp.csv", str(caught.exception))

    def test_a_mechanism_csv_without_repo_name_is_refused(self):
        """A truncated CSV would otherwise count its lines as configuration artifacts in no
        repositories, and the figure would show too few repositories without a message."""
        self.write_all({})
        _write_csv(self.data / "rules.csv", ["path"], [("a",), ("b",)])
        with self.assertRaises(SystemExit) as caught:
            mod.load_artifact_stats()
        self.assertIn("repo_name", str(caught.exception))

    def test_a_field_longer_than_the_default_limit_of_the_csv_module_is_read(self):
        """The detection step joins every file that a context file references into
        `referenced_file`, a column without a bound on its length. The script runs in a process
        of its own, as the analysis stage runs it, and the field size limit is then the one that
        its imports set."""
        self.write_all({})
        references = "|".join(f"docs/part-{n}.md" for n in range(20_000))
        _write_csv(self.data / "context-files.csv", ["repo_name", "path", "referenced_file"],
                   [("o/a", "CLAUDE.md", references)])
        program = ("import sys\n"
                   "sys.path.insert(0, sys.argv[1])\n"
                   "import artifact_distribution\n"
                   "stats, repositories = artifact_distribution.load_artifact_stats()\n"
                   "print(stats['Context Files']['configuration_artifacts'], repositories)\n")
        result = subprocess.run(
            [sys.executable, "-c", program, str(SCRIPT_DIR)], capture_output=True, text=True,
            env={**os.environ, "DATA_COLLECTION_OUTPUT": str(self.data), "MPLBACKEND": "Agg",
                 "PYTHONPATH": str(HERE.parent)})
        self.assertEqual((result.returncode, result.stdout), (0, "1 1\n"), result.stderr)

    def test_the_csvs_these_tests_write_are_the_mechanism_csvs(self):
        self.assertEqual(list(mod.MECHANISM_CSVS.values()), TABLES)

    def test_a_mechanism_with_no_row_is_labeled_zero(self):
        """A bar at zero has a label, so that a mechanism that no repository uses can be told
        from a mechanism that the figure left out."""
        self.write_all({"context-files.csv": [("o/a", "CLAUDE.md"), ("o/b", "AGENTS.md")],
                        "skills.csv": [("o/a", "x")]})
        plt, fig, ax = _recording_pyplot()
        with mock.patch.object(mod, "plt", plt), contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        bars, texts = _drawn(ax)
        self.assertEqual(len(texts), 20)
        for series in ("Configuration artifacts", "Repositories"):
            self.assertEqual(bars[("Hooks", series)], 0)
            self.assertEqual(texts[("Hooks", series)], "0")
        self.assertEqual(texts[("Context Files", "Configuration artifacts")], "2")
        self.assertEqual(texts[("Skills", "Repositories")], "1")


@unittest.skipUnless(DEPS, "numpy, matplotlib and seaborn are needed")
class TestOverTheFixture(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(mod, "DATA_DIR", FIXTURE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_counts_computed_by_hand(self):
        stats, n_repos = mod.load_artifact_stats()
        self.assertEqual(stats, {
            "Context Files": {"configuration_artifacts": 160, "repos": 31},
            "Skills": {"configuration_artifacts": 161, "repos": 17},
            "Commands": {"configuration_artifacts": 16, "repos": 2},
            "Subagents": {"configuration_artifacts": 40, "repos": 9},
            "Rules": {"configuration_artifacts": 21, "repos": 5},
            "Settings": {"configuration_artifacts": 32, "repos": 21},
            "Hooks": {"configuration_artifacts": 7, "repos": 5},
            "MCP": {"configuration_artifacts": 7, "repos": 5},
            "Plugins": {"configuration_artifacts": 11, "repos": 5},
            "LSP": {"configuration_artifacts": 0, "repos": 0},
        })
        self.assertEqual(n_repos, 32)

    def test_the_repository_counts_agree_with_the_flag_columns(self):
        """The figure's distinct repository count must agree with the mechanism flag
        count in repos.csv."""
        with open(FIXTURE / "repos.csv", newline="", encoding="utf-8") as f:
            repos = list(csv.DictReader(f))
        flags = {"Context Files": "context_file", "Skills": "skills", "Commands": "commands",
                 "Subagents": "subagents", "Rules": "rules", "Settings": "settings",
                 "Hooks": "hooks", "MCP": "mcp", "Plugins": "plugins", "LSP": "lsp"}
        stats, _ = mod.load_artifact_stats()
        for label, column in flags.items():
            with self.subTest(mechanism=label):
                flagged = sum(1 for r in repos if r[column].upper() == "TRUE")
                self.assertEqual(stats[label]["repos"], flagged)

    def test_each_bar_and_its_label_show_the_count_of_the_mechanism_on_its_tick(self):
        """The test pairs each count with the bar on its tick and fails for counts left in CSV
        order under reversed tick labels."""
        plt, fig, ax = _recording_pyplot()
        with mock.patch.object(mod, "plt", plt), contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        counts = {"Context Files": (160, 31), "Skills": (161, 17), "Commands": (16, 2),
                  "Subagents": (40, 9), "Rules": (21, 5), "Settings": (32, 21), "Hooks": (7, 5),
                  "MCP": (7, 5), "Plugins": (11, 5), "LSP": (0, 0)}
        bars, texts = _drawn(ax)
        expected = {}
        for label, (configuration_artifacts, repos) in counts.items():
            expected[(label, "Configuration artifacts")] = configuration_artifacts
            expected[(label, "Repositories")] = repos
        self.assertEqual(bars, expected)
        self.assertEqual(texts, {key: f"{n:,}" for key, n in expected.items()})
        self.assertIn("n = 455 configuration artifacts from 32 repositories", ax.set_title.call_args.args[0])
        self.assertEqual(list(ax.set_yticklabels.call_args.args[0]),
                         ["LSP", "Plugins", "MCP", "Hooks", "Settings", "Rules", "Subagents", "Commands",
                          "Skills", "Context Files"])
        fig.savefig.assert_called_once()


@unittest.skipUnless(DEPS, "numpy, matplotlib and seaborn are needed")
class TestMainOverTheFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.before = {p.name for p in SCRIPT_DIR.iterdir()}

    def test_the_run(self):
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(self.tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(SCRIPT_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        pdf = self.tmp / "dataset" / "artifact_distribution.pdf"
        self.assertIn(f"Wrote {pdf}", logs.messages(result.stderr))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertNotIn("[WARN]", logs.messages(result.stderr))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)


if __name__ == "__main__":
    unittest.main()
