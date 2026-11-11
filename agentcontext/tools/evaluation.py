"""The detection accuracy evaluation: `python -m agentcontext evaluation sample|report`.

`sample` draws seeded samples from a collection and writes them, with two measurements of the
heuristics, into the collection's evaluation directory, `data/collection/evaluation/<day>/`,
named by the day on which the collection started (`unmatched.collection_started`). Every draw
numbers its items from 1, and one directory per collection keeps the items of different
evaluations apart. The directory holds these files:

- `path_precision.csv`: rows of the mechanism CSVs, up to `PATH_PRECISION_PER_CELL` per tool
  and mechanism, each labeled with whether the configuration artifact is configuration of that
  tool and mechanism
- `path_recall.csv`: repositories that no output CSV lists and that are not clone failures, and
  repositories that an output CSV lists, each labeled with the paths of its file list that a
  tool loads as configuration, and
  `path_recall_candidates.csv`, the paths of their file lists that name one of the six tools or
  a file that the tools share, or that are at a place that a path heuristic names, each marked
  with whether the CSV of a mechanism lists it, only other.csv lists it, or no CSV does
- `commit_precision.csv`: matched commits, `COMMIT_PRECISION_PER_TOOL` per tool and per CSV of
  the tools outside the six, and one commit for each heuristic that these commits leave out
- `commit_recall.csv`: commits that the cloning step read, drawn uniformly from the
  per-repository commit records, and `mentions.csv`, phrasings that name a tool in commits that
  no heuristic matched, drawn by their number of commits
- `import_precision.csv`: rows of imports.csv, up to `IMPORT_PRECISION_PER_CELL` per import
  heuristic and target status, each labeled with whether the tool loads the target through the
  import as the row states
- `planning_precision.csv`: rows of planning-documents.csv, up to `PLANNING_PRECISION_PER_CELL`
  per framework or tool, each labeled with whether the file is a planning document of that
  framework or tool
- `commit_heuristic_attribution.csv`: per commit heuristic, the commits and repositories that it
  credits, the commits that only it credits, and the repositories that would lose the tool
  without it
- `path_heuristic_yield.csv`: per path heuristic, the rows of the mechanism CSVs with a
  configuration artifact that its path reads, and the rows that no other heuristic reads

The samples leave out the repositories that the heuristics' `evidence` links, because the
heuristics were read from those repositories. A stratum of `import_precision.csv` or
`planning_precision.csv` with every row in such a repository is drawn from those rows, and its
items have `from_evidence` set to `yes`. `sample.json` records the seed, the sizes, the SHA-256
of every input and the strata drawn from those repositories. Beside it, `sample` keeps an
xz-compressed copy of the mentions CSV that it read, such as `all_ai_commits-mentions.csv.xz`,
because every commit reclassification rewrites that CSV, and git ignores that copy, because the
CSV can name people. `commit_recall.csv` keeps the identity of a tool or a bot, the message of a
commit that a heuristic credits without the identities, the mentions and the profile links of
people, and the link of every commit, and `commit_recall-texts.csv` beside it, which git ignores
as well, keeps the rest for the labelers. The labels are written by hand or by an agent into
`labels/<sample>.csv` under the criteria of docs/tools/evaluation.md. `report` reads them in the
evaluation directory that it is given, with the resolutions of `labels/resolutions.csv`, and
writes `report.json` and `report.md` there: precision and recall with Wilson intervals, the
agreement of the two labeling passes, and `review.csv`, the items that a person resolves. The
precision of the imports and of the planning documents is given over all strata and without the
strata drawn from the repositories that the `evidence` links.

`sample --only import_precision planning_precision --into DIR` draws only the samples of
imports.csv and planning-documents.csv (`ONLY_SAMPLES`) into DIR, the directory of an earlier
draw, with the seed that its sample.json records, and adds a record of the draw to sample.json
under `later_draws`. The samples and the records of the earlier draw stay as they are.

Exit codes: 0 when the files are written, 1 when an input is missing, a commit CSV differs from
the archive that git tracks beside it or 7zz cannot list that archive (`agentcontext.archives`),
the day on which the collection started is unknown, the collection's evaluation directory
already holds a draw, or a label is not allowed by the criteria. With `--only`, 1 also when the
directory holds no draw, when it holds one of the samples or its labels, or when `--seed` names
another seed than the draw's.

Requirements: Python 3.14 (stdlib only), and 7zz where git tracks the archive of a commit CSV.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import lzma
import math
import random
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

from agentcontext import (REPO_ROOT, STAGE_DIR, archives, cli, commits, csv_archive, heuristics,
                          logs, planning_schema, resume, reuse)
from agentcontext.checks import unmatched
from agentcontext.git import clone
from agentcontext.paths import AGENTIC_WORKFLOW_SUFFIX
from agentcontext.repos import repo_name_to_dir
from agentcontext.steps import import_detection

csv.field_size_limit(sys.maxsize)

COMPONENT = "evaluation"
# The directory of the evaluations, with one directory per evaluated collection.
EVALUATION_DIR = STAGE_DIR / "evaluation"
OUTPUT_DIR = STAGE_DIR / "output"
VALIDATED_DIR = STAGE_DIR / "data" / "validated"

# GROUNDING: PATH_PRECISION_PER_CELL rows bound a cell's precision only roughly: Ten of ten
# correct still allow a Wilson lower bound of 0.72. The cells together bound the overall path
# precision to an interval about 0.05 wide around a precision of 0.95.
SEED = 20261013
PATH_PRECISION_PER_CELL = 10
RECALL_UNCONFIGURED = 200
RECALL_CONFIGURED = 100
CANDIDATES_PER_REPOSITORY = 200
COMMIT_PRECISION_PER_TOOL = 60
COMMIT_RECALL_COMMITS = 1000
MENTIONS = 200
# The rows of imports.csv per import heuristic and target status, and of planning-documents.csv
# per framework or tool. A cell of an import heuristic and a status keeps apart what the rows of
# imports.csv state differently: the tool, the importing files and the syntax of the heuristic,
# its conditions and limits, and what happens to the target. The planning documents have fewer
# strata than the imports have cells, and each stratum gets more rows.
IMPORT_PRECISION_PER_CELL = 7
PLANNING_PRECISION_PER_CELL = 20
# What the tracked samples write in place of what can name a person.
PERSON_MARK = "(a person)"
# An address in a line of a commit. After the token of a trailer, such as `Co-authored-by:`, the
# name before an address in angle brackets is all that follows the token on its line, parentheses
# and quotes included. Elsewhere, the name is up to five words before the address, without the
# punctuation that ends a token of a trailer. An address alone ends in a label that begins with a
# letter, as a top-level domain does, so that a package and its version, such as
# `@tiptap/extension-collaboration-cursor@0.0.0-beta.0`, are no address.
_NAMED_ADDRESS = re.compile(
    r"(?<![\w\-])(?P<token>[A-Za-z][\w\-]*[ \t]*:[ \t]*)(?P<value>[^<>:\r\n]*?)[ \t]*"
    r"<(?P<trailer_address>[^<>\s@]+@[^<>\s]+)>"
    r"|(?P<name>(?:[^\s<>:;,()\"'`]+[ \t]+){0,4}[^\s<>:;,()\"'`]*)[ \t]*<(?P<address>[^<>\s@]+@[^<>\s]+)>"
    r"|(?P<alone>[\w.+\-\[\]]+@[\w\-]+(?:\.[\w\-]+)*\.[^\W\d_][\w\-]*)")
# The file beside the tracked sample with the identities and the messages for the labelers, which
# git ignores.
RECALL_TEXTS = "commit_recall-texts.csv"
# The share of every sample that a second pass labels again, for the agreement of the passes.
SECOND_PASS_SHARE = 0.2
# The outcomes of `ai_commits-repos.csv` for a repository of `clone_failed.csv`, as the comment
# above `resume.TRANSIENT_OUTCOMES` lists them. The cloning step keeps no file list of such a
# repository.
CLONE_FAILURES = (*resume.TRANSIENT_OUTCOMES, *clone.PERMANENT_OUTCOMES, clone.SIZE_BOUND_EXCEEDED,
                  clone.TIME_BOUND_EXCEEDED, clone.FILE_NAME_TOO_LONG)
Z = 1.959963984540054  # the 0.975 quantile of the standard normal

# A path that names one of the six tools or one of the shared files that they read. The
# candidate paths of a file list for path recall are the paths that CANDIDATE matches and the
# paths at a place that a row of heuristics/config_path_patterns.csv names (`candidate_places`),
# where the row's path matches as `agentcontext.reuse` matches it: its components anywhere in the
# path, in any case. Both deliberately match more paths than the path heuristics read, because
# recall measures the paths that they match and the path heuristics miss. The rows add
# configuration paths that CANDIDATE does not match, such as REVIEW.md, .github/lsp.json and the
# GitHub Agentic Workflows in .github/workflows/*.md. A new row adds its place without a change
# to this module.
#
# A row adds no place where its path is a generic name or directory that the row reads only by
# its content, beside a plugin's manifest or among a project's other files. Such names would fill
# the CANDIDATES_PER_REPOSITORY candidates of a repository with files that are no configuration:
# - the parts of a plugin under a plugin placeholder, such as `<claude-plugin>/skills`, which
#   would match every skills/ directory
# - a file kept beside a plugin's manifest (`plugin_sibling`): README.md, LICENSE and
#   package.json
# - a plugin manifest or marketplace with a bare name, plugin.json and marketplace.json, which
#   many programs use. The rows of the vendor folders, such as .plugin/plugin.json, name these
#   files there.
# - a directory that its row does not keep whole (`unclassified_dir`, `copy_dir_gated`). The
#   other rows name the files of `.github` that count, because the directory also holds a
#   project's CI, and the `copy_dir` rows name the plugin manifest directories whole.
# - a YAML workflow, which is the project's CI unless its content runs an agent
#   (`workflow_uses:`, `workflow_command:`). A `workflow_engine:` row names the Markdown files of
#   its directory, the format of a GitHub Agentic Workflow.
CANDIDATE = re.compile(
    r"claude|copilot|cursor|gemini|codex|antigravity|opencode|(?:^|/)\.?agents?(?:/|\.md$)|agents?\.md$"
    r"|mcp|(?:^|/)skill\.md$|\.aiexclude$|\.github/(?:instructions|prompts|chatmodes|agents|hooks)/",
    re.IGNORECASE)
# The match modes of the rows that name no candidate place, as the comment above explains.
NO_CANDIDATE_MODES = ("plugin_sibling", "unclassified_dir", "copy_dir_gated", "workflow_uses:",
                      "workflow_command:")

# The labels that the criteria of docs/tools/evaluation.md allow, per sample, and the labels that
# count as a correct detection (precision) or as configuration or a credit to find (recall).
LABELS = {
    "path_precision": ("correct", "wrong_tool", "wrong_mechanism", "not_configuration"),
    "path_recall": ("missed", "none"),
    "commit_precision": ("correct", "wrong_tool", "not_contribution"),
    "commit_recall": ("credits", "none"),
    "mentions": ("attribution", "not_attribution"),
    "import_precision": ("correct", "not_loaded", "wrong_target"),
    "planning_precision": ("correct", "wrong_framework", "not_planning_document"),
}
LABEL_COLUMNS = ("item", "pass", "label", "tool", "mechanism", "attribution_correct", "paths",
                 "uncertain", "evidence", "note")
SAMPLES = tuple(LABELS)
# The samples that are drawn from an output CSV alone, with that CSV. `sample --only` draws them
# into the directory of an earlier draw. `report` reads each of them where the directory holds
# it, and the directory of an evaluation can lack both.
ONLY_SAMPLES = {"import_precision": import_detection.CSV_NAME,
                "planning_precision": planning_schema.CSV_NAME}


# -----------------------------------------------------------------------------
# Shared pieces
# -----------------------------------------------------------------------------

def wilson(successes: int, n: int, z: float = Z) -> tuple:
    """The Wilson score interval of a proportion, (low, high), or (0, 1) for no observations."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def stratified(strata: list) -> tuple:
    """The population-weighted proportion over strata of (population, successes, n) and a
    normal interval around it: (estimate, low, high). The variance takes each stratum's
    proportion with two successes and two failures added, (k + 2) / (n + 4), as Agresti and
    Coull adjust a proportion, and applies the finite population correction.

    A sample of `PATH_PRECISION_PER_CELL` rows a cell leaves most cells all correct. With their
    own proportions, the stratified variance is zero there and would state a certainty that so
    few rows cannot give. The weighted average of the cells' Wilson bounds would treat every
    cell's worst case as happening at once. Jeffreys posteriors would pull every all-correct
    cell toward one half, and their interval would then miss the estimate.
    """
    strata = [(w, k, n) for w, k, n in strata if n]
    total = sum(w for w, _, _ in strata)
    if not total:
        return (0.0, 0.0, 1.0)
    estimate = sum(w * k / n for w, k, n in strata) / total
    variance = 0.0
    for w, k, n in strata:
        adjusted = (k + 2) / (n + 4)
        correction = (w - n) / (w - 1) if w > 1 else 0.0
        variance += (w / total) ** 2 * adjusted * (1 - adjusted) / (n + 4) * correction
    half = Z * math.sqrt(variance)
    return (estimate, max(0.0, estimate - half), min(1.0, estimate + half))


def evidence_repositories(heuristics_dir: Path) -> set:
    """Every repository that an `evidence` link of a heuristics CSV names, lowercased."""
    found = set()
    for path in sorted(heuristics_dir.glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                for link in (row.get("evidence") or "").split():
                    m = re.match(r"https://github\.com/([^/]+)/([^/]+)", link)
                    if m and m.group(1) != "apps":
                        found.add(f"{m.group(1)}/{m.group(2)}".lower())
    return found


def read_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, columns: list, rows: list) -> None:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out.getvalue(), encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def keep_copy(path: Path, directory: Path) -> Path:
    """An xz-compressed copy of `path` in `directory`, under the name of `path` with `.xz`.
    The copy is written to that name with `.partial` appended and renamed to the `.xz` name once
    it is complete, so that a copy that stops part way leaves no file under the `.xz` name."""
    target = directory / f"{path.name}.xz"
    partial = target.with_name(target.name + ".partial")
    directory.mkdir(parents=True, exist_ok=True)
    with open(path, "rb") as source, lzma.open(partial, "wb", preset=commits.LOG_PRESET) as copy:
        shutil.copyfileobj(source, copy)
    partial.replace(target)
    return target


def tree_digest(root: Path) -> str:
    """The SHA-256 of a directory: every file under it, its path relative to `root` and its own
    SHA-256, in path order. The file lists and the per-repository commit records have one file
    per repository, and one digest records each directory."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(f"{path.relative_to(root).as_posix()}\0{sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def spread(rows: list, size: int, rng: random.Random) -> list:
    """Up to `size` rows of `rows`, at random, one per repository before a second of any."""
    shuffled = list(rows)
    rng.shuffle(shuffled)
    chosen, seen, rest = [], set(), []
    for row in shuffled:
        (rest if row["repo_name"] in seen else chosen).append(row)
        seen.add(row["repo_name"])
    return (chosen + rest)[:size]


def second_pass(items: list, rng: random.Random) -> set:
    """The items that a second pass labels: a seeded `SECOND_PASS_SHARE` of them."""
    count = math.ceil(len(items) * SECOND_PASS_SHARE)
    return set(rng.sample([row["item"] for row in items], count))


# -----------------------------------------------------------------------------
# The samples
# -----------------------------------------------------------------------------

def sample_path_precision(output_dir: Path, rng: random.Random, excluded: set) -> list:
    """Up to `PATH_PRECISION_PER_CELL` rows of every tool and mechanism of the mechanism CSVs."""
    cells = defaultdict(list)
    for mechanism, (filename, artifact) in heuristics.MECHANISM_CSVS.items():
        for row in read_csv(output_dir / filename):
            cells[(row["tool"], mechanism)].append({
                "repo_name": row["repo_name"], "tool": row["tool"], "mechanism": mechanism,
                "artifact": row[artifact], "github_link": row["github_link"]})
    items = []
    for (tool, mechanism), rows in sorted(cells.items()):
        eligible = [r for r in rows if r["repo_name"].lower() not in excluded]
        for row in spread(eligible, PATH_PRECISION_PER_CELL, rng):
            items.append({**row, "cell_rows": len(rows),
                          "local_path": f"data/validated/{repo_name_to_dir(row['repo_name'])}/"
                                        f"{row['artifact']}"})
    for number, row in enumerate(items, start=1):
        row["item"] = f"pp-{number:04d}"
    return items


def _artifacts_by_repository(output_dir: Path) -> dict:
    """{repository: {mechanism: the paths of the configuration artifacts that the CSV of the
    mechanism names in it}}, with other.csv under `other`."""
    found = defaultdict(lambda: defaultdict(set))
    for mechanism, (filename, artifact) in heuristics.MECHANISM_CSVS.items():
        for row in read_csv(output_dir / filename):
            found[row["repo_name"]][mechanism].add(row[artifact])
    return found


def candidate_places(rows: list) -> list:
    """The places that the path heuristics `rows` name for path recall, as paths that
    `reuse._glob` matches anywhere in a path: the path of every row except the rows that the
    comment above `CANDIDATE` leaves out, and for a `workflow_engine:` row the Markdown files of
    its directory."""
    places = set()
    for row in rows:
        mode, path = row["match_mode"], row["path"]
        if (heuristics.plugin_placeholder(path) or mode.startswith(NO_CANDIDATE_MODES)
                or row["mechanism"] == "plugins" and "/" not in path):
            continue
        if mode.startswith("workflow_engine:"):
            path = f"{path}/*{AGENTIC_WORKFLOW_SUFFIX}"
        places.add(path)
    return sorted(places)


def candidate_pattern(rows: list) -> re.Pattern:
    """The pattern of the candidate paths: `CANDIDATE` or a place of `candidate_places`, which
    matches where `reuse._glob` of the place matches. The places share the start and the end of
    their globs, and the pattern tests them once rather than once per place, which makes the
    search of a long file list many times faster."""
    places = "|".join(map(reuse._glob_components, candidate_places(rows)))
    return re.compile(f"{CANDIDATE.pattern}|{reuse._GLOB_START}(?:{places}){reuse._GLOB_END}",
                      re.IGNORECASE)


def candidates(entries: list, vendored: set, pattern: re.Pattern) -> list:
    """The paths of a file list that `pattern` (`candidate_pattern`) matches, outside vendored
    directories."""
    return [path for _mode, path in entries
            if pattern.search(path) and not vendored & set(path.split("/")[:-1])]


def _lists(path: str, artifacts: set) -> bool:
    """Whether `artifacts` contains the path, or a directory that contains it."""
    return path in artifacts or any(path.startswith(a.rstrip("/") + "/") for a in artifacts)


def detected(path: str, artifacts: dict) -> str:
    """`yes` when the CSV of a mechanism lists the path, or a directory that contains it, `other`
    when only other.csv does, and empty otherwise. `artifacts` is a repository's entry of
    `_artifacts_by_repository`. A path that only other.csv lists is a file of a configuration
    directory that the detection gave no mechanism."""
    if any(_lists(path, found) for mechanism, found in artifacts.items() if mechanism != "other"):
        return "yes"
    return "other" if _lists(path, artifacts.get("other", set())) else ""


def sample_path_recall(output_dir: Path, file_lists_dir: Path, rng: random.Random,
                       excluded: set) -> tuple:
    """Repositories without detected configuration and with it, one item each, and their
    candidate paths, up to `CANDIDATES_PER_REPOSITORY` of a repository at random.

    A repository without detected configuration is a repository of `ai_commits-repos.csv` that
    no output CSV lists, other.csv included, and that is not a clone failure (`CLONE_FAILURES`),
    such as a repository from which the cloning step copied no file or one from which the
    cleanup removed every collected file because the detection found no mechanism. In the
    second, the cloning step copied files that the detection did not accept as configuration."""
    repos_rows = read_csv(output_dir / "ai_commits-repos.csv")
    artifacts = _artifacts_by_repository(output_dir)

    def listed(repo):
        return (file_lists_dir / f"{repo_name_to_dir(repo)}{clone.FILE_LIST_SUFFIX}").is_file()

    pools = {
        "unconfigured": sorted(r["repo_name"] for r in repos_rows
                               if r["outcome"] not in CLONE_FAILURES
                               and r["repo_name"] not in artifacts),
        "configured": sorted(r["repo_name"] for r in repos_rows if r["repo_name"] in artifacts),
    }
    sizes = {"unconfigured": RECALL_UNCONFIGURED, "configured": RECALL_CONFIGURED}
    pattern = candidate_pattern(heuristics.load_config_path_patterns())
    items, paths = [], []
    for stratum, pool in pools.items():
        eligible = [r for r in pool if r.lower() not in excluded and listed(r)]
        for repo in sorted(rng.sample(eligible, min(sizes[stratum], len(eligible)))):
            entries = clone.read_file_list(
                file_lists_dir / f"{repo_name_to_dir(repo)}{clone.FILE_LIST_SUFFIX}")
            found = candidates(entries, heuristics.VENDORED_DIRS, pattern)
            chosen = sorted(rng.sample(found, min(CANDIDATES_PER_REPOSITORY, len(found))))
            item = f"pr-{len(items) + 1:04d}"
            items.append({"item": item, "stratum": stratum, "repo_name": repo,
                          "candidates_in_repo": len(found), "candidates_listed": len(chosen),
                          "repositories_in_stratum": len(pool)})
            paths += [{"item": item, "path": path,
                       "detected": detected(path, artifacts.get(repo, {}))}
                      for path in chosen]
    return items, paths


def sample_commit_precision(output_dir: Path, rng: random.Random, excluded: set) -> list:
    """`COMMIT_PRECISION_PER_TOOL` commits per tool of the commit CSV and of the CSV of the
    tools outside the six, and one commit for each heuristic that these commits leave out."""
    columns = ("repo_name", "commit_sha", "github_link", "ai_tool", "matched_heuristics",
               "attributions", "commit_message")
    by_tool, by_heuristic, items = defaultdict(list), defaultdict(list), []
    for row in read_csv(output_dir / "all_ai_commits.csv"):
        if row["repo_name"].lower() in excluded:
            continue
        slim = {c: row[c] for c in columns}
        for tool in row["ai_tool"].split(","):
            by_tool[tool].append(slim)
        for heuristic in filter(None, row["matched_heuristics"].split(";")):
            by_heuristic[heuristic].append(slim)
    tool_of = {row["id"]: row["tool"] for row in commits.AI_COMMIT_PATTERNS}
    for tool in sorted(by_tool):
        for row in spread(by_tool[tool], COMMIT_PRECISION_PER_TOOL, rng):
            items.append({**row, "stratum": f"tool:{tool}", "tool": tool,
                          "stratum_rows": len(by_tool[tool])})
    covered = {h for row in items for h in row["matched_heuristics"].split(";")}
    for heuristic in sorted(set(by_heuristic) - covered):
        row = rng.choice(by_heuristic[heuristic])
        items.append({**row, "stratum": f"heuristic:{heuristic}", "tool": tool_of[heuristic],
                      "stratum_rows": len(by_heuristic[heuristic])})
        covered.update(row["matched_heuristics"].split(";"))
    other = [{c: row[c] for c in columns} for row in read_csv(output_dir / "other_ai_commits.csv")
             if row["repo_name"].lower() not in excluded]
    for row in spread(other, COMMIT_PRECISION_PER_TOOL, rng):
        items.append({**row, "stratum": "other_tools", "tool": row["ai_tool"],
                      "stratum_rows": len(other)})
    for number, row in enumerate(items, start=1):
        row["item"] = f"cp-{number:04d}"
    return items


def read_log(path: Path):
    """The commits of a commit log, in the order that `git log` wrote them."""
    with lzma.open(path, "rb") as f:
        yield from commits._nul_records_from(f)


# The author heuristics of both commit heuristics files other than a convention, as (tool,
# pattern). Such a heuristic reads the account of the tool or of an agent that runs it (`account`
# in the Attributions of docs/heuristics.md), and no person's.
_ACCOUNT_HEURISTICS = tuple(
    (row["tool"], heuristics.compile_commit_pattern(row))
    for row in (*commits.AI_COMMIT_PATTERNS, *commits.OTHER_AI_COMMIT_PATTERNS)
    if row["scope"] == "author" and row["attribution"] != "convention")
# The six tools by name, in lowercase.
_SIX_TOOLS = frozenset(row["tool"].lower() for row in commits.AI_COMMIT_PATTERNS)
# A parenthesis in a name, which can hold one more, as in `(Claude Opus 4.8 (1M context))`.
_PARENTHESIS = re.compile(r"\((?:[^()]|\([^()]*\))*\)")
# A mailbox of a service that reads no mail, followed by a separator and more or by nothing, at
# the domain of an automation: the internal bots of Weblate (`DEFAULT_INTERNAL_BOT_EMAIL_TEMPLATE`
# in weblate/auth/defaults.py of weblate 06ca66345304: "noreply-{scope}-{name}@weblate.org") and
# the default address of Crowdin's GitHub action (`github_user_email` in action.yml of
# crowdin/github-action d651c77756ac: 'support+bot@crowdin.com').
_SERVICE_MAILBOX = re.compile(r"(?:noreply|no-reply|support)(?:[-+_.].*)?")
# The escapes of a zero-width space after the `@` of a mention, with which Dependabot and Renovate
# keep a mention in the release notes that they quote from notifying or linking the login: the
# character, which Dependabot puts into a code span (`insert_zero_width_space_in_mention` of
# dependabot-core: `mention.sub("@", "@\u200B")`), and its HTML entities, as Renovate writes one
# (`sanitizeMarkdown` of renovate: `part.replace(regEx(/@/g), '@&#8203;')`). A pattern of the
# mentions CSV holds an entity as `commits.mention_pattern` writes it, with `0` for its digits.
_ZERO_WIDTH_SPACES = ("\u200b", "&#8203;", "&#x200b;")
_ESCAPES = tuple(dict.fromkeys((*_ZERO_WIDTH_SPACES,
                                *map(commits.mention_pattern, _ZERO_WIDTH_SPACES))))
# A login of letters, digits and hyphens. It can end in a hyphen, because GitHub's API records
# accounts of the type User with such a login.
_LOGIN = r"[A-Za-z0-9][A-Za-z0-9-]*"
# A GitHub mention of a login: `@`, which an escape of a zero-width space can follow, and a login,
# or the login of an app's bot user with `[bot]`. A mention is not part of an address. A login
# before a slash is the scope of an npm package, as in `@tiptap/extension-collaboration-cursor`,
# and a login before a dot and a letter names a file, as in `@AGENTS.md`.
_LOGIN_MENTION = re.compile(r"(?<![\w.+\-@/])@(?P<escape>(?i:"
                            + "|".join(map(re.escape, _ESCAPES)) + r"))?"
                            rf"(?P<login>{_LOGIN}(?:\[bot\])?)(?![\w/@\-\[]|\.\w)")
# A link to the profile of a GitHub account, with the login as its whole path: at github.com, as
# Dependabot links a mention (`"#{mention_profile_base_url}/#{mention.delete_prefix('@')}"`),
# and at the hosts to which Renovate redirects the links to github.com in what it quotes,
# togithub.com (renovate f7f0d8750f60: "Change github redirect links from renovatebot.com/gh/ to
# togithub.com") and redirect.github.com (`massageMarkdown` of renovate: "replace all github.com
# links with redirect.github.com"). A link with more path after the first component, such as the
# link of a repository, links no profile.
_PROFILE_LINK = re.compile(r"(?<![\w.\-])(?:https?://)?(?:(?:www|redirect)\.|to)?github\.com/"
                           rf"(?P<login>{_LOGIN})(?![\w/\-]|\.\w)", re.IGNORECASE)
# Code in Markdown, as CommonMark reads it, where an `@` is code and no mention of a person, such
# as the route decorator `@generation`. A code span is a run of backticks, the code and a run of
# as many backticks on the same line, neither run next to another backtick. A fenced code block
# opens at a line that starts with at least three backticks or tildes after up to three spaces,
# unless another backtick follows a run of backticks on the line. It ends at the next line that
# starts with at least as many of the same character, with only whitespace after them, or at the
# end of the text.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def _code(text: str) -> list:
    """The (start, end) offsets of the code of a text: each fenced code block with its fences,
    and each code span outside such a block."""
    found, fence, start = [], None, 0
    for line in text.split("\n"):
        offset, start = start, start + len(line) + 1
        run = _FENCE.fullmatch(line.rstrip("\r"))
        if fence is not None:
            found[-1] = (found[-1][0], start)
            if (run and run.group(1)[0] == fence[0] and len(run.group(1)) >= len(fence)
                    and not run.group(2).strip()):
                fence = None
        elif run and not (run.group(1)[0] == "`" and "`" in run.group(2)):
            fence = run.group(1)
            found.append((offset, start))
        else:
            found += [(offset + m.start(), offset + m.end()) for m in _CODE_SPAN.finditer(line)]
    return found


def _account_tools(text: str) -> set:
    """The tools of the author heuristics other than a convention that read `text`."""
    return {tool for tool, pattern in _ACCOUNT_HEURISTICS if pattern.search(text)}


def _begins_with_one_of_the_six(text: str) -> bool:
    """Whether a text begins with the name of one of the six tools as a word, in the form in
    which the mentions CSV reads a tool in a line (`commits.tool_mentions`)."""
    return commits._MENTION.match(text) is not None


def _is_one_of_the_six(text: str) -> bool:
    """Whether the words of a text, without the mark of an app and the word `bot`, are the name of
    one of the six tools, alone or followed by words for the kind of tool that `unmatched` reads
    after a tool's name, as in `gemini-cli`. A person's login can begin with a tool's name, as the
    login in the line of mn-0153 of the evaluation of 2026-10-05 does."""
    words = unmatched._words(text.lower())
    return (bool(words) and words[0] in _SIX_TOOLS
            and all(word in unmatched._TOOL_WORDS for word in words[1:]))


def _names_the_tool(name: str, address: str, tools: set) -> bool:
    """Whether a name, in lowercase, names one of `tools`, the tools of the account heuristics
    that read the address of its identity. At GitHub's noreply address, the name has the words of
    the login, without the mark of an app and the word `bot`. At any other address, the name
    begins with the tool, as `unmatched` reads a tool at the start of a name, or with a label of
    the domain of the address and then the tool, as in `GitHub Copilot <copilot@github.com>`,
    where copilot-trailer-02 reads `github` before `copilot`. At either, an account heuristic of
    the same tool can read the name instead."""
    if tools & _account_tools(name):
        return True
    login = unmatched._github_account(address)
    if login is not None:
        words = unmatched._words(name)
        return bool(words) and words == unmatched._words(login)
    named = {tool.lower() for tool in tools}
    first, _, rest = name.partition(" ")
    organization = first in address.partition("@")[2].split(".")
    return unmatched._tool_of(name) in named or (organization and unmatched._tool_of(rest) in named)


def _reads_no_mail(address: str) -> bool:
    """Whether an address is the GitHub address of an app or a mailbox `noreply` or `no-reply`."""
    lowered = address.lower()
    return unmatched._APP_MARK in lowered or lowered.partition("@")[0] in ("noreply", "no-reply")


def _at_the_automation(name: str, address: str) -> bool:
    """Whether a name, in lowercase, that a row of not_ai_commits.csv reads before any
    parenthesis is at a mailbox of the automation itself, at a domain with that row as a label:
    a mailbox of a service that reads no mail, alone or before a separator (`_SERVICE_MAILBOX`), as
    `Weblate <noreply-mt-weblate@weblate.org>`, or a mailbox that spells the words of the name
    before the row, as `Hosted Weblate <hosted@weblate.org>`, the push user of Hosted Weblate. A
    mailbox with any other words, such as the one in `Weblate Jane <jane@weblate.org>`, can be a
    person's."""
    local, _, domain = address.partition("@")
    head = name.partition("(")[0].strip()
    spelled = unmatched._WORD_SEPARATORS.sub("", head)
    return any(_SERVICE_MAILBOX.fullmatch(local) is not None
               or unmatched._WORD_SEPARATORS.sub("", local) + row == spelled
               for row in unmatched.NOT_AI_COMMITS
               if unmatched.explains(row, head) and row in domain.split("."))


def _keeps(name: str, address: str) -> bool:
    """Whether the tracked samples keep the identity `name <address>` whole: GitHub as the
    committer of a web commit, a bot at an address of no person, a tool at an address that an
    author heuristic other than a convention reads (`_names_the_tool`), an identity that such a
    heuristic reads with its name and address together and neither alone, and the name of one of
    the six tools at an address that reads no mail (`_is_one_of_the_six`, `_reads_no_mail`). A
    name names a bot when it has the mark of an app or the word `bot` (`unmatched._names_a_bot`)
    or when a row of not_ai_commits.csv reads it (`unmatched._negative_in`), as the row
    `gemini-cli-robot` reads the identity under which the release workflows of
    google-gemini/gemini-cli commit. Such a row also keeps the name at a mailbox of the
    automation itself (`_at_the_automation`)."""
    name, address = name.lower(), address.lower()
    if name == "github" and address == "noreply@github.com":
        return True
    if ((unmatched._names_a_bot(name) or unmatched._negative_in(name))
            and unmatched._address_names_no_person(name, address)):
        return True
    if _at_the_automation(name, address):
        return True
    tools = _account_tools(address)
    if tools:
        return _names_the_tool(name, address, tools)
    if _account_tools(f"{name} {address}") - _account_tools(name):
        return True
    return _reads_no_mail(address) and _is_one_of_the_six(name)


def _without_person(name: str, address: str) -> str:
    """The identity `name <address>` as the tracked samples keep it, with `PERSON_MARK` for what
    can name a person. When `_keeps` keeps the last words of the name with the address, they stay
    after `PERSON_MARK`, which replaces the words before them. Otherwise `PERSON_MARK` replaces the
    name, followed by each parenthesis of the name that begins with the name of one of the six
    tools, and by the address when an author heuristic other than a convention reads it."""
    if _keeps(name, address):
        return f"{name} <{address}>"
    words = name.split()
    for start in range(1, len(words)):
        rest = " ".join(words[start:])
        if _keeps(rest, address):
            return f"{PERSON_MARK} {rest} <{address}>"
    marked = " ".join([PERSON_MARK, *(parenthesis for parenthesis in _PARENTHESIS.findall(name)
                                      if _begins_with_one_of_the_six(parenthesis[1:].lstrip()))])
    return f"{marked} <{address}>" if _account_tools(address.lower()) else marked


def _keeps_login(login: str) -> bool:
    """Whether a GitHub login is the login of a tool or a bot: a login that names a bot
    (`unmatched._names_a_bot`) or that is a whole row of not_ai_commits.csv, as `dependabot` and
    `gemini-cli-robot` are, a login that an author heuristic other than a convention reads alone
    or with the mark of an app, as `copilot-swe-agent` and `coderabbitai`, or the name of one of
    the six tools (`_is_one_of_the_six`), as `claude` and `gemini-cli` are. A login that ends in a
    hyphen is not the name of a tool, even when it begins with one."""
    login = login.lower()
    return bool(unmatched._names_a_bot(login) or login in unmatched.NOT_AI_COMMITS
                or _account_tools(login) or _account_tools(login + unmatched._APP_MARK)
                or (_is_one_of_the_six(login) and not login.endswith("-")))


def _logins_without_person(text: str) -> str:
    """A text with `PERSON_MARK` for each GitHub mention of a login and for the login of each link
    to a profile (`_PROFILE_LINK`), except the login of a tool or a bot (`_keeps_login`). An `@`
    directly before a login in a code span or a fenced code block (`_code`) is code, and no
    mention, such as a decorator. An `@` with an escape of a zero-width space after it is a
    mention there as well, because the escape only keeps the mention from notifying or linking the
    login, as in the code span that Dependabot writes for a mention, `@`, U+200B and the login. A
    profile link is marked in code as well."""
    code = _code(text)
    def mention(match: re.Match) -> str:
        if (match.group("escape") is None
                and any(start <= match.start() < end for start, end in code)):
            return match.group(0)
        return match.group(0) if _keeps_login(match.group("login")) else PERSON_MARK
    def link(match: re.Match) -> str:
        if _keeps_login(match.group("login")):
            return match.group(0)
        return match.group(0)[:match.start("login") - match.start()] + PERSON_MARK
    return _PROFILE_LINK.sub(link, _LOGIN_MENTION.sub(mention, text))


def identity_without_person(identity: str) -> str:
    """An author or committer identity of `commit_recall.csv` as the tracked sample keeps it,
    because the commit logs stay out of the repository and out of the release: as
    `_without_person` keeps it, with each GitHub mention of a person's login and the login of each
    link to a person's profile marked (`_logins_without_person`). `commit_recall-texts.csv` keeps
    the identity for the labelers, and git ignores that file.

    The identity of a bot stays, and so does an identity at an address that an author heuristic
    other than a convention reads when its name names the tool of that heuristic, as `Claude
    <noreply@anthropic.com>` at the address of `claude-author-04` does. The login of GitHub's
    noreply address can lack the mark of a bot, as `Copilot` in
    `198982749+Copilot@users.noreply.github.com` of the bot user of the app copilot-swe-agent
    (`copilot-author-05`) does, and the name of the app, `copilot-swe-agent[bot]`, stays there
    because `copilot-author-02` reads it. Any other name at such an address can be a person's, and
    the address stays after `PERSON_MARK`. An identity also stays when such a heuristic reads its
    name together with its address and neither alone, as `codex-author-07` reads `Codex
    <noreply@openai.com>`."""
    name, _, address = identity.rpartition(" <")
    name, address = name.strip(), address.rstrip(">").strip()
    kept = _without_person(name, address)
    return _logins_without_person(identity if kept == f"{name} <{address}>" else kept)


def _address_without_person(address: str) -> str:
    """An address without a name as the tracked samples keep it: the address, when
    `identity_without_person` keeps it under the name that the address gives, the login of
    GitHub's noreply address or else the mailbox, or `PERSON_MARK`."""
    lowered = address.lower()
    name = unmatched._github_account(lowered) or lowered.partition("@")[0]
    return PERSON_MARK if identity_without_person(f"{name} <{address}>") == PERSON_MARK else address


def line_without_people(line: str) -> str:
    """A line of a commit as the tracked sample of mentions keeps it, or a whole message as the
    tracked sample of commit recall keeps it, with each GitHub mention of a person's login and the
    login of each link to a person's profile marked (`_logins_without_person`). After the token of
    a trailer, the name before an address is all that follows the token on its line, and the
    identity stays as `identity_without_person` keeps it. Elsewhere in the line, the words before
    an address can include words of the line before the name, as `Generated with` in `Generated
    with Cursor Agent <cursoragent@cursor.com>`, and all of them stay when an identity of their
    last words stays. Otherwise the identity of all of them stays as `identity_without_person`
    keeps it. An address alone stays as `_address_without_person` keeps it. A name without an
    address stays, and so does a login outside a mention and a link to a profile."""
    def mark(match: re.Match) -> str:
        if match.group("alone") is not None:
            return _address_without_person(match.group("alone"))
        text, token = match.group(0), match.group("token") or ""
        if token:
            name, address = match.group("value").strip(), match.group("trailer_address")
        else:
            name, address = match.group("name").strip(), match.group("address")
            words = name.split()
            if any(_keeps(" ".join(words[start:]), address) for start in range(len(words))):
                return text
        if not name:
            return text.rpartition("<")[0] + (
                f"<{address}>" if _address_without_person(address) == address else PERSON_MARK)
        kept = _without_person(name, address)
        return text if kept == f"{name} <{address}>" else token + kept
    return _logins_without_person(_NAMED_ADDRESS.sub(mark, line))


def without_people(item: dict) -> dict:
    """A commit of the sample of recall as the tracked sample keeps it, with its link as the
    evidence. The message of a commit that a heuristic credits stays as `line_without_people`
    keeps a line, and the message of any other commit is left out, because it can name people in
    any words."""
    credited = item["detected"] or item["detected_other"]
    return {**item, "author": identity_without_person(item["author"]),
            "committer": identity_without_person(item["committer"]),
            "message": line_without_people(item["message"]) if credited else ""}


def sample_commit_recall(output_dir: Path, logs_dir: Path, rng: random.Random,
                         excluded: set) -> list:
    """`COMMIT_RECALL_COMMITS` commits that the cloning step read, each equally likely, with
    what the current heuristics find in them."""
    repos_rows = [r for r in read_csv(output_dir / "ai_commits-repos.csv")
              if r["outcome"] == "history_read" and r["commits_read"]
              and r["repo_name"].lower() not in excluded
              and commits.stored_log_path(logs_dir, r["repo_name"]).is_file()]
    total = sum(int(r["commits_read"]) for r in repos_rows)
    drawn = sorted(rng.sample(range(total), min(COMMIT_RECALL_COMMITS, total)))
    wanted, start, k = defaultdict(set), 0, 0
    for row in repos_rows:
        end = start + int(row["commits_read"])
        while k < len(drawn) and drawn[k] < end:
            wanted[row["repo_name"]].add(drawn[k] - start)
            k += 1
        start = end
    items = []
    for repo in sorted(wanted):
        for index, record in enumerate(read_log(commits.stored_log_path(logs_dir, repo))):
            if index not in wanted[repo]:
                continue
            fields = (record["author_name"], record["author_email"], record["committer_name"],
                      record["committer_email"], record["subject"], record["body"])
            items.append({
                "repo_name": repo, "commit_sha": record["hash"],
                "github_link": f"https://github.com/{repo}/commit/{record['hash']}",
                "author": f"{record['author_name']} <{record['author_email']}>",
                "committer": f"{record['committer_name']} <{record['committer_email']}>",
                "message": record["message"].rstrip("\n"),
                "detected": ",".join(sorted(commits.detect_ai_tools_in_fields(*fields))),
                "detected_other": ",".join(sorted(commits.detect_other_ai_tools_in_fields(*fields))),
                "commits_read": total})
    for number, row in enumerate(items, start=1):
        row["item"] = f"cr-{number:04d}"
    return items


def read_now(line: str) -> set:
    """The tools that the commit heuristics credit for a line of the mentions CSV, read as a line
    of a commit message without the author and committer fields."""
    return commits.detect_ai_tools_in_fields("", "", "", "", "chore: update", line,
                                             check_author=False)


def sample_mentions(mentions_csv: Path, rng: random.Random) -> list:
    """`MENTIONS` phrasings of the mentions CSV, each drawn with a weight of its commits, and
    whether a heuristic matches its line now."""
    rows = read_csv(mentions_csv)
    keyed = sorted(rows, key=lambda r: rng.random() ** (1 / max(1, int(r["commits"]))),
                   reverse=True)
    items = []
    for row in keyed[:MENTIONS]:
        items.append({"tool": row["tool"], "pattern": row["pattern"], "commits": row["commits"],
                      "repos": row["repos"], "example_sha": row["shas"].split(";")[0],
                      "line": row["line"], "read_now": ",".join(sorted(read_now(row["line"]))),
                      "commits_in_csv": sum(int(r["commits"]) for r in rows)})
    for number, row in enumerate(items, start=1):
        row["item"] = f"mn-{number:04d}"
    return items


def stratified_rows(rows: list, key, size: int, rng: random.Random, excluded: set) -> tuple:
    """(items, strata from the evidence repositories): up to `size` rows of each stratum of
    `rows`, which `key` names, drawn as `spread` draws them, each with its `stratum`, the rows of
    the stratum in `stratum_rows` and `from_evidence`. A stratum with a row outside the
    repositories of `excluded` is drawn from such rows. A stratum with every row in such a
    repository is drawn from all its rows, with the same size and generator, because it would
    otherwise have no item. Its items have `from_evidence` set to `yes`, and {stratum: rows}
    names each such stratum. The strata are drawn in the order of their names."""
    strata = defaultdict(list)
    for row in rows:
        strata[key(row)].append(row)
    items, from_evidence = [], {}
    for stratum, members in sorted(strata.items()):
        eligible = [r for r in members if r["repo_name"].lower() not in excluded]
        if not eligible:
            from_evidence[stratum] = len(members)
        for row in spread(eligible or members, size, rng):
            items.append({**row, "stratum": stratum, "stratum_rows": len(members),
                          "from_evidence": "" if eligible else "yes"})
    return items, from_evidence


# The columns of imports.csv and of planning-documents.csv that an item keeps.
IMPORT_COLUMNS = ("repo_name", "head_sha", "tool", "heuristic", "root", "importer",
                  "importer_category", "line", "import", "target", "target_status",
                  "target_category", "target_location", "depth", "github_link")
PLANNING_COLUMNS = ("repo_name", "head_sha", planning_schema.PATH_COLUMN,
                    *planning_schema.DETAIL_COLUMNS, "github_link")
# The tree under data/collection/ that holds a found target, by the archive that publishes it
# (`import_detection.location`): the validated tree for the archive of the collected files, and
# the imported tree, to which the cloning step copies every file that an import reaches.
TARGET_TREES = {import_detection.COLLECTED_ARCHIVE: import_detection.VALIDATED_DIR,
                import_detection.IMPORTED_ARCHIVE: import_detection.IMPORTED_DIR}


def sample_import_precision(output_dir: Path, rng: random.Random, excluded: set) -> tuple:
    """(items, strata from the evidence repositories) of imports.csv: up to
    `IMPORT_PRECISION_PER_CELL` rows of each import heuristic and target status
    (`stratified_rows`). An item names the importing file in the validated tree where it starts
    the chain, and in the imported tree where an import reached it, and a found target in the
    tree of its `target_location`."""
    rows = [{c: row[c] for c in IMPORT_COLUMNS}
            for row in read_csv(output_dir / ONLY_SAMPLES["import_precision"])]
    items, from_evidence = stratified_rows(
        rows, lambda r: f"{r['heuristic']}/{r['target_status']}", IMPORT_PRECISION_PER_CELL,
        rng, excluded)
    for number, row in enumerate(items, start=1):
        directory = repo_name_to_dir(row["repo_name"])
        tree = (import_detection.VALIDATED_DIR if row["importer"] == row["root"]
                else import_detection.IMPORTED_DIR)
        target_tree = TARGET_TREES.get(row["target_location"])
        row["item"] = f"im-{number:04d}"
        row["importer_local_path"] = (tree / directory / row["importer"]).as_posix()
        row["target_local_path"] = ((target_tree / directory / row["target"]).as_posix()
                                    if target_tree else "")
    return items, from_evidence


def sample_planning_precision(output_dir: Path, rng: random.Random, excluded: set) -> tuple:
    """(items, strata from the evidence repositories) of planning-documents.csv: up to
    `PLANNING_PRECISION_PER_CELL` rows of each framework or tool (`stratified_rows`), each with
    the planning document in the validated tree."""
    rows = [{c: row[c] for c in PLANNING_COLUMNS}
            for row in read_csv(output_dir / ONLY_SAMPLES["planning_precision"])]
    items, from_evidence = stratified_rows(rows, lambda r: r["framework"],
                                           PLANNING_PRECISION_PER_CELL, rng, excluded)
    for number, row in enumerate(items, start=1):
        row["item"] = f"pd-{number:04d}"
        row["local_path"] = (import_detection.VALIDATED_DIR / repo_name_to_dir(row["repo_name"])
                             / row[planning_schema.PATH_COLUMN]).as_posix()
    return items, from_evidence


def sample_output_csvs(names: list, output_dir: Path, seed: int, excluded: set) -> tuple:
    """({sample: items}, {sample: strata from the evidence repositories}) of the samples `names`
    of `ONLY_SAMPLES`, each with a generator of its own."""
    draws = {"import_precision": (sample_import_precision, "im"),
             "planning_precision": (sample_planning_precision, "pd")}
    samples, from_evidence = {}, {}
    for name in names:
        function, suffix = draws[name]
        samples[name], from_evidence[name] = function(
            output_dir, random.Random(f"{seed}:{suffix}"), excluded)
    return samples, from_evidence


# -----------------------------------------------------------------------------
# The measurements
# -----------------------------------------------------------------------------

def commit_heuristic_attribution(output_dir: Path) -> list:
    """Per commit heuristic of both files: commits, repositories, the commits for which no other
    heuristic credits the tool, and the repositories that would lose every commit of the tool
    without it."""
    rows = []
    for csv_name, patterns in (("all_ai_commits.csv", commits.AI_COMMIT_PATTERNS),
                               ("other_ai_commits.csv", commits.OTHER_AI_COMMIT_PATTERNS)):
        tool_of = {row["id"]: row["tool"] for row in patterns}
        counted = defaultdict(lambda: {"commits": 0, "repos": set(), "sole_commits": 0})
        # Per (repository, tool): the number of commits that each heuristic alone credits.
        credits = defaultdict(Counter)
        commits_of = Counter()
        for row in read_csv(output_dir / csv_name):
            by_tool = defaultdict(list)
            for heuristic in filter(None, row["matched_heuristics"].split(";")):
                by_tool[tool_of[heuristic]].append(heuristic)
            for tool, found in by_tool.items():
                commits_of[(row["repo_name"], tool)] += 1
                for heuristic in found:
                    entry = counted[heuristic]
                    entry["commits"] += 1
                    entry["repos"].add(row["repo_name"])
                    if len(found) == 1:
                        entry["sole_commits"] += 1
                        credits[(row["repo_name"], tool)][heuristic] += 1
        lost = Counter(heuristic for key, sole in credits.items() for heuristic, n in sole.items()
                       if len(sole) == 1 and n == commits_of[key])
        for row in patterns:
            entry = counted[row["id"]]
            rows.append({"csv": csv_name, "id": row["id"], "tool": row["tool"],
                         "scope": row["scope"], "attribution": row["attribution"], "commits": entry["commits"],
                         "repos": len(entry["repos"]), "sole_commits": entry["sole_commits"],
                         "repos_lost_if_removed": lost[row["id"]]})
    return rows


def path_heuristic_yield(output_dir: Path) -> list:
    """Per path heuristic: the rows of its mechanism's CSV with a tool that it names and a
    configuration artifact that its path reads, and the rows that no other heuristic reads. The
    detection records no heuristic per row, and this function therefore matches the path as
    `reuse` does, anywhere in the configuration artifact's path."""
    rows = heuristics.load_config_path_patterns()
    published = defaultdict(list)
    for mechanism, (filename, artifact) in heuristics.MECHANISM_CSVS.items():
        for row in read_csv(output_dir / filename):
            published[mechanism].append((set(row["tool"].lower().split("|")), row[artifact],
                                         row["repo_name"]))
    globs = [re.compile(reuse._glob(row["path"]), re.IGNORECASE) for row in rows]
    readers = defaultdict(list)
    for index, row in enumerate(rows):
        tools = set(row["tool"].lower().split("|"))
        for k, (row_tools, artifact, repo) in enumerate(published.get(row["mechanism"], ())):
            if (tools & row_tools or "any" in tools | row_tools) and globs[index].search(artifact):
                readers[(row["mechanism"], k)].append(index)
    counts = defaultdict(lambda: {"rows": 0, "repos": set(), "rows_alone": 0})
    for (mechanism, k), indexes in readers.items():
        for index in indexes:
            entry = counts[index]
            entry["rows"] += 1
            entry["repos"].add(published[mechanism][k][2])
            entry["rows_alone"] += len(indexes) == 1
    return [{"tool": row["tool"], "mechanism": row["mechanism"], "path": row["path"],
             "match_mode": row["match_mode"], "rows": counts[i]["rows"],
             "repos": len(counts[i]["repos"]), "rows_alone": counts[i]["rows_alone"]}
            for i, row in enumerate(rows)]


# -----------------------------------------------------------------------------
# sample
# -----------------------------------------------------------------------------

SAMPLE_COLUMNS = {
    "path_precision": ["item", "tool", "mechanism", "repo_name", "artifact", "github_link",
                       "local_path", "cell_rows"],
    "path_recall": ["item", "stratum", "repo_name", "candidates_in_repo", "candidates_listed",
                    "repositories_in_stratum"],
    "commit_precision": ["item", "stratum", "tool", "repo_name", "commit_sha", "github_link",
                         "ai_tool", "matched_heuristics", "attributions", "commit_message",
                         "stratum_rows"],
    "commit_recall": ["item", "repo_name", "commit_sha", "github_link", "author", "committer",
                      "message", "detected", "detected_other", "commits_read"],
    "mentions": ["item", "tool", "pattern", "commits", "repos", "example_sha", "line",
                 "read_now", "commits_in_csv"],
    "import_precision": ["item", "stratum", "tool", "heuristic", "target_status", "repo_name",
                         "head_sha", "root", "importer", "importer_category", "line", "import",
                         "target", "target_category", "target_location", "depth", "github_link",
                         "importer_local_path", "target_local_path", "stratum_rows",
                         "from_evidence"],
    "planning_precision": ["item", "stratum", "framework", "document", "repo_name", "head_sha",
                           planning_schema.PATH_COLUMN, "github_link", "local_path",
                           "stratum_rows", "from_evidence"],
}
# The entries of `sizes` in sample.json for the samples of `ONLY_SAMPLES`.
ONLY_SIZES = {"import_precision": {"import_precision_per_cell": IMPORT_PRECISION_PER_CELL},
              "planning_precision": {"planning_precision_per_cell": PLANNING_PRECISION_PER_CELL}}


def write_samples(out: Path, samples: dict, seed: int) -> None:
    """Mark the items of each sample of `samples` that a second pass labels, with a generator of
    the sample's own, and write the sample into `out`. The sample of commit recall writes its
    texts for the labelers beside it, and the samples of commit recall and of the mentions are
    tracked without what can name a person."""
    for name, items in samples.items():
        rng = random.Random(f"{seed}:second:{name}")
        labelled = [row for row in items if name != "path_recall" or row["candidates_listed"]]
        again = second_pass(labelled, rng)
        for row in items:
            row["second_pass"] = "yes" if row["item"] in again else ""
        tracked = items
        if name == "commit_recall":
            write_csv(out / RECALL_TEXTS, SAMPLE_COLUMNS[name] + ["second_pass"], items)
            tracked = [without_people(row) for row in items]
        elif name == "mentions":
            tracked = [{**row, "line": line_without_people(row["line"]),
                        "pattern": line_without_people(row["pattern"])} for row in items]
        write_csv(out / f"{name}.csv", SAMPLE_COLUMNS[name] + ["second_pass"], tracked)
        logs.info(COMPONENT, f"Drew {len(items):,} item(s) for the sample {name}, "
                             f"{len(again):,} of them for a second pass.")


def log_strata_from_evidence(from_evidence: dict) -> None:
    """Log each sample with strata drawn from the repositories that the `evidence` of a
    heuristic links, because every row of such a stratum is in such a repository."""
    for name, strata in from_evidence.items():
        if strata:
            listed = ", ".join(f"{stratum} with {rows:,} row(s)"
                               for stratum, rows in strata.items())
            logs.info(COMPONENT, f"The sample {name} draws the items of {len(strata):,} "
                                 f"strata ({listed}) from the repositories that the evidence of "
                                 f"the heuristics links, because these repositories hold every "
                                 f"row of each, and sets from_evidence to yes for these items.")


def heuristics_digests() -> dict:
    """The SHA-256 of every heuristics file, by its name."""
    return {p.name: sha256(p) for p in sorted(heuristics.HEURISTICS_DIR.glob("*.csv"))}


def input_name(path: Path) -> str:
    """The name under which sample.json records an input: its path in the repository, or its
    name outside the repository."""
    return str(path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path.name)


def sample_only(args) -> int:
    """Draw the samples of `--only` into the evaluation directory `--into`, which holds an
    earlier draw, and add a record of this draw to its sample.json under `later_draws`. The
    record names the samples, the evidence repositories left out, the sizes, the inputs and the
    heuristics with their SHA-256 and the strata drawn from the evidence repositories, and what
    sample.json recorded before stays unchanged. The draw takes the seed that sample.json
    records, and it exits 1 when the directory holds no draw, when it already holds one of the
    samples or its labels, when `--seed` names another seed or when an input is missing."""
    out, names = args.into, list(dict.fromkeys(args.only))
    record_path = out / "sample.json"
    if not record_path.is_file():
        logs.error(COMPONENT, f"{out} holds no draw ({record_path} is missing). Draw the "
                              f"samples of the collection first.")
        return 1
    record = json.loads(record_path.read_text(encoding="utf-8"))
    seed = record["seed"] if args.seed is None else args.seed
    if seed != record["seed"]:
        logs.error(COMPONENT, f"{out} was drawn with the seed {record['seed']}, and --seed "
                              f"gives {seed}. Leave out --seed to draw with the seed of the "
                              f"evaluation.")
        return 1
    # A second draw of a sample would give the labels of the first draw's items to other items.
    present = [name for name in names
               if (out / f"{name}.csv").exists() or (out / "labels" / f"{name}.csv").exists()]
    if present:
        logs.error(COMPONENT, f"{out} already holds the sample or the labels of "
                              f"{', '.join(present)}. Remove the sample and its labels to draw "
                              f"it again.")
        return 1
    inputs = [args.output_dir / ONLY_SAMPLES[name] for name in names]
    missing = [str(p) for p in inputs if not p.is_file()]
    if missing:
        logs.error(COMPONENT, f"{len(missing):,} input(s) are missing ({', '.join(missing)}).")
        return 1
    excluded = evidence_repositories(heuristics.HEURISTICS_DIR)
    samples, from_evidence = sample_output_csvs(names, args.output_dir, seed, excluded)
    write_samples(out, samples, seed)
    log_strata_from_evidence(from_evidence)
    sizes = {key: value for name in names for key, value in ONLY_SIZES[name].items()}
    record.setdefault("later_draws", []).append({
        "samples": names, "excluded_repositories": len(excluded),
        "sizes": {**sizes, "second_pass_share": SECOND_PASS_SHARE},
        "inputs": {input_name(p): sha256(p) for p in inputs},
        "heuristics": heuristics_digests(), "strata_from_evidence": from_evidence})
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    logs.info(COMPONENT, f"Wrote the samples {', '.join(names)} to {out} and the record of the "
                         f"draw to {record_path}.")
    return 0


def main_sample(argv=None) -> int:
    parser = cli.ArgumentParser(
        prog="python -m agentcontext evaluation sample",
        description="Draw the samples of the detection accuracy evaluation into the subdirectory "
                    "of --evaluation-dir named by the day on which the collection started, and "
                    "write two measurements of the heuristics: the commits that each commit "
                    "heuristic matches and the rows that each path heuristic matches. With "
                    "--only and --into, draw only the named samples of imports.csv and "
                    "planning-documents.csv into the directory of an earlier draw.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help="The directory of the collection's output CSVs (default: %(default)s)")
    parser.add_argument("--file-lists", type=Path,
                        help="data/file_lists of the collection's handover, required without "
                             "--only")
    parser.add_argument("--commit-logs", type=Path,
                        help="collect_commits of the collection's handover, required without "
                             "--only")
    parser.add_argument("--mentions", type=Path,
                        help="output/all_ai_commits-mentions.csv of the collection's handover, "
                             "required without --only")
    parser.add_argument("--finished", type=Path, default=unmatched.FINISHED,
                        help="The list of finished repositories of the collection. The day on "
                             "which the collection started comes from the record of the "
                             "collection in progress beside it, or from the list when there is "
                             "no such record (default: %(default)s)")
    parser.add_argument("--evaluation-dir", type=Path, default=EVALUATION_DIR,
                        help="The directory of the evaluations. The samples are written into "
                             "its subdirectory named by the day on which the collection "
                             "started (default: %(default)s)")
    parser.add_argument("--seed", type=int,
                        help=f"The seed of the random draws (default: {SEED}, and with --only "
                             f"the seed that the sample.json of --into records)")
    parser.add_argument("--only", nargs="+", choices=tuple(ONLY_SAMPLES), metavar="SAMPLE",
                        help=f"Draw only these samples ({' and '.join(ONLY_SAMPLES)}) into the "
                             f"directory of --into")
    parser.add_argument("--into", type=Path,
                        help="With --only, the directory of an evaluation that holds a draw, "
                             "such as data/collection/evaluation/<day>")
    args = parser.parse_args(argv)
    if args.only or args.into:
        if not (args.only and args.into):
            parser.error("--only and --into go together")
        return sample_only(args)
    if None in (args.file_lists, args.commit_logs, args.mentions):
        parser.error("--file-lists, --commit-logs and --mentions are required without --only")
    seed = SEED if args.seed is None else args.seed
    inputs = [args.output_dir / name for name in
              ("ai_commits-repos.csv", "all_ai_commits.csv", "other_ai_commits.csv",
               *(filename for filename, _ in heuristics.MECHANISM_CSVS.values()),
               *ONLY_SAMPLES.values())]
    missing = [str(p) for p in [*inputs, args.mentions, args.finished] if not p.is_file()]
    missing += [str(p) for p in (args.file_lists, args.commit_logs) if not p.is_dir()]
    if missing:
        logs.error(COMPONENT, f"{len(missing):,} input(s) are missing ({', '.join(missing)}).")
        return 1
    # In a checkout, git tracks the archives of the commit CSVs, and a CSV that differs from its
    # archive is not the committed input. The samples drawn from it would not be the samples of
    # the commit.
    for name in ("all_ai_commits.csv", "other_ai_commits.csv"):
        try:
            archives.check_copy(args.output_dir / name, csv_archive(args.output_dir / name))
        except (archives.CopyDiffers, archives.ArchiveUnreadable) as e:
            logs.error(COMPONENT, f"{str(e).rstrip('.')}.")
            return 1
    try:
        day = unmatched.collection_started(args.finished)
    except ValueError as e:
        logs.error(COMPONENT, f"{str(e).rstrip('.')}.")
        return 1
    out = args.evaluation_dir / day
    # Each draw numbers its items from 1. A second draw into the directory would give the labels
    # of the first draw's items to other items.
    if (out / "sample.json").exists():
        logs.error(COMPONENT, f"{out} already holds the samples of the collection that started "
                              f"on {day}. Remove the directory to draw them again.")
        return 1
    excluded = evidence_repositories(heuristics.HEURISTICS_DIR)
    recall_items, recall_paths = sample_path_recall(args.output_dir, args.file_lists,
                                                    random.Random(f"{seed}:pr"), excluded)
    write_csv(out / "path_recall_candidates.csv", ["item", "path", "detected"], recall_paths)
    of_output_csvs, from_evidence = sample_output_csvs(list(ONLY_SAMPLES), args.output_dir,
                                                       seed, excluded)
    samples = {
        "path_precision": sample_path_precision(args.output_dir, random.Random(f"{seed}:pp"),
                                                excluded),
        "path_recall": recall_items,
        "commit_precision": sample_commit_precision(args.output_dir,
                                                    random.Random(f"{seed}:cp"), excluded),
        "commit_recall": sample_commit_recall(args.output_dir, args.commit_logs,
                                              random.Random(f"{seed}:cr"), excluded),
        "mentions": sample_mentions(args.mentions, random.Random(f"{seed}:mn")),
        **of_output_csvs,
    }
    write_samples(out, samples, seed)
    log_strata_from_evidence(from_evidence)
    write_csv(out / "commit_heuristic_attribution.csv",
              ["csv", "id", "tool", "scope", "attribution", "commits", "repos", "sole_commits",
               "repos_lost_if_removed"], commit_heuristic_attribution(args.output_dir))
    write_csv(out / "path_heuristic_yield.csv",
              ["tool", "mechanism", "path", "match_mode", "rows", "repos", "rows_alone"],
              path_heuristic_yield(args.output_dir))
    record = {"seed": seed, "excluded_repositories": len(excluded),
              "sizes": {"path_precision_per_cell": PATH_PRECISION_PER_CELL,
                        "recall_unconfigured": RECALL_UNCONFIGURED,
                        "recall_configured": RECALL_CONFIGURED,
                        "candidates_per_repository": CANDIDATES_PER_REPOSITORY,
                        "commit_precision_per_tool": COMMIT_PRECISION_PER_TOOL,
                        "commit_recall_commits": COMMIT_RECALL_COMMITS, "mentions": MENTIONS,
                        **{key: value for sizes in ONLY_SIZES.values()
                           for key, value in sizes.items()},
                        "second_pass_share": SECOND_PASS_SHARE},
              "inputs": {input_name(p): sha256(p)
                         for p in [*inputs, args.mentions, args.finished]},
              "trees": {"file_lists": tree_digest(args.file_lists),
                        "commit_logs": tree_digest(args.commit_logs)},
              "heuristics": heuristics_digests(), "strata_from_evidence": from_evidence}
    # Every commit reclassification rewrites the mentions CSV, and the CSV that a
    # reclassification in a working copy wrote is in no handover. The copy keeps the CSV with the
    # SHA-256 that the record names, so that a later draw can read it again.
    keep_copy(args.mentions, out)
    (out / "sample.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    logs.info(COMPONENT, f"Wrote the samples and the measurements to {out}.")
    return 0


# -----------------------------------------------------------------------------
# report
# -----------------------------------------------------------------------------

def read_labels(evaluation_dir: Path, name: str) -> tuple:
    """({item: {pass: label row}}, problems) from `labels/<name>.csv`."""
    path = evaluation_dir / "labels" / f"{name}.csv"
    labels, problems = defaultdict(dict), []
    if not path.is_file():
        return labels, [f"{path} is missing"]
    for row in read_csv(path):
        if row["label"] not in LABELS[name]:
            problems.append(f"In {path.name}, pass {row['pass']} gives the item {row['item']} "
                            f"the label {row['label']!r}, not one of {LABELS[name]}")
        labels[row["item"]][row["pass"]] = row
    return labels, problems


def final_labels(items: list, labels: dict, resolutions: dict) -> tuple:
    """({item: the label row that the estimates use}, [rows for review.csv]): a resolution where
    a person gave one, else the first pass. An item goes to review when a pass is uncertain or the
    two passes disagree, unless it is resolved."""
    final, review = {}, []
    for row in items:
        passes = labels.get(row["item"], {})
        first, second = passes.get("1"), passes.get("2")
        if row["item"] in resolutions:
            final[row["item"]] = resolutions[row["item"]]
            continue
        if first is None:
            continue
        final[row["item"]] = first
        why = []
        if any((p or {}).get("uncertain") == "yes" for p in (first, second)):
            why.append("uncertain")
        if second is not None and second["label"] != first["label"]:
            why.append("passes disagree")
        if why:
            review.append({"item": row["item"], "why": "; ".join(why),
                           "first": first["label"], "second": (second or {}).get("label", ""),
                           "evidence": first["evidence"], "note": first["note"]})
    return final, review


def agreement(labels: dict) -> dict:
    """Percent agreement and Cohen's kappa over the items that both passes labeled."""
    pairs = [(p["1"]["label"], p["2"]["label"]) for p in labels.values() if "1" in p and "2" in p]
    if not pairs:
        return {"items": 0}
    observed = sum(a == b for a, b in pairs) / len(pairs)
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[k] * second[k] for k in first) / len(pairs) ** 2
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {"items": len(pairs), "agreement": round(observed, 4), "kappa": round(kappa, 4)}


def proportion(successes: int, n: int) -> dict:
    low, high = wilson(successes, n)
    return {"successes": successes, "n": n, "estimate": round(successes / n, 4) if n else None,
            "low": round(low, 4), "high": round(high, 4)}


def report_path_precision(items: list, final: dict) -> dict:
    cells = defaultdict(lambda: [0, 0, 0])
    for row in items:
        if row["item"] in final:
            cell = cells[(row["tool"], row["mechanism"])]
            cell[0] = int(row["cell_rows"])
            cell[1] += final[row["item"]]["label"] == "correct"
            cell[2] += 1
    by = {f"{t}/{m}": proportion(k, n) for (t, m), (_, k, n) in sorted(cells.items())}
    estimate, low, high = stratified(list(cells.values()))
    per_mechanism = defaultdict(list)
    for (_, mechanism), cell in cells.items():
        per_mechanism[mechanism].append(cell)
    return {"overall": {"estimate": round(estimate, 4), "low": round(low, 4), "high": round(high, 4),
                        "items": sum(c[2] for c in cells.values())},
            "per_mechanism": {m: dict(zip(("estimate", "low", "high"),
                                          (round(v, 4) for v in stratified(c))))
                              for m, c in sorted(per_mechanism.items())},
            "per_cell": by}


def report_path_recall(items: list, final: dict, candidate_rows: list) -> dict:
    """Per stratum, the share of repositories with configuration that the detection missed, and
    over the repositories with configuration, the share of configuration paths that it found. A
    repository with a file list that contains no candidate missed nothing. A configuration path
    that only other.csv lists, with `detected` set to `other`, counts as not found, because the
    detection gave it no mechanism."""
    found_by = {(row["item"], row["path"]): row["detected"] == "yes" for row in candidate_rows}
    per = defaultdict(lambda: [0, 0])
    population = {}
    files = [0, 0]
    unknown = []
    for row in items:
        population[row["stratum"]] = int(row["repositories_in_stratum"])
        chosen = final.get(row["item"])
        if chosen is None and int(row["candidates_listed"]):
            continue
        per[row["stratum"]][1] += 1
        per[row["stratum"]][0] += bool(chosen) and chosen["label"] == "missed"
        for path in filter(None, (chosen or {}).get("paths", "").split(";")):
            if (row["item"], path) not in found_by:
                unknown.append(f"{row['item']}: {path}")
                continue
            if row["stratum"] == "configured":
                files[0] += found_by[(row["item"], path)]
                files[1] += 1
    result = {f"{stratum}_repositories_with_missed_configuration": proportion(k, n)
              for stratum, (k, n) in sorted(per.items())}
    share = result.get("unconfigured_repositories_with_missed_configuration", {})
    result["unconfigured_repositories_in_collection"] = population.get("unconfigured", 0)
    result["missed_repositories_estimate"] = {
        k: round(share[k] * population.get("unconfigured", 0))
        for k in ("estimate", "low", "high") if share.get(k) is not None}
    result["configuration_paths_found_in_configured_repositories"] = proportion(*files)
    result["paths_not_in_the_candidates"] = unknown
    return result


def report_commit_precision(items: list, final: dict) -> dict:
    strata = defaultdict(lambda: [0, 0, 0])
    coverage = {}
    attributions = [0, 0]
    for row in items:
        if row["item"] not in final:
            continue
        chosen = final[row["item"]]
        correct = chosen["label"] == "correct"
        if chosen.get("attribution_correct") in ("yes", "no"):
            attributions[0] += chosen["attribution_correct"] == "yes"
            attributions[1] += 1
        if row["stratum"].startswith("heuristic:"):
            coverage[row["stratum"][len("heuristic:"):]] = chosen["label"]
            continue
        stratum = strata[row["stratum"]]
        stratum[0] = int(row["stratum_rows"])
        stratum[1] += correct
        stratum[2] += 1
    tools = [v for k, v in strata.items() if k.startswith("tool:")]
    estimate, low, high = stratified(tools)
    return {"tools": {"estimate": round(estimate, 4), "low": round(low, 4),
                      "high": round(high, 4), "items": sum(v[2] for v in tools)},
            "per_stratum": {k: proportion(v[1], v[2]) for k, v in sorted(strata.items())},
            "attributions_correct": proportion(*attributions),
            "heuristics_seen_once": coverage}


def report_commit_recall(items: list, final: dict) -> dict:
    credited, found, per_tool = 0, 0, defaultdict(lambda: [0, 0])
    false_positive = 0
    for row in items:
        chosen = final.get(row["item"])
        if chosen is None:
            continue
        detected = set(filter(None, row["detected"].split(",")))
        if chosen["label"] == "credits":
            tools = set(filter(None, chosen.get("tool", "").split("|")))
            credited += 1
            found += bool(tools & detected) if tools else bool(detected)
            for tool in tools:
                per_tool[tool][0] += tool in detected
                per_tool[tool][1] += 1
        elif detected:
            false_positive += 1
    return {"commits": len(items), "credited": credited,
            "recall": proportion(found, credited),
            "per_tool": {t: proportion(k, n) for t, (k, n) in sorted(per_tool.items())},
            "detected_without_credit": false_positive}


def report_mentions(items: list, final: dict) -> dict:
    labelled = [row for row in items if row["item"] in final]
    missed = [row for row in labelled
              if final[row["item"]]["label"] == "attribution" and not row["read_now"]]
    return {"phrasings": len(labelled),
            "attributions_no_heuristic_reads": proportion(len(missed), len(labelled)),
            "commits_of_those": sum(int(row["commits"]) for row in missed)}


def weighted_precision(strata: list) -> dict:
    """The precision over strata of [rows, correct items, labeled items], weighted by the rows
    of each stratum (`stratified`), with the labeled items and the rows of these strata, and
    without an estimate where no item is labeled."""
    items, rows = sum(n for _, _, n in strata), sum(w for w, _, _ in strata)
    if not items:
        return {"estimate": None, "low": None, "high": None, "items": 0, "rows": rows}
    estimate, low, high = stratified(strata)
    return {"estimate": round(estimate, 4), "low": round(low, 4), "high": round(high, 4),
            "items": items, "rows": rows}


def report_stratified_precision(items: list, final: dict) -> dict:
    """The precision of a sample of `ONLY_SAMPLES`, weighted by the rows of each stratum, over
    all strata and without the strata of the items with `from_evidence` set to `yes`, which the
    draw took from the repositories that the `evidence` of a heuristic links. The heuristics of
    such a stratum were read off these repositories, and the precision over all strata can
    therefore be biased toward the heuristics. The result also gives the precision of each
    stratum with its rows, the number of each label and {stratum: rows} of the strata drawn from
    those repositories."""
    strata = defaultdict(lambda: [0, 0, 0])
    labels = Counter()
    from_evidence = {row["stratum"]: int(row["stratum_rows"]) for row in items
                     if row["from_evidence"] == "yes"}
    for row in items:
        if row["item"] in final:
            stratum = strata[row["stratum"]]
            stratum[0] = int(row["stratum_rows"])
            stratum[1] += final[row["item"]]["label"] == "correct"
            stratum[2] += 1
            labels[final[row["item"]]["label"]] += 1
    return {"overall": weighted_precision(list(strata.values())),
            "without_strata_from_evidence": weighted_precision(
                [v for k, v in strata.items() if k not in from_evidence]),
            "per_stratum": {k: {**proportion(v[1], v[2]), "rows": v[0],
                                "from_evidence": k in from_evidence}
                            for k, v in sorted(strata.items())},
            "labels": dict(sorted(labels.items())),
            "strata_from_evidence": dict(sorted(from_evidence.items()))}


def main_report(argv=None) -> int:
    parser = cli.ArgumentParser(
        prog="python -m agentcontext evaluation report",
        description="Compute precision and recall with Wilson intervals from the labels of "
                    "the detection accuracy evaluation, and list the items that a person has "
                    "to resolve.")
    parser.add_argument("evaluation", type=Path,
                        help=f"The evaluation's directory, with the samples and labels/, such "
                             f"as {EVALUATION_DIR}/<day the collection started>")
    args = parser.parse_args(argv)
    problems, results, review_rows = [], {}, []
    resolutions_path = args.evaluation / "labels" / "resolutions.csv"
    resolutions = ({row["item"]: row for row in read_csv(resolutions_path)}
                   if resolutions_path.is_file() else {})
    sample_of = {}
    for name in SAMPLES:
        path = args.evaluation / f"{name}.csv"
        if path.is_file():
            sample_of.update((row["item"], name) for row in read_csv(path))
    # A resolution is a person's label and takes the place of both passes, and it is checked
    # against the same criteria. Otherwise a misspelled label would count as "not correct", and
    # a resolution of an item that no sample contains would be dropped silently.
    for item, row in resolutions.items():
        if item not in sample_of:
            problems.append(f"{resolutions_path.name} resolves the item {item}, which is in no "
                            f"sample")
        elif row.get("label") not in LABELS[sample_of[item]]:
            problems.append(f"{resolutions_path.name} gives the item {item} the label "
                            f"{row.get('label')!r}, not one of {LABELS[sample_of[item]]}")
    for name in SAMPLES:
        path = args.evaluation / f"{name}.csv"
        # An evaluation can lack the samples of the output CSVs alone, and then it lacks
        # their labels too.
        if (name in ONLY_SAMPLES and not path.is_file()
                and not (args.evaluation / "labels" / f"{name}.csv").is_file()):
            continue
        if not path.is_file():
            problems.append(f"{path} is missing")
            continue
        items = read_csv(path)
        labels, found = read_labels(args.evaluation, name)
        problems += found
        # A repository with a file list that contains no candidate needs no label.
        unlabelled = [row["item"] for row in items if row["item"] not in labels
                      and row["item"] not in resolutions
                      and (name != "path_recall" or int(row["candidates_listed"]))]
        if unlabelled:
            problems.append(f"{len(unlabelled):,} item(s) of the sample {name} have no label, "
                            f"such as {unlabelled[0]}")
        final, review = final_labels(items, labels, resolutions)
        review_rows += [{"sample": name, **row} for row in review]
        if name == "path_precision":
            results[name] = report_path_precision(items, final)
        elif name == "path_recall":
            results[name] = report_path_recall(
                items, final, read_csv(args.evaluation / "path_recall_candidates.csv"))
        elif name == "commit_precision":
            results[name] = report_commit_precision(items, final)
        elif name == "commit_recall":
            results[name] = report_commit_recall(items, final)
        elif name == "mentions":
            results[name] = report_mentions(items, final)
        else:
            results[name] = report_stratified_precision(items, final)
        results[name]["agreement"] = agreement(labels)
        results[name]["to_review"] = len(review)
    if problems:
        for problem in problems:
            logs.error(COMPONENT, f"{problem}.")
        return 1
    (args.evaluation / "report.json").write_text(json.dumps(results, indent=2) + "\n",
                                                     encoding="utf-8")
    write_csv(args.evaluation / "review.csv",
              ["sample", "item", "why", "first", "second", "evidence", "note"], review_rows)
    (args.evaluation / "report.md").write_text(render(results), encoding="utf-8")
    logs.info(COMPONENT, f"Wrote report.json, report.md and review.csv to {args.evaluation}, "
                         f"with {len(review_rows):,} item(s) to review.")
    return 0


def _interval(entry: dict) -> str:
    if entry.get("estimate") is None:
        return "no items"
    return f"{entry['estimate']:.3f} [{entry['low']:.3f}, {entry['high']:.3f}]"


def render(results: dict) -> str:
    """The report as Markdown: each estimate with its 95% interval."""
    lines = ["# Detection accuracy", "",
             "`python -m agentcontext evaluation report` wrote these estimates, each with its "
             "interval at a confidence of 0.95.",
             ""]
    pp = results.get("path_precision")
    if pp:
        lines += ["## Path precision", "",
                  f"Precision, weighted by the rows of each tool and mechanism: "
                  f"{_interval(pp['overall'])} over {pp['overall']['items']} rows.", "", "| Mechanism | Precision |", "|---|---|"]
        lines += [f"| {m} | {_interval(v)} |" for m, v in pp["per_mechanism"].items()]
        lines.append("")
    pr = results.get("path_recall")
    if pr:
        lines += ["## Path recall", "",
                  f"Repositories without detected configuration that contain a configuration "
                  f"path: {_interval(pr.get('unconfigured_repositories_with_missed_configuration', {}))}, "
                  f"an estimated {pr['missed_repositories_estimate'].get('estimate', 0)} of the "
                  f"{pr['unconfigured_repositories_in_collection']} repositories that the "
                  f"stratum held when the sample was drawn.",
                  f"Repositories with detected configuration in which the detection missed a "
                  f"configuration path: "
                  f"{_interval(pr.get('configured_repositories_with_missed_configuration', {}))}.",
                  f"Configuration paths that the detection found in the repositories with detected "
                  f"configuration: "
                  f"{_interval(pr['configuration_paths_found_in_configured_repositories'])}.", ""]
    cp = results.get("commit_precision")
    if cp:
        lines += ["## Commit precision", "",
                  f"Precision over the tools of the commit CSV, weighted by their commits: "
                  f"{_interval(cp['tools'])} over {cp['tools']['items']} commits.", "", "| Stratum | Precision |", "|---|---|"]
        lines += [f"| {k} | {_interval(v)} |" for k, v in cp["per_stratum"].items()]
        lines += ["", f"Attributions correct: {_interval(cp['attributions_correct'])}.", ""]
    cr = results.get("commit_recall")
    if cr:
        lines += ["## Commit recall", "",
                  f"{cr['credited']} of {cr['commits']} commits drawn at random from the commit "
                  f"logs credit a tool. The heuristics find {_interval(cr['recall'])} of them.",
                  ""]
    mn = results.get("mentions")
    if mn:
        lines += ["## Unmatched mentions", "",
                  f"Phrasings labeled attribution that no heuristic matches: "
                  f"{_interval(mn['attributions_no_heuristic_reads'])}, in "
                  f"{mn['commits_of_those']} commits.", ""]
    for name, title, unit, stratum in (
            ("import_precision", "Import precision", "imports", "Heuristic and status"),
            ("planning_precision", "Planning document precision", "planning documents",
             "Framework or tool")):
        result = results.get(name)
        if not result:
            continue
        overall, without = result["overall"], result["without_strata_from_evidence"]
        lines += [f"## {title}", "",
                  f"Precision, weighted by the rows of each stratum: {_interval(overall)} over "
                  f"{overall['items']} {unit} of strata with {overall['rows']} rows.", ""]
        if result["strata_from_evidence"]:
            listed = ", ".join(f"{k} ({rows} {'row' if rows == 1 else 'rows'})"
                               for k, rows in result["strata_from_evidence"].items())
            lines += [f"Precision without the strata drawn from the repositories that the "
                      f"evidence of the heuristics links: {_interval(without)} over "
                      f"{without['items']} {unit} of strata with {without['rows']} rows.", "",
                      f"The draw took these strata from those repositories, because they hold "
                      f"every row of each: {listed}. The heuristics of these strata were read "
                      f"off the same repositories, and the precision over all strata can "
                      f"therefore be biased toward the heuristics.", ""]
        lines += [f"| {stratum} | Rows | Precision |", "|---|---|---|"]
        lines += [f"| {k} | {v['rows']} | {_interval(v)} |"
                  for k, v in result["per_stratum"].items()]
        lines.append("")
    lines += ["## Agreement of the two passes", "", "| Sample | Items | Agreement | Kappa |",
              "|---|---|---|---|"]
    for name, result in results.items():
        a = result["agreement"]
        lines.append(f"| {name} | {a['items']} | {a.get('agreement', '')} | {a.get('kappa', '')} |")
    return "\n".join(lines) + "\n"


SUBCOMMANDS = {"sample": main_sample, "report": main_report}
SUBCOMMAND_SUMMARY = ("sample draws the samples and measures the heuristics, and report computes "
                      "precision and recall from the labels")


def main(argv=None) -> int:
    return cli.subcommand_main("evaluation", SUBCOMMANDS, SUBCOMMAND_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
