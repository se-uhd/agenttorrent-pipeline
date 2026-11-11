"""Check current dataset documentation and archived inputs against their source files.

Counts are derived from the current outputs. A change to grounded detection behavior
may change those counts, and the documentation must then describe the new dataset. The
counts of an earlier paper do not determine correctness.

Requirements: Python 3.14 (stdlib, and 7zz for the archive checks)
Run with: python3 -m unittest tests.test_stated_facts -v
"""

import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from agentcontext import REPO_ROOT, SEVEN_ZIP, STAGE_DIR, csv_archive, heuristics

STAGE = STAGE_DIR
OUTPUT = STAGE / 'output'
DOCUMENT = REPO_ROOT / 'docs/dataset.md'


def rows(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def current_counts():
    """Count the current collection lists, mechanism CSVs, imports and planning documents,
    without fixed totals."""
    paths = [STAGE / 'data/collected/repos.csv', STAGE / 'data/collected/clone_failed.csv',
             STAGE / 'data/validated/repos.csv', OUTPUT / 'repos.csv']
    paths += [OUTPUT / filename for filename, _ in heuristics.MECHANISM_CSVS.values()]
    paths += [OUTPUT / 'imports.csv', OUTPUT / 'planning-documents.csv']
    return {path.relative_to(STAGE).as_posix(): len(rows(path)) for path in paths}


class TestStatedFacts(unittest.TestCase):
    def test_dataset_overview_counts_match_current_inputs_and_outputs(self):
        """Each row of the overview states the rows of its file through a statistic computed
        from that file, and the count equals the current row count of the file."""
        statistics = json.loads((REPO_ROOT / 'docs/statistics.json').read_text())
        rows = re.findall(r'^\| `([^`]+\.csv)` \| <!--s:([\w.]+)-->([\d,]+)<!--/s--> \|$',
                          DOCUMENT.read_text(), re.M)
        for name, key, count in rows:
            with self.subTest(name=name):
                self.assertEqual(statistics[key]['from'], {'rows': f'data/collection/{name}'})
        stated = {name: int(count.replace(',', '')) for name, _, count in rows}
        self.assertEqual(stated, current_counts())

    def test_snapshot_names_current_scan_dates_and_current_population(self):
        snapshot = json.loads((OUTPUT / 'snapshot.json').read_text())
        collected = rows(STAGE / 'data/collected/repos.csv')
        dates = sorted({row['scanned_at'] for row in collected if row['scanned_at']})
        self.assertEqual(snapshot['collection']['scanned_at_window'],
                         {'first': dates[0], 'last': dates[-1]})
        self.assertEqual(snapshot['collection']['counts']['sampled'], len(collected))
        self.assertEqual(snapshot['configured']['repositories'], len(rows(OUTPUT / 'repos.csv')))
        self.assertNotIn('reconstructed_from', snapshot['collection'])

    def test_the_stated_scan_times_are_those_of_the_collected_list(self):
        """The scan window that docs/dataset.md states runs from the first to the last
        repository's scan in the collected list, not to the end of the run."""
        match = re.search(r"scanned them\s+from (\S+\s+\S+) to (\S+\s+\S+)\s+UTC",
                          DOCUMENT.read_text())
        self.assertIsNotNone(match, f"{DOCUMENT} states no scan times")
        times = sorted(row['scanned_at'] for row in rows(STAGE / 'data/collected/repos.csv')
                       if row['scanned_at'])

        def minute(time):
            return time[:16].replace('T', ' ')

        self.assertEqual(tuple(' '.join(group.split()) for group in match.groups()),
                         (minute(times[0]), minute(times[-1])))

    def test_the_collection_readme_names_the_day_of_the_tracked_unmatched_attributions(self):
        """data/collection/0_README.md states the day on which the collection of the tracked
        unmatched attributions started. The name of the tracked archive contains that day, and
        `unmatched reduce` replaces the archive after each collection."""
        readme = (REPO_ROOT / 'data/collection/0_README.md').read_text(encoding='utf-8')
        row = next(line for line in readme.splitlines()
                   if line.startswith('| `output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z` |'))
        stated = re.findall(r'collection that started on (\d{4}-\d{2}-\d{2})', row)
        days = [match.group(1) for path in (REPO_ROOT / 'data/collection/output').iterdir()
                if (match := re.fullmatch(r'all_ai_commits-unmatched-(\d{4}-\d{2}-\d{2})\.csv\.7z',
                                          path.name))]
        self.assertEqual(len(days), 1, days)
        self.assertEqual(stated, days)

    def test_snapshot_separates_failed_clones_from_missing_commit_metadata(self):
        counts = json.loads((OUTPUT / 'snapshot.json').read_text())['collection']['counts']
        failures = {row['repo_name'] for row in rows(STAGE / 'data/collected/clone_failed.csv')}
        missing = {row['repo_name'] for row in rows(STAGE / 'data/collected/repos.csv')
                   if not row['head_sha'].strip()}
        self.assertEqual(counts['clone_failed'], len(failures))
        self.assertEqual(counts['missing_head_sha'], len(missing))

    def test_every_published_configuration_uses_its_collected_commit(self):
        collected = {row['repo_name']: row['head_sha']
                     for row in rows(STAGE / 'data/collected/repos.csv')}
        configured = {row['repo_name']: row['head_sha'] for row in rows(OUTPUT / 'repos.csv')}
        self.assertTrue(configured)
        self.assertEqual({repo: collected.get(repo) for repo in configured}, configured)
        for filename, _ in heuristics.MECHANISM_CSVS.values():
            with self.subTest(table=filename):
                mismatches = [(row['repo_name'], row['head_sha']) for row in rows(OUTPUT / filename)
                              if configured.get(row['repo_name']) != row['head_sha']]
                self.assertEqual(mismatches, [])

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), '7zz is needed')
    def test_commit_archives_are_readable_and_contain_only_configured_repositories(self):
        csv.field_size_limit(sys.maxsize)
        configured = {row['repo_name'] for row in rows(OUTPUT / 'repos.csv')}
        for name in ('all_ai_commits.csv', 'other_ai_commits.csv'):
            with self.subTest(table=name):
                archive = csv_archive(OUTPUT / name)
                self.assertTrue(archive.is_file(), str(archive))
                with subprocess.Popen([SEVEN_ZIP, 'e', '-so', str(archive)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, encoding='utf-8') as process:
                    try:
                        reader = csv.DictReader(process.stdout)
                        self.assertIn('commit_sha', reader.fieldnames)
                        unknown = {row['repo_name'] for row in reader} - configured
                        error = process.stderr.read()
                        code = process.wait()
                    finally:
                        process.stdout.close()
                        process.stderr.close()
                self.assertEqual(code, 0, error)
                self.assertEqual(unknown, set())

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), '7zz is needed')
    def test_extracted_commit_csvs_match_the_tracked_archives(self):
        checked = 0
        for name in ('all_ai_commits.csv', 'other_ai_commits.csv'):
            plain = OUTPUT / name
            if not plain.is_file():
                continue
            checked += 1
            with self.subTest(table=name), plain.open('rb') as stream:
                expected = hashlib.file_digest(stream, 'sha256').hexdigest()
                with subprocess.Popen([SEVEN_ZIP, 'e', '-so', str(csv_archive(plain))],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    try:
                        actual = hashlib.file_digest(process.stdout, 'sha256').hexdigest()
                        error = process.stderr.read()
                        code = process.wait()
                    finally:
                        process.stdout.close()
                        process.stderr.close()
                self.assertEqual(code, 0, error)
                self.assertEqual(actual, expected, f'repack {name} after regeneration')
        if not checked:
            self.skipTest('the commit CSVs are not extracted (the test of the archive population '
                          'still runs)')



class TestTheProductionBuildRecord(unittest.TestCase):
    """The subsample run of the recorded build against the run before it, recounted from the
    two run lists that docs/validation/ tracks."""

    def test_entered_and_left_are_the_run_lists_difference(self):
        """The run at the source commit, and the earlier run of the same day that the record
        contains beside it, each against the run list of its own reference."""
        validation = REPO_ROOT / 'docs/validation'
        run = json.loads((validation / 'production-build.json').read_text())['subsample_run']
        runs = [('subsample_run', run)]
        if 'earlier_run' in run:
            runs.append(('earlier_run', run['earlier_run']))
        for name, recorded in runs:
            with self.subTest(run=name):
                names = {key: {row['repo_name'] for row in rows(validation / recorded[key])}
                         for key in ('run_list', 'reference_run_list')}
                entered = len(names['run_list'] - names['reference_run_list'])
                left = len(names['reference_run_list'] - names['run_list'])
                self.assertEqual((recorded['repositories'], recorded['entered'], recorded['left']),
                                 (len(names['run_list']), entered, left))


if __name__ == '__main__':
    unittest.main()
