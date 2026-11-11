"""
Tests for agentcontext.steps.file_history: the git columns filled from the cache, or from a clone.

Strategy:
  - Because the step's output is a file, most tests build a small `output/` with the
    mechanism CSVs and a cache, run the step offline, and assert on the bytes that it wrote.
  - The output CSVs belong to the detection step, and this step fills four columns of each
    in place. The columns that it must leave unchanged matter as much as the columns that it
    fills. For each group of the detection step's columns, a test asserts that the group
    comes through unchanged.
  - The reproduction of the published rows depends on the offline mode. A run that reaches
    the network to fill a row that the cache already contains would still produce the right
    CSV. The tests therefore assert that nothing is fetched, not only that the values are
    right.
  - The tests that run git build repositories with `git init` and redirect
    `https://github.com/...` to a local `file://` remote through `GIT_CONFIG_*`. They
    redirect every URL, including the URL of the repository that is meant to be gone.
    Nothing reaches a network, and the outcome does not depend on what a server returns.
  - `memory` has its own test. The step reads the CSVs through the standard library,
    because pandas' default NA handling reads an `n/a` as blank.
  - The mechanism CSVs committed in the stage directory get their own class, which skips
    where they have not been generated. The claim that an offline run reproduces the
    current published rows byte for byte is then a test rather than a note in a commit
    message.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_steps_file_history -v
"""

import csv
import functools
import importlib
import inspect
import io
import contextlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock
from pathlib import Path
from agentcontext import STAGE_DIR
from tests._fixtures import dsm_writes_during_removal
_git = importlib.import_module("agentcontext.steps.file_history")
_facts = importlib.import_module("agentcontext.caches")
heuristics = importlib.import_module("agentcontext.heuristics")
repos = importlib.import_module("agentcontext.repos")
bounds = importlib.import_module("agentcontext.git.bounds")
_meta = importlib.import_module("agentcontext.git.history")

HEAD = "a" * 40
SCANNED_AT = "2026-03-29"
HEAD_COMMIT_DATE = "2026-03-01T00:00:00Z"
MEASURED = _facts.FileHistory("2026-02-01T10:00:00Z", "4", "c" * 40, "d" * 40)


def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)


def _commit_file(repo: Path, relative: str, content: str, message: str,
                 date: str = "2020-01-01T00:00:00+00:00") -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _run(["git", "add", relative], cwd=repo)
    _run(["git", "commit", "-q", "-m", message], cwd=repo,
         env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _full_halfway(real_fdopen):
    """An `os.fdopen` with a file that writes half of what it is given and then raises
    ENOSPC, which simulates a disk that fills up while the temporary file is written."""
    def fdopen(fd, *args, **kwargs):
        f = real_fdopen(fd, *args, **kwargs)
        real_write = f.write

        def write(text):
            real_write(text[:len(text) // 2])
            f.flush()
            raise OSError(28, "No space left on device")
        f.write = write
        return f
    return fdopen


class _StepCase(unittest.TestCase):
    """An `output/` with the output CSVs, a `repos/` tree with the validated list, the collected
    list at `data/collected/repos.csv`, and a cache beside them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.repos = self.root / "repos"
        self.output.mkdir()
        self.repos.mkdir()
        self.cache_path = self.root / "file_history.csv"
        self.collected_csv = self.root / "data" / "collected" / "repos.csv"
        self.validated_csv = self.repos / "repos.csv"
        self.slept = []       # no test waits for a clone retry
        # No test reaches the network. The probe of `wait_out_an_outage` responds unless a test
        # patches it.
        probe = mock.patch.object(_git.guards, "remote_responds", return_value=True)
        probe.start()
        self.addCleanup(probe.stop)
        self.write_tables()
        self.write_repos_csv()
        self.write_list(self.collected_csv, [("owner/repo", HEAD)])
        self.write_list(self.validated_csv, [("owner/repo", HEAD)])

    @staticmethod
    def write_list(path: Path, heads) -> None:
        """A collected or a validated list with a row for each `(repo_name, head_sha)` of
        `heads`, with the columns that the two lists begin with."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["repo_name", "scanned_at", "branch", "head_sha", "head_commit_date"])
            for repo_name, head_sha in heads:
                writer.writerow([repo_name, SCANNED_AT, "main", head_sha, HEAD_COMMIT_DATE])

    def output_heads(self) -> set:
        """The `(repo_name, head_sha)` of every row of the output CSVs. In a data root that a
        run left, the validated list names the same heads, because the detection step takes
        the head of a row from that list."""
        heads = set()
        for filename, _ in _git.HISTORY_TABLES.values():
            with open(self.output / filename, newline="", encoding="utf-8") as f:
                heads.update((row["repo_name"], row["head_sha"]) for row in csv.DictReader(f))
        return heads

    def current_heads(self) -> set:
        """The heads at which a run of the step keeps the rows of the caches: the heads of the
        two lists and of the output CSVs. A test may change the heads of the output CSVs after
        `setUp` without writing the validated list again."""
        return _git.current_heads(self.collected_csv, self.validated_csv) | self.output_heads()

    # What the detection step writes into the columns that this step must not change. Each of
    # these columns keeps its value through a fill, and a test below asserts that for each
    # group of columns.
    DETECT_VALUES = {
        "name": "one", "memory": "n/a", "scripts": "0", "references": "0", "assets": "0",
        "language": "English",
        "line_count": "12", "is_reference": "FALSE", "referenced_file": "",
        "first_commit_ai_created": "TRUE", "first_commit_ai_tool": "claude",
        "#ai_commits": "3", "ai_commit_tools": "claude", "tool": "claude",
    }

    def header(self, mechanism: str) -> list:
        """The published header, from the heuristics: the header that the detection step
        writes."""
        return _git.table_header(mechanism)

    def base_row(self, mechanism: str, repo="owner/repo", artifact_path=None,
                 head=HEAD) -> dict:
        """One row as the detection step leaves it: every column filled except the git
        columns."""
        _, artifact = _git.HISTORY_TABLES[mechanism]
        row = {c: self.DETECT_VALUES.get(c, "") for c in self.header(mechanism)}
        row.update({"repo_name": repo, "scanned_at": SCANNED_AT, "branch": "main",
                    "head_sha": head, "head_commit_date": HEAD_COMMIT_DATE,
                    artifact: artifact_path or f"{mechanism}/one.md",
                    "github_link": "https://example.invalid", "dataset_version": "2.0.0"})
        return row

    def write_tables(self, extra_rows=(), line_terminator="\n") -> None:
        for mechanism, (filename, _) in _git.HISTORY_TABLES.items():
            rows = [self.base_row(mechanism)]
            rows.extend(dict(r) for m, r in extra_rows if m == mechanism)
            with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.header(mechanism),
                                        lineterminator=line_terminator)
                writer.writeheader()
                writer.writerows(rows)

    # The flag columns of the fixture's repos.csv, which `reconcile_repos_csv` rewrites.
    FLAG_COLUMNS = ("claude", "gemini", "codex", "copilot", "cursor", "opencode",
                    "claude.md", "gemini.md", "agents.md", "copilot-instructions",
                    "cursorrules", "context_file", "settings", "skills", "subagents",
                    "commands", "hooks", "rules", "mcp", "plugins", "lsp", "other",
                    "no_specific_tool")

    def write_repos_csv(self, listed=None) -> None:
        """Write repos.csv with a row for each repository in `listed`, {repo_name: {flag:
        value}}. A flag is FALSE unless the repository's entry sets it, and `listed` defaults
        to `owner/repo` alone."""
        listed = {"owner/repo": {}} if listed is None else listed
        columns = (["repo_name", "scanned_at", "branch", "head_sha", "head_commit_date"]
                   + list(self.FLAG_COLUMNS) + ["github_link", "dataset_version"])
        with open(self.output / "repos.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for repo_name, flags in listed.items():
                writer.writerow({**{c: "FALSE" for c in columns},
                                 "repo_name": repo_name, "scanned_at": SCANNED_AT,
                                 "branch": "main", "head_sha": HEAD,
                                 "head_commit_date": HEAD_COMMIT_DATE,
                                 "github_link": "https://example.invalid",
                                 "dataset_version": "2.0.0", **flags})

    def seed_cache(self, facts=MEASURED, mechanisms=None) -> None:
        cache = _facts.FileHistoryCache()
        for mechanism in (mechanisms or _git.HISTORY_TABLES):
            _, artifact = _git.HISTORY_TABLES[mechanism]
            cache.put(_facts.FileHistoryKey("owner/repo", HEAD, mechanism,
                                        f"{mechanism}/one.md"), facts)
        cache.save(self.cache_path)

    def fill(self, mechanism: str, offline=True, retry_missing_history=False,
             github_token=None) -> dict:
        cache = _facts.FileHistoryCache.load(self.cache_path)
        summary = _git.fill_table(mechanism, self.output, cache, self.repos,
                                  workers=1, github_token=github_token,
                                  offline=offline,
                                  retry_missing_history=retry_missing_history,
                                  sleep=self.slept.append)
        cache.save(self.cache_path)
        return summary

    def build_all(self, offline=True, retry=False) -> tuple:
        """Run every CSV through `build_all` and return (summaries, what it logged)."""
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            summaries = _git.build_all(self.output, self.cache_path, self.repos, 1,
                                       None, offline, retry, sleep=self.slept.append,
                                       heads=self.current_heads())
        return summaries, out.getvalue()

    def path_of(self, mechanism: str) -> Path:
        filename, _ = _git.HISTORY_TABLES[mechanism]
        return self.output / filename

    def CSV(self, mechanism: str) -> tuple:
        """One mechanism CSV as (header, rows). The step fills the CSV in place, and there is
        no second file beside it to read."""
        with open(self.path_of(mechanism), newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader.fieldnames), list(reader)


# ---------------------------------------------------------------------------
# What this step fills, and what it must leave alone
# ---------------------------------------------------------------------------
class TestColumns(unittest.TestCase):
    """Four columns belong to this step, and every other column of a CSV belongs to the
    detection step. Because no second file beside the CSV holds the detection step's values,
    a column that this step overwrites is lost, rather than disagreeing with a second
    file."""

    def test_every_per_mechanism_csv_contains_the_columns_this_step_fills(self):
        for mechanism in heuristics.MECHANISM_CSVS:
            with self.subTest(mechanism=mechanism):
                header = heuristics.csv_header(mechanism)
                for column in _git.FILLED_COLUMNS:
                    self.assertIn(column, header)

    def test_no_per_mechanism_csv_contains_is_empty(self):
        """`is_empty` could never be TRUE, because the collected file cleanup deletes every
        empty file before the detection step runs."""
        for mechanism in heuristics.MECHANISM_CSVS:
            with self.subTest(mechanism=mechanism):
                self.assertNotIn("is_empty", heuristics.csv_header(mechanism))

    def test_language_is_not_a_column_this_step_fills(self):
        """`language` belongs to the detection step, which reads it from the validated tree.
        The cache does not have it, and a value from the cache would describe another version
        of the configuration artifact than the rest of its row."""
        self.assertNotIn("language", _git.FILLED_COLUMNS)
        self.assertNotIn("language", _facts.FILE_HISTORY_COLUMNS)


# ---------------------------------------------------------------------------
# Filling a CSV from the cache alone
# ---------------------------------------------------------------------------
class TestFillTable(_StepCase):
    def setUp(self):
        super().setUp()
        self.seed_cache()

    def test_a_cached_row_is_filled_without_a_clone(self):
        summary = self.fill("skills")
        self.assertEqual(summary["fetched"], 0)
        _, rows = self.CSV("skills")
        self.assertEqual(rows[0]["created_at"], "2026-02-01T10:00:00Z")
        self.assertEqual(rows[0]["#commits"], "4")
        self.assertEqual(rows[0]["first_commit_sha"], "c" * 40)
        self.assertEqual(rows[0]["last_commit_sha"], "d" * 40)

    def test_the_header_is_the_heuristics_header_and_does_not_move(self):
        before, _ = self.CSV("skills")
        self.fill("skills")
        after, _ = self.CSV("skills")
        self.assertEqual(after, self.header("skills"))
        self.assertEqual(after, before)

    def test_every_column_the_detection_step_wrote_comes_through_unchanged(self):
        """context-files.csv has the most columns that the detection step writes and this
        step keeps: the four reference columns, the four AI columns and `language`. A fill
        that wrote the cache row wholesale, or that rebuilt the header from the columns that
        it fills, would blank all nine."""
        _, before = self.CSV("context_file")
        self.fill("context_file")
        _, after = self.CSV("context_file")
        kept = [c for c in self.header("context_file") if c not in _git.FILLED_COLUMNS]
        self.assertEqual({c: after[0][c] for c in kept},
                         {c: before[0][c] for c in kept})
        self.assertEqual(after[0]["language"], "English")
        self.assertEqual(after[0]["is_reference"], "FALSE")
        self.assertEqual(after[0]["#ai_commits"], "3")

    def test_a_timestamp_that_does_not_parse_is_written_blank_with_a_warning(self):
        """A damaged repository can carry a date that does not parse, and the cache keeps
        git's text for it. A step that refused the date would stop at every Start. Published
        as it is, the text would be the only value in another format in a column of UTC
        values."""
        self.seed_cache(facts=_facts.FileHistory(created_at="last tuesday", commits="4",
                                                 first_commit_sha="c" * 40,
                                                 last_commit_sha="d" * 40))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            summary = self.fill("skills")
        _, rows = self.CSV("skills")
        self.assertEqual({c: rows[0][c] for c in _git.FILLED_COLUMNS},
                         {"created_at": "", "#commits": "4", "first_commit_sha": "c" * 40,
                          "last_commit_sha": "d" * 40})
        self.assertEqual(summary["undated"], {("owner/repo", "skills/one.md")})
        self.assertIn("had 1 created_at value(s) that are not ISO 8601 timestamps",
                      err.getvalue())
        self.assertIn("'last tuesday'. Left them blank. The values are in 1 repositories "
                      "(owner/repo).\n", err.getvalue())

    def test_scanned_at_is_not_run_through_the_utc_converter(self):
        """In the March sample, `scanned_at` is the constant `2026-03-29`, the first day of
        the collection window. Normalized like a date column, it would become an instant at
        midnight, a scan time that nothing measured."""
        self.fill("skills")
        _, rows = self.CSV("skills")
        self.assertEqual(rows[0]["scanned_at"], SCANNED_AT)
        self.assertNotIn("scanned_at", _git.TIMESTAMP_COLUMNS)

    def test_a_memory_of_n_a_is_written_as_n_a(self):
        """With pandas' default NA handling, `n/a` becomes NaN, and a step that read the CSV
        that way would publish the row blank."""
        self.fill("subagents")
        _, rows = self.CSV("subagents")
        self.assertEqual(rows[0]["memory"], "n/a")

    def test_a_mechanism_csv_with_no_language_column_does_not_gain_one(self):
        self.fill("hooks")
        columns, _ = self.CSV("hooks")
        self.assertNotIn("language", columns)

    def test_the_row_order_is_the_one_the_detection_step_wrote(self):
        """This step rewrites a CSV that the detection step owns, and it keeps that step's
        row order. Sorting here would show the whole file as changed."""
        extra = [("rules", self.base_row("rules", repo="aaa/repo",
                                         artifact_path="rules/zz.md"))]
        self.write_tables(extra_rows=extra)
        cache = _facts.FileHistoryCache.load(self.cache_path)
        cache.put(_facts.FileHistoryKey("aaa/repo", HEAD, "rules", "rules/zz.md"), MEASURED)
        cache.save(self.cache_path)
        self.fill("rules")
        _, rows = self.CSV("rules")
        self.assertEqual([r["repo_name"] for r in rows], ["owner/repo", "aaa/repo"])

    def test_a_rewritten_mechanism_csv_ends_its_lines_with_lf(self):
        """Every writer of the pipeline ends its lines with LF, and the step writes LF whether the
        CSV that it fills ends its lines with LF or with CRLF."""
        for terminator in ("\n", "\r\n"):
            with self.subTest(terminator=repr(terminator)):
                self.write_tables(line_terminator=terminator)
                self.fill("hooks")
                raw = self.path_of("hooks").read_bytes()
                self.assertNotIn(b"\r\n", raw)
                self.assertTrue(raw.endswith(b"\n"))

    def test_a_created_at_that_is_not_utc_is_normalized(self):
        cache = _facts.FileHistoryCache.load(self.cache_path)
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "hooks", "hooks/one.md"),
                  _facts.FileHistory("2026-02-01T12:00:00+02:00", "1", "e" * 40, "f" * 40))
        cache.save(self.cache_path)
        self.fill("hooks")
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["created_at"], "2026-02-01T10:00:00Z")

    def test_a_created_at_in_a_zone_past_24_hours_is_its_epoch_in_utc(self):
        """git prints a commit's offset of +2500 as `+25:00` in `%cI`, and `datetime` refuses
        that offset as a time zone. The step writes the value that it reads from a real commit
        as the instant of the commit's epoch seconds, rather than raising and failing."""
        from datetime import datetime, timezone
        from tests._fixtures import _add_commit_in_zone, _init_git_repo
        repo = self.root / "zone"
        _init_git_repo(repo)
        _add_commit_in_zone(repo, "hooks/one.md", "# one\n", 1700000000, "+2500")
        shown = subprocess.run(["git", "-C", str(repo), "show", "-s", "--format=%cI%n%ct",
                                "HEAD"], capture_output=True, text=True, check=True).stdout
        committer_date, epoch = shown.split()
        self.assertTrue(committer_date.endswith("+25:00"), committer_date)
        cache = _facts.FileHistoryCache.load(self.cache_path)
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "hooks", "hooks/one.md"),
                  _facts.FileHistory(committer_date, "1", "e" * 40, "f" * 40))
        cache.save(self.cache_path)
        self.fill("hooks")
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["created_at"], datetime.fromtimestamp(
            int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_a_row_the_cache_records_under_another_mechanism_is_not_a_miss(self):
        """A path that a heuristic reclassifies into another mechanism keeps its history. A
        lookup by its new mechanism alone would miss the cached row and clone the
        repository."""
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "commands", "skills/one.md"),
                  MEASURED)
        cache.save(self.cache_path)
        summary = self.fill("skills")
        self.assertEqual(summary["fetched"], 0)
        _, rows = self.CSV("skills")
        self.assertEqual(rows[0]["#commits"], "4")

    def test_a_mechanism_csv_with_only_a_header_keeps_only_its_header(self):
        path = self.path_of("mcp")
        path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n",
                        encoding="utf-8")
        summary = self.fill("mcp")
        self.assertEqual(summary["rows"], 0)
        columns, rows = self.CSV("mcp")
        self.assertEqual(rows, [])
        self.assertEqual(columns, self.header("mcp"))

    def test_a_mechanism_csv_missing_a_column_the_lookup_reads_stops_the_run(self):
        """The cache lookup reads `head_sha`, and the planner groups the misses by
        `head_sha`, `head_commit_date` and `branch`. Without the check, a CSV that lacks one
        of these columns would raise a KeyError from inside the planner."""
        for column in ("head_sha", "head_commit_date", "branch"):
            with self.subTest(column=column):
                self.write_tables()
                path = self.path_of("mcp")
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                lines[0] = lines[0].replace(column, column + "_renamed")
                path.write_text("".join(lines), encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    self.fill("mcp")
                self.assertIn(column, str(caught.exception))

    def test_filling_twice_writes_the_same_bytes(self):
        """A second fill of a CSV that this step has already filled writes the same bytes,
        and the offline reproduction of the published rows depends on this property."""
        self.fill("other")
        first = self.path_of("other").read_bytes()
        self.fill("other")
        self.assertEqual(self.path_of("other").read_bytes(), first)


# ---------------------------------------------------------------------------
# What an offline run rejects
# ---------------------------------------------------------------------------
class TestOffline(_StepCase):
    def test_a_row_missing_from_the_cache_stops_an_offline_run(self):
        with self.assertRaises(ValueError) as caught:
            self.fill("skills")
        self.assertIn("--offline", str(caught.exception))
        self.assertIn("1 row", str(caught.exception))

    def test_the_mechanism_csv_is_left_as_it_was_when_the_step_refuses(self):
        """This step writes the CSV that the detection step wrote, not a second file that a
        failed run could leave absent. A failed run has to leave that CSV unchanged."""
        before = self.path_of("skills").read_bytes()
        with self.assertRaises(ValueError):
            self.fill("skills")
        self.assertEqual(self.path_of("skills").read_bytes(), before)

    def test_a_blank_row_is_not_refetched_unless_requested(self):
        self.seed_cache(facts=_facts.FileHistory())
        summary = self.fill("hooks")
        self.assertEqual(summary["fetched"], 0)
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["created_at"], "")

    def test_offline_writes_nothing_when_any_mechanism_csv_is_short(self):
        """A raise from inside the loop would leave the output CSVs that the loop had reached
        rewritten and the rest as they were, neither the old output nor the new one. Because
        the step fills the CSVs in place, the rewritten CSVs are the output, and the mix could
        not be undone."""
        self.seed_cache(mechanisms=["settings"])
        before = {m: self.path_of(m).read_bytes()
                  for m in heuristics.MECHANISM_CSVS}
        with self.assertRaises(ValueError) as caught:
            self.build_all(offline=True)
        self.assertIn("--offline", str(caught.exception))
        for mechanism in heuristics.MECHANISM_CSVS:
            self.assertEqual(self.path_of(mechanism).read_bytes(), before[mechanism],
                             f"{mechanism} was written before the run failed")

    def test_a_row_the_run_could_not_measure_keeps_what_the_last_run_published(self):
        """A clone that a timeout stopped is a failure of the run, and blanking the row
        because of that failure would replace a measurement with nothing."""
        self.seed_cache()
        self.fill("hooks")
        _, before = self.CSV("hooks")
        self.assertEqual(before[0]["created_at"], "2026-02-01T10:00:00Z")
        self.cache_path.unlink()
        original = _git.measure_repo
        _git.measure_repo = lambda *a, **k: ({}, "git_unavailable")
        self.addCleanup(setattr, _git, "measure_repo", original)
        summary = self.fill("hooks", offline=False)
        self.assertEqual(summary["no_history"], 1)
        _, after = self.CSV("hooks")
        self.assertEqual(after[0]["created_at"], "2026-02-01T10:00:00Z")

    def test_retry_missing_history_turns_a_row_without_one_into_a_fetch(self):
        self.seed_cache(facts=_facts.FileHistory())
        with self.assertRaises(ValueError) as caught:
            self.fill("hooks", retry_missing_history=True)
        self.assertIn("--offline", str(caught.exception))


# ---------------------------------------------------------------------------
# Measuring, against a local remote
# ---------------------------------------------------------------------------
class TestMeasure(_StepCase):
    def setUp(self):
        super().setUp()
        self.remote = self.root / "remote"
        _init_repo(self.remote)
        self.first = _commit_file(self.remote, "hooks/one.md", "# hooks\n" + "x" * 80,
                                  "add hooks", "2021-05-04T09:00:00+00:00")
        self.head = _commit_file(self.remote, "other.txt", "other\n", "unrelated",
                                 "2021-06-04T09:00:00+00:00")
        self._saved_token = os.environ.pop("GITHUB_TOKEN", None)
        self.addCleanup(self._restore_token)
        # Two redirects keep every clone of this class off the network. The second points the
        # repository that is meant to be gone at a path that is not a repository, and git then
        # writes one of the stderr messages that `_clone_outcome` reads as `clone_failed`.
        # Without that redirect, the clone would go to github.com. The outcome would then
        # depend on the server's response, which differs between macOS and a CI runner.
        self.absent = self.root / "absent"
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"
        os.environ["GIT_CONFIG_KEY_1"] = f"url.file://{self.absent}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/gone/repo.git"
        self.addCleanup(self._clear_git_config)

    def _restore_token(self):
        if self._saved_token is not None:
            os.environ["GITHUB_TOKEN"] = self._saved_token

    def _clear_git_config(self):
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                    "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
            os.environ.pop(key, None)

    def test_a_miss_is_measured_and_written_back_to_the_cache(self):
        self.write_tables()
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                text = path.read_text(encoding="utf-8").replace(HEAD, self.head)
                path.write_text(text, encoding="utf-8")
        summary = self.fill("hooks", offline=False)
        self.assertEqual(summary["fetched"], 1)
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["created_at"], "2021-05-04T09:00:00Z")
        self.assertEqual(rows[0]["first_commit_sha"], self.first)
        cache = _facts.FileHistoryCache.load(self.cache_path)
        key = _facts.FileHistoryKey("owner/repo", self.head, "hooks", "hooks/one.md")
        self.assertEqual(cache.get(key).created_at, "2021-05-04T09:00:00Z")

    def test_an_unavailable_collected_commit_preserves_rows_and_both_caches(self):
        _commit_file(self.remote, "CLAUDE.md", "# instructions\n", "add instructions")
        missing = "f" * 40
        self._tables_at_the_remote_head([
            ("context_file", self.base_row("context_file", artifact_path="CLAUDE.md"))])
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                path.write_text(path.read_text().replace(self.head, missing))
        self.seed_cache()
        ai_path = self.root / "ai_authorship.csv"
        ai_cache = _facts.AiAuthorshipCache()
        ai_cache.put(_facts.AiAuthorshipKey("trusted/repo", self.first, "CLAUDE.md"),
                     _facts.AiAuthorship("TRUE", "claude", "1", "claude"))
        ai_cache.save(ai_path)
        # The collected list names the repository of the AI row too, so that neither cache
        # loses a row.
        self.write_list(self.collected_csv, [("owner/repo", HEAD), ("trusted/repo", self.first)])
        before = {path: path.read_bytes() for path in (self.cache_path, ai_path)}
        repo_dir = self.repos / "owner§repo"
        repo_dir.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            summaries = _git.build_all(
                self.output, self.cache_path, self.repos, 1, None, False, True,
                sleep=self.slept.append, ai_cache_path=ai_path, heads=self.current_heads())
        self.assertTrue(repo_dir.is_dir())
        self.assertTrue(all(summary["pruned"] == 0 for summary in summaries))
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original)
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["head_sha"], missing)
        self.assertEqual(rows[0]["#commits"], "")
        _, rows = self.CSV("context_file")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["head_sha"], missing)
        self.assertEqual(rows[-1]["#commits"], "")

    def _tables_at_the_remote_head(self, extra_rows=()):
        self.write_tables(extra_rows)
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                text = path.read_text(encoding="utf-8").replace(HEAD, self.head)
                path.write_text(text, encoding="utf-8")

    def test_an_offline_run_measures_the_repository_it_is_told_to(self):
        """The subsample run makes the history fetch of one repository fail in the cloning
        step. It then runs this step offline except for that repository, as a container run
        does after a history fetch that failed on the network."""
        self._tables_at_the_remote_head()
        with contextlib.redirect_stdout(io.StringIO()):
            _git.build_all(self.output, self.cache_path, self.repos, 1, None, True, False,
                           sleep=self.slept.append, offline_except=frozenset({"owner/repo"}),
                           heads=self.current_heads())
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["created_at"], "2021-05-04T09:00:00Z")
        key = _facts.FileHistoryKey("owner/repo", self.head, "hooks", "hooks/one.md")
        self.assertTrue(_facts.FileHistoryCache.load(self.cache_path).get(key).is_complete())

    def test_an_offline_run_still_refuses_every_other_repository(self):
        """A miss outside the named repository is a row that the cloning step should have
        measured, and a fetch would hide that failure."""
        self._tables_at_the_remote_head(
            [("hooks", self.base_row("hooks", repo="other/repo", head=self.head))])
        before = {m: self.path_of(m).read_bytes() for m in heuristics.MECHANISM_CSVS}
        with self.assertRaises(ValueError) as caught, \
                contextlib.redirect_stdout(io.StringIO()):
            _git.build_all(self.output, self.cache_path, self.repos, 1, None, True, False,
                           sleep=self.slept.append, offline_except=frozenset({"owner/repo"}),
                           heads=self.current_heads())
        self.assertIn("--offline", str(caught.exception))
        self.assertIn("1 row(s)", str(caught.exception))
        for mechanism in heuristics.MECHANISM_CSVS:
            self.assertEqual(self.path_of(mechanism).read_bytes(), before[mechanism])

    def test_a_row_the_cache_records_as_blank_is_measured_again(self):
        """A record can leave a row blank, or write a bare 0 for a row that it never
        measured. Treated as measured, such a row would be published blank in every run. The
        command line therefore retries these rows unless the run passes `--keep-blank`."""
        self.write_tables()
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                text = path.read_text(encoding="utf-8").replace(HEAD, self.head)
                path.write_text(text, encoding="utf-8")
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("owner/repo", self.head, "hooks", "hooks/one.md"),
                  _facts.FileHistory(commits="0"))
        cache.save(self.cache_path)
        summaries, _ = self.build_all(offline=False, retry=True)
        _, rows = self.CSV("hooks")
        self.assertEqual((rows[0]["created_at"], rows[0]["#commits"]),
                         ("2021-05-04T09:00:00Z", "1"))
        key = _facts.FileHistoryKey("owner/repo", self.head, "hooks", "hooks/one.md")
        self.assertTrue(_facts.FileHistoryCache.load(self.cache_path).get(key).is_complete())

    def test_a_blank_row_of_a_repository_that_is_gone_is_kept_rather_than_pruned(self):
        """A retry tests whether the history can be read now, not whether the repository
        still exists. `chainapsis/keplr-wallet` is in the March sample, and GitHub no longer
        serves it. Pruning it on a retry would take it out of a sample collected when it
        existed. The step still prunes a repository with rows that missed the cache."""
        filename, _ = heuristics.MECHANISM_CSVS["hooks"]
        with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.header("hooks"), lineterminator="\n")
            writer.writeheader()
            writer.writerow(self.base_row("hooks", repo="gone/repo",
                                          artifact_path="hooks/one.md"))
        for mechanism, (other, _) in heuristics.MECHANISM_CSVS.items():
            if mechanism != "hooks":
                (self.output / other).write_text(
                    ",".join(self.header(mechanism)) + "\n", encoding="utf-8")
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("gone/repo", HEAD, "hooks", "hooks/one.md"),
                  _facts.FileHistory())
        cache.save(self.cache_path)
        (self.repos / repos.repo_name_to_dir("gone/repo")).mkdir(parents=True)
        summaries, _ = self.build_all(offline=False, retry=True)
        hooks = next(s for s in summaries if s["mechanism"] == "hooks")
        self.assertEqual((hooks["pruned"], hooks["no_history"]), (0, 1))
        _, rows = self.CSV("hooks")
        self.assertEqual([(r["repo_name"], r["created_at"]) for r in rows], [("gone/repo", "")])
        self.assertTrue((self.repos / repos.repo_name_to_dir("gone/repo")).exists())

    def test_a_gone_repository_with_a_blank_retry_and_a_cache_miss_is_pruned(self):
        for mechanism, (filename, _) in heuristics.MECHANISM_CSVS.items():
            rows = [self.base_row(mechanism, repo="gone/repo",
                                 artifact_path=f"{mechanism}/one.md")] if mechanism in ("hooks", "mcp") else []
            _git.write_table(self.output / filename, self.header(mechanism), rows)
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("gone/repo", HEAD, "hooks", "hooks/one.md"),
                  _facts.FileHistory())
        cache.save(self.cache_path)
        tree = self.repos / repos.repo_name_to_dir("gone/repo")
        tree.mkdir(parents=True)
        summaries, _ = self.build_all(offline=False, retry=True)
        pruned = {row["mechanism"]: row["pruned"] for row in summaries}
        self.assertEqual((pruned["hooks"], pruned["mcp"]), (1, 1))
        self.assertFalse(tree.exists())
        self.assertEqual(self.CSV("hooks")[1], [])
        self.assertEqual(self.CSV("mcp")[1], [])

    def test_a_measured_context_file_gets_its_ai_columns_too(self):
        """A file with a history that the cloning step could not read reaches this step with
        blank git columns and blank AI columns, and no later step clones for the file. This
        step therefore derives the AI columns from the commits that the git columns come from
        and writes them into the CSV and into `ai_authorship.csv`."""
        _commit_file(self.remote, "context_file/one.md", "# context\n",
                     "add context\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                     "2021-05-05T09:00:00+00:00")
        head = _commit_file(self.remote, "context_file/one.md", "# context\nmore\n",
                            "edit by hand", "2021-05-06T09:00:00+00:00")
        self.write_tables()
        blank = {c: "" for c in heuristics.AI_COLUMNS}
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                text = path.read_text(encoding="utf-8").replace(HEAD, head)
                path.write_text(text, encoding="utf-8")
        columns, rows = self.CSV("context_file")
        _git.write_table(self.path_of("context_file"), columns,
                         [dict(row, **blank) for row in rows])
        ai_cache_path = self.root / "ai_authorship.csv"
        with contextlib.redirect_stdout(io.StringIO()):
            _git.build_all(self.output, self.cache_path, self.repos, 1, None, False, False,
                           sleep=self.slept.append, ai_cache_path=ai_cache_path,
                           heads=self.current_heads())
        _, rows = self.CSV("context_file")
        expected = {"first_commit_ai_created": "TRUE", "first_commit_ai_tool": "Claude",
                    "#ai_commits": "1", "ai_commit_tools": "Claude"}
        self.assertEqual({c: rows[0][c] for c in heuristics.AI_COLUMNS}, expected)
        cached = _facts.AiAuthorshipCache.load(ai_cache_path).get(
            _facts.AiAuthorshipKey("owner/repo", head, "context_file/one.md"))
        self.assertEqual(cached.as_row(), expected)
        header, _ = self.CSV("hooks")
        self.assertFalse(set(heuristics.AI_COLUMNS) & set(header),
                         "a CSV without the AI columns does not gain them")

    def test_a_run_without_the_ai_cache_leaves_the_ai_columns_as_the_detection_step_wrote(self):
        """A caller that does not own the AI cache runs this step with an `ai_cache_path` of
        None. The AI columns then keep the detection step's values, even for a row that this
        step measures."""
        self.write_tables()
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                text = path.read_text(encoding="utf-8").replace(HEAD, self.head)
                path.write_text(text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            _git.build_all(self.output, self.cache_path, self.repos, 1, None, False, False,
                           sleep=self.slept.append, heads=self.current_heads())
        _, rows = self.CSV("context_file")
        self.assertEqual({c: rows[0][c] for c in heuristics.AI_COLUMNS},
                         {c: self.DETECT_VALUES[c] for c in heuristics.AI_COLUMNS})
        self.assertFalse((self.root / "ai_authorship.csv").exists())

    def test_the_measurement_is_taken_at_the_recorded_commit(self):
        """A commit made after the scan must not become `last_commit_sha`. `--before=<head
        commit date>` would select the same commit indirectly, through the date, and could not
        report a failure."""
        scanned_at = self.head
        later = _commit_file(self.remote, "hooks/one.md", "# hooks\n" + "y" * 90,
                             "edit after the scan", "2022-01-01T09:00:00+00:00")
        self.assertNotEqual(later, scanned_at)
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                text = path.read_text(encoding="utf-8").replace(HEAD, scanned_at)
                path.write_text(text, encoding="utf-8")
        self.fill("hooks", offline=False)
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["last_commit_sha"], self.first)

    def test_a_repository_that_will_not_clone_is_pruned_and_named(self):
        self.write_tables(extra_rows=[
            ("hooks", self.base_row("hooks", repo="gone/repo",
                                    artifact_path="hooks/one.md"))])
        summary = self.fill("hooks", offline=False)
        self.assertEqual(summary["pruned"], 1)
        self.assertEqual(self.slept, [10, 20], "a wait between each two of the three attempts")
        _, rows = self.CSV("hooks")
        self.assertEqual([r["repo_name"] for r in rows], ["owner/repo"])

    def test_a_repository_that_is_gone_is_pruned_from_every_mechanism_csv(self):
        """A pruning limited to the rows that missed the cache would delete `repos/<repo>`,
        while the output CSVs kept every row that the cache could fill and `repos.csv` kept
        the repository with its flags set."""
        for mechanism in ("hooks", "mcp"):
            filename, artifact = heuristics.MECHANISM_CSVS[mechanism]
            with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.header(mechanism),
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerow(self.base_row(mechanism, repo="gone/repo",
                                              artifact_path=f"{mechanism}/one.md"))
        # Only one of the two rows misses the cache, which fills the other row. A pruning of
        # the miss alone would leave the output CSVs inconsistent. The two paths differ,
        # because `for_artifact` would otherwise fill both rows from the one cached row.
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("gone/repo", HEAD, "mcp", "mcp/one.md"), MEASURED)
        cache.save(self.cache_path)
        (self.repos / repos.repo_name_to_dir("gone/repo")).mkdir(parents=True)
        summaries, _ = self.build_all(offline=False)
        pruned = {s["mechanism"]: s["pruned"] for s in summaries}
        self.assertEqual(pruned["hooks"], 1)
        self.assertEqual(pruned["mcp"], 1, "the cached CSV kept a repository that git "
                                           "reports as gone")
        for mechanism in ("hooks", "mcp"):
            _, rows = self.CSV(mechanism)
            self.assertEqual(rows, [])
        self.assertFalse((self.repos / repos.repo_name_to_dir("gone/repo")).exists())

    def test_a_prune_writes_the_mechanism_csv_with_lf(self):
        """The step writes LF whether the CSV that it prunes ends its lines with LF or with
        CRLF."""
        filename, artifact = heuristics.MECHANISM_CSVS["hooks"]
        path = self.output / filename
        rows = [self.base_row("hooks"),
                self.base_row("hooks", repo="gone/repo", artifact_path="hooks/one.md")]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.header("hooks"),
                                    lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "hooks", "hooks/one.md"), MEASURED)
        cache.save(self.cache_path)
        self.build_all(offline=False)
        raw = path.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(raw.decode().count("gone/repo"), 0)

    def _one_file_in_two_tables(self) -> str:
        """Put `shared/one.md` in the hooks CSV and the mcp CSV, commit it to the remote, move
        the rows of every CSV to the new commit, and return that commit."""
        for mechanism in ("hooks", "mcp"):
            filename, artifact = heuristics.MECHANISM_CSVS[mechanism]
            rows = [self.base_row(mechanism, artifact_path="shared/one.md")]
            with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.header(mechanism),
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
        _commit_file(self.remote, "shared/one.md", "# shared\n" + "z" * 80, "add shared",
                     "2021-07-04T09:00:00+00:00")
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.remote).stdout.strip()
        for path in self.output.glob("*.csv"):
            text = path.read_text(encoding="utf-8").replace(HEAD, head)
            path.write_text(text, encoding="utf-8")
        return head

    def _cached_on_disk(self, head: str) -> dict:
        """The created_at that the cache file records for `shared/one.md`, per mechanism."""
        cache = _facts.FileHistoryCache.load(self.cache_path)
        found = {}
        for mechanism in ("hooks", "mcp"):
            facts = cache.get(_facts.FileHistoryKey("owner/repo", head, mechanism,
                                                    "shared/one.md"))
            found[mechanism] = facts.created_at if facts is not None else None
        return found

    def test_a_repository_in_two_mechanism_csvs_is_cloned_once(self):
        """The step builds the output CSVs one after another, and a repository can have
        configuration artifacts under more than one mechanism. A run without a cache that
        measured one CSV at a time would clone such a repository once per CSV. The logged line
        alone would not catch a pass that measured once per CSV."""
        self._one_file_in_two_tables()
        with mock.patch.object(_git, "measure_repo", wraps=_git.measure_repo) as measured:
            _, report = self.build_all(offline=False)
        self.assertEqual(measured.call_count, 1)
        self.assertEqual(measured.call_args.args[0], "owner/repo")
        self.assertIn("shared/one.md", measured.call_args.args[4])
        self.assertIn("across 1 repositories", report)
        self.assertNotRegex(report, r"Measuring \d+ row\(s\) of \S+ across",
                            "a CSV fetched from the network after the measuring pass")
        for mechanism in ("hooks", "mcp"):
            _, rows = self.CSV(mechanism)
            self.assertEqual(rows[0]["created_at"], "2021-07-04T09:00:00Z")

    def test_what_the_run_measured_is_saved_to_the_cache_file(self):
        """The cache records what the step cloned for. Every other test saves the cache
        through the fixture's own `fill`, and a `build_all` that never saved it would pass
        those tests."""
        head = self._one_file_in_two_tables()
        self.build_all(offline=False)
        self.assertEqual(self._cached_on_disk(head),
                         {"hooks": "2021-07-04T09:00:00Z", "mcp": "2021-07-04T09:00:00Z"})

    def test_a_mechanism_csv_that_cannot_be_written_keeps_what_was_measured(self):
        """A full disk at the first CSV comes after every clone of the run. Losing the
        measurements with the CSV would mean cloning every repository again."""
        head = self._one_file_in_two_tables()
        with mock.patch.object(_git, "write_table",
                               side_effect=OSError(28, "No space left on device")), \
                self.assertRaises(OSError):
            self.build_all(offline=False)
        self.assertEqual(self._cached_on_disk(head),
                         {"hooks": "2021-07-04T09:00:00Z", "mcp": "2021-07-04T09:00:00Z"})

    def test_an_interrupt_after_the_measuring_keeps_what_was_measured(self):
        """An operator stops a long pass with Ctrl+C, and the step keeps what the pass has
        measured."""
        head = self._one_file_in_two_tables()
        real = _git.measure_missing

        def measure_then_stop(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(_git, "measure_missing", measure_then_stop), \
                self.assertRaises(KeyboardInterrupt), \
                contextlib.redirect_stderr(io.StringIO()):
            self.build_all(offline=False)
        self.assertEqual(self._cached_on_disk(head),
                         {"hooks": "2021-07-04T09:00:00Z", "mcp": "2021-07-04T09:00:00Z"})

    def test_a_repository_that_will_not_clone_is_retried_once_not_once_per_mechanism_csv(self):
        """The measuring pass makes its three attempts. A CSV that then finds the row still
        missing must not start the backoff again."""
        for mechanism in ("hooks", "mcp"):
            filename, artifact = heuristics.MECHANISM_CSVS[mechanism]
            with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.header(mechanism),
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerow(self.base_row(mechanism, repo="gone/repo",
                                              artifact_path="shared/one.md"))
        summaries, _ = self.build_all(offline=False)
        self.assertEqual(self.slept, [10, 20])
        pruned = {s["mechanism"]: s["pruned"] for s in summaries}
        self.assertEqual(pruned["hooks"], 1)
        self.assertEqual(pruned["mcp"], 1)

    def test_a_history_query_that_fails_leaves_the_row_blank(self):
        """Every query method returns a falsy default on an exception, and the code after it
        reads a falsy default as "this file has no history". A row recorded as `0` would read
        as complete, and no later run would measure it again."""
        original = bounds.run_bounded

        def fail_the_count(cmd, *args, **kwargs):
            # The query over every ref of the clone (see history.PATH_QUERIES).
            if cmd[0] == "git" and "log" in cmd and "--all" in cmd:
                raise subprocess.TimeoutExpired(cmd, 1)
            return original(cmd, *args, **kwargs)

        bounds.run_bounded = fail_the_count
        self.addCleanup(setattr, bounds, "run_bounded", original)
        self.write_tables()
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                path.write_text(path.read_text(encoding="utf-8").replace(HEAD, self.head),
                                encoding="utf-8")
        self.fill("hooks", offline=False)
        cache = _facts.FileHistoryCache.load(self.cache_path)
        self.assertEqual(len(cache), 0, "a failed query was cached as a measurement")
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["#commits"], "")

    def test_an_artifact_inside_a_submodule_is_read_from_the_submodule(self):
        """A parent repository tracks a submodule as a gitlink, and `git log` in the parent
        lists nothing for a path below the gitlink. Read from the parent, the row would get no
        history at all, although the submodule has the file."""
        inner = self.root / "inner"
        _init_repo(inner)
        inner_sha = _commit_file(inner, "hooks/one.md", "# inner\n" + "q" * 90,
                                 "add inner", "2021-02-02T09:00:00+00:00")
        _run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
              f"file://{inner}", "vendor"], cwd=self.remote)
        _run(["git", "commit", "-q", "-m", "add submodule"], cwd=self.remote,
             env={**os.environ, "GIT_AUTHOR_DATE": "2021-08-01T09:00:00+00:00",
                  "GIT_COMMITTER_DATE": "2021-08-01T09:00:00+00:00"})
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.remote).stdout.strip()

        filename, artifact = heuristics.MECHANISM_CSVS["hooks"]
        with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.header("hooks"),
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerow(self.base_row("hooks", artifact_path="vendor/hooks/one.md",
                                          head=head))
        os.environ["GIT_CONFIG_COUNT"] = "3"
        os.environ["GIT_CONFIG_KEY_2"] = "protocol.file.allow"
        os.environ["GIT_CONFIG_VALUE_2"] = "always"
        self.fill("hooks", offline=False)
        _, rows = self.CSV("hooks")
        self.assertEqual(rows[0]["created_at"], "2021-02-02T09:00:00Z")
        self.assertEqual(rows[0]["first_commit_sha"], inner_sha)

    def test_one_repository_raising_does_not_end_the_mechanism_csv(self):
        """`future.result()` re-raises the worker's exception. The step catches it, because
        one OSError would otherwise end the CSV that the step is building and every CSV after
        it."""
        original = _git.measure_repo

        def raise_for_one(repo_name, *args, **kwargs):
            if repo_name == "boom/repo":
                raise OSError("no file descriptors")
            return original(repo_name, *args, **kwargs)

        _git.measure_repo = raise_for_one
        self.addCleanup(setattr, _git, "measure_repo", original)
        self.write_tables(extra_rows=[
            ("hooks", self.base_row("hooks", repo="boom/repo",
                                    artifact_path="hooks/one.md"))])
        for path in self.output.glob("*.csv"):
            if path.name != "repos.csv":
                path.write_text(path.read_text(encoding="utf-8").replace(HEAD, self.head),
                                encoding="utf-8")
        summary = self.fill("hooks", offline=False)
        self.assertEqual(summary["rows"], 2, "the CSV was not written")
        self.assertEqual(summary["no_history"], 1)

    def test_a_clone_that_could_not_run_keeps_the_row_instead_of_pruning_it(self):
        """A failure of the run is not a finding about the repository. git's message tells
        the two apart, and a server's response differs between machines. The test asserts on
        the classification of the message, not on a clone."""
        unavailable = _meta.GitFileMetadata()
        unavailable.transient_failures = 1
        unavailable.last_clone_stderr = ("fatal: unable to access: Could not resolve host")
        self.assertEqual(_meta._clone_outcome(unavailable), "git_unavailable")
        gone = _meta.GitFileMetadata()
        gone.last_clone_stderr = ("fatal: '/nowhere' does not appear to be a git "
                                  "repository")
        self.assertEqual(_meta._clone_outcome(gone), "clone_failed")


# ---------------------------------------------------------------------------
# reconcile_repos_csv
# ---------------------------------------------------------------------------
class TestReconcile(_StepCase):
    def _published(self, rows, listed) -> None:
        """Write the mechanism CSVs with `rows`, {mechanism: [(repo_name, path, tool), ...]},
        and a repos.csv that lists `listed` in the form that `write_repos_csv` takes."""
        for mechanism, (filename, _) in heuristics.MECHANISM_CSVS.items():
            with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.header(mechanism),
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerows({**self.base_row(mechanism, repo=repo, artifact_path=path),
                                  "tool": tool}
                                 for repo, path, tool in rows.get(mechanism, ()))
        self.write_repos_csv(listed)

    def _reconciled(self) -> tuple:
        """Run `reconcile_repos_csv` and return (kept, dropped, {repo_name: flags written})."""
        with contextlib.redirect_stdout(io.StringIO()):
            kept, dropped = _git.reconcile_repos_csv(self.output)
        with open(self.output / "repos.csv", newline="", encoding="utf-8") as f:
            written = {row["repo_name"]: {c: row[c] for c in self.FLAG_COLUMNS}
                       for row in csv.DictReader(f)}
        return kept, dropped, written

    def _flags(self, *true) -> dict:
        return {c: "TRUE" if c in true else "FALSE" for c in self.FLAG_COLUMNS}

    def test_the_flags_are_rebuilt_from_the_mechanism_csvs(self):
        """The test asserts the flags and not only the counts, because the counts would pass
        a rebuild that wrote every flag FALSE or kept the flags that the list already had. No
        row sets the stale `copilot` and `skills` flags, and the rebuild must remove them."""
        self._published(
            {"context_file": [("owner/repo", "CLAUDE.md", "claude"),
                              ("agents/only", "AGENTS.md", "any")],
             "rules": [("owner/repo", ".cursor/rules/x.mdc", "cursor")]},
            {"owner/repo": {"copilot": "TRUE", "skills": "TRUE"}, "agents/only": {}})
        kept, dropped, written = self._reconciled()
        self.assertEqual((kept, dropped), (2, []))
        self.assertEqual(written["owner/repo"],
                         self._flags("claude", "cursor", "claude.md", "context_file",
                                     "rules"))
        self.assertEqual(written["agents/only"],
                         self._flags("agents.md", "context_file", "no_specific_tool"))

    def test_the_plugin_and_lsp_flags_follow_their_mechanism_csvs(self):
        """The rebuild derives `plugins` from the rows rather than copying it from the list.
        Otherwise a repository would keep the flag after the pruning removed its plugin rows.
        The rebuild derives `lsp` the same way."""
        self._published(
            {"lsp": [("lsp/repo", ".github/lsp.json", "copilot")],
             "context_file": [("stale/repo", "CLAUDE.md", "claude")]},
            {"lsp/repo": {}, "stale/repo": {"plugins": "TRUE", "lsp": "TRUE"}})
        kept, dropped, written = self._reconciled()
        self.assertEqual((kept, dropped), (2, []))
        self.assertEqual(written["lsp/repo"], self._flags("copilot", "lsp"))
        self.assertEqual(written["stale/repo"],
                         self._flags("claude", "claude.md", "context_file"))

    def test_the_tool_flags_are_the_tools_of_the_rows_not_of_their_paths(self):
        """The path of each row below points to another tool than the row's `tool`, or to
        none. A workflow that runs the Codex action is in `.github/workflows`, which
        `tool_for_path` assigns to Copilot. A Copilot plugin's skill is in a directory that no
        tool owns. A Cursor skill that `.claude/skills` links to counts for Claude as well. A
        rebuild that recomputed the tools from the paths and the tree would set another tool's
        flag for a workflow that runs one tool's action."""
        self._published(
            {"settings": [("wf/repo", ".github/workflows/review.yml", "codex")],
             "plugins": [("cp/repo", "plugins/lint/.github/plugin/plugin.json", "copilot")],
             "skills": [("cp/repo", "plugins/lint/skills/lint/SKILL.md", "copilot"),
                        ("linked/repo", ".cursor/skills/x/SKILL.md", "claude|cursor")]},
            {"wf/repo": {"copilot": "TRUE", "gemini": "TRUE"}, "cp/repo": {},
             "linked/repo": {}})
        kept, dropped, written = self._reconciled()
        self.assertEqual((kept, dropped), (3, []))
        self.assertEqual(written["wf/repo"], self._flags("codex", "settings"))
        self.assertEqual(written["cp/repo"], self._flags("copilot", "skills", "plugins"))
        self.assertEqual(written["linked/repo"], self._flags("claude", "cursor", "skills"))

    def test_an_other_row_sets_no_tool_flag(self):
        """other.csv contains files that no tool reads as configuration, and its rows set no
        tool flag. Otherwise carbon-design-system/carbon's `.github/prompts/*.md` would set the
        repository's `copilot` flag, which no row of the ten mechanisms explains."""
        self._published(
            {"context_file": [("o/a", "AGENTS.md", "any")],
             "other": [("o/a", ".github/prompts/notes.md", "copilot")]},
            {"o/a": {"copilot": "TRUE"}})
        kept, dropped, written = self._reconciled()
        self.assertEqual((kept, dropped), (1, []))
        self.assertEqual(written["o/a"], self._flags("agents.md", "context_file", "other",
                                                     "no_specific_tool"))

    def test_portable_skills_alone_are_no_specific_tool(self):
        """A repository with only configuration that several tools read, such as AGENTS.md,
        `.agents/skills` or a root `.mcp.json`, counts for no specific tool. A tool's own file
        beside the shared configuration gives the repository that tool's flag and not
        `no_specific_tool`."""
        self._published(
            {"skills": [("portable/repo", ".agents/skills/x/SKILL.md", "any"),
                        ("mixed/repo", ".agents/skills/x/SKILL.md", "any")],
             "settings": [("mixed/repo", ".claude/settings.json", "claude")]},
            {"portable/repo": {}, "mixed/repo": {}})
        kept, dropped, written = self._reconciled()
        self.assertEqual((kept, dropped), (2, []))
        self.assertEqual(written["portable/repo"], self._flags("skills", "no_specific_tool"))
        self.assertEqual(written["mixed/repo"], self._flags("claude", "skills", "settings"))

    def test_a_repository_whose_rows_were_all_pruned_is_dropped(self):
        """A repository that lost every configuration artifact to the pruning has no rows
        left. A rebuild that kept the repository would publish it without any
        configuration artifact."""
        self._published({"context_file": [("owner/repo", "CLAUDE.md", "claude")]},
                        {"owner/repo": {}, "pruned/repo": {"claude": "TRUE",
                                                           "claude.md": "TRUE"}})
        kept, dropped, written = self._reconciled()
        self.assertEqual((kept, dropped), (1, ["pruned/repo"]))
        self.assertEqual(sorted(written), ["owner/repo"])

    def test_a_mechanism_csv_without_the_tool_column_stops_the_run(self):
        """A mechanism CSV from a dataset version that predates the `tool` column gives the
        rebuild no tools, and every tool flag would come out FALSE."""
        self._published({"context_file": [("owner/repo", "CLAUDE.md", "claude")]},
                        {"owner/repo": {"claude": "TRUE"}})
        path = self.output / heuristics.MECHANISM_CSVS["hooks"][0]
        header = [c for c in self.header("hooks") if c != "tool"]
        path.write_text(",".join(header) + "\n", encoding="utf-8")
        before = (self.output / "repos.csv").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as caught:
            _git.reconcile_repos_csv(self.output)
        self.assertIn("lacks the tool column", str(caught.exception))
        self.assertEqual((self.output / "repos.csv").read_bytes(), before)

    def test_a_tool_value_detection_cannot_write_stops_the_run(self):
        for value in ("", "Claude", "cursor|claude", "any|claude"):
            with self.subTest(value=value):
                self._published({"context_file": [("owner/repo", "CLAUDE.md", value)]},
                                {"owner/repo": {}})
                with contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaises(ValueError) as caught:
                    _git.reconcile_repos_csv(self.output)
                self.assertIn("not a tool column value", str(caught.exception))

    def _rewrite_fails(self, failure) -> None:
        """Reconcile under `failure`, which raises ENOSPC, and assert that repos.csv is
        unchanged and that no temporary file is left beside it."""
        (self.repos / repos.repo_name_to_dir("owner/repo")).mkdir(parents=True)
        self.seed_cache()
        for mechanism in heuristics.MECHANISM_CSVS:
            self.fill(mechanism)
        before = (self.output / "repos.csv").read_bytes()
        with failure, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(OSError) as caught:
            _git.reconcile_repos_csv(self.output)
        self.assertEqual(caught.exception.errno, 28)
        self.assertEqual((self.output / "repos.csv").read_bytes(), before,
                         "the failed rewrite changed the published list")
        self.assertEqual(sorted(p.name for p in self.output.glob("repos.csv.*")), [])

    def test_a_disk_that_fills_during_the_rewrite_leaves_the_published_list(self):
        """A failure inside the rewrite, such as a full disk, must not leave the published
        list empty or short of rows while the mechanism CSVs stay complete.
        `test_a_mechanism_csv_without_the_tool_column_stops_the_run` fails before the
        temporary file exists, and a writer that rewrote repos.csv in place would pass that
        test too."""
        self._rewrite_fails(mock.patch("os.fdopen", _full_halfway(os.fdopen)))

    def test_a_failed_rename_leaves_the_published_list_as_it_was(self):
        self._rewrite_fails(mock.patch(
            "os.replace", side_effect=OSError(28, "No space left on device")))

    def test_a_header_missing_a_flag_the_rebuild_computes_stops_the_run(self):
        """The shared writer drops a field that the header lacks. In an output/ written under
        older heuristics, repos.csv would lose the new flag unnoticed, because verify_tables
        does not check the header of repos.csv."""
        (self.repos / repos.repo_name_to_dir("owner/repo")).mkdir(parents=True)
        self.seed_cache()
        for mechanism in heuristics.MECHANISM_CSVS:
            self.fill(mechanism)
        # Remove the column from the header and from every row, as in an output/ written
        # under heuristics that predate the flag. The rebuild computes the flag, and the
        # rebuilt row then has a field that the header lacks.
        path = self.output / "repos.csv"
        raw = path.read_bytes().decode("utf-8")
        lines = raw.split("\n")
        header = lines[0].split(",")
        drop = header.index("no_specific_tool")
        kept = [",".join(v for i, v in enumerate(line.split(",")) if i != drop) if line else line
                for line in lines]
        path.write_bytes("\n".join(kept).encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as caught:
            _git.reconcile_repos_csv(self.output)
        self.assertIn("no_specific_tool", str(caught.exception))

    def test_a_row_longer_than_its_header_stops_the_run(self):
        """`DictReader` puts the extra fields of such a row under its restkey, and the shared
        writer would drop them."""
        (self.repos / repos.repo_name_to_dir("owner/repo")).mkdir(parents=True)
        self.seed_cache()
        for mechanism in heuristics.MECHANISM_CSVS:
            self.fill(mechanism)
        path = self.output / "repos.csv"
        raw = path.read_bytes().decode("utf-8")
        lines = raw.split("\n")
        lines[1] = lines[1] + ",extra"
        path.write_bytes("\n".join(lines).encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError) as caught:
            _git.reconcile_repos_csv(self.output)
        self.assertIn("more fields than the header", str(caught.exception))


# ---------------------------------------------------------------------------
# verify_tables
# ---------------------------------------------------------------------------
class TestVerify(_StepCase):
    def setUp(self):
        super().setUp()
        self.seed_cache()
        for mechanism in heuristics.MECHANISM_CSVS:
            self.fill(mechanism)

    def _verify(self) -> tuple:
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            ok = _git.verify_tables(self.output)
        return ok, out.getvalue()

    def test_filled_mechanism_csvs_pass(self):
        ok, _ = self._verify()
        self.assertTrue(ok)

    def test_a_header_the_heuristics_does_not_name_fails(self):
        """The check compares the file with the header that the heuristics declare, the
        header that the detection step writes and that every reader of the CSV expects."""
        path = self.path_of("mcp")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[0] = lines[0].replace("dataset_version", "version")
        path.write_text("".join(lines), encoding="utf-8")
        ok, report = self._verify()
        self.assertFalse(ok)
        self.assertIn("mcp", report)
        self.assertIn("that the heuristics define", report)

    def test_a_blank_value_on_a_measured_row_fails(self):
        path = self.path_of("mcp")
        path.write_text(path.read_text(encoding="utf-8").replace("2026-02-01T10:00:00Z", ""),
                        encoding="utf-8")
        ok, report = self._verify()
        self.assertFalse(ok)
        self.assertIn("created_at", report)

    def test_a_blank_row_is_reported_and_not_failed(self):
        """A row with a history that the pipeline could not read is not an absent
        configuration artifact."""
        self.seed_cache(facts=_facts.FileHistory())
        self.fill("mcp")
        ok, report = self._verify()
        self.assertTrue(ok)
        self.assertIn("Kept 1 row(s) of mcp.csv with blank git columns", report)

    def test_a_created_at_that_the_run_left_blank_passes_and_no_other(self):
        """The run leaves `created_at` blank where git's date does not parse, on a row with a
        measured history. Such a row passes the check where the run names it, and the same
        blank on another row still fails."""
        self.seed_cache(facts=_facts.FileHistory("9999-12-31T23:30:00-01:00", "4", "c" * 40,
                                                 "d" * 40))
        with contextlib.redirect_stderr(io.StringIO()):
            summary = self.fill("mcp")
        undated = frozenset(("mcp", *key) for key in summary["undated"])
        self.assertEqual(undated, {("mcp", "owner/repo", "mcp/one.md")})
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertTrue(_git.verify_tables(self.output, undated))
            self.assertFalse(_git.verify_tables(self.output))
        self.assertIn("1 measured row(s) of mcp.csv have a blank created_at", out.getvalue())


class TestDatesThatDoNotParse(_StepCase):
    """git prints a commit of the year 10000 in UTC as `9999-12-31T23:30:00-01:00` in the
    offset -0100, and an instant 30 minutes before the year 10000 in UTC as
    `10000-01-01T00:30:00+01:00` in the offset +0100. Neither parses, and the cache keeps git's
    text. If the step refused such a date, one such repository would stop the step at every
    Start, and the run would never reach the release."""

    def setUp(self):
        super().setUp()
        self.remote = self.root / "remote"
        _init_repo(self.remote)
        self.creation = _commit_file(self.remote, "hooks/one.md", "# hooks\n", "add hooks",
                                     "@253402302600 -0100")
        self.head = _commit_file(self.remote, "other.txt", "other\n", "unrelated",
                                 "@253402299000 +0100")
        self.created_at = _run(["git", "log", "-1", "--format=%cI", self.creation],
                               cwd=self.remote).stdout.strip()
        self.head_commit_date = _run(["git", "show", "-s", "--format=%cI", "HEAD"],
                                     cwd=self.remote).stdout.strip()
        token = os.environ.pop("GITHUB_TOKEN", None)
        if token is not None:
            self.addCleanup(os.environ.__setitem__, "GITHUB_TOKEN", token)
        # The step clones `owner/repo` from the local repository above, and nothing reaches a
        # network. The cache alone fills the rows of `other/repo`.
        os.environ.update({"GIT_CONFIG_COUNT": "1",
                           "GIT_CONFIG_KEY_0": f"url.file://{self.remote}.insteadOf",
                           "GIT_CONFIG_VALUE_0": "https://github.com/owner/repo.git"})
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.addCleanup(os.environ.pop, key, None)
        for mechanism, (filename, _) in _git.HISTORY_TABLES.items():
            rows = [self.base_row(mechanism, repo="other/repo")]
            if mechanism == "hooks":
                rows.append(self.base_row(mechanism, artifact_path="hooks/one.md",
                                          head=self.head)
                            | {"head_commit_date": self.head_commit_date})
            with open(self.output / filename, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.header(mechanism),
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
        self.write_repos_csv({"owner/repo": {"hooks": "TRUE"}, "other/repo": {}})
        for path in (self.collected_csv, self.validated_csv):
            self.write_list(path, [("owner/repo", self.head), ("other/repo", HEAD)])
        cache = _facts.FileHistoryCache()
        for mechanism in _git.HISTORY_TABLES:
            cache.put(_facts.FileHistoryKey("other/repo", HEAD, mechanism,
                                            f"{mechanism}/one.md"), MEASURED)
        cache.save(self.cache_path)

    def _main(self, *extra) -> tuple:
        argv = ["--output-dir", str(self.output), "--validated-dir", str(self.repos),
                "--cache", str(self.cache_path),
                "--ai-authorship", str(self.root / "ai_authorship.csv"),
                "--clone-dir", str(self.root / "clones"), *extra]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _git.main(argv)
        return code, err.getvalue()

    def test_the_dates_are_blank_and_the_step_exits_0(self):
        self.assertEqual((self.created_at, self.head_commit_date),
                         ("9999-12-31T23:30:00-01:00", "10000-01-01T00:30:00+01:00"))
        code, log = self._main()
        self.assertEqual(code, 0, log)
        _, rows = self.CSV("hooks")
        late = next(row for row in rows if row["repo_name"] == "owner/repo")
        self.assertEqual({c: late[c] for c in ("head_commit_date", *_git.FILLED_COLUMNS)},
                         {"head_commit_date": "", "created_at": "", "#commits": "1",
                          "first_commit_sha": self.creation,
                          "last_commit_sha": self.creation})
        for mechanism in _git.HISTORY_TABLES:
            with self.subTest(mechanism=mechanism):
                _, rows = self.CSV(mechanism)
                other = next(row for row in rows if row["repo_name"] == "other/repo")
                self.assertEqual((other["head_commit_date"], other["created_at"]),
                                 (HEAD_COMMIT_DATE, MEASURED.created_at))
        warnings = [line for line in log.splitlines()
                    if line.startswith("WARNING file_history: ") and "blank" in line]
        self.assertEqual(len(warnings), 1, log)
        self.assertIn("hooks.csv had 2 created_at and head_commit_date value(s) that are not",
                      warnings[0])
        self.assertTrue(warnings[0].endswith("in 1 repositories (owner/repo)."), warnings[0])

    def test_the_cache_keeps_git_s_text_so_an_offline_run_repeats_the_output(self):
        """A blank in the cache records a history that was not read. A run clones for such a
        row again, and an offline run refuses it."""
        self.assertEqual(self._main()[0], 0)
        cache = _facts.FileHistoryCache.load(self.cache_path)
        facts = cache.get(_facts.FileHistoryKey("owner/repo", self.head, "hooks",
                                                "hooks/one.md"))
        self.assertEqual(facts.created_at, self.created_at)
        self.assertTrue(facts.is_complete())
        written = {mechanism: self.path_of(mechanism).read_bytes()
                   for mechanism in _git.HISTORY_TABLES}
        code, log = self._main("--offline")
        self.assertEqual(code, 0, log)
        self.assertEqual({mechanism: self.path_of(mechanism).read_bytes()
                          for mechanism in _git.HISTORY_TABLES}, written)


# ---------------------------------------------------------------------------
# Against the committed sample
# ---------------------------------------------------------------------------
class TestTheCommittedMechanismCsvs(unittest.TestCase):
    """The cached file histories must reproduce the current output rows, the rows of the
    planning documents included.

    The cloning step measures paths before the detection step assigns mechanisms. A row can
    be filled from the cache row of its exact mechanism or from the path history at the same
    commit.
    """

    STAGE = STAGE_DIR

    @classmethod
    def setUpClass(cls):
        for name in ("output/skills.csv", "file_history.csv", "data/collected/repos.csv",
                     "data/validated/repos.csv"):
            if not (cls.STAGE / name).exists():
                raise unittest.SkipTest(f"{name} has not been generated")

    def test_every_published_row_has_an_entry_in_the_cache(self):
        cache = _facts.FileHistoryCache.load(_facts.FILE_HISTORY_PATH)
        missing = []
        for mechanism, (filename, artifact) in _git.HISTORY_TABLES.items():
            with open(self.STAGE / "output" / filename, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = _facts.FileHistoryKey(row["repo_name"], row["head_sha"], mechanism,
                                            row[artifact])
                    facts = cache.get(key) or cache.for_artifact(
                        row["repo_name"], row["head_sha"], row[artifact])
                    if facts is None:
                        missing.append(key)
        self.assertEqual(missing, [], "an offline run would have to clone for these")

    def test_every_published_header_is_the_one_the_heuristics_declare(self):
        for mechanism, (filename, _) in _git.HISTORY_TABLES.items():
            with self.subTest(CSV=filename):
                with open(self.STAGE / "output" / filename, newline="",
                          encoding="utf-8") as f:
                    header = next(csv.reader(f), [])
                self.assertEqual(header, _git.table_header(mechanism))

    def test_an_offline_run_rewrites_the_committed_mechanism_csvs_byte_for_byte(self):
        """The test covers every mechanism CSV and reads and writes nothing outside the
        temporary copy. It copies the caches in, and `repos/` is an empty directory, which an
        offline run never needs because none of its rows can reach a clone. This step fills
        the AI columns from `ai_authorship.csv` with the values that the detection step joined
        from that file, and the join here therefore changes no byte of `context-files.csv`
        either. The caches keep only the rows at the heads of the committed lists, and these
        rows still fill every output row."""
        with tempfile.TemporaryDirectory() as tmp:
            produced = Path(tmp) / "output"
            shutil.copytree(self.STAGE / "output", produced)
            cache_path = Path(tmp) / "file_history.csv"
            shutil.copyfile(_facts.FILE_HISTORY_PATH, cache_path)
            ai_cache_path = Path(tmp) / "ai_authorship.csv"
            shutil.copyfile(_facts.AI_AUTHORSHIP_PATH, ai_cache_path)
            repos = Path(tmp) / "repos"
            repos.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                _git.build_all(produced, cache_path, repos, 1, None, offline=True,
                               retry_missing_history=False, ai_cache_path=ai_cache_path,
                               heads=_git.current_heads(
                                   self.STAGE / "data" / "collected" / "repos.csv",
                                   self.STAGE / "data" / "validated" / "repos.csv"))
            for filename, _ in _git.HISTORY_TABLES.values():
                with self.subTest(CSV=filename):
                    self.assertEqual((produced / filename).read_bytes(),
                                     (self.STAGE / "output" / filename).read_bytes())


class TestTheStopHandler(unittest.TestCase):
    """A stop in this step arrives during the pass in which every clone happens, before the
    first save. The handler catches SIGINT as well as SIGTERM, because the runner starts the
    step in the caller's process group, where Ctrl+C reaches it directly."""

    def test_both_signals_are_handled_and_restored(self):
        import signal
        before = {sig: signal.getsignal(sig)
                  for sig in (signal.SIGTERM, signal.SIGINT)}
        seen = {}

        def spy(args):
            for sig in (signal.SIGTERM, signal.SIGINT):
                seen[sig] = signal.getsignal(sig)
            return 7

        with mock.patch.object(_git, "_run", spy), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = _git.main([])
        self.assertEqual(code, 7)
        self.assertEqual(set(seen), {signal.SIGTERM, signal.SIGINT})
        for sig, handler in seen.items():
            with self.subTest(signal=sig):
                self.assertNotIn(handler, (signal.SIG_DFL, signal.default_int_handler),
                                 "the step ran under the default disposition")
        for sig, handler in before.items():
            with self.subTest(signal=sig):
                self.assertIs(signal.getsignal(sig), handler)

    def test_the_handler_kills_the_commands_in_flight_before_raising(self):
        """A raise alone would leave the process waiting for the clone. The pool consists of
        threads, and a thread cannot be terminated. Cancelling futures drops only the work that
        has not started."""
        import signal
        killed = []
        with mock.patch.object(_git.bounds, "stop_running",
                               lambda: killed.append(True) or 0):
            handler = None

            def capture(args):
                nonlocal handler
                handler = signal.getsignal(signal.SIGTERM)
                return 0

            with mock.patch.object(_git, "_run", capture), \
                    contextlib.redirect_stdout(io.StringIO()):
                _git.main([])
            self.assertTrue(callable(handler))
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
        self.assertEqual(killed, [True], "the running commands were not stopped")


class TestTheStopIsNotReentered(unittest.TestCase):
    """Ctrl+C reaches this step twice, once from the process group and once forwarded by
    the runner. The second signal can arrive during the cache save that the first signal
    began. `replace_file` would then unlink its temporary file and re-raise, and what the
    clones measured, which the save exists to keep, would be lost. The handler therefore
    returns on every signal after the first."""

    def test_a_second_signal_is_ignored_and_the_stop_runs_once(self):
        stops = []

        def fake_run(args):
            handler = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)        # the runner's signal, after the group's
            handler(signal.SIGINT, None)         # and the other signal
            return 7

        with (mock.patch.object(_git, "_run", fake_run),
              mock.patch.object(_git.bounds, "stop_running", lambda: stops.append(1)),
              mock.patch.object(_git.os, "_exit") as exit_):
            _git.main([])
        self.assertEqual(len(stops), 1, "the stop ran again during the cache save")
        exit_.assert_called_once_with(7)

    def test_a_stopped_run_removes_its_credential_helper(self):
        # A stopped run ends with `os._exit`, which runs no `atexit` handler. The step has to
        # remove the helper that passes the GitHub access token to git before that call, or
        # the helper stays in the temporary directory.
        created = []

        def fake_run(args):
            from agentcontext.git import auth
            created.append(auth.askpass_script())
            with self.assertRaises(KeyboardInterrupt):
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 130

        with (mock.patch.object(_git, "_run", fake_run),
              mock.patch.object(_git.bounds, "stop_running", lambda: 0),
              mock.patch.object(_git.os, "_exit") as exit_):
            _git.main([])
        exit_.assert_called_once_with(130)
        self.assertFalse(os.path.exists(created[0]))

    def test_the_previous_dispositions_are_restored(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        with mock.patch.object(_git, "_run", lambda args: 0):
            _git.main([])
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)


class TestTheStaleTemporary(unittest.TestCase):
    """`write_table` names its temporary file with `mkstemp`, and the name is random. The
    branch that removes the file covers a raise, but not a SIGKILL, an OOM kill or the
    `os._exit` with which a stop handler ends the process. The next run picks another name,
    and a leftover file would stay. Because these CSVs are tracked, a leftover file fails
    `release`'s inventory and shows in the next `git status`."""

    def test_a_stray_temporary_file_beside_the_csv_is_removed_before_the_csv_is_written(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        CSV = tmp / "context-files.csv"
        stray = tmp / "context-files.csv.abc12345.tmp"
        stray.write_text("what a killed run left\n", encoding="utf-8")
        _git.write_table(CSV, ["repo_name"], [{"repo_name": "o/r"}])
        self.assertFalse(stray.exists(), "the leftover temporary file remained after the write")
        self.assertEqual([p.name for p in tmp.iterdir()], ["context-files.csv"])


class TestTheRewrittenCsvsReachTheDisk(unittest.TestCase):
    """This step rewrites the CSVs of the detection step in place before the runner records it.
    If a rename reaches the disk before the data, a power loss leaves an empty CSV. The step
    then cannot read the CSV at any later Start, while the detection step stays recorded. The
    step syncs the data before the rename and the directory after it."""

    def _events(self, write) -> tuple:
        """The syncs and renames of `write`, with the kind and the inode of each synced file."""
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            info = os.fstat(fd)
            events.append(("sync", stat.S_ISDIR(info.st_mode), info.st_ino, info.st_size))
            real_fsync(fd)

        def replace(source, destination):
            events.append(("rename",))
            real_replace(source, destination)

        with mock.patch("os.fsync", fsync), mock.patch("os.replace", replace):
            write()
        return events

    def test_write_table_and_write_retained_sync_the_file_and_the_directory(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        table = tmp / "skills.csv"
        commits = tmp / "all_ai_commits.csv"
        commits.write_text("repo_name,commit_sha\r\no/a,1\r\no/b,2\r\n", encoding="utf-8")
        for name, path, write in (
                ("write_table", table,
                 lambda: _git.write_table(table, ["repo_name"], [{"repo_name": "o/r"}])),
                ("write_retained", commits, lambda: _git.write_retained(commits, {"o/a"}))):
            with self.subTest(writer=name):
                events = self._events(write)
                written = path.stat()
                self.assertEqual(events, [("sync", False, written.st_ino, written.st_size),
                                          ("rename",),
                                          ("sync", True, tmp.stat().st_ino, mock.ANY)])


class TestAStopDuringTheFirstSaveKeepsThePass(_StepCase):
    """A stop during the first save of the caches after the measuring pass keeps both caches,
    because the step saves inside the `try` that saves again on a stop. A row of the git cache
    without its row of the AI cache would leave the AI columns of the path blank, since no
    lookup of a later run reads the AI cache."""

    RECORD = {"hash": "c" * 40, "author_name": "a", "author_email": "a@example.invalid",
              "committer_name": "a", "committer_email": "a@example.invalid",
              "author_ts": "1700000000", "committer_date": "2026-02-01T10:00:00Z",
              "subject": "Add CLAUDE.md", "body": "Co-Authored-By: Claude <noreply@anthropic.com>"}

    def test_a_stop_at_each_write_of_the_first_save_keeps_both_caches(self):
        # The data and then the directory of ai_authorship.csv, then of file_history.csv.
        for stop_at in (1, 2, 3, 4):
            with self.subTest(stop_at=stop_at):
                self.setUp()
                self._stop_at(stop_at)

    def _stop_at(self, stop_at: int) -> None:
        row = self.base_row('context_file', repo='new/repo', artifact_path='CLAUDE.md')
        for column in ("first_commit_ai_created", "first_commit_ai_tool", "#ai_commits",
                       "ai_commit_tools"):
            row[column] = ""
        self.write_tables(extra_rows=[('context_file', row)])
        self.seed_cache()
        self.write_repos_csv({'owner/repo': {}, 'new/repo': {}})
        for name in ('owner§repo', 'new§repo'):
            (self.repos / name).mkdir(exist_ok=True)
        heads = [("owner/repo", HEAD), ("new/repo", HEAD)]
        self.write_list(self.collected_csv, heads)
        self.write_list(self.validated_csv, heads)
        ai_path = self.root / 'ai_authorship.csv'
        measured = []

        def measure(repo_name, head_sha, date, branch, artifacts, token, sleep, scratch,
                    path_histories):
            measured.append(repo_name)
            path_histories['CLAUDE.md'] = _meta.PathCommits([self.RECORD], [self.RECORD],
                                                            [self.RECORD])
            return {'CLAUDE.md': MEASURED}, ''

        argv = ['--output-dir', str(self.output), '--validated-dir', str(self.repos),
                '--collected-csv', str(self.collected_csv), '--cache', str(self.cache_path),
                '--ai-authorship', str(ai_path), '--no-verify']
        real_fsync, calls = os.fsync, []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == stop_at:
                os.kill(os.getpid(), signal.SIGTERM)
            return real_fsync(fd)

        with mock.patch.object(_git, 'measure_repo', side_effect=measure), \
                mock.patch.object(_git.bounds, 'stop_running', lambda: 0), \
                mock.patch.object(os, 'fsync', fsync), \
                mock.patch.object(_git.os, '_exit'), \
                contextlib.redirect_stderr(io.StringIO()):
            _git.main(argv)
        self.assertEqual(measured, ['new/repo'])
        self.assertEqual(_facts.FileHistoryCache.load(self.cache_path).for_artifact(
            'new/repo', HEAD, 'CLAUDE.md'), MEASURED)
        measured.clear()
        with mock.patch.object(_git, 'measure_repo', side_effect=measure), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_git.main(argv), 0)
        _, rows = self.CSV('context_file')
        published = [r for r in rows if r['repo_name'] == 'new/repo'][0]
        self.assertEqual(published["#ai_commits"], "1")


class TestAnOutageOfTheNetwork(_StepCase):
    """An outage of the network or of GitHub's git service during the pass leaves the rows that
    the step measures blank. If the step exited 0, the runner would record it as complete, and
    the release would publish the blank rows. Like the cloning step, this step therefore
    measures those repositories again in retry rounds, and while the network does not respond,
    again after each wait. It exits 4 when the network never responds."""

    def _main(self, outcomes: dict, responds: list, on_probe=lambda: None) -> tuple:
        """Run the step over `owner/repo`, which the cache fills, and over a `CLAUDE.md` of
        each repository in `outcomes`, {repo_name: [what each measurement returns]}.
        `responds` lists what each probe of the network returns. Return the exit code, the
        repositories measured in order, the pauses and waits, and what the step logged."""
        names = ['owner/repo', *outcomes]
        self.write_tables(extra_rows=[
            ('context_file', self.base_row('context_file', repo=name, artifact_path='CLAUDE.md'))
            for name in outcomes])
        self.seed_cache()
        self.write_repos_csv({name: {} for name in names})
        for name in names:
            (self.repos / name.replace('/', '§')).mkdir(exist_ok=True)
        self.write_list(self.collected_csv, [(name, HEAD) for name in names])
        self.write_list(self.validated_csv, [(name, HEAD) for name in names])
        measured, slept = [], []

        def measure(repo_name, *args):
            measured.append(repo_name)
            return outcomes[repo_name].pop(0)

        def probe(*args, **kwargs):
            on_probe()
            return responds.pop(0)

        argv = ['--output-dir', str(self.output), '--validated-dir', str(self.repos),
                '--collected-csv', str(self.collected_csv), '--cache', str(self.cache_path),
                '--ai-authorship', str(self.root / 'ai_authorship.csv'), '--no-verify']
        err = io.StringIO()
        with mock.patch.object(_git, 'measure_repo', side_effect=measure), \
                mock.patch.object(_git.guards, 'remote_responds', side_effect=probe), \
                mock.patch.object(_git.guards, 'PROBE_WAITS', (60, 120)), \
                mock.patch.object(_git, 'build_all',
                                  functools.partial(_git.build_all, sleep=slept.append)), \
                contextlib.redirect_stderr(err):
            code = _git.main(argv)
        return code, measured, slept, err.getvalue()

    def cached(self, repo_name: str):
        """What the cache on disk holds for the `CLAUDE.md` of `repo_name`."""
        return _facts.FileHistoryCache.load(self.cache_path).for_artifact(repo_name, HEAD,
                                                                          'CLAUDE.md')

    def test_a_spell_of_failures_that_ends_before_the_pass_does_is_measured_again(self):
        """GitHub returns HTTP 400 to fetches for a short time, and the probe after the pass
        responds. Only a second attempt fills the row."""
        code, measured, slept, _err = self._main(
            {'new/repo': [({}, 'git_unavailable'), ({'CLAUDE.md': MEASURED}, '')]}, [])
        self.assertEqual((code, measured), (0, ['new/repo', 'new/repo']))
        self.assertEqual(slept, [_git.guards.RETRY_PAUSE_SECONDS])
        self.assertEqual(self.cached('new/repo'), MEASURED)

    def test_a_clone_that_a_bound_stopped_is_measured_once(self):
        code, measured, slept, _err = self._main({'new/repo': [({}, 'bound_exceeded')]}, [])
        self.assertEqual((code, measured, slept), (0, ['new/repo'], []))

    def test_an_outage_that_lasts_ends_the_step_with_exit_4(self):
        code, measured, slept, err = self._main(
            {'new/repo': [({}, 'git_unavailable')] * 3}, [False, False, False])
        self.assertEqual((code, measured), (4, ['new/repo'] * 3))
        pause = _git.guards.RETRY_PAUSE_SECONDS
        self.assertEqual(slept, [pause, pause, 60, 120])
        self.assertIn("did not respond after the waits of", err)
        self.assertIsNone(self.cached('new/repo'))

    def test_an_outage_that_passes_measures_the_repositories_again(self):
        code, measured, _slept, _err = self._main(
            {'new/repo': [({}, 'git_unavailable')] * 3 + [({'CLAUDE.md': MEASURED}, '')]},
            [False, True])
        self.assertEqual((code, measured), (0, ['new/repo'] * 4))
        self.assertEqual(self.cached('new/repo'), MEASURED)

    def test_a_failure_of_the_repository_alone_leaves_the_row_blank(self):
        code, measured, _slept, _err = self._main(
            {'new/repo': [({}, 'git_unavailable')] * 3}, [True])
        self.assertEqual((code, measured), (0, ['new/repo'] * 3))
        self.assertIsNone(self.cached('new/repo'))

    def test_the_measurements_of_the_pass_are_on_disk_during_the_waits(self):
        """A power loss or a kill during hours of waits would otherwise lose every measurement
        of the pass, and the next Start would clone those repositories again."""
        on_disk = []
        code, _measured, _slept, _err = self._main(
            {'done/repo': [({'CLAUDE.md': MEASURED}, '')],
             'new/repo': [({}, 'git_unavailable')] * 3},
            [False, False, False], on_probe=lambda: on_disk.append(self.cached('done/repo')))
        self.assertEqual(code, 4)
        self.assertEqual(on_disk, [MEASURED] * 3)

    def test_what_a_retry_round_measures_is_on_disk_during_the_waits(self):
        on_disk = []
        code, _measured, _slept, _err = self._main(
            {'late/repo': [({}, 'git_unavailable'), ({'CLAUDE.md': MEASURED}, '')],
             'new/repo': [({}, 'git_unavailable')] * 3},
            [False, False, False], on_probe=lambda: on_disk.append(self.cached('late/repo')))
        self.assertEqual(code, 4)
        self.assertEqual(on_disk, [MEASURED] * 3)


class TestAFailureReachesTheOperatorAsALine(unittest.TestCase):
    """Every error on which this step can end is an `ERROR:` line and exit 1, not a
    traceback.

    The documented exit codes of the step state this property. `reconcile_repos_csv` reads
    the tree that this run has just pruned and the output CSVs that it has just written. It
    raises the same exception classes from the same causes as the loop before it, and the
    step turns them into a line the same way."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output = self.tmp / "output"
        self.output.mkdir()
        self.validated = self.tmp / "validated"
        self.validated.mkdir()
        for filename, _ in _git.HISTORY_TABLES.values():
            (self.output / filename).write_text("repo_name\n", encoding="utf-8")
        (self.output / "repos.csv").write_text("repo_name\n", encoding="utf-8")
        _StepCase.write_list(self.tmp / "data" / "collected" / "repos.csv", [])
        _StepCase.write_list(self.validated / "repos.csv", [])

    def _argv(self):
        return ["--output-dir", str(self.output), "--validated-dir", str(self.validated),
                "--cache", str(self.tmp / "file_history.csv"), "--offline"]

    def test_a_blank_row_is_retried_unless_the_run_is_offline_or_told_to_keep_it(self):
        """The retry is the default rather than a flag that no run passes. Otherwise the rows
        that a record left blank would be published blank on every run."""
        signature = inspect.signature(_git.build_all)
        argv = ["--output-dir", str(self.output), "--validated-dir", str(self.validated),
                "--cache", str(self.tmp / "file_history.csv")]
        for extra, retry in (([], True), (["--keep-blank"], False), (["--offline"], False)):
            with self.subTest(argv=extra):
                with (mock.patch.object(_git, "build_all", return_value=[]) as build,
                      mock.patch.object(_git, "reconcile_repos_csv", return_value=(0, [])),
                      contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    _git.main(argv + extra)
                bound = signature.bind(*build.call_args.args, **build.call_args.kwargs).arguments
                self.assertEqual(bound["retry_missing_history"], retry)

    def test_an_offline_exception_makes_the_run_offline_for_everything_else(self):
        signature = inspect.signature(_git.build_all)
        argv = ["--output-dir", str(self.output), "--validated-dir", str(self.validated),
                "--cache", str(self.tmp / "file_history.csv"),
                "--offline-except", "o/a", "--offline-except", "o/b"]
        with (mock.patch.object(_git, "build_all", return_value=[]) as build,
              mock.patch.object(_git, "reconcile_repos_csv", return_value=(0, [])),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            _git.main(argv)
        bound = signature.bind(*build.call_args.args, **build.call_args.kwargs).arguments
        self.assertIs(bound["offline"], True)
        self.assertIs(bound["retry_missing_history"], False)
        self.assertEqual(bound["offline_except"], frozenset({"o/a", "o/b"}))

    def test_each_error_class_of_the_except_clause_is_a_line_and_exit_one(self):
        """The text of an OSError and of a KeyError names only the file or the column, and the
        line says which operation failed. A ValueError of the step is a sentence already."""
        for error, line in (
                (OSError(13, "Permission denied"),
                 "Could not read or write a file of the step ([Errno 13] Permission denied)."),
                (ValueError("The header of x.csv lacks head_sha."),
                 "The header of x.csv lacks head_sha."),
                (KeyError("head_sha"), "An input CSV of the step lacks the column 'head_sha'.")):
            with self.subTest(error=type(error).__name__):
                err = io.StringIO()
                with (mock.patch.object(_git, "reconcile_repos_csv", side_effect=error),
                      mock.patch.object(_git, "build_all", return_value=[]),
                      contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(err)):
                    code = _git.main(self._argv())
                self.assertEqual(code, 1)
                self.assertEqual(err.getvalue().splitlines()[-1], f"ERROR file_history: {line}",
                                 err.getvalue()[-200:])


class TestBuildAllPreservesRowsAfterHistoryFailures(_StepCase):

    def test_unavailable_history_preserves_prior_values_and_repository(self):
        self.seed_cache()
        self.fill('hooks')
        self.cache_path.unlink()
        self.seed_cache(mechanisms=[m for m in heuristics.MECHANISM_CSVS if m != 'hooks'])
        repo = self.repos / 'owner§repo'
        repo.mkdir()
        with mock.patch.object(_git, 'measure_repo', return_value=({}, 'git_unavailable')):
            summaries, _ = self.build_all(offline=False)
        hooks = next(s for s in summaries if s['mechanism'] == 'hooks')
        self.assertEqual((hooks['pruned'], hooks['no_history']), (0, 1))
        _, rows = self.CSV('hooks')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['created_at'], '2026-02-01T10:00:00Z')
        self.assertEqual(rows[0]['#commits'], '4')
        self.assertEqual(rows[0]['first_commit_sha'], 'c' * 40)
        self.assertEqual(rows[0]['last_commit_sha'], 'd' * 40)
        self.assertTrue(repo.is_dir())

    def test_worker_exception_preserves_the_row_with_blank_git_columns(self):
        self.write_tables(extra_rows=[('hooks', self.base_row(
            'hooks', repo='boom/repo', artifact_path='hooks/one.md'))])
        self.seed_cache()
        with mock.patch.object(_git, 'measure_repo', side_effect=OSError('fixture failure')):
            summaries, _ = self.build_all(offline=False)
        hooks = next(s for s in summaries if s['mechanism'] == 'hooks')
        self.assertEqual((hooks['rows'], hooks['no_history']), (2, 1))
        _, rows = self.CSV('hooks')
        self.assertEqual({r['repo_name'] for r in rows}, {'owner/repo', 'boom/repo'})
        self.assertEqual(next(r for r in rows if r['repo_name'] == 'boom/repo')['created_at'], '')


class TestARemovalThatDsmWritesInto(_StepCase):
    """DSM writes an `@eaDir` into a directory of the validated tree at any time, also into
    the tree of a repository that git reports as gone while the step removes that tree. A
    plain removal would then raise ENOTEMPTY, and the step would exit 1 after its clones."""

    def test_the_tree_is_removed_and_the_step_exits_0(self):
        self.write_tables(extra_rows=[('context_file', self.base_row(
            'context_file', repo='gone/repo', artifact_path='CLAUDE.md'))])
        self.seed_cache()
        self.write_repos_csv({'owner/repo': {}, 'gone/repo': {}})
        (self.repos / 'owner§repo').mkdir()
        gone = self.repos / 'gone§repo'
        (gone / '.claude').mkdir(parents=True)
        (gone / 'CLAUDE.md').write_text('# gone\n', encoding='utf-8')
        (gone / '.claude' / 'settings.json').write_text('{}\n', encoding='utf-8')
        argv = ["--output-dir", str(self.output), "--validated-dir", str(self.repos),
                "--cache", str(self.cache_path), "--ai-authorship",
                str(self.root / "ai_authorship.csv"), "--no-verify"]
        with (mock.patch.object(_git, 'measure_repo', return_value=({}, 'clone_failed')),
              dsm_writes_during_removal('settings.json') as written,
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(_git.main(argv), 0)
        self.assertEqual(len(written), 1)
        self.assertFalse(gone.exists())
        _, context = self.CSV('context_file')
        self.assertEqual({row['repo_name'] for row in context}, {'owner/repo'})


class TestPrunedRepositoriesLeaveCommitOutputs(_StepCase):

    def test_both_commit_outputs_drop_only_the_removed_repository(self):
        self.write_tables(extra_rows=[('context_file', self.base_row(
            'context_file', repo='gone/repo', artifact_path='CLAUDE.md'))])
        self.seed_cache()
        self.write_repos_csv({'owner/repo': {}, 'gone/repo': {}})
        for name in ('owner§repo', 'gone§repo'):
            (self.repos / name).mkdir()
        columns = ['repo_name', 'commit_sha', 'matched_heuristics', 'commit_message']
        kept = {'repo_name':'owner/repo', 'commit_sha':'a'*40,
                'matched_heuristics':'claude-trailer-07;claude-full-body-07', 'commit_message':'A, message\\nbody'}
        for filename in ('all_ai_commits.csv', 'other_ai_commits.csv'):
            with (self.output / filename).open('w', newline='') as out:
                writer = csv.DictWriter(out, fieldnames=columns)
                writer.writeheader()
                writer.writerows([kept, {**kept, 'repo_name':'gone/repo'}])
        with mock.patch.object(_git, 'measure_repo', return_value=({}, 'clone_failed')):
            self.build_all(offline=False)
        _git.reconcile_repos_csv(self.output)
        self.assertFalse((self.repos / 'gone§repo').exists())
        for filename in ('repos.csv', 'all_ai_commits.csv', 'other_ai_commits.csv'):
            with self.subTest(filename=filename), (self.output / filename).open(newline='') as inp:
                reader = csv.DictReader(inp)
                rows = list(reader)
                self.assertEqual([row['repo_name'] for row in rows], ['owner/repo'])
                if filename != 'repos.csv':
                    self.assertEqual(reader.fieldnames, columns)
                    self.assertEqual(rows, [kept])

    def test_a_failed_removal_of_a_gone_repository_keeps_the_measured_rows(self):
        """The step saves the caches before it removes the repositories that git reports as
        gone. If it saved them only after the removal, a stop or a failed removal would discard
        a whole measuring pass, and the next Start would clone every repository of the pass
        again."""
        self.write_tables(extra_rows=[
            ('context_file', self.base_row('context_file', repo='new/repo',
                                           artifact_path='CLAUDE.md')),
            ('context_file', self.base_row('context_file', repo='gone/repo',
                                           artifact_path='AGENTS.md'))])
        self.seed_cache()
        self.write_repos_csv({'owner/repo': {}, 'new/repo': {}, 'gone/repo': {}})
        for name in ('owner§repo', 'new§repo', 'gone§repo'):
            (self.repos / name).mkdir()

        def measure(repo_name, *args, **kwargs):
            return ({}, 'clone_failed') if repo_name == 'gone/repo' else ({'CLAUDE.md': MEASURED}, '')

        with mock.patch.object(_git, 'measure_repo', side_effect=measure), \
                mock.patch.object(_git, '_prune_from_repos',
                                  side_effect=OSError(5, "Input/output error")), \
                self.assertRaises(OSError):
            self.build_all(offline=False)
        self.assertEqual(_facts.FileHistoryCache.load(self.cache_path).for_artifact(
            'new/repo', HEAD, 'CLAUDE.md'), MEASURED)

    def test_a_rerun_after_a_stop_before_the_commit_outputs_drops_their_rows(self):
        """The first run finds `gone/repo` gone, prunes it from the mechanism CSVs and stops on
        a full disk at the first commit CSV. The rerun has no miss for that repository and
        finds nothing gone. It still has to drop the repository's commit rows, which would
        otherwise stay for good while the rerun exits 0."""
        self.write_tables(extra_rows=[('context_file', self.base_row(
            'context_file', repo='gone/repo', artifact_path='CLAUDE.md'))])
        self.seed_cache()
        self.write_repos_csv({'owner/repo': {}, 'gone/repo': {}})
        for name in ('owner§repo', 'gone§repo'):
            (self.repos / name).mkdir()
        columns = ['repo_name', 'commit_sha']
        for filename in ('all_ai_commits.csv', 'other_ai_commits.csv'):
            with (self.output / filename).open('w', newline='') as out:
                writer = csv.DictWriter(out, fieldnames=columns)
                writer.writeheader()
                writer.writerows([{'repo_name': 'owner/repo', 'commit_sha': '1' * 40},
                                  {'repo_name': 'gone/repo', 'commit_sha': '2' * 40}])
        argv = ["--output-dir", str(self.output), "--validated-dir", str(self.repos),
                "--cache", str(self.cache_path), "--ai-authorship",
                str(self.root / "ai_authorship.csv"), "--no-verify"]
        real_write = _git.write_retained

        def full_at_the_commit_outputs(path, *args, **kwargs):
            if Path(path).name == 'all_ai_commits.csv':
                raise OSError(28, "No space left on device")
            return real_write(path, *args, **kwargs)

        with (mock.patch.object(_git, 'measure_repo', return_value=({}, 'clone_failed')),
              mock.patch.object(_git, 'write_retained', full_at_the_commit_outputs),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(_git.main(argv), 1)
        _, context = self.CSV('context_file')
        self.assertEqual({row['repo_name'] for row in context}, {'owner/repo'})
        with (mock.patch.object(_git, 'measure_repo', return_value=({}, 'clone_failed')),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(_git.main(argv), 0)
        for filename in ('repos.csv', 'all_ai_commits.csv', 'other_ai_commits.csv'):
            with self.subTest(filename=filename), (self.output / filename).open(newline='') as inp:
                self.assertEqual([row['repo_name'] for row in csv.DictReader(inp)],
                                 ['owner/repo'])


class TestTheCommitOutputsAreRestrictedARowAtATime(unittest.TestCase):
    """The commit CSVs grow with every collection. Held in memory with their messages, they
    would take many times their size while the file history step restricts them. The step
    reads and writes them a row at a time."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "all_ai_commits.csv"
        columns = ["repo_name", "commit_sha", "commit_message"]
        message = "Fix the parser, again\n\n" + "x" * 4000
        with self.path.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out, lineterminator="\r\n")
            writer.writerow(columns)
            for number in range(5000):
                writer.writerow([f"o/r{number % 50}", f"{number:040x}", message])

    def test_the_memory_does_not_grow_with_the_file(self):
        kept = {f"o/r{number}" for number in range(49)}
        tracemalloc.start()
        try:
            _git.restrict_commit_csvs(self.tmp, kept)
            _size, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * 1024 ** 2)
        with self.path.open(newline="", encoding="utf-8") as inp:
            rows = list(csv.DictReader(inp))
        self.assertEqual(len(rows), 4900)
        self.assertNotIn("o/r49", {row["repo_name"] for row in rows})

    def test_a_temporary_file_that_a_kill_left_is_removed_with_nothing_to_drop(self):
        """The release refuses an output directory with a file that it does not publish. A
        later run therefore removes the temporary file of a rewrite that a kill ended, also
        when it has no row to drop."""
        stale = self.tmp / "all_ai_commits.csv.abcdefgh.tmp"
        stale.write_text("repo_name\n", encoding="utf-8")
        before = self.path.read_bytes()
        _git.restrict_commit_csvs(self.tmp, {f"o/r{number}" for number in range(50)})
        self.assertFalse(stale.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_bytes_are_those_of_the_table_writer(self):
        kept = {f"o/r{number}" for number in range(49)}
        columns, rows = _git.read_table(self.path)
        expected = self.tmp / "expected.csv"
        _git.write_table(expected, columns, [row for row in rows if row["repo_name"] in kept])
        _git.restrict_commit_csvs(self.tmp, kept)
        self.assertEqual(self.path.read_bytes(), expected.read_bytes())

    def test_the_restricted_csv_ends_each_record_with_lf(self):
        """The step writes LF whether the commit CSV that it restricts ends its records with LF
        or with CRLF. The fixture's commit CSV ends its records with CRLF."""
        kept = {f"o/r{number}" for number in range(49)}
        _git.restrict_commit_csvs(self.tmp, kept)
        self.assertNotIn(b"\r\n", self.path.read_bytes())
        with self.path.open(newline="", encoding="utf-8") as inp:
            self.assertEqual({row["repo_name"] for row in csv.DictReader(inp)}, kept)


class TestARowLongerThanItsHeader(_StepCase):
    """`DictReader` stores the fields past the end of the header under the key None. A writer
    of the header's columns alone would drop these fields while the run exits 0. Such a row
    therefore stops the run, as it does for `repos.csv`."""

    def test_a_mechanism_csv_with_one_stops_the_run_before_any_csv_is_written(self):
        self.seed_cache()
        # The last CSV that the loop writes. A failure from inside the loop would come after
        # the loop had rewritten every other CSV.
        path = self.path_of(list(heuristics.MECHANISM_CSVS)[-1])
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines[1] = lines[1].rstrip("\n") + ',"EXTRA,FIELD"\n'
        path.write_text("".join(lines), encoding="utf-8")
        before = {m: self.path_of(m).read_bytes() for m in heuristics.MECHANISM_CSVS}
        with self.assertRaises(ValueError) as caught:
            self.build_all(offline=True)
        self.assertIn("more fields than the header", str(caught.exception))
        self.assertIn(path.name, str(caught.exception))
        for mechanism in heuristics.MECHANISM_CSVS:
            self.assertEqual(self.path_of(mechanism).read_bytes(), before[mechanism],
                             f"{mechanism} was written before the run failed")

    def test_the_writer_refuses_one_and_leaves_the_file_as_it_was(self):
        """The commit CSVs reach the writer as they are read, without passing through the
        planner."""
        target = self.output / "all_ai_commits.csv"
        target.write_text("repo_name,commit_sha\no/r,1\n", encoding="utf-8")
        for row in ({"repo_name": "o/r", "commit_sha": "1", None: ["EXTRA"]},
                    {"repo_name": "o/r", "commit_sha": "1", "unnamed": "x"}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                _git.write_table(target, ["repo_name", "commit_sha"], [row])
            self.assertEqual(target.read_text(encoding="utf-8"), "repo_name,commit_sha\no/r,1\n")


class TestThePlanningDocuments(_StepCase):
    """The planning documents have the git columns of the mechanism CSVs, and this step fills
    these columns in `planning-documents.csv` in the same pass. The planning documents belong
    to the configured repositories, and a repository that `repos.csv` no longer lists loses
    them."""

    PATH = "specs/001-x/spec.md"

    def _planning(self, rows) -> None:
        with open(self.path_of(_git.planning_schema.HISTORY_KEY), "w", newline="",
                  encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.header(_git.planning_schema.HISTORY_KEY),
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def _row(self, repo="owner/repo") -> dict:
        return self.base_row(_git.planning_schema.HISTORY_KEY, repo=repo, artifact_path=self.PATH)

    def test_a_row_is_filled_from_the_history_that_the_cloning_step_measured(self):
        """The cloning step measures every copied file under no mechanism, and the planning
        documents are copied files."""
        self.seed_cache(mechanisms=list(heuristics.MECHANISM_CSVS))
        self._planning([self._row()])
        cache = _facts.FileHistoryCache.load(self.cache_path)
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "", self.PATH), MEASURED)
        cache.save(self.cache_path)
        self.build_all(offline=True)
        _, rows = self.CSV(_git.planning_schema.HISTORY_KEY)
        self.assertEqual([{c: r[c] for c in _git.FILLED_COLUMNS} for r in rows],
                         [MEASURED.as_row()])

    def test_the_summary_table_puts_the_counts_of_the_planning_documents_under_the_headers(
            self):
        """The step logs a table with a row for each CSV. `planning_document` is a longer name
        than any mechanism, and the counts of its row stand under the headers as the counts of
        every other row do."""
        self.seed_cache(mechanisms=list(heuristics.MECHANISM_CSVS))
        self._planning([self._row()])
        cache = _facts.FileHistoryCache.load(self.cache_path)
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "", self.PATH), MEASURED)
        cache.save(self.cache_path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _git.main(["--output-dir", str(self.output), "--validated-dir",
                              str(self.repos), "--collected-csv", str(self.collected_csv),
                              "--cache", str(self.cache_path), "--offline", "--no-verify"])
        self.assertEqual(code, 0, err.getvalue())
        messages = [line.split("file_history: ", 1)[1] for line in err.getvalue().splitlines()
                    if "file_history: " in line]
        names = {*_git.HISTORY_TABLES}
        table = [message for message in messages
                 if message.split(" ", 1)[0] in names | {"mechanism"}]
        self.assertEqual(table[0].split(" ", 1)[0], "mechanism", messages)
        self.assertIn(_git.planning_schema.HISTORY_KEY, [row.split(" ", 1)[0] for row in table])
        self.assertEqual({len(row) for row in table}, {len(table[0])}, "\n".join(table))

    def test_an_offline_run_refuses_a_planning_document_that_the_cache_lacks(self):
        self.seed_cache(mechanisms=list(heuristics.MECHANISM_CSVS))
        self._planning([self._row()])
        with self.assertRaises(ValueError) as caught:
            self.build_all(offline=True)
        self.assertIn(_git.planning_schema.CSV_NAME, str(caught.exception))

    def test_a_repository_that_repos_csv_no_longer_lists_loses_its_planning_documents(self):
        """`owner/gone` has planning documents and no configuration artifact left.
        `reconcile_repos_csv` drops it from repos.csv and drops its planning documents with
        it."""
        self.write_repos_csv({"owner/repo": {}, "owner/gone": {}})
        self._planning([self._row(), self._row("owner/gone")])
        kept, dropped = _git.reconcile_repos_csv(self.output)
        self.assertEqual((kept, dropped), (1, ["owner/gone"]))
        _, rows = self.CSV(_git.planning_schema.HISTORY_KEY)
        self.assertEqual([r["repo_name"] for r in rows], ["owner/repo"])

    def test_the_header_is_verified_as_the_detection_step_writes_it(self):
        self.seed_cache()
        self.build_all(offline=True)
        self.assertTrue(_git.verify_tables(self.output))
        columns, _ = self.CSV(_git.planning_schema.HISTORY_KEY)
        with open(self.path_of(_git.planning_schema.HISTORY_KEY), "w", newline="",
                  encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[c for c in columns if c != "line_count"],
                                    lineterminator="\n")
            writer.writeheader()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(_git.verify_tables(self.output))


class TestTheCachesFollowTheOutputDirectory(_StepCase):
    """The defaults of `--cache` and `--ai-authorship` follow `--output-dir`, as the default
    of `--validated-dir` does. A run over a scratch output directory then writes its
    measurements beside that directory rather than into the tracked `file_history.csv` and
    `ai_authorship.csv`."""

    def test_a_run_over_another_output_directory_writes_the_caches_beside_it(self):
        stage = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)

        def measured(repo_name, head_sha, head_commit_date, branch, artifacts, *args):
            path_histories = args[-1]
            for artifact in artifacts:
                path_histories[artifact] = _meta.PathCommits([], [], [])
            return {artifact: MEASURED for artifact in artifacts}, ""

        with (mock.patch.object(_facts, "FILE_HISTORY_PATH", stage / "file_history.csv"),
              mock.patch.object(_facts, "AI_AUTHORSHIP_PATH", stage / "ai_authorship.csv"),
              mock.patch.object(_git, "measure_repo", measured),
              contextlib.redirect_stderr(io.StringIO())):
            code = _git.main(["--output-dir", str(self.output), "--validated-dir",
                              str(self.repos), "--no-verify"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), [],
                         "the run wrote into the stage's caches")
        self.assertEqual(len(_facts.FileHistoryCache.load(self.cache_path)),
                         len(_git.HISTORY_TABLES))
        self.assertTrue((self.root / "ai_authorship.csv").is_file())


class TestTheCloningStepsLaterMeasurementCounts(unittest.TestCase):
    """The cloning step writes only complete rows, each without a mechanism, and this step
    measures a row for a mechanism only where the cache has no row for the commit. When a later
    collection measures a path again at the same commit, the lookups take the cloning step's
    complete row rather than the row for the mechanism. For TrafficGuard/typedai's AGENTS.md,
    the row for the mechanism gives the `created_at` 2025-09-22, while the first commit, which
    both rows name, is of 2024-07-13."""

    OLD = _facts.FileHistory("2025-09-22T12:40:25Z", "11", "a" * 40, "b" * 40)
    NEW = _facts.FileHistory("2024-07-13T06:15:57Z", "11", "a" * 40, "b" * 40)

    def cache(self, collected) -> "_facts.FileHistoryCache":
        cache = _facts.FileHistoryCache(path=Path("file_history.csv"))
        cache.put(_facts.FileHistoryKey("o/r", HEAD, "context_file", "AGENTS.md"), self.OLD)
        cache.put(_facts.FileHistoryKey("o/r", HEAD, "", "AGENTS.md"), collected)
        return cache

    def test_the_lookups_take_the_cloning_steps_complete_row(self):
        from agentcontext.steps import detection
        cache = self.cache(self.NEW)
        self.assertEqual(detection.git_columns_for(cache, "o/r", HEAD, "context_file",
                                                   "AGENTS.md")["created_at"],
                         "2024-07-13T06:15:57Z")
        known, missing, _ = _git._plan_misses(
            "context_file", [{"repo_name": "o/r", "head_sha": HEAD, "head_commit_date": "",
                              "branch": "main", "context_file": "AGENTS.md"}],
            "context_file", cache, retry_missing_history=False)
        self.assertEqual((known, dict(missing)), ({("o/r", "AGENTS.md"): self.NEW}, {}))

    def test_an_incomplete_row_of_the_cloning_step_leaves_the_measured_row(self):
        from agentcontext.steps import detection
        cache = self.cache(_facts.FileHistory())
        self.assertEqual(detection.git_columns_for(cache, "o/r", HEAD, "context_file",
                                                   "AGENTS.md")["created_at"],
                         "2025-09-22T12:40:25Z")
        self.assertEqual(_git.without_superseded(cache), (cache, 0))

    def test_the_superseded_row_leaves_the_cache(self):
        pruned, removed = _git.without_superseded(self.cache(self.NEW))
        self.assertEqual(removed, 1)
        self.assertEqual(set(pruned.keys()), {_facts.FileHistoryKey("o/r", HEAD, "", "AGENTS.md")})
        self.assertTrue(pruned.dirty)


class TestRetainHeads(unittest.TestCase):
    """`retain_heads` keeps the rows of a cache at the `(repo_name, head_sha)` that it is given,
    whatever their mechanism and path, and removes every other row."""

    def test_the_rows_at_other_heads_go_and_the_lookup_forgets_them(self):
        def key(repo="owner/repo", head=HEAD, mechanism="", artifact="CLAUDE.md"):
            return _facts.FileHistoryKey(repo, head, mechanism, artifact)
        cache = _facts.FileHistoryCache(path=Path("file_history.csv"))
        kept = {key(), key(mechanism="skills", artifact="x.md")}
        for each in (*kept, key(head="b" * 40), key(repo="left/repo")):
            cache.put(each, MEASURED)
        # The lookup builds its index here, before the removal.
        self.assertIsNotNone(cache.for_artifact("owner/repo", "b" * 40, "CLAUDE.md"))
        cache.dirty = False
        retained, removed = _git.retain_heads(cache, {("owner/repo", HEAD)})
        self.assertEqual(removed, 2)
        self.assertEqual(set(retained.keys()), kept)
        self.assertIsNone(retained.for_artifact("owner/repo", "b" * 40, "CLAUDE.md"))
        self.assertTrue(retained.dirty, "a save after the removal has to write the cache")
        self.assertEqual(retained.path, Path("file_history.csv"))

    def test_a_cache_with_nothing_to_remove_is_left_as_it_is(self):
        cache = _facts.FileHistoryCache()
        cache.put(_facts.FileHistoryKey("owner/repo", HEAD, "", "CLAUDE.md"), MEASURED)
        cache.dirty = False
        retained, removed = _git.retain_heads(cache, {("owner/repo", HEAD)})
        self.assertEqual(removed, 0)
        self.assertIs(retained, cache)
        self.assertFalse(retained.dirty, "a run that changes nothing writes nothing")

    def test_the_ai_authorship_cache_keeps_the_same_heads(self):
        current = _facts.AiAuthorshipKey("owner/repo", HEAD, "CLAUDE.md")
        values = _facts.AiAuthorship("TRUE", "claude", "1", "claude")
        cache = _facts.AiAuthorshipCache(path=Path("ai_authorship.csv"))
        cache.put(current, values)
        cache.put(_facts.AiAuthorshipKey("owner/repo", "b" * 40, "CLAUDE.md"), values)
        retained, removed = _git.retain_heads(cache, {("owner/repo", HEAD)})
        self.assertEqual(removed, 1)
        self.assertIsInstance(retained, _facts.AiAuthorshipCache)
        self.assertEqual(list(retained.keys()), [current])
        self.assertTrue(retained.dirty)
        self.assertEqual(retained.path, Path("ai_authorship.csv"))


class TestTheCachesKeepTheRowsOfTheCollection(_StepCase):
    """The step is the last step of a run that writes `file_history.csv` and `ai_authorship.csv`,
    and it keeps their rows at the heads that the collected list or the validated list names.
    No step reads a row at a head that a repository has moved past, because the next collection
    takes a repository over only at the head that the collected list names. If the step kept
    such rows, the tracked caches would grow with every collection toward the file size that
    GitHub refuses."""

    EARLIER = "b" * 40
    # The heads of the repositories that only one list or neither list names.
    LEFT = "e" * 40
    UNCONFIGURED = "f" * 40
    STALE = "9" * 40

    def setUp(self):
        super().setUp()
        self.ai_path = self.root / "ai_authorship.csv"
        # The rows that fill the output CSVs, at `HEAD`, and one row of each cache at every
        # head of the test.
        self.seed_cache()
        cache = _facts.FileHistoryCache.load(self.cache_path)
        ai_cache = _facts.AiAuthorshipCache()
        for head in self.every_head():
            cache.put(_facts.FileHistoryKey(*head, "", "CLAUDE.md"), MEASURED)
            ai_cache.put(_facts.AiAuthorshipKey(*head, "CLAUDE.md"),
                         _facts.AiAuthorship("TRUE", "claude", "1", "claude"))
        cache.save(self.cache_path)
        ai_cache.save(self.ai_path)
        # `unconfigured/repo` has no configuration artifact left after the cleanup, and the
        # validated list leaves it out.
        self.write_list(self.collected_csv, [("owner/repo", HEAD),
                                             ("unconfigured/repo", self.UNCONFIGURED)])
        self.write_list(self.validated_csv, [("owner/repo", HEAD)])

    def every_head(self) -> list:
        return [("owner/repo", HEAD), ("owner/repo", self.EARLIER), ("left/repo", self.LEFT),
                ("unconfigured/repo", self.UNCONFIGURED), ("stale/repo", self.STALE)]

    def run_step(self) -> tuple:
        """Run the step offline, as a run over a sample that the caches cover does. The step
        measures nothing, and the rows that it removes are the only change to the caches."""
        argv = ["--output-dir", str(self.output), "--validated-dir", str(self.repos),
                "--offline", "--no-verify"]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _git.main(argv)
        return code, err.getvalue()

    def heads_in(self, path: Path) -> set:
        """The `(repo_name, head_sha)` of the rows of a cache."""
        with open(path, newline="", encoding="utf-8") as f:
            return {(row["repo_name"], row["head_sha"]) for row in csv.DictReader(f)}

    def test_the_rows_at_an_earlier_head_go_and_those_at_the_current_head_stay(self):
        before = _facts.FileHistoryCache.load(self.cache_path)
        at_head = {key for key in before.keys() if key.repo_name == "owner/repo"
                   and key.head_sha == HEAD}
        code, log = self.run_step()
        self.assertEqual(code, 0, log)
        for path in (self.cache_path, self.ai_path):
            with self.subTest(cache=path.name):
                self.assertIn(("owner/repo", HEAD), self.heads_in(path))
                self.assertNotIn(("owner/repo", self.EARLIER), self.heads_in(path))
        after = _facts.FileHistoryCache.load(self.cache_path)
        self.assertEqual({key for key in after.keys() if key.repo_name == "owner/repo"},
                         at_head)
        self.assertIn("Removed 3 rows of file_history.csv and 3 rows of ai_authorship.csv at "
                      "heads that neither the collected list nor the validated list names",
                      log)

    def test_the_rows_of_a_repository_that_neither_list_names_go(self):
        """A repository that left the input list loses its collected files
        (`reuse.remove_left`). A later collection that names it again clones it and measures
        its history anew, and no step reads its old rows again."""
        code, log = self.run_step()
        self.assertEqual(code, 0, log)
        for path in (self.cache_path, self.ai_path):
            with self.subTest(cache=path.name):
                self.assertNotIn(("left/repo", self.LEFT), self.heads_in(path))

    def test_a_repository_that_only_the_collected_list_names_keeps_its_rows(self):
        """The next collection takes such a repository over at the same head, and a heuristic
        that then detects one of its files reads the row that the cloning step measured."""
        code, log = self.run_step()
        self.assertEqual(code, 0, log)
        for path in (self.cache_path, self.ai_path):
            with self.subTest(cache=path.name):
                self.assertIn(("unconfigured/repo", self.UNCONFIGURED), self.heads_in(path))

    def test_a_head_that_only_the_validated_list_names_keeps_its_rows(self):
        """The detection step joins the caches at the heads of the validated list, also where
        the validated tree did not come from the collected list beside it."""
        self.write_list(self.validated_csv, [("owner/repo", HEAD), ("stale/repo", self.STALE)])
        code, log = self.run_step()
        self.assertEqual(code, 0, log)
        for path in (self.cache_path, self.ai_path):
            with self.subTest(cache=path.name):
                self.assertIn(("stale/repo", self.STALE), self.heads_in(path))

    def test_without_the_collected_list_the_step_stops_and_leaves_the_caches(self):
        before = {path: path.read_bytes() for path in (self.cache_path, self.ai_path)}
        self.collected_csv.unlink()
        code, log = self.run_step()
        self.assertEqual(code, 1, log)
        self.assertIn(f"The collected list {self.collected_csv.resolve()} does not exist", log)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_a_list_without_its_head_column_stops_the_step_and_leaves_the_caches(self):
        before = {path: path.read_bytes() for path in (self.cache_path, self.ai_path)}
        self.collected_csv.write_text("repo_name\nowner/repo\n", encoding="utf-8")
        code, log = self.run_step()
        self.assertEqual(code, 1, log)
        self.assertRegex(log, r"The header of \S+ lacks head_sha")
        self.assertEqual({path: path.read_bytes() for path in before}, before)


class TestTheClonesAStoppedRunLeft(unittest.TestCase):
    """A stop ends the step with `os._exit` while threads are inside `measure_repo`, and the
    `finally` of `measure_repo` then never removes their clones. The next run removes these
    clones from `--clone-dir` before it clones anything, and it leaves everything that is not
    such a clone."""

    setUp = TestAFailureReachesTheOperatorAsALine.setUp
    _argv = TestAFailureReachesTheOperatorAsALine._argv

    def test_the_clones_of_an_earlier_run_are_removed_and_nothing_else(self):
        scratch = self.tmp / "_scratch"
        scratch.mkdir()
        # A clone as `measure_repo` names it, with git's read-only objects in it.
        left = Path(tempfile.mkdtemp(prefix=_meta.CLONE_PREFIX, dir=scratch))
        subprocess.run(["git", "init", "-q", str(left)], check=True)
        (left / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(left), "add", "."], check=True)
        objects = [p for p in (left / ".git" / "objects").rglob("*") if p.is_file()]
        self.assertTrue(objects)
        for path in objects:
            path.chmod(0o444)
        # The cloning step's clone of a repository, and a link with the clone's prefix to a
        # directory outside, neither of which this step made.
        other = scratch / "owner§repo"
        other.mkdir()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "kept.txt").write_text("kept\n", encoding="utf-8")
        (scratch / f"{_meta.CLONE_PREFIX}link").symlink_to(outside, target_is_directory=True)
        with (mock.patch.object(_git, "build_all", return_value=[]),
              mock.patch.object(_git, "reconcile_repos_csv", return_value=(0, [])),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO()) as err):
            _git.main(self._argv() + ["--clone-dir", str(scratch), "--no-verify"])
        self.assertFalse(left.exists(), err.getvalue())
        self.assertIn("Removed 1 clones that an earlier run left", err.getvalue())
        self.assertTrue(other.is_dir())
        self.assertTrue((scratch / f"{_meta.CLONE_PREFIX}link").is_symlink())
        self.assertTrue((outside / "kept.txt").is_file())


if __name__ == "__main__":
    unittest.main()
