#!/usr/bin/env python3
"""
Tests for _website/build_site.py

Strategy:
  - The site's constants and the classifier of context files come from the analysis stage's
    one shared module rather than from a copy here. A private copy of the classifier could
    miss a category, and the `category.notna()` filters would drop its rows from every chart
    on the site while the analysis figures count them.
  - The suite checks that the module imports, so that the site can be built.
  - `usage_example.py` runs over a release that the suite writes without the label `unclear`,
    and it selects the repositories that the release labels `true`.
  - The mechanism-by-tool heatmap counts the configuration artifacts attributed to each tool
    by the `tool` column of the mechanism CSVs, over a fixture with expected cells that are
    written out by hand.

Run with:  python3 -m unittest test_build_site -v
Requirements: Python 3.14, pandas
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load():
    spec = importlib.util.spec_from_file_location("build_site", HERE / "build_site.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheModuleImports(unittest.TestCase):

    def test_the_site_builder_imports(self):
        self.assertTrue(hasattr(_load(), "classify_context_file"))


class TestTheClassifierIsTheSharedOne(unittest.TestCase):
    """One classifier for the site and the analysis, derived from the heuristics."""

    @classmethod
    def setUpClass(cls):
        cls.site = _load()
        sys.path.insert(0, str(ROOT / "4_data-analysis"))
        import _shared
        cls.shared = _shared

    def test_context_categories_have_independent_expected_labels(self):
        cases = {
            "CLAUDE.md": "CLAUDE.md", "packages/app/CLAUDE.local.md": "CLAUDE.md",
            "AGENTS.md": "AGENTS.md", "AGENTS.override.md": "AGENTS.md",
            "GEMINI.md": "GEMINI.md", ".github/copilot-instructions.md": "Copilot Instructions",
            ".github/instructions/python.instructions.md": "Copilot Instructions",
            ".cursorrules": ".cursorrules", ".gemini/styleguide.md": "Gemini Style Guide",
            ".cursor/BUGBOT.md": "Cursor BUGBOT",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(self.site.classify_context_file(path), expected)
                self.assertEqual(self.site.classify_reference(path), expected)
        for path in ("README.md", "docs/style.md", "not-AGENTS.md"):
            with self.subTest(path=path):
                self.assertEqual(self.site.classify_context_file(path), "Named in Configuration")
                self.assertIsNone(self.site.classify_reference(path))

    def test_every_shared_category_has_the_analysis_color_on_the_site(self):
        """The color map is the site's own, and a category with no entry is drawn gray.
        Each color matches the analysis figures, which draw the same labels from
        `_shared.COLORS`."""
        for label in self.shared.CONTEXT_FILE_CATEGORIES:
            with self.subTest(label=label):
                self.assertIn(label, self.site.CONTEXT_FILE_COLORS)
                self.assertEqual(self.site.CONTEXT_FILE_COLORS[label],
                                 self.shared.COLORS.get(label),
                                 "the site and the analysis draw this label differently")

    def test_only_a_file_that_a_configuration_names_is_named_in_configuration(self):
        """A row that no category matches is shown as named in configuration. As the release
        README states, only a Codex configuration, a Gemini CLI settings file or extension
        manifest and the `instructions` of an OpenCode configuration name a context file. Every
        other row therefore has a category of its own. The test reads the detection step's
        tracked output, so that a checkout without it fails rather than skips."""
        import csv
        table = ROOT / "data" / "collection" / "output" / "context-files.csv"
        self.assertTrue(table.is_file(), f"{table} is tracked, but this checkout does not "
                                         f"contain it")
        with open(table, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        named = [row for row in rows if self.site.classify_context_file(row["context_file"])
                 == "Named in Configuration"]
        others = sorted({(row["tool"], row["context_file"]) for row in named
                         if not set(row["tool"].split("|")) <= {"codex", "gemini", "opencode"}})
        self.assertEqual(others, [], f"{len(others)} file(s) that no configuration names "
                                     f"have no category")
        self.assertLess(len(named), len(rows))

    def test_the_two_the_copy_did_not_have(self):
        for path, label in ((".gemini/styleguide.md", "Gemini Style Guide"),
                            (".cursor/BUGBOT.md", "Cursor BUGBOT")):
            with self.subTest(path=path):
                self.assertEqual(self.site.classify_context_file(path), label)


class TestConfigurationBrowser(unittest.TestCase):

    def test_every_mechanism_has_a_tab_and_a_linked_path(self):
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required to execute the browser code')
        source = (HERE / 'index.html').read_text()
        names = ['initArtifacts', 'getArtifactPathCol', 'getArtifactColumns',
                 'renderArtifactPage', 'showingText']
        functions = '\n'.join(re.search(
            r'function ' + name + r'\([^\n]*\)[\s\S]*?^}', source, re.M).group()
            for name in names)
        script = r'''
const nodes = {};
const buttons = [];
const loaded = [];
const document = {
  getElementById: key => nodes[key] ||= {appendChild: b => buttons.push(b),
    querySelectorAll: () => buttons},
  createElement: () => ({classList: {add() {}, remove() {}}})
};
let currentArtifactTab = 'context_files', artifactPage = 0;
const artifactData = {}, REPOS_PER_PAGE = 25;
const loadArtifact = key => loaded.push(key);
const fmt = String, escapeHtml = String, renderPagination = () => {};
''' + functions + r'''
initArtifacts();
const result = [];
for (const button of buttons) {
  button.onclick();
  const key = loaded.at(-1), column = getArtifactPathCol(key);
  artifactData[key] = [{repo_name:'o/r', [column]:'example-path',
    github_link:'https://example.org/config', '#commits':1}];
  renderArtifactPage(key);
  result.push({key, column, html:nodes['artifact-content'].innerHTML});
}
console.log(JSON.stringify(result));
'''
        run = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                             check=True)
        rows = json.loads(run.stdout)
        from agentcontext.heuristics import MECHANISM_CSVS
        expected = {('context_files' if key == 'context_file' else key): column
                    for key, (_, column) in MECHANISM_CSVS.items() if key != 'other'}
        # The two extensions beside the configuration artifacts, each with the column that
        # names its file: the planning document, and the importing file of an import.
        expected.update({'planning_documents': 'planning_document', 'imports': 'importer'})
        self.assertEqual({row['key']: row['column'] for row in rows}, expected)
        for row in rows:
            with self.subTest(mechanism=row['key']):
                self.assertIn('<a href="https://example.org/config"', row['html'])
                self.assertIn('example-path</a>', row['html'])


class TestBrowserLists(unittest.TestCase):
    """The repository list, tested with the page's own code in Node."""

    def _node(self, names, fixture):
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required to execute the browser code')
        source = (HERE / 'index.html').read_text()
        code = '\n'.join(re.search(r'(?:async )?function ' + name + r'\([^\n]*\)[\s\S]*?^}',
                                    source, re.M).group() for name in names)
        run = subprocess.run(['node', '-e', code + '\n' + fixture],
                             capture_output=True, text=True, check=True)
        return json.loads(run.stdout)

    def test_an_empty_list_shows_no_repositories_rather_than_counting_from_one(self):
        """A search that matches nothing shows "No repositories", not "Showing 1-0 of 0
        repositories"."""
        fixture = r'''
const nodes = {};
const document = {getElementById: key => nodes[key] ||= {}};
const REPOS_PER_PAGE = 25, TOOL_KEYS = [], CONFIG_KEYS = [];
const escapeHtml = String, renderPagination = () => {};
let repoPage = 0, FILTERED = [];
renderRepoPage();
const empty = nodes['repo-results-text'].textContent;
FILTERED = Array.from({length: 30}, (_, i) => ({repo_name: 'o/r' + i, github_link: ''}));
repoPage = 1;
renderRepoPage();
console.log(JSON.stringify([empty, nodes['repo-results-text'].textContent]));
'''
        self.assertEqual(self._node(['fmt', 'showingText', 'renderRepoPage'], fixture),
                         ['No repositories', 'Showing 26\u201330 of 30 repositories'])

    def test_a_detail_page_is_fetched_once(self):
        """The cache of the detail pages stores the repositories by name and is looked up by
        name. A second expansion on the same page then fetches nothing."""
        fixture = r'''
const fetched = [];
const fetch = async url => { fetched.push(url); return {ok: false, json: async () =>
  [{repo_name: 'o/a'}, {repo_name: 'o/b'}]}; };
const INDEX = [{repo_name: 'o/a', _page: 3}, {repo_name: 'o/b', _page: 3}];
const PAGE_CACHE = {}, CONTENT_CACHE = {}, LOADED_PAGES = new Set();
const renderRepoDetail = () => {};
const card = name => { const body = {}; const c = {dataset: {repo: name},
  classList: {contains: () => false, toggle() {}}, querySelector: () => body};
  return {parentElement: c}; };
(async () => {
  await toggleRepo(card('o/a'));
  await toggleRepo(card('o/b'));
  console.log(JSON.stringify(fetched.filter(u => u.includes('repos/'))));
})();
'''
        self.assertEqual(self._node(['toggleRepo'], fixture), ['data/repos/page_3.json'])


def _commit_browser_code(source: str) -> str:
    """The commit browser's code that the tests run in Node.js: the tool lists and the filter
    helper that it uses, and `loadCommitPage`."""
    parts = [re.search(r'^const SIX_COMMIT_TOOLS = [^\n]*$', source, re.M).group(),
             re.search(r'^const OTHER_COMMIT_TOOLS = [^\n]*$', source, re.M).group(),
             re.search(r'^function creditsTool\([^\n]*\)[\s\S]*?^}', source, re.M).group(),
             re.search(r'async function loadCommitPage\([^\n]*\)[\s\S]*?^}', source, re.M).group()]
    return "\n".join(parts)


class TestCommitBrowserFilters(unittest.TestCase):

    def test_other_tools_matches_a_commit_that_credits_a_tool_outside_the_six(self):
        """A commit of other_commits.csv credits a tool outside the six, and a commit in both
        commit CSVs lists the tools of both. The filter "Other tools" matches both, and the
        filter of a tool matches only the commits that credit it."""
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required to execute the browser code')
        source = (HERE / 'index.html').read_text()
        script = _commit_browser_code(source) + r'''
const commits = [{ai_tool:'Claude'}, {ai_tool:'Claude,Amp'}, {ai_tool:'Devin'}];
console.log(JSON.stringify({
  other: commits.map(c => creditsTool(c, OTHER_COMMIT_TOOLS)),
  claude: commits.map(c => creditsTool(c, 'Claude'))}));
'''
        run = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), {"other": [False, True, True],
                                                  "claude": [True, True, False]})

    def test_multitool_matches_and_search_limit_are_visible(self):
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required to execute the browser code')
        source = (HERE / 'index.html').read_text()
        function = _commit_browser_code(source)
        script = r'''
const nodes = {'commit-search': {value:''}};
const document = {
  getElementById: key => nodes[key] ||= {},
  querySelectorAll: () => [{dataset:{tool:'Claude'}}]
};
const SUMMARY = {total_listed_commits:5001}, REPOS_PER_PAGE = 25;
const commitPageCache = {}, fmt = String;
let commitPage = 0, rendered, paginationTotal;
const renderCommits = rows => rendered = rows;
const renderPagination = (id, total) => paginationTotal = total;
const fetch = async url => ({json: async () => {
  const page = Number(url.match(/page_(\d+)/)[1]);
  return Array.from({length:25}, (_, i) => ({commit_sha:`${page}-${i}`,
    ai_tool: page === 0 && i === 0 ? 'Claude,Copilot' : 'Gemini',
    commit_message:'Example commit'}));
}});
''' + function + r'''
loadCommitPage().then(() => console.log(JSON.stringify({rendered, paginationTotal,
  info:nodes['commit-results-info'].textContent})));
'''
        run = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                             check=True)
        result = json.loads(run.stdout)
        self.assertEqual([row['commit_sha'] for row in result['rendered']], ['0-0'])
        self.assertEqual(result['paginationTotal'], 1)
        self.assertIn('among the newest 5000 of 5001 commits', result['info'])

    def test_the_search_reads_the_subject_line(self):
        """The commit pages hold the subject line of each message, and the search finds a word of
        it."""
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required to execute the browser code')
        source = (HERE / 'index.html').read_text()
        function = _commit_browser_code(source)
        script = r'''
const nodes = {'commit-search': {value:'parser'}};
const document = {getElementById: key => nodes[key] ||= {}, querySelectorAll: () => []};
const SUMMARY = {total_listed_commits:2}, REPOS_PER_PAGE = 25;
const commitPageCache = {}, fmt = String;
let commitPage = 0, rendered;
const renderCommits = rows => rendered = rows;
const renderPagination = () => {};
const fetch = async url => ({json: async () => [
  {commit_sha:'match', ai_tool:'Claude', commit_subject:'Fix the parser'},
  {commit_sha:'other', ai_tool:'Claude', commit_subject:'Update the docs'}]});
''' + function + r'''
loadCommitPage().then(() => console.log(JSON.stringify(rendered.map(row => row.commit_sha))));
'''
        run = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                             check=True)
        self.assertEqual(json.loads(run.stdout), ['match'])


class TestMechanismJsonExport(unittest.TestCase):

    def test_every_mechanism_is_loaded_and_exported_including_empty_lsp(self):
        import csv
        import json
        import tempfile
        from unittest import mock
        mechanisms = {'context_files':'context_file', 'settings':'settings',
            'skills':'skills.md', 'subagents':'subagent', 'commands':'command',
            'hooks':'hook', 'rules':'rule', 'mcp':'mcp', 'plugins':'plugin', 'lsp':'lsp',
            'planning_documents':'planning_document', 'imports':'importer'}
        filenames = {'planning_documents': 'planning-documents.csv', 'imports': 'imports.csv'}
        for empty_lsp in (False, True):
            with self.subTest(empty_lsp=empty_lsp), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                release, output = root / 'release', root / 'site'
                release.mkdir()
                (release / 'repos.csv').write_text('repo_name,scanned_at\no/r,2026-03-29\n')
                (release / 'commits.csv').write_text('repo_name,commit_sha,ai_tool\n')
                (release / 'other_commits.csv').write_text('repo_name,commit_sha,ai_tool\n')
                expected = {}
                for key, column in mechanisms.items():
                    rows = [] if key == 'lsp' and empty_lsp else [{
                        'repo_name':'o/r', column:f'{key}/example', '#commits':3}]
                    expected[key] = rows
                    with (release / filenames.get(key, f'{key}.csv')).open('w', newline='') as out:
                        writer = csv.DictWriter(out, fieldnames=['repo_name', column, '#commits'])
                        writer.writeheader()
                        writer.writerows(rows)
                builder = _load()
                with mock.patch.object(builder, 'DATA_DIR', release), \
                     mock.patch.object(builder, 'SITE_DIR', output):
                    *_, loaded = builder.load_data()
                    builder.build_artifact_jsons(loaded)
                actual = {path.stem: json.loads(path.read_text())
                          for path in (output / 'data/artifacts').glob('*.json')}
                self.assertEqual(actual, expected)


    def test_the_values_that_pandas_reads_as_missing_are_kept(self):
        """`n/a` is the `memory` of a subagent of a tool without per-agent memory and the
        `language` of a file too short for language detection. By default, pandas reads `n/a`
        as missing. A blank cell is still missing, and the missing value marks an unscanned
        repository."""
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            release, output = Path(tmp) / 'release', Path(tmp) / 'site'
            release.mkdir()
            (release / 'repos.csv').write_text(
                'repo_name,scanned_at,homepage\no/r,2026-10-05,NA\no/unscanned,,\n')
            (release / 'commits.csv').write_text('repo_name,commit_sha,ai_tool,commit_message\n'
                                                 'o/r,a,Claude,null\n')
            (release / 'other_commits.csv').write_text('repo_name,commit_sha,ai_tool,commit_message\n')
            for key in ('context_files', 'settings', 'skills', 'commands', 'hooks', 'rules',
                        'mcp', 'plugins', 'lsp'):
                (release / f'{key}.csv').write_text('repo_name,language\no/r,n/a\n')
            (release / 'subagents.csv').write_text('repo_name,memory,language\no/r,n/a,\n')
            for name in ('planning-documents.csv', 'imports.csv'):
                (release / name).write_text('repo_name,language\no/r,n/a\n')
            builder = _load()
            with mock.patch.object(builder, 'DATA_DIR', release), \
                 mock.patch.object(builder, 'SITE_DIR', output):
                repos, configured, commits, artifacts = builder.load_data()
                builder.build_artifact_jsons(artifacts)
            self.assertEqual(list(configured['repo_name']), ['o/r'])
            self.assertEqual(configured['homepage'].tolist(), ['NA'])
            self.assertEqual(commits['commit_message'].tolist(), ['null'])
            self.assertEqual(json.loads((output / 'data/artifacts/subagents.json').read_text()),
                             [{'repo_name': 'o/r', 'memory': 'n/a', 'language': None}])
            self.assertEqual(json.loads((output / 'data/artifacts/skills.json').read_text()),
                             [{'repo_name': 'o/r', 'language': 'n/a'}])


@unittest.skipUnless(__import__('shutil').which('7zz'), '7zz is not on PATH')
class TestBrowserValues(unittest.TestCase):

    def _run(self, function, fixture):
        import json
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required to execute the browser code')
        source = (HERE / 'index.html').read_text()
        code = re.search(r'function ' + function + r'\([^\n]*\)[\s\S]*?^}', source, re.M).group()
        run = subprocess.run(['node', '-e', code + '\n' + fixture],
                             capture_output=True, text=True, check=True)
        return json.loads(run.stdout)

    def test_a_subject_line_is_shown_as_the_page_stores_it(self):
        """The page decodes nothing, and a literal backslash followed by n stays text."""
        import json
        encoded = 'Path C:\\name and a literal \\n marker'
        fixture = r'''
const node = {}, document = {getElementById: () => node};
const TOOL_LABELS = {}, BADGE_CLASSES = {}, escapeHtml = String;
const commitPage = 0;
renderCommits([{commit_subject:SUBJECT, repo_name:'o/r'}]);
console.log(JSON.stringify(node.innerHTML));
'''.replace('SUBJECT', json.dumps(encoded))
        rendered = self._run('renderCommits', fixture)
        self.assertIn('Path C:\\name and a literal \\n marker', rendered)

    def test_the_details_link_each_configuration_artifact_to_github(self):
        import json
        fixture = r'''
const PAGE_CACHE = {'o/r': {files: [
  {tab: 'context_files', path: 'CLAUDE.md', link: 'https://github.com/o/r/blob/main/CLAUDE.md'},
  {tab: 'skills', path: '.claude/skills/fix/SKILL.md', link: 'https://github.com/o/r/blob/main/.claude/skills/fix/SKILL.md'}]}};
const MECHANISM_LABELS = {context_file: 'Context Files', skills: 'Skills'};
const TOOL_KEYS = [], CONFIG_KEYS = [], TOOL_LABELS = {}, fmt = String, escapeHtml = String;
const container = {};
renderRepoDetail(container, 'o/r');
console.log(JSON.stringify(container.innerHTML));
'''
        html = self._run('renderRepoDetail', fixture)
        self.assertIn('Context Files</span><span class="detail-value"><a href="https://github.com/'
                      'o/r/blob/main/CLAUDE.md" target="_blank">CLAUDE.md</a>', html)
        self.assertIn('Skills</span><span class="detail-value"><a href="https://github.com/o/r/'
                      'blob/main/.claude/skills/fix/SKILL.md" target="_blank">'
                      '.claude/skills/fix/SKILL.md</a>', html)

    def test_no_specific_tool_is_a_category_without_increasing_the_tool_count(self):
        rendered = self._run('renderDashboard', r'''
const nodes = {}, document = {getElementById: key => nodes[key] ||= {}};
const SUMMARY = {configured_repos:1, total_commits:1, mechanism_counts:{Skills:1},
  tool_counts:{Claude:1,Copilot:1,Cursor:1,Gemini:1,Codex:1,OpenCode:1,'No specific tool':1}};
const TOOL_COLORS = {}, TOOL_LABELS = {no_specific_tool:'No specific tool'}, fmt = String;
renderDashboard();
console.log(JSON.stringify(nodes['stats-grid'].innerHTML));
''')
        self.assertRegex(rendered, r'<div class="stat-value">6</div>\s*<div class="stat-label">Tools</div>')


class TestCreationTimeline(unittest.TestCase):
    """The timeline of the site plots the families of `4_data-analysis/rq3/adoption_timeline.py`
    from the same constants of `_shared`."""

    def test_the_families_of_agentconfigs_1_0_0_and_the_planning_documents_from_2023_on(self):
        """kdeldycke/meta-package-manager renamed a development document of 2021 to claude.md
        and then to AGENTS.md, and its creation is no adoption. A BUGBOT.md is a context file of
        no plotted family. The skills and the planning documents are families of their own."""
        import pandas as pd
        builder = _load()
        artifacts = {
            "context_files": pd.DataFrame([
                {"repo_name": "kdeldycke/meta-package-manager", "context_file": "AGENTS.md",
                 "created_at": "2021-10-08T20:16:15Z"},
                {"repo_name": "o/a", "context_file": "CLAUDE.md", "created_at": "2025-03-10T00:00:00Z"},
                {"repo_name": "o/b", "context_file": ".cursor/BUGBOT.md",
                 "created_at": "2025-04-10T00:00:00Z"},
            ]),
            "skills": pd.DataFrame([{"repo_name": "o/c", "created_at": "2025-05-01T00:00:00Z"}]),
            "subagents": pd.DataFrame({"repo_name": [], "created_at": []}),
            "planning_documents": pd.DataFrame([{"repo_name": "o/d",
                                                 "created_at": "2025-05-20T00:00:00Z"}]),
        }
        chart = builder.build_chart_cumulative_adoption(artifacts)
        lines = {s["label"]: [(p["date"], p["value"]) for p in s["data"]] for s in chart["series"]}
        self.assertEqual(lines, {"CLAUDE.md": [("2025-03", 1), ("2025-05", 1)],
                                 "Skills": [("2025-05", 1)],
                                 "Planning Documents": [("2025-05", 1)]})
        self.assertEqual(chart["n_repos"], 3)
        self.assertEqual(chart["start"], "2023-01-01")

    def test_the_planning_documents_have_the_analysis_color(self):
        sys.path.insert(0, str(ROOT / "4_data-analysis"))
        import _shared
        self.assertEqual(_load().CONTEXT_FILE_COLORS["Planning Documents"],
                         _shared.COLORS["Planning Documents"])


class TestConfigToolHeatmap(unittest.TestCase):
    """The explorer's mechanism-by-tool heatmap counts attributed configuration artifacts."""

    def test_a_cell_counts_the_configuration_artifacts_attributed_to_the_tool(self):
        """A cell that crossed the repository flags would count a repository with a Claude
        context file and another tool's command under Claude x Commands. In the fixture, o/a
        has the Claude flag and a command attributed only to Cursor. o/b has one rules file that
        Claude and Cursor both read. o/c has only a shared AGENTS.md, and o/d has a Copilot
        command beside a shared AGENTS.md, which counts for no tool."""
        import pandas as pd
        tools = ['claude', 'copilot', 'cursor', 'gemini', 'codex', 'opencode', 'no_specific_tool']
        mechanisms = ['context_file', 'skills', 'subagents', 'commands', 'rules', 'settings',
                      'hooks', 'mcp', 'plugins', 'lsp']
        flags = {
            'o/a': ({'claude', 'cursor'}, {'context_file', 'commands'}),
            'o/b': ({'claude', 'cursor'}, {'rules'}),
            'o/c': ({'no_specific_tool'}, {'context_file'}),
            'o/d': ({'copilot'}, {'context_file', 'commands'}),
        }
        configured = pd.DataFrame([
            {'repo_name': repo, **{t: t in on for t in tools},
             **{m: m in present for m in mechanisms}}
            for repo, (on, present) in flags.items()])
        rows = {
            'context_files': [('o/a', 'claude'), ('o/c', 'any'), ('o/d', 'any')],
            'commands': [('o/a', 'cursor'), ('o/d', 'copilot')],
            'rules': [('o/b', 'claude|cursor')],
        }
        artifacts = {key: pd.DataFrame(rows.get(key, []), columns=['repo_name', 'tool'])
                     for key in ('context_files', 'settings', 'skills', 'subagents', 'commands',
                                 'hooks', 'rules', 'mcp', 'plugins', 'lsp')}

        chart = _load().build_chart_config_tool_heatmap(configured, artifacts)

        columns = ['Claude', 'Copilot', 'Cursor', 'Gemini', 'Codex', 'OpenCode', 'No specific tool']
        zero = [0, 0, 0, 0, 0, 0, 0]
        expected = {
            'Context Files': [1, 0, 0, 0, 0, 0, 1],
            'Skills': zero, 'Subagents': zero,
            'Commands': [0, 1, 1, 0, 0, 0, 0],
            'Rules': [1, 0, 1, 0, 0, 0, 0],
            'Settings': zero, 'Hooks': zero, 'MCP': zero, 'Plugins': zero, 'LSP': zero,
        }
        cells = {(row, column): count
                 for row, counts in zip(chart['rows'], chart['counts'])
                 for column, count in zip(chart['cols'], counts)}
        self.assertEqual(cells, {(row, column): count
                                 for row, counts in expected.items()
                                 for column, count in zip(columns, counts)})
        self.assertEqual(chart['cols'], columns)
        self.assertEqual(chart['tool_totals'], {'Claude': 2, 'Copilot': 1, 'Cursor': 2,
                                                'Gemini': 0, 'Codex': 0, 'OpenCode': 0,
                                                'No specific tool': 1})
        self.assertEqual(chart['percentages'][chart['rows'].index('Commands')],
                         [0, 100.0, 50.0, 0, 0, 0, 0])
        self.assertEqual(chart['n_repos'], 4)


class TestWebsiteBuildInputs(unittest.TestCase):

    def test_other_tool_commits_are_available_for_download(self):
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _load()
            for filename in builder.DOWNLOAD_FILES:
                (root / filename).write_text("x")
            contents = 'repo_name,ai_tool\no/r,Devin\n'
            (root / 'other_commits.csv').write_text(contents)
            with mock.patch.object(builder, 'DATA_DIR', root), \
                 mock.patch.object(builder, 'SITE_DIR', root / 'site'):
                builder.build_downloads_json('1.0.0')
            downloads = json.loads((root / 'site/data/downloads.json').read_text())
            found = [row for row in downloads if row['filename'] == 'other_commits.csv']
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]['size'], len(contents.encode()))
            self.assertEqual(found[0]['url'], 'https://assets.empirical-software.engineering/'
                                              'data/agenttorrent/1.0.0/other_commits.csv')

    def test_a_release_file_that_is_missing_stops_the_build(self):
        # Listed with size 0 and a link to the download address, a file that the release lacks
        # would be published as a download that the site cannot describe.
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _load()
            for filename in builder.DOWNLOAD_FILES:
                if filename != "hooks.csv":
                    (root / filename).write_text("x")
            with mock.patch.object(builder, 'DATA_DIR', root), \
                 mock.patch.object(builder, 'SITE_DIR', root / 'site'), \
                 self.assertLogs("build_site", "ERROR") as logged, \
                 self.assertRaises(SystemExit):
                builder.build_downloads_json('1.0.0')
            self.assertIn("hooks.csv", "\n".join(logged.output))
            self.assertFalse((root / 'site/data/downloads.json').exists())

    def test_the_downloads_of_the_release_are_staged_for_the_asset_host(self):
        """The directory of the release under _assets/ holds every download file with the bytes
        of the release."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _load()
            for filename in builder.DOWNLOAD_FILES:
                (root / filename).write_text(f"bytes of {filename}")
            with mock.patch.object(builder, 'DATA_DIR', root), \
                 mock.patch.object(builder, 'ASSETS_DIR', root / 'assets'):
                builder.stage_downloads('1.0.0')
            self.assertEqual(sorted(p.name for p in (root / 'assets/1.0.0').iterdir()),
                             sorted(builder.DOWNLOAD_FILES))
            for filename in builder.DOWNLOAD_FILES:
                with self.subTest(filename=filename):
                    self.assertEqual((root / 'assets/1.0.0' / filename).read_text(),
                                     f"bytes of {filename}")

    def test_the_version_comes_from_the_provenance_of_the_release(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _load()
            (root / 'provenance.json').write_text('{"dataset_version": "1.0.0"}')
            with mock.patch.object(builder, 'DATA_DIR', root):
                self.assertEqual(builder.release_version(), '1.0.0')
            (root / 'provenance.json').write_text('{}')
            with mock.patch.object(builder, 'DATA_DIR', root), \
                 self.assertLogs("build_site", "ERROR"), self.assertRaises(SystemExit):
                builder.release_version()

    def test_the_commit_pages_hold_the_subject_lines(self):
        """The whole messages would make the site larger than GitHub Pages publishes, and the
        pages hold the first line of each message, newest first."""
        import json
        import tempfile
        from unittest import mock
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _load()
            commits = pd.DataFrame([{
                'repo_name': 'o/r', 'commit_timestamp': f'2026-10-0{day}T00:00:00Z',
                'branch': 'main', 'commit_sha': f'{day}' * 40,
                'github_link': 'https://github.com/o/r', 'ai_tool': 'Claude',
                'commit_message': f'Fix {day}\n\nCo-authored-by: Claude <noreply@anthropic.com>'}
                for day in range(1, 4)])
            with mock.patch.object(builder, 'SITE_DIR', root / 'site'):
                builder.paginate_commits(commits)
            page = json.loads((root / 'site/data/commits/page_0.json').read_text())
            self.assertEqual([c['commit_subject'] for c in page], ['Fix 3', 'Fix 2', 'Fix 1'])
            self.assertTrue(all('commit_message' not in c for c in page))

    def test_a_commit_of_both_commit_csvs_is_listed_once_with_the_tools_of_both(self):
        """The commit browser lists the commits of commits.csv and other_commits.csv. A commit
        that credits Claude Code and Amp has a row in each file, and the browser lists it once."""
        import pandas as pd
        builder = _load()
        columns = ['repo_name', 'commit_timestamp', 'branch', 'commit_sha', 'github_link',
                   'commit_message', 'ai_tool', 'matched_heuristics', 'attributions']
        def row(sha, tool):
            return dict(zip(columns, ['o/r', '2026-10-01T00:00:00Z', 'main', sha * 40,
                                      'https://github.com/o/r', 'Fix', tool, 'x', f'{tool}:standard']))
        listed = builder.listed_commits(pd.DataFrame([row('a', 'Claude'), row('c', 'Codex')]),
                                        pd.DataFrame([row('a', 'Amp'), row('b', 'Devin')]))
        self.assertEqual(sorted(zip(listed['commit_sha'].str[0], listed['ai_tool'])),
                         [('a', 'Claude,Amp'), ('b', 'Devin'), ('c', 'Codex')])

    def test_the_repository_pages_list_the_configuration_artifacts_with_their_links(self):
        import json
        import tempfile
        from unittest import mock
        import pandas as pd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _load()
            configured = pd.DataFrame([{'repo_name': 'o/r', 'stargazers': 2},
                                       {'repo_name': 'o/s', 'stargazers': 1}])
            artifacts = {
                'skills': pd.DataFrame([{'repo_name': 'o/r', 'skills.md': '.claude/skills/a/SKILL.md',
                                         'github_link': 'https://github.com/o/r/blob/m/.claude/skills/a/SKILL.md'}]),
                'context_files': pd.DataFrame([{'repo_name': 'o/r', 'context_file': 'CLAUDE.md',
                                                'github_link': 'https://github.com/o/r/blob/m/CLAUDE.md'}])}
            with mock.patch.object(builder, 'SITE_DIR', root / 'site'):
                builder.paginate_repos(configured, artifacts)
            page = json.loads((root / 'site/data/repos/page_0.json').read_text())
        self.assertEqual({r['repo_name']: r['files'] for r in page}, {
            'o/r': [{'tab': 'context_files', 'path': 'CLAUDE.md',
                     'link': 'https://github.com/o/r/blob/m/CLAUDE.md'},
                    {'tab': 'skills', 'path': '.claude/skills/a/SKILL.md',
                     'link': 'https://github.com/o/r/blob/m/.claude/skills/a/SKILL.md'}],
            'o/s': []})

    def test_a_site_larger_than_github_pages_publishes_stops_the_build(self):
        builder = _load()
        builder.check_site_size(builder.PAGES_LIMIT_BYTES)
        with self.assertLogs("build_site", "ERROR"), self.assertRaises(SystemExit):
            builder.check_site_size(builder.PAGES_LIMIT_BYTES + 1)

    def test_usage_example_uses_the_build_interpreter(self):
        import os
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'usage_example.py').write_text(
                'import sys\nfrom pathlib import Path\nPath("interpreter.txt").write_text(sys.executable)\n')
            wrong_python = root / 'python3'
            wrong_python.write_text('#!/bin/sh\nexit 49\n')
            wrong_python.chmod(0o755)
            builder = _load()
            with mock.patch.object(builder, 'SCRIPT_DIR', root), \
                 mock.patch.object(builder, 'DATA_DIR', root), \
                 mock.patch.dict(os.environ, PATH=str(root)):
                builder.test_usage_example()
            self.assertEqual((root / 'interpreter.txt').read_text(), sys.executable)


class TestTheUsageExample(unittest.TestCase):
    """usage_example.py over a release that the test writes, with the columns that the example
    reads."""

    def _write(self, path: Path, header: str, *rows: str) -> None:
        path.write_text('\n'.join((header, *rows)) + '\n', encoding='utf-8')

    def test_it_selects_the_engineered_projects_without_an_unclear_label(self):
        """A release with `engineered_project` holding `true`, `false` and blanks alone, as after
        a run in which the model gave no label `unclear`, is read by pandas as booleans unless the
        example reads the column as text. Two of the four repositories are labeled `true`, and
        one of them is configured."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root / 'repos.csv',
                        'repo_name,engineered_project,scanned_at,claude,copilot,cursor,gemini,'
                        'codex,opencode,no_specific_tool,mcp,stargazers,mainLanguage',
                        'owner/configured,true,2026-10-30,TRUE,FALSE,FALSE,FALSE,FALSE,FALSE,'
                        'FALSE,FALSE,1500,Python',
                        'owner/engineered,true,,,,,,,,,,20,Go',
                        'owner/tutorial,false,,,,,,,,,,5,C',
                        'owner/unlabeled,,,,,,,,,,,3,Rust')
            self._write(root / 'context_files.csv',
                        'repo_name,context_file,created_at,first_commit_ai_created,'
                        'first_commit_ai_tool,is_reference',
                        'owner/configured,AGENTS.md,2026-01-01T00:00:00Z,TRUE,"Claude,Codex",FALSE',
                        'owner/configured,CLAUDE.md,2026-01-02T00:00:00Z,FALSE,,TRUE')
            self._write(root / 'imports.csv',
                        'repo_name,import,target,target_status,target_category,depth',
                        'owner/configured,@AGENTS.md,AGENTS.md,found,context_file,1',
                        'owner/configured,@docs/gone.md,docs/gone.md,missing,,1')
            self._write(root / 'planning-documents.csv', 'repo_name,framework,created_at',
                        'owner/configured,spec-kit,2026-02-01T00:00:00Z')
            self._write(root / 'commits.csv', 'repo_name,commit_timestamp,ai_tool,attributions',
                        'owner/configured,2026-01-01T00:00:00Z,"Claude,Codex",'
                        'Claude:standard;Codex:account')
            self._write(root / 'skills.csv', 'repo_name,scripts', 'owner/configured,TRUE',
                        'owner/configured,FALSE')
            result = subprocess.run(
                [sys.executable, '-c',
                 'import runpy, sys\n'
                 'found = runpy.run_path(sys.argv[1])\n'
                 'print(len(found["engineered_repos"]), len(found["configured_repos"]))',
                 str(HERE / 'usage_example.py')],
                capture_output=True, text=True, cwd=root, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], '1 of 2 engineered projects are configured')
        self.assertIn('50% of the skills have a scripts directory', lines)
        self.assertEqual(lines[-1].split(), ['2', '1'])


if __name__ == "__main__":
    unittest.main()
