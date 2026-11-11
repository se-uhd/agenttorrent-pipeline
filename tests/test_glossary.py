"""Tests for GLOSSARY.md: the repository uses one word per concept.

A second word for one thing is how two readers end up describing different systems. Every row
of that document is a concept with one word and the words it retires, such as *registry* for the
heuristics, *shape* for a path glob, *config type* and *config option* for a mechanism,
*diagnostic* for a log message and *catch-all* for `other.csv`.

Each entry below is a banned term with the pattern that catches it in its banned sense and the
files allowed to carry it. Most of these words have an ordinary English sense that is not the
banned one, so the patterns ask for the collocations rather than the bare word. Every exception
is in GLOSSARY.md too, in the reasons of its section "Retired vocabulary", and the two lists are
held against each other here so the document cannot stop describing the code unnoticed.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_glossary -v
"""

import ast
import io
import re
import subprocess
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "GLOSSARY.md"

# Records of what was true when they were written, and the sample and the data drawn from it,
# which carry whatever the repositories in it call their files. `CHANGELOG.md` is not among them:
# its `## [Unreleased]` section is the release notes of the version being prepared, so a name it
# uses has to be a name the tree still has. The data of the collection stage is its trees under
# `data/` and its CSV and JSON files, and its `0_README.md` is prose. An evaluation's records
# are data of the same kind: repository names and each model's response. So are the results of
# the calibration of the classification under `data/classification/calibration/`, and so is the license
# table of the sampling frame, with its example repositories, and so are the classification
# parameters that past runs recorded under `data/classification/labels/`, which are keyed by their
# SHA-256 and quote the prompts of those runs. The run lists of the subsample run under
# `docs/validation/` name repositories in the sample, such as `kubeflow/model-registry`. The
# dependency locks under `locks/`, which uv writes, name distributions of PyPI in the words of
# their projects, and their `0_README.md` is prose.
NOT_PROSE = re.compile(
    r"^(docs/history/|GLOSSARY\.md$|tests/test_glossary\.py$|locks/[^/]*\.txt$"
    r"|data/collection/(data/|.*\.(csv|json)$)"
    r"|data/classification/calibration/"
    r"|data/classification/evaluation/|data/classification/labels/"
    r"|data/sampling/ghs_licenses\.csv$|data/sampling/educational/"
    r"|data/analysis/rq2/repo_metadata\.csv$"
    r"|docs/validation/subsample-run-list[^/]*\.csv$"
    r"|4_data-analysis/testdata/|_website/_site/|_plans/)")

# The file types whose words are prose, and the files named by type rather than by extension.
# `index.html` is the public page of the dataset, `compose.yaml` and the Dockerfile are what an
# operator reads, so all three are read. The comments of an ignore file explain which outputs
# are tracked, so every `.gitignore` is read too.
PROSE_SUFFIXES = ("py", "md", "txt", "yml", "yaml", "sh", "csv", "json", "toml", "html")
PROSE_NAMES = ("Dockerfile", ".gitignore")

# A code span quotes an identifier, so `artifact` naming a column of file_history.csv is code
# rather than a word this glossary chooses. In CHANGELOG.md emphasis is quoted too, because the
# entry that announces a rename has to name what it renamed.
CODE_SPAN = re.compile(r"`[^`]*`")
EMPHASIS = re.compile(r"\*[^*]+\*")


# A link names somebody else's repository or page: `kubeflow/model-registry` in the evidence
# of a commit heuristic is that project's word, not this one's.
LINK = re.compile(r"https?://\S+")


def prose_of(relative_path: str, line: str) -> str:
    """The part of a line whose words are the repository's own vocabulary."""
    text = LINK.sub(" ", CODE_SPAN.sub(" ", line))
    return EMPHASIS.sub(" ", text) if relative_path == "CHANGELOG.md" else text


def _literal_text(node) -> str:
    """The literal parts of a string or an f-string, without the expressions in its braces,
    which are code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(part.value for part in node.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + " " + _literal_text(node.right)
    return ""


def printed_strings(tree) -> list:
    """(line number, text) of every string a user reads when the program runs: an argparse
    `help=` or `description=`, a message passed to `logs.error`, `logs.warning` or `logs.info`
    or to the entrypoint's `log` and `log_error`, and the message of a raised exception, which
    the program logs."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            text = _literal_text(node.exc.args[0])
            if text:
                found.append((node.exc.args[0].lineno, text))
            continue
        if not isinstance(node, ast.Call):
            continue
        values = [k.value for k in node.keywords if k.arg in ("help", "description")]
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "logs" and func.attr in ("error", "warning", "info")):
            values += node.args
        if isinstance(func, ast.Name) and func.id in ("log", "log_error"):
            values += node.args[:1]
        for value in values:
            text = _literal_text(value)
            if text:
                found.append((value.lineno, text))
    return found


MARKUP = re.compile(r"<(script|style|code)\b.*?</\1\s*>|<!--.*?-->|<[^>]*>", re.I | re.S)


def visible_text(html: str) -> str:
    """The page as a reader sees it: tags, comments, scripts and styles blanked out, and every
    line where it was. A class name or an element id is code, as an identifier is in Python, and
    so is the text of a `code` element, as a code span is in Markdown."""
    return MARKUP.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), html)


def prose_lines(relative_path: str, text: str):
    """(line number, prose) for every line whose words are prose.

    In a Python file that is its comments, its docstrings and the strings it prints: its
    `help=` texts and its log messages, the literal parts of them. Identifiers are not prose: a
    dict called `artifacts` or a column called `matched_heuristics` is code, and renaming it is
    a separate decision from the words a reader is given. Every other file is prose throughout.
    """
    if relative_path.endswith(".html"):
        text = visible_text(text)
    if not relative_path.endswith(".py"):
        for number, line in enumerate(text.splitlines(), 1):
            yield number, line
        return
    lines = text.splitlines()
    wanted = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                wanted.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, IndentationError):
        pass
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                wanted.update(range(node.value.lineno, node.value.end_lineno + 1))
    for number in sorted(wanted):
        if number <= len(lines):
            yield number, lines[number - 1]
    if tree is not None:
        for number, printed in printed_strings(tree):
            for part in printed.splitlines():
                yield number, part


# term -> (pattern for the banned sense, files allowed to carry it)
BANNED = {
    "run record": (re.compile(r"\brun[ -]records?\b(?! (?:for|each|a|the|what|itself|every|whatever)\b)", re.I), ()),
    "registry": (re.compile(r"\bregistr(y|ies)\b", re.I), ()),
    "ruleset": (re.compile(r"\brule[ -]?sets?\b", re.I), ()),
    # The form of a record, a line or a file is its columns, its format or its layout.
    # `.shape` is pandas' attribute, which GLOSSARY.md records.
    # The heuristics of the planning documents name the files of other projects, such as Agent
    # OS's `shape.md`, BMAD's `docs/qa/gates/` and the framework Frame with its `.frame/`.
    "shape": (re.compile(r"(?<!\.)\bshap(e|es|ed|ing)\b", re.I), ("4_data-analysis/rq3/cooccurrence_analysis.py",
                                                                 "heuristics/planning_document_patterns.csv")),
    "collapsed path": (re.compile(r"\bcollapsed paths?\b", re.I), ()),
    "config type": (re.compile(r"\bconfig(uration)?[- ]types?\b", re.I), ()),
    "config option": (re.compile(r"\bconfig(uration)?[- ]options?\b", re.I), ()),
    # One term for the unit, the paper's: a configuration artifact, of which a single file is
    # one kind. A workflow artifact is GitHub's own word for what a run uploads, and
    # `upload-artifact` is an action's name. "Harness Engineering Artifacts" is in the dataset's
    # title.
    "artifact": (re.compile(r"(?<!configuration )(?<!configuration-)(?<!workflow )(?<!workflow-)"
                            r"(?<!upload-)(?<!download-)(?<!harness engineering )"
                            r"(?<![\w-])artifacts?\b", re.I), ()),
    "corpus": (re.compile(r"\bcorpus\b", re.I), ()),
    # Only where the unit is meant. "Configuration file" is right for a file, which is what the
    # papers use it for when they count the files in the archive, so the sense a pattern cannot
    # judge is left to the reader and to the section "Retired vocabulary" of GLOSSARY.md.
    "configuration file (for the unit)": (
        re.compile(r"\bone configuration file\b|\bper configuration file\b"
                   r"|\ba configuration file of (a|the) mechanism\b", re.I), ()),
    # The repositories of `dev_subsample.csv` are a subsample of the sample. "A subset of the
    # rows" is ordinary English and stays, so the pattern asks for the senses that named them.
    "subset (the subsample)": (
        re.compile(r"\b(development|dev)[- ]subsets?\b|\bsubset run\b|\bdev_subset\b"
                   r"|\bthe subset\b(?! of)", re.I), ()),
    # What a repository covers is an entry of the `coverage` column, which neither "feature" nor
    # "a case" says. A feature of a tool is ordinary English, so the pattern asks for the
    # subsample's sense.
    "feature (a coverage entry)": (
        re.compile(r"\bfeature (set|matrix|keys?)\b|\b(carries|covers|matched|matches) "
                   r"[\d,]* ?features\b|\b[\d,]+ features\b"
                   r"|\bfeatures? (a |the )?repository (covers|carries)\b", re.I), ()),
    # A minimum is not a floor outside mathematics. `math.floor` and `minimum_free_bytes` are code.
    "floor": (re.compile(r"(?<!math\.)(?<!_)\bfloor(s|ed|ing)?\b(?!_)", re.I), ()),
    # One name for version-control history, the one the studies use.
    "git history": (re.compile(r"\b(git|revision)[- ]history\b", re.I), ()),
    "catch-all": (re.compile(r"\bcatch[- ]alls?\b", re.I), ()),
    "residue": (re.compile(r"\bresidue\b", re.I), ()),
    # The check over what nothing matched is `unmatched paths` and `unmatched commits`, which
    # are the names of the step and the module. Sweep, capitalized, is the name of a tool.
    "sweep": (re.compile(r"\b(?:sweep|(?i:sweeps|swept|sweeping))\b"),
              ("heuristics/other_ai_commit_patterns.csv",)),
    "raw file": (re.compile(r"\braw[- ]files?\b", re.I), ()),
    # A repository in the sampling frame, not a member of it.
    "frame member": (re.compile(r"\bframe[- ]members?\b"
                                r"|\bmembers? (of|in) (the|a|each|every) (sampling )?frames?\b", re.I), ()),
    # A repository is in the sampling frame and in the sample. "Out of a sample" is motion and
    # keeps its preposition.
    "repository of the sample": (re.compile(r"\brepositor(y|ies)(?: (?!out\b)\w+)? of (the|a|an|each|"
                                            r"every|its|this|that) (sampling frames?|samples?)\b",
                                            re.I), ()),
    # A word for counting a population, used for a file that labels commits. The file is
    # `output/identity-labels.csv` and what was done to it is a reading.
    "census": (re.compile(r"\bcensus(es)?\b", re.I), ()),
    # A commit pattern finds a credit, which does not show who wrote the change: an AI commit is
    # a commit that credits a tool.
    "ai-authored commit": (re.compile(r"\bAI[- ](?:co-)?authored\b|\bco-authored by AI\b", re.I), ()),
    # A file whose history was not read, said as a measurement that is absent. What is absent
    # is a row of file_history.csv or the four git columns.
    "unmeasured": (re.compile(r"\bunmeasured\b", re.I), ()),
    # How a commit credits a tool is its attribution, the column's name.
    "kind of signal": (re.compile(r"\bkinds? of signal\b|\bsignal[ _]kinds?\b", re.I), ()),
    # The log sense only, which is what the collocations below are. A diagnostic script is one
    # of the scripts under `2_classification/diagnostics/`, which analyze data, and that sense
    # keeps the word.
    # The singular in the log sense, and the two plurals that only ever meant log output. The
    # plural elsewhere is the diagnostic scripts, which keep the word.
    "diagnostic": (re.compile(r"\b(a|the|every|one|its|no) diagnostic\b(?!s)(?! script)"
                              r"|\bdiagnostics (on stderr|through)\b", re.I), ()),
    "finished list": (re.compile(r"finished[- ]lists?\b", re.I), ()),
    # `output/*.csv` are the output CSVs. A LaTeX table, a markdown table and a TOML table keep
    # the word, so the pattern asks for the senses this pipeline had two words for.
    "table (of data)": (re.compile(r"\b(the tables|mechanism tables?|detail tables?|commit tables?"
                                   r"|published tables?|per-mechanism tables?)\b"
                                   r"|\b(march|april|september|real|release) tables?\b"
                                   r"|\bthe other table\b|\btables? of (its|their) own\b", re.I), ()),
    # "a kind of configuration artifact" is the paper's own wording and says something else.
    "kind of configuration": (re.compile(r"\bkinds? of configuration\b(?! artifact)"
                                         r"|\bnine kinds\b|\bten kinds\b", re.I), ()),
    # "A case" for what a repository covers. The letter-case sense and a test case stay, so the
    # pattern asks for the collocations that named an entry of `coverage`.
    # Applying the commit heuristics again is the reclassification, and matching the message
    # heuristics against the commit CSV again is its validation. The Wayback Machine's replay
    # of a capture is that service's word.
    "replay": (re.compile(r"(?<!Machine )\breplay(s|ed|ing)?\b", re.I), ()),
    "stored log": (re.compile(r"\bstored (commit )?logs?\b", re.I), ()),
    # "Walk" would name reading a commit history, the repository and its commits, the run and a
    # pass over files alike. Python's `os.walk` and `ast.walk` are the library's names.
    "walk": (re.compile(r"(?<!os\.)(?<!ast\.)\bwalk(s|ed|ing)?\b", re.I), ()),
    "sidecar": (re.compile(r"\bsidecars?\b", re.I), ()),
    "handover archive": (re.compile(r"\b(handover|NAS) archives?\b", re.I), ()),
    "writable copy": (re.compile(r"\bwritable (cop(y|ies)|handovers?)\b", re.I), ()),
    "content check": (re.compile(r"\bcontent checks?\b", re.I), ()),
    "regression baseline": (re.compile(r"\bregression baselines?\b", re.I), ()),
    # A figure is a plot that an analysis script writes, and a number that a document states is
    # a statistic.
    "stated figure": (re.compile(r"\bstated figures?\b|\bfigures of the doc(ument)?(s|ation)\b"
                                 r"|\bagentcontext figures\b|\btools/figures\b|\btest_tools_figures\b",
                                 re.I), ()),
    # The underscore of the match mode `copy_dir_gated` is a word character, so the mode is not
    # a word this pattern reads.
    "gate": (re.compile(r"\bgat(e|es|ed|ing)\b", re.I),
             ("heuristics/planning_document_patterns.csv", "heuristics/not_configuration.csv")),
    # A language's crawl date names a date; "position" named it by a metaphor.
    "crawl position": (re.compile(r"\bcrawl[ _-]positions?\b", re.I), ()),
    # The sampling window is counted back from its end date. Regular expressions and paths keep
    # their anchors, so the pattern asks for the window's senses only.
    "anchor (the sampling window)": (
        re.compile(r"\bwindow'?s? anchor\b|\banchored at (?:GHS|the crawl|the oldest|\d{4}-\d\d-\d\d)"
                   r"|\banchor_source\b|--anchor\b", re.I), ()),
    # A program, a service, a check or a repository does not ask or answer: a step queries
    # GitHub, GitHub returns a response, a check tests a condition. The license name
    # "BSD - ask to endorse" is SPDX's, and ask-bonk is the name of Cloudflare's GitHub app.
    "ask": (re.compile(r"\b(ask|asks|asked|asking)\b(?!-bonk\b)", re.I),
            ("1_sampling/config/spdx_licenses.json", "heuristics/ai_commit_patterns.csv",
             "heuristics/other_ai_commit_patterns.csv")),
    "answer": (re.compile(r"\b(answer|answers|answered|answering|unanswered)\b", re.I), ()),
    # A sampling frame is always the sampling frame. A pandas table is a DataFrame.
    "frame (for the sampling frame)": (
        re.compile(r"(?<!sampling )(?<!sampling-)(?<!sampling_)\bframes?\b", re.I),
        ("heuristics/planning_document_patterns.csv",)),
    "case (a coverage entry)": (
        re.compile(r"\bcovers every case\b|\bcases? (a|the) (repository|subsample)\b"
                   r"|\bset of cases\b|\bcases? not yet covered\b|\bcase matrix\b"
                   r"|\bcarr(y|ies) [\d,]* ?cases\b|\bcases? per repository\b", re.I), ()),    # A pilot's checklist for what a literal word names, the prerequisites of the pending steps.
    "preflight": (re.compile(r"\bpre-?flight", re.I), ()),
    # Lowercase only, so that the titles of the AIware 2026 papers keep their words. The vendors'
    # quotes in the notes of the planning document heuristics keep theirs.
    "coding tool": (re.compile(r"\b(?:agentic )?(?:AI )?coding tools?\b"),
                    ("heuristics/planning_document_patterns.csv",)),
    "ai tool": (re.compile(r"\bAI tools?\b"), ("heuristics/planning_document_patterns.csv",)),
}

# Retired words the suite does not refuse, each with the reason: the retired sense cannot be
# told from the ordinary one by a pattern, so a person reading the text is the check.
LEFT_TO_THE_READER = {
    "report": "a report is also what a check writes to a file and what a person files, and "
              "only the sense of what a command prints is retired",
    "rule": "`rules` is also a mechanism of the dataset, `.cursor/rules` and `.claude/rules`, "
            "with a column and a CSV of its own, and only the sense of a heuristic is retired",
    "token": "a token is also an attribution in the unmatched attributions and a word of a "
             "`--step-arg` value, and only the bare word for a GitHub access token is retired; "
             "tests/test_logs.py holds the log messages and help texts to it",
    "working copy": "the working copy is an extracted handover, and only the sense of "
                    "`data/validated/` is retired",
    "working tree": "git's working tree keeps the word, and only the sense of `data/validated/` "
                    "is retired",
    "engine": "the engine of a GitHub Agentic Workflow is the tool that runs it, and only the "
              "sense of `agentcontext/runner.py` is retired",
    "evaluation": "each of the two evaluations is an evaluation, and only a bare word that could "
                  "mean either is retired",
    "theme": "a tool's color theme keeps the word, and only the sense of a change is retired",
    "agent run": "\"the agent runs\" is a verb, and only the noun for one execution of a coding "
                 "agent is retired",
}

# The exceptions this suite allows, as GLOSSARY.md has to name them, and the exception that
# INTERNAL.md has to name, which only the private repository tracks.
DOCUMENTED_EXCEPTIONS = ("docs/history/", "rules.csv", "diagnostic script",
                         "workflow artifact", ".shape", "file_history.csv")
INTERNAL_EXCEPTIONS = ("_plans/",)


def tracked_prose() -> list:
    """Every tracked file whose words are this repository's own."""
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split("\0")
    return [f for f in listing
            if not NOT_PROSE.match(f)
            and (f.rsplit(".", 1)[-1] in PROSE_SUFFIXES or Path(f).name in PROSE_NAMES)]


ATTRIBUTION_TRAILER = re.compile(
    r"^(?:Co-authored-by|Signed-off-by|Reviewed-by|Acked-by|Tested-by):", re.I)


# A line that begins a block of its own, such as an item of a list, a heading, a row of a table or
# a quotation, continues no sentence of the line before it. The comment sign of a Python comment
# begins each of its lines, and a sentence of a comment runs on past it.
BLOCK_START = re.compile(r"^\s*(?:[-*+|>#]|\d+[.)])\s")
COMMENT_SIGN = re.compile(r"^\s*#\s?")


def banned_in(relative_path: str, lines: list, pattern) -> list:
    """The numbers of the lines of prose among `lines`, (line number, prose) pairs, in which
    `pattern` finds a banned term: in the line itself, or across its end and the start of the
    next line, where a wrapped sentence puts a term of two words on two lines."""
    found = [number for number, line in lines if pattern.search(line)]
    for (number, line), (following, after) in zip(lines, lines[1:]):
        if following != number + 1:
            continue
        if relative_path.endswith(".py"):
            line, after = COMMENT_SIGN.sub("", line), COMMENT_SIGN.sub("", after)
        elif BLOCK_START.match(after):
            continue
        first, second = line.strip(), after.strip()
        if not (first and second):
            continue
        joined = f"{first} {second}"
        if any(match.start() < len(first) < match.end() for match in pattern.finditer(joined)):
            found.append(number)
    return sorted(set(found))


def commit_prose(message: str) -> str:
    """Join wrapped prose after removing attribution trailers and code spans."""
    lines = [line for line in message.splitlines()
             if not ATTRIBUTION_TRAILER.match(line.strip())]
    return prose_of("commit", " ".join(" ".join(lines).split()))


class TestCommitProse(unittest.TestCase):

    def test_wrapped_unit_and_attribution_are_not_retired_vocabulary(self):
        message = ("Count each configuration\nartifact once\n\n"
                   "Co-authored-by: Artifact Registry <writer@example.com>\n")
        text = commit_prose(message)
        self.assertEqual(text, "Count each configuration artifact once")
        for term, (pattern, _) in BANNED.items():
            with self.subTest(term=term):
                self.assertIsNone(pattern.search(text))

    def test_bare_unit_and_output_csv_alias_are_rejected(self):
        for term, text in [("artifact", "Fix the artifact total"),
                           ("table (of data)", "Regenerate the March\ntables")]:
            with self.subTest(term=term):
                self.assertIsNotNone(BANNED[term][0].search(commit_prose(text)))


class TestWrappedTerms(unittest.TestCase):
    """A sentence that wraps puts a banned term of two words on two lines, as a CHANGELOG entry
    put "a coding" and "tool" on two lines, and a check of each line alone misses it."""

    def test_a_term_wrapped_across_two_lines_is_found(self):
        pattern = BANNED["coding tool"][0]
        lines = [(1, "- The release README describes the commits that credit a coding"),
                 (2, "  tool, because a credit does not show who wrote the change.")]
        self.assertEqual(banned_in("CHANGELOG.md", lines, pattern), [1])
        docstring = [(7, '    """The breaks over the coding'), (8, "    tools with an owner.")]
        self.assertEqual(banned_in("agentcontext/x.py", docstring, pattern), [7])
        comment = [(3, "    # the coding"), (4, "    # tools with an owner")]
        self.assertEqual(banned_in("agentcontext/x.py", comment, pattern), [3])

    def test_lines_that_no_sentence_spans_are_not_joined(self):
        pattern = BANNED["coding tool"][0]
        for lines in ([(1, "- an entry about coding"), (2, "- tools of another entry")],
                      [(1, "a paragraph about coding"), (3, "tools in the next one")],
                      [(1, "a sentence about coding"), (2, "## Tools")]):
            with self.subTest(lines=lines):
                self.assertEqual(banned_in("docs/x.md", lines, pattern), [])


class TestRetiredCompounds(unittest.TestCase):

    def test_hyphenated_terms_are_also_retired(self):
        examples = {
            "config type": "configuration-type flags",
            "config option": "config-options",
            "subset (the subsample)": "dev-subset repositories",
            "git history": "revision-history query",
            "raw file": "raw-file count",
            "frame member": "frame-member labels",
        }
        for term, phrase in examples.items():
            with self.subTest(term=term):
                pattern = BANNED[term][0]
                self.assertIsNotNone(pattern.search(phrase))
                self.assertIsNotNone(pattern.search(phrase.replace("-", " ")))

    def test_a_word_retired_in_any_sense_is_refused_in_every_form(self):
        examples = {
            "ruleset": ("two rulesets",),
            "shape": ("the shaped output", "shaping the rows"),
            "floor": ("floored to the month",),
            "sweep": ("swept the list", "sweeping it", "a sweep of the rows", "Sweeps the list"),
        }
        for term, phrases in examples.items():
            for phrase in phrases:
                with self.subTest(term=term, phrase=phrase):
                    self.assertIsNotNone(BANNED[term][0].search(phrase))

    def test_the_name_of_the_tool_sweep_is_not_refused(self):
        for phrase in ("Sweep's prefix", "the merges of Sweep"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(BANNED["sweep"][0].search(phrase))

    def test_code_in_html_is_not_prose(self):
        page = "<p>with the prefix <code>sweep/</code>,\na sweep</p>\n"
        self.assertEqual([(number, BANNED["sweep"][0].findall(line))
                          for number, line in prose_lines("_website/index.html", page)],
                         [(1, []), (2, ["sweep"])])

class TestPrintedMessages(unittest.TestCase):

    def test_no_printed_message_joins_clauses_with_a_semicolon(self):
        """A log message, a help text or an exception text is sentences, with context in
        parentheses: a semicolon joins two clauses that read better as two sentences."""
        offenders = []
        for f in tracked_prose():
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse((ROOT / f).read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
                continue
            offenders += [f"{f}:{number}: {text.strip()[:120]}"
                          for number, text in printed_strings(tree) if ";" in text]
        self.assertEqual(offenders, [])

    def test_no_printed_message_narrates_the_program(self):
        """A log message, a help text or an exception text states what happened or what is
        true. In one where the program narrates its own reasoning, such as "This start compares
        calendar months ..." or "..., so the step probes it again", the operator reads the
        narration before the fact."""
        narration = re.compile(r"\b[Tt]his start\b"
                               r"|, so (this|the (run|step|runner|start|stage|pre-checks))\b"
                               r"|\b[Tt]he (run|step) (reads|keeps|leaves|takes|decides|probes|"
                               r"measures)\b")
        offenders = []
        for f in tracked_prose():
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse((ROOT / f).read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
                continue
            offenders += [f"{f}:{number}: {text.strip()[:120]}"
                          for number, text in printed_strings(tree) if narration.search(text)]
        self.assertEqual(offenders, [])


class TestOneWordPerConcept(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.files = tracked_prose()
        if not cls.files:
            raise unittest.SkipTest("not inside a git checkout")

    def test_no_banned_term_survives(self):
        prose = {}
        for f in self.files:
            try:
                text = (ROOT / f).read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            prose[f] = [(number, prose_of(f, line)) for number, line in prose_lines(f, text)]
        for term, (pattern, allowed) in BANNED.items():
            offenders = [f"{f}:{number}: {dict(lines)[number].strip()[:120]}"
                         for f, lines in prose.items() if f not in allowed
                         for number in banned_in(f, lines, pattern)]
            with self.subTest(term=term):
                self.assertEqual(offenders, [], f"{term!r}: GLOSSARY.md names the word to use")

    def test_latest_commit_uses_the_glossary(self):
        result = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        text = commit_prose(result.stdout)
        for term, (pattern, _) in BANNED.items():
            with self.subTest(term=term):
                self.assertIsNone(pattern.search(text),
                                  f"latest commit uses retired vocabulary: {term}")

    def test_every_exception_is_written_down(self):
        """A file allowed to keep a banned term is named in GLOSSARY.md, with the reason."""
        glossary = GLOSSARY.read_text(encoding="utf-8")
        for _term, (_pattern, allowed) in BANNED.items():
            for f in allowed:
                with self.subTest(allowed=f):
                    self.assertTrue(Path(f).name in glossary or Path(f).parent.name in glossary,
                                    f"{f} keeps a banned term and GLOSSARY.md does not say why")
        for name in DOCUMENTED_EXCEPTIONS:
            with self.subTest(exception=name):
                self.assertIn(name, glossary)
        internal = ROOT / "INTERNAL.md"
        for name in INTERNAL_EXCEPTIONS if internal.is_file() else ():
            with self.subTest(exception=name):
                self.assertIn(name, internal.read_text(encoding="utf-8"))

    def test_every_word_this_suite_refuses_is_retired_in_writing(self):
        """A reader who meets a refused word looks it up, so the section "Retired vocabulary" of
        GLOSSARY.md names it.

        The key is the concept's name in `BANNED`, and the first column of that table is where
        the document lists what not to write, so the two are held against each other rather
        than by eye.
        """
        retired = " ".join(row.split("|")[1].lower() for row in self.retired_rows())
        for term in BANNED:
            with self.subTest(term=term):
                word = term.split(" (")[0]          # "shape (of a path)" is written "shape"
                self.assertIn(word, retired, f'{term}: The section "Retired vocabulary" of '
                                             "GLOSSARY.md does not name it")

    def test_every_retired_word_is_refused_or_left_to_the_reader_on_purpose(self):
        """The other direction: A row of the section "Retired vocabulary" that no entry of
        `BANNED` refuses leaves the retired word free to come back. A word whose retired sense a
        pattern cannot tell from its ordinary one is named in `LEFT_TO_THE_READER` with the
        reason."""
        for row in self.retired_rows():
            first = row.split("|")[1]
            words = re.findall(r'"([^"]+)"', first)
            with self.subTest(row=first.strip()[:60]):
                refused = any(word.lower() == term.split(" (")[0].lower()
                              or term.split(" (")[0].lower() in word.lower()
                              for word in words for term in BANNED)
                self.assertTrue(refused or any(w in LEFT_TO_THE_READER for w in words),
                                f"{first.strip()}: no entry of BANNED refuses it")

    def test_every_retired_row_says_what_to_write_and_why(self):
        """Three cells: the word, its replacement, and the reason. A row without the reason
        leaves the rule open to being argued again."""
        for row in self.retired_rows():
            retired, instead, why = (c.strip() for c in row.strip("|").split("|"))
            with self.subTest(retired=retired[:40]):
                self.assertTrue(instead, f"{retired[:40]}: nothing to write instead")
                self.assertTrue(why, f"{retired[:40]}: no reason")

    def test_the_pre_checks_entry_says_that_they_are_no_step(self):
        """The glossary entry Pre-checks says that the pre-checks are not a step, and no step of the
        runner is one."""
        from agentcontext import runner
        keys = {s.key for s in runner.CORE_STEPS + runner.COMMIT_RECLASSIFICATION_STEPS}
        self.assertNotIn("pre_checks", keys)
        self.assertIn("The pre-checks are not a step", " ".join(self.term("Pre-checks").split()))

    def test_the_stage_entry_names_every_stage_the_runner_groups_steps_into(self):
        from agentcontext import runner
        stages = {s.stage for s in runner.CORE_STEPS + runner.COMMIT_RECLASSIFICATION_STEPS}
        entry = " ".join(self.term("Stage").split())
        self.assertIn("sampling, classification, collection, publication and analysis", entry)
        for stage in stages:
            with self.subTest(stage=stage):
                self.assertIn(stage, entry)

    def test_the_coverage_entry_names_every_prefix_the_subsample_uses(self):
        """The glossary entry `coverage` shows one example per kind, and `dev_subsample.csv` is
        where they are.

        The entry names the prefixes `heuristic`, `mechanism` and `tool` and shows one of the
        others, so the reader does not meet a kind of string the glossary does not account for.
        """
        import csv
        path = ROOT / "data" / "collection" / "dev_subsample.csv"
        if not path.is_file():
            self.skipTest("dev_subsample.csv is not present")
        entry = self.term("`coverage`")
        with path.open(encoding="utf-8") as f:
            prefixes = {c.split(":")[0] for row in csv.DictReader(f)
                        for c in row["coverage"].split(";") if ":" in c}
        for prefix in ("heuristic", "mechanism", "tool"):
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, prefixes)
                self.assertIn(f"{prefix}:", entry)
        # The rest are branches of the collection's code, which the entry shows by example
        # rather than one by one, so at least one of its examples has to be among them.
        rest = prefixes - {"heuristic", "mechanism", "tool"}
        self.assertTrue(rest, "no code-path prefix left for the entry's fourth kind")
        self.assertTrue(any(f"{p}:" in entry for p in rest),
                        f"the entry shows none of {sorted(rest)}")

    @staticmethod
    def term(name: str) -> str:
        """The paragraph of `**<name>.**`, to the blank line after it."""
        text = GLOSSARY.read_text(encoding="utf-8")
        start = text.index(f"**{name}.**")
        return text[start:text.index("\n\n", start)]

    @staticmethod
    def retired_rows() -> list:
        """The rows of the section "Retired vocabulary", without its header and separator."""
        text = GLOSSARY.read_text(encoding="utf-8")
        section = text.split("## Retired vocabulary", 1)[1].split("\n## ", 1)[0]
        rows = [l for l in section.splitlines()
                if l.startswith("|") and l.count("|") == 4 and "---" not in l]
        return rows[1:]


class TestTrackedProseNames(unittest.TestCase):

    def test_spaces_and_unicode_names_reach_the_glossary_check(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            names = ['docs/review note.md', 'docs/révision.md']
            for name in names:
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_text('The registry contains entries.\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            with mock.patch.dict(globals(), ROOT=root):
                case = TestOneWordPerConcept('test_no_banned_term_survives')
                case.files = tracked_prose()
                result = unittest.TestResult()
                case.run(result)
            self.assertEqual(result.errors, [])
            self.assertTrue(result.failures, 'retired vocabulary was not reported')
            report = '\n'.join(message for _, message in result.failures)
            for name in names:
                self.assertIn(name, report)

    def test_the_comments_of_an_ignore_file_reach_the_glossary_check(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            names = ['.gitignore', 'data/sampling/.gitignore']
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# The answers that a stopped query left.\n*.partial.jsonl\n')
            subprocess.run(['git', '-C', str(root), 'add', '-f', '.'], check=True)
            with mock.patch.dict(globals(), ROOT=root):
                case = TestOneWordPerConcept('test_no_banned_term_survives')
                case.files = tracked_prose()
                result = unittest.TestResult()
                case.run(result)
            self.assertEqual(result.errors, [])
            report = '\n'.join(message for _, message in result.failures)
            for name in names:
                self.assertIn(f"'{name}:1: ", report)


RETIRED_IDENTIFIERS = {
    "MECHANISM_TABLES", "NAMED_OPTIONS", "declaring_rules", "DECLARING_RULES",
    "_DECLARING_RULES", "_PLUGIN_PART_RULES", "_MANIFEST_FIELD_RULES",
    "rule_key", "match_rules", "unmatched_rules", "floor_bytes", "floor_gb",
    "record_run", "RECORD_MEMBERS", "AI_COMMITS_STAGES", "CollectConfig",
    "REPLAY_KINDS", "replay_kind", "REPLAY_GAPS", "check_replay_report", "run_replay",
    "replay_step", "replay_path", "walk_commits", "walk_needs_redo", "WALKED_COLUMNS",
    "walk_artifact", "walk_repo_files", "walk_prunable", "_walk_evidence", "walked_csv", "unwalked",    "preflight",
}


def retired_identifiers(tree) -> set[str]:
    """Retired names in Python identifiers, independently of comments and strings."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                names.add(node.asname)
    return names & RETIRED_IDENTIFIERS


class TestInternalIdentifiers(unittest.TestCase):
    def test_retired_names_are_found_even_without_prose(self):
        tree = ast.parse("from package import MECHANISM_TABLES\n"
                         "def match_rules(floor_bytes):\n"
                         "    return source.declaring_rules(floor_bytes)\n")
        self.assertEqual(retired_identifiers(tree),
                         {"MECHANISM_TABLES", "match_rules", "floor_bytes", "declaring_rules"})

    def test_schema_names_and_distinct_technical_senses_are_preserved(self):
        tree = ast.parse("rules = {'artifact': 'path', 'registry': 'plugins.json'}\n"
                         "sampling_frame = table.shape\n"
                         "def stop(signum, stack_frame): pass\n")
        self.assertEqual(retired_identifiers(tree), set())

    def test_tracked_python_uses_the_current_internal_names(self):
        failures = []
        for relative in tracked_prose():
            if not relative.endswith(".py") or relative == "tests/test_glossary.py":
                continue
            names = retired_identifiers(ast.parse((ROOT / relative).read_text(encoding="utf-8")))
            if names:
                failures.append(f"{relative}: {', '.join(sorted(names))}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
