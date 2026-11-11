#!/usr/bin/env python3
"""Filter a GHS retrieval into the sampling frame.

Reads the retrieval that `ghs_retrieval.json` names from the cache of retrievals, with GitHub's
response for each of its rows, and applies eight filter steps. It records what each step removed
and what remained. Where GitHub returned a field, the filter steps read GitHub's value and the
sampling frame contains it, because GHS refreshes a repository only when it crawls a push to it.
GitHub supplies the current name, GitHub's repository id, the description, the timestamps in
UTC, the archived, disabled, locked, fork, private and mirror flags, the license, the primary
language, the stars and the default branch with its HEAD commit. GHS's database id and name stay
beside them as `ghsId` and `ghsName`. The other fields are GHS's: the commit, contributor, issue,
pull request and watcher counts, the topics and the code metrics.

  1. Gone: GitHub has no public repository under the name, or blocks access to it, or the
     GitHub query left the row unresolved, or GitHub found the repository without its id, or
     GitHub created the repository under the name after the window's bound on the creation
     date, which GHS applied to an earlier repository under that name
  2. Duplicates: rows that GitHub resolves to one repository, which share its creation time.
     The row that GHS has under the current name is kept
  3. Cleanup: archived, disabled, locked and forked repositories
  4. Missing data: no pull request or issue count
  5. Licenses: OSI-approved by the SPDX list of config/spdx_licenses.json, minus the
     exclusions of config/licenses.csv, the names compared without their quotes
  6. Languages: the most common primary languages that the retrieval's end date covers
  7. Educational: tutorials, courses, lists and the like, by topic and name
  8. Activity: at least the median commits and watchers of what remains

Repositories at one head commit, such as a mirror and the repository that it copies, all stay in
the sampling frame. The classification keeps one of them in the sample.

The step `languages` ranks only the languages that the end date of the retrieval covers. The
end date leaves out a language with a crawl date before the end date or without a crawl date,
because GHS lacks the pushes of that language after its crawl date. Where the step would
keep such a language, it leaves the language out with a warning that names it and its crawl
date, keeps the next most common language in its place, and records the language in its values
under `left_out`.

Writes `ghs_filtered.json`: the sampling frame in `items`, and in `_metadata` the retrieval that
it came from, the config and the record of the filter steps, under the key `stages`, which
`4_data-analysis/dataset/filter_steps_table.py` renders for the paper. Writes `ghs_licenses.csv`
beside it: every license that reached the license step, with its repositories, its count in the
previous sampling frame, its decision and its review in `config/licenses.csv`.
`--check-licenses` reads that CSV and exits 2 when a license that the sampling frame keeps is
not reviewed, when the sampling frame excludes a license that the review no longer excludes, or
when the tracked SPDX list and the review disagree. `--refresh-spdx` replaces the tracked SPDX
list with the current list before a NAS run and exits 2 when an OSI approval changed, so that a
person reviews the change.

Usage:
    python3 4_filter_ghs.py
    python3 4_filter_ghs.py --config config/ghs_filter.yaml --dry-run
    python3 4_filter_ghs.py --top-languages 5 --commits-min 500
    python3 4_filter_ghs.py --check-licenses
    python3 4_filter_ghs.py --refresh-spdx
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from agentcontext import SAMPLING_DIR, caches, logs
from agentcontext import cli

import ghs_cache
from ghs_cache import CacheError, upper_first

COMPONENT = "repository_filtering"

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config" / "ghs_filter.yaml"
SPDX_LICENSE_URL = "https://raw.githubusercontent.com/spdx/license-list-data/main/json/licenses.json"

# GitHub's names of the GNU licenses, mapped to the SPDX names of the same licenses. GitHub
# omits "only".
GITHUB_TO_SPDX = {
    "GNU General Public License v2.0": "GNU General Public License v2.0 only",
    "GNU General Public License v3.0": "GNU General Public License v3.0 only",
    "GNU Lesser General Public License v2.1": "GNU Lesser General Public License v2.1 only",
    "GNU Lesser General Public License v3.0": "GNU Lesser General Public License v3.0 only",
    "GNU Affero General Public License v3.0": "GNU Affero General Public License v3.0 only",
    "GNU Library General Public License v2": "GNU Library General Public License v2 only",
}


# A field of GitHub's response, and the GHS field that it replaces in the sampling frame.
GITHUB_FIELDS = {
    "name": "name", "id": "id", "createdAt": "createdAt", "pushedAt": "pushedAt",
    "updatedAt": "updatedAt", "isArchived": "isArchived", "isDisabled": "isDisabled",
    "isLocked": "isLocked", "isFork": "isFork", "license": "license",
    "primaryLanguage": "mainLanguage", "stargazers": "stargazers",
    "defaultBranch": "defaultBranch", "headSha": "lastCommitSHA",
    "headCommittedAt": "lastCommit", "isPrivate": "isPrivate", "isMirror": "isMirror",
    "description": "description",
}


@dataclass
class FilterConfig:
    """What the eight filter steps remove. The defaults are the values of the shipped config."""

    output_path: str = "ghs_filtered.json"

    filter_gone: bool = True
    filter_duplicates: bool = True

    filter_cleanup: bool = True
    exclude_archived: bool = True
    exclude_disabled: bool = True
    exclude_locked: bool = True
    exclude_forks: bool = True

    filter_missing_data: bool = True

    filter_licenses: bool = True
    # The reviewed licenses, relative to this script's directory, and the table of every license
    # that reached the license step, relative to the stage's data directory.
    licenses_path: str = "config/licenses.csv"
    license_report_path: str = "ghs_licenses.csv"
    license_min_count_percentile: float = 0.0

    filter_languages: bool = True
    top_n_languages: int = 10

    filter_educational: bool = True
    educational_topic_regex: str = ""
    educational_name_regex: str = ""

    filter_activity: bool = True
    commits_min: int | None = None
    commits_min_percentile: float | None = 0.5
    watchers_min: int | None = None
    watchers_min_percentile: float | None = 0.5

    dry_run: bool = False

    # The SPDX license list that the review of config/licenses.csv was made against, relative
    # to this script's directory. The filter only reads the list, and a run in an empty data root
    # uses the list in the image. `--refresh-spdx` replaces the list before a run, so
    # that a person reviews the changes.
    spdx_list_path: str = "config/spdx_licenses.json"

    @classmethod
    def from_yaml(cls, path: Path) -> "FilterConfig":
        """The config at `path`. A key that the dataclass does not define is rejected."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        unknown = sorted(set(data) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"{path} has the keys {', '.join(unknown)}, which the filter does "
                             f"not read")
        return cls(**{k: v for k, v in data.items() if v is not None})


@dataclass
class FilterStep:
    """What one filter step removed and kept. The field `stage` is the step's number, under the
    name that the sampling frame's `_metadata.stages` records."""

    stage: int
    key: str
    description: str
    removed: int
    remaining: int
    values: dict = field(default_factory=dict)


# =============================================================================
# The rows
# =============================================================================

def merge(ghs: dict, github: dict) -> dict:
    """A GHS row with GitHub's response over the fields that the response contains, and GHS's id
    and name kept beside them."""
    item = dict(ghs, ghsId=ghs.get("id"), ghsName=ghs.get("name"),
                githubStatus=github["status"])
    if github["status"] == "found":
        repository = github["repository"]
        for source, target in GITHUB_FIELDS.items():
            item[target] = repository.get(source)
    return item


def load_rows(cache_dir: Path, pointer: Path) -> tuple[list[dict], dict]:
    """The merged rows of the retrieval that the pointer names, and its index row."""
    row = ghs_cache.pointed_retrieval(pointer, cache_dir)
    rid = row["retrieval_id"]
    if not row["github_file_sha256"]:
        raise CacheError(f"retrieval {rid} has no responses from GitHub. Run 2_query_github.py "
                         f"first")
    ghs_rows = ghs_cache.read_rows(cache_dir, rid, "ghs", row["ghs_file_sha256"])
    github_rows = ghs_cache.read_rows(cache_dir, rid, "github", row["github_file_sha256"])
    items = []
    for number, (ghs, github) in enumerate(zip(ghs_rows, github_rows, strict=True), 1):
        if github.get("ghsName") != ghs.get("name"):
            raise CacheError(f"GitHub's response in row {number} of retrieval {rid} is for "
                             f"{github.get('ghsName')!r}, but the GHS row is for "
                             f"{ghs.get('name')!r}")
        lacking = (sorted(set(GITHUB_FIELDS) - set(github["repository"]))
                   if github["status"] == "found" else [])
        if lacking:
            # A response recorded before the query requested the field. The query requests it
            # again (`2_query_github.complete`), and a sampling frame without the field would
            # have blanks.
            raise CacheError(f"GitHub's response in row {number} of retrieval {rid} has no "
                             f"{', '.join(lacking)}. Run 2_query_github.py again")
        items.append(merge(ghs, github))
    return items, row


# =============================================================================
# Licenses
# =============================================================================

def read_spdx_list(path: Path) -> dict:
    """The tracked SPDX list: `license_list_version`, `release_date`, `read`, `source` and
    `osi_approved`, the names of the OSI-approved licenses. A list that is missing, unreadable or
    names no license is rejected, because the license step would remove every repository."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if (not isinstance(data, dict) or not isinstance(data.get("osi_approved"), list)
            or not data["osi_approved"] or not data.get("license_list_version")):
        raise ValueError(f"{path} names no OSI-approved license or no list version")
    return data


def fetch_spdx_list() -> dict:
    """The current SPDX list in the tracked list's format. A failed fetch, or a payload with no
    OSI-approved license, raises an exception."""
    response = requests.get(SPDX_LICENSE_URL, timeout=30)
    response.raise_for_status()
    data = response.json()
    listed = data.get("licenses", []) if isinstance(data, dict) else []
    osi = sorted({entry["name"] for entry in listed if entry.get("isOsiApproved")})
    if not osi:
        raise ValueError(f"{SPDX_LICENSE_URL} lists no OSI-approved license")
    return {"license_list_version": data.get("licenseListVersion", ""),
            "release_date": data.get("releaseDate", ""),
            "read": datetime.now(timezone.utc).date().isoformat(),
            "source": SPDX_LICENSE_URL, "osi_approved": osi}


def refresh_spdx_list(path: Path) -> int:
    """`--refresh-spdx`: write the current SPDX list over the tracked list and name every license
    that gained or lost its OSI approval. Returns 0 when the approvals are unchanged, 2 when they
    changed, which is a reading task before the next run, and 1 when the list cannot be
    fetched."""
    try:
        current = fetch_spdx_list()
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        logs.error(COMPONENT, f"The SPDX list cannot be fetched ({error}).")
        return 1
    try:
        tracked = read_spdx_list(path)
    except (OSError, ValueError) as error:
        logs.warning(COMPONENT, f"No tracked SPDX list to compare with ({error}).")
        tracked = {"license_list_version": "", "osi_approved": []}
    added = sorted(set(current["osi_approved"]) - set(tracked["osi_approved"]))
    withdrawn = sorted(set(tracked["osi_approved"]) - set(current["osi_approved"]))
    caches.replace_file(path, json.dumps(current, indent=1) + "\n")
    logs.info(COMPONENT, f"Wrote SPDX list {current['license_list_version']} to {path} "
                         f"({len(current['osi_approved']):,} OSI-approved licenses, replacing "
                         f"version {tracked['license_list_version'] or 'none'}).")
    for name in added:
        logs.warning(COMPONENT, f"The SPDX list now marks {name} as OSI-approved.")
    for name in withdrawn:
        logs.warning(COMPONENT, f"The SPDX list no longer marks {name} as OSI-approved.")
    if added or withdrawn:
        logs.warning(COMPONENT, "Filter the latest retrieval, run --check-licenses, and review "
                                "every license that the check names in config/licenses.csv.")
        return 2
    return 0


def is_osi_approved(license_name: str | None, osi: set[str]) -> bool:
    """Whether a license name, in GitHub's spelling or GHS's, is an OSI-approved SPDX license."""
    if not license_name:
        return False
    keys = {ghs_cache.license_key(name) for name in osi}
    return (ghs_cache.license_key(license_name) in keys
            or ghs_cache.license_key(GITHUB_TO_SPDX.get(ghs_cache.license_key(license_name))) in keys)


# The reviewed licenses: each row a license in GitHub's spelling, its SPDX id, the decision
# (`software`, kept when OSI-approved, or `excluded`), the reason, the page that it was read on
# and the date. A license that the sampling frame keeps and that this list does not cover is a
# reading task.
LICENSE_COLUMNS = ("license", "spdx_id", "decision", "reason", "source", "read")
DECISIONS = ("software", "excluded")
REPORT_COLUMNS = ("license", "repositories", "previous_repositories", "osi_approved", "decision",
                  "reviewed", "examples")
EXAMPLES = 5


def load_reviewed_licenses(path: Path) -> dict:
    """{license key: row} of the reviewed licenses. A missing column, an unknown decision, a
    blank reason, source or date, or a license listed twice is rejected."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = sorted(set(LICENSE_COLUMNS) - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"{path} lacks the columns {', '.join(missing)}")
        reviewed = {}
        for number, row in enumerate(reader, 2):
            if row["decision"] not in DECISIONS:
                raise ValueError(f"line {number} of {path} has the decision "
                                 f"{row['decision']!r}, which is not one of "
                                 f"{', '.join(DECISIONS)}")
            if not row["reason"].strip() or not row["source"].strip() or not row["read"].strip():
                raise ValueError(f"the license {row['license']} in line {number} of {path} has "
                                 f"no reason, source or date")
            key = ghs_cache.license_key(row["license"])
            if key in reviewed:
                raise ValueError(f"the license {row['license']} is listed a second time in line "
                                 f"{number} of {path}")
            reviewed[key] = row
    return reviewed


def license_examples(names: list) -> list:
    """Up to `EXAMPLES` repository names, the same examples for the same names whatever their
    order: the smallest by the SHA-256 of the name, which spreads them over the alphabet."""
    return sorted(names, key=lambda name: hashlib.sha256(name.encode("utf-8")).hexdigest())[:EXAMPLES]


def license_table(items: list, osi: set[str], reviewed: dict, allowed: set) -> list:
    """One row per license that reached the license step, the most common first: its
    repositories, whether the SPDX list marks it OSI-approved, the step's decision, the
    review's decision and example repositories. A repository without a license counts under the
    empty name."""
    names: dict = {}
    for item in items:
        names.setdefault(item.get("license") or "", []).append(item["name"])
    rows = []
    for name, repos in sorted(names.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        approved = is_osi_approved(name, osi)
        decision = ("kept" if name in allowed else "excluded" if approved
                    else "not_osi_approved")
        rows.append({"license": name, "repositories": len(repos), "osi_approved": approved,
                     "decision": decision,
                     "reviewed": (reviewed.get(ghs_cache.license_key(name)) or {}).get("decision", ""),
                     "examples": license_examples(repos)})
    return rows


def quantile(values: list, share: float) -> float:
    """The `share` quantile of `values` by linear interpolation, numpy's default method."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def allowed_licenses(items: list, osi: set[str], exclusions: list, percentile: float) -> set:
    """The licenses kept: OSI-approved, not excluded, and as common as `percentile` requires."""
    counts = Counter(item.get("license") for item in items)
    excluded = {ghs_cache.license_key(name) for name in exclusions}
    allowed = {name for name in counts
               if is_osi_approved(name, osi) and ghs_cache.license_key(name) not in excluded}
    if percentile and allowed:
        threshold = quantile([counts[name] for name in allowed], percentile)
        allowed = {name for name in allowed if counts[name] >= threshold}
    return allowed


# =============================================================================
# The filter steps
# =============================================================================

def _step(number, key, description, before, kept, **values) -> tuple[list, FilterStep]:
    return kept, FilterStep(number, key, description, len(before) - len(kept), len(kept), values)


def step_gone(items: list, config: FilterConfig, created_max: str) -> tuple[list, FilterStep]:
    """The rows of repositories that GitHub does not have or that the later stages cannot use:

    - rows that GitHub has no public repository for under GHS's name, or blocks access to
    - rows that the GitHub query left `unresolved`, because no response says whether GitHub has
      them (`values.unresolved`)
    - rows of repositories that GitHub found without an id (`values.without_id`)
    - rows of public repositories with an id that GitHub created after `created_max`, the day
      YYYY-MM-DD that bounds the sampling window's creation dates
      (`values.created_after_window`)

    GitHub's GraphQL schema declares a repository's `databaseId` as `Int`, which may be null.
    The later stages key a repository by that id, and a row without an id would stop the
    classification. The duplicates step would also take rows without an id that were created in
    the same second for one repository and keep only one of them.

    GHS applied `created_max` to the creation date of the repository that it crawled under the
    name. A repository that GitHub created under the name after that day is another repository:
    the repository that GHS crawled was deleted, renamed or transferred, and the name was taken
    again. If the step kept the row, it would put a repository created outside the window into
    the sampling frame, with GHS's counts of the earlier repository. The step compares days
    alone, as the comparison of retrievals does (`3_compare_retrievals.py`). The hour that GHS
    compared with its bound cannot be read, because GHS writes its times on a 12-hour clock
    without AM/PM, while the day is the same in GHS's and GitHub's times. A repository created
    on the day of `created_max` stays."""
    if not config.filter_gone:
        return _step(1, "gone", "skipped", items, items, unresolved=0, without_id=0,
                     created_after_window=0)
    present = [item for item in items
               if item["githubStatus"] == "found" and not item.get("isPrivate")
               and item.get("id") is not None]
    kept = [item for item in present if (item.get("createdAt") or "")[:10] <= created_max]
    unresolved = sum(1 for item in items if item["githubStatus"] == "unresolved")
    without_id = sum(1 for item in items
                     if item["githubStatus"] == "found" and item.get("id") is None)
    return _step(1, "gone", "no public repository on GitHub under the name, or one created "
                            "after the window, or no response or id from GitHub", items, kept,
                  unresolved=unresolved, without_id=without_id,
                  created_after_window=len(present) - len(kept))


def duplicate_choice(group: list) -> dict:
    """The row of a group of duplicates that the sampling frame keeps: the row that GHS has under
    the current name, else the row with the most commits, else the row with the lowest GHS id."""
    return min(group, key=lambda item: (
        ghs_cache.name_key(item["ghsName"]) != ghs_cache.name_key(item["name"]),
        -(item.get("commits") or 0), item["ghsId"]))


def step_duplicates(items: list, config: FilterConfig) -> tuple[list, FilterStep]:
    """Rows that GitHub resolves to one repository. A renamed or transferred repository can
    appear twice, because GHS adds a row when it crawls a repository under a new name and keeps
    the row under the old name. Such rows share GitHub's id and creation time. Creation time
    alone does not identify a repository, because repositories that a migration created in the
    same second share it.
    """
    if not config.filter_duplicates:
        return _step(2, "duplicates", "skipped", items, items)
    groups: dict = {}
    for item in items:
        groups.setdefault((item["id"], item["createdAt"]), []).append(item)
    keep = {id(duplicate_choice(group)) for group in groups.values()}
    kept = [item for item in items if id(item) in keep]
    # Two repositories under one name. The query finds them when one repository gives up the
    # name and another takes it while the responses of the retrieval are gathered. Because the
    # later stages join by name, a name may stand for one repository only. The
    # responses do not tell which of the two repositories holds the name now, and the step
    # leaves both out. The next retrieval queries the name again.
    ids_by_name: dict = {}
    for item in kept:
        ids_by_name.setdefault(ghs_cache.name_key(item["name"]), set()).add(item["id"])
    shared = {name for name, ids in ids_by_name.items() if len(ids) > 1}
    kept = [item for item in kept if ghs_cache.name_key(item["name"]) not in shared]
    return _step(2, "duplicates", "rows that GitHub resolves to one repository", items, kept,
                  groups=sum(1 for group in groups.values() if len(group) > 1),
                  **({"shared_names": len(shared)} if shared else {}))


def step_cleanup(items: list, config: FilterConfig) -> tuple[list, FilterStep]:
    """Archived, disabled, locked and forked repositories, by GitHub's current flags."""
    flags = [flag for flag, on in (("isArchived", config.exclude_archived),
                                   ("isDisabled", config.exclude_disabled),
                                   ("isLocked", config.exclude_locked),
                                   ("isFork", config.exclude_forks)) if on]
    kept = items if not config.filter_cleanup else [
        item for item in items if not any(item.get(flag) for flag in flags)]
    return _step(3, "cleanup", "skipped" if not config.filter_cleanup else
                  "flagged " + ", ".join(flag[2:].lower() for flag in flags), items, kept,
                  flags=flags if config.filter_cleanup else [])


def step_missing_data(items: list, config: FilterConfig) -> tuple[list, FilterStep]:
    """Rows without a pull request or an issue count."""
    kept = items if not config.filter_missing_data else [
        item for item in items
        if item.get("totalPullRequests") is not None and item.get("totalIssues") is not None]
    return _step(4, "missing_data", "skipped" if not config.filter_missing_data else
                  "no pull request or issue count", items, kept)


def step_licenses(items: list, config: FilterConfig, osi: set[str],
                  reviewed: dict | None = None) -> tuple[list, FilterStep]:
    """Rows with a license that is not OSI-approved or that `reviewed` excludes. A license that
    the step keeps and that `reviewed` does not list stays, so that an unattended run continues,
    and the step names it in a warning and in its `unreviewed` value."""
    reviewed = reviewed or {}
    if not config.filter_licenses:
        # The table is written even when the step is off, because the runner requires the file
        # that the step declares. The license check then names for a review every license of
        # the sampling frame, which keeps all licenses.
        table = license_table(items, osi, reviewed,
                              {item.get("license") or "" for item in items})
        return _step(5, "licenses", "skipped", items, items, licenses=table)
    exclusions = [row["license"] for row in reviewed.values() if row["decision"] == "excluded"]
    allowed = allowed_licenses(items, osi, exclusions, config.license_min_count_percentile)
    kept = [item for item in items if item.get("license") in allowed]
    table = license_table(items, osi, reviewed, allowed)
    unreviewed = [row["license"] for row in table
                  if row["decision"] == "kept" and row["reviewed"] != "software"]
    for row in table:
        if row["license"] in unreviewed:
            logs.warning(COMPONENT, f"The license step kept {row['repositories']:,} repositories "
                                    f"under {row['license']}, which {config.licenses_path} does "
                                    f"not cover. Review the license and add it to "
                                    f"{config.licenses_path} as software or excluded.")
    return _step(5, "licenses", f"not one of {len(allowed)} allowed licenses", items, kept,
                  allowed=sorted(allowed), unreviewed=unreviewed, licenses=table)


def left_out_reason(name: str, day: str | None) -> str:
    """Why the end date of the sampling window does not cover the language `name`, given its
    crawl date `day` or None."""
    return (f"the crawl date of {name}, {day}, is before the end date of the sampling window"
            if day else f"GHS returned no crawl date for {name}")


def step_languages(items: list, config: FilterConfig,
                   left_out: dict | None = None) -> tuple[list, FilterStep]:
    """Rows outside the `top_n_languages` most common primary languages that the end date of the
    retrieval covers, or, with `filter_languages: false`, outside every language that it covers.
    The language's name decides a tie in count, so that the kept set does not depend on the row
    order.

    `left_out` holds the languages that the end date left out, each with its crawl date or None
    (`left_out_languages`). The window does not cover the repositories of such a language
    completely, because GHS lacks its pushes after its crawl date. The step goes down the ranking
    until it has kept its number of languages and passes over each such language, and the next
    most common language takes its place. The step names each language that it passed over in a
    warning and records it with its crawl date in `values.left_out`. A language that the end
    date left out and that ranks after the last kept language is not named, because the step
    would not keep it in any case."""
    left_out = left_out or {}
    counts = Counter(item.get("mainLanguage") for item in items if item.get("mainLanguage"))
    ranked = sorted(counts, key=lambda name: (-counts[name], name))
    number = config.top_n_languages if config.filter_languages else len(ranked)
    top, passed = [], {}
    for name in ranked:
        if len(top) == number:
            break
        if name in left_out:
            passed[name] = left_out[name]
        else:
            top.append(name)
    for name, day in passed.items():
        replaced = (". Kept the next most common language in its place"
                    if config.filter_languages else "")
        logs.warning(COMPONENT, f"Filter step 6 (languages) left out {name}, because "
                                f"{left_out_reason(name, day)}{replaced}.")
    passed = dict(sorted(passed.items()))
    if not config.filter_languages:
        if not passed:
            return _step(6, "languages", "skipped", items, items)
        kept = [item for item in items if item.get("mainLanguage") not in passed]
        return _step(6, "languages", "a language that the end date of the retrieval left out",
                     items, kept, left_out=passed)
    kept = [item for item in items if item.get("mainLanguage") in set(top)]
    covers = " that the end date covers" if passed else ""
    return _step(6, "languages", f"not one of the top {config.top_n_languages} languages{covers}",
                  items, kept, top=config.top_n_languages, languages=sorted(top), left_out=passed)


def step_educational(items: list, config: FilterConfig) -> tuple[list, FilterStep]:
    """Tutorials, courses, lists and the like, by a topic or the repository part of the current
    name. The owner part is not matched, because it would remove software such as
    MercuryWorkshop's and amplify-education's."""
    if not config.filter_educational:
        return _step(7, "educational", "skipped", items, items)
    topic = re.compile(config.educational_topic_regex) if config.educational_topic_regex else None
    name = (re.compile(config.educational_name_regex, re.IGNORECASE)
            if config.educational_name_regex else None)
    kept = [item for item in items
            if not (topic and any(topic.search(t) for t in item.get("topics") or []))
            and not (name and name.search(item["name"].split("/", 1)[-1]))]
    return _step(7, "educational", "an educational or resource topic or name", items, kept)


def step_activity(items: list, config: FilterConfig) -> tuple[list, FilterStep]:
    """Rows below the commit and watcher thresholds: absolute when the config gives one, else
    the configured quantile of what remains."""
    if not config.filter_activity or not items:
        return _step(8, "activity", "skipped", items, items)
    thresholds = {}
    for column, absolute, share in (("commits", config.commits_min, config.commits_min_percentile),
                                    ("watchers", config.watchers_min,
                                     config.watchers_min_percentile)):
        if absolute is not None:
            thresholds[column] = absolute
        elif share is not None:
            thresholds[column] = quantile([item.get(column) or 0 for item in items], share)
    kept = [item for item in items
            if all((item.get(column) or 0) >= value for column, value in thresholds.items())]
    description = ", ".join(f"{column} below {value:g}" for column, value in thresholds.items())
    return _step(8, "activity", description or "no threshold", items, kept,
                  **{f"{column}_min": value for column, value in thresholds.items()})


def run_steps(items: list, config: FilterConfig, osi: set[str], reviewed: dict | None = None,
              left_out: dict | None = None, *, created_max: str) -> tuple[list, list]:
    """The eight filter steps in order, with `created_max`, the window's bound on the creation
    date, for the step `gone`, and `left_out`, the languages that the end date of the retrieval
    left out, for the step `languages`. Returns the sampling frame and the record of each
    step."""
    results = []
    for apply in (lambda rows, cfg: step_gone(rows, cfg, created_max),
                  step_duplicates, step_cleanup, step_missing_data,
                  lambda rows, cfg: step_licenses(rows, cfg, osi, reviewed),
                  lambda rows, cfg: step_languages(rows, cfg, left_out),
                  step_educational, step_activity):
        items, result = apply(items, config)
        results.append(result)
        logs.info(COMPONENT, f"Filter step {result.stage} ({result.key}, {result.description}) "
                             f"removed {result.removed:,} rows and left {result.remaining:,}.")
    return items, results


# =============================================================================
# The window: the languages that it does not cover and its bound on the creation date
# =============================================================================

def left_out_languages(row: dict) -> dict:
    """The languages that the end date of the retrieval with the index row `row` left out, each
    with its crawl date or None. These are the languages with a crawl date earlier than the
    window's end date or without a crawl date, read from the crawl dates that the download
    records in the index (`crawl_dates`). The window does not cover the repositories of such a
    language completely, because GHS lacks its pushes after its crawl date. The step `languages`
    ranks only the other languages.

    The download ends the window on the oldest crawl date of the languages that it does not
    leave out. It leaves out only languages with a crawl date older than its lag limit or
    without a crawl date, and such a crawl date is older than that end date (`resolve_window` in
    1_download_ghs.py). A retrieval with a date as its end date records no crawl date and leaves
    no language out. Crawl dates that cannot be read raise `CacheError`, because the step could
    not tell which languages the window covers."""
    text = row.get("crawl_dates") or ""
    if not text:
        return {}
    try:
        dates = json.loads(text)
    except ValueError as error:
        raise CacheError(f"the crawl dates of retrieval {row['retrieval_id']} in the index are "
                         f"not JSON ({error})") from error
    if not isinstance(dates, dict):
        raise CacheError(f"the crawl dates of retrieval {row['retrieval_id']} in the index are "
                         f"not an object of languages")
    return {name: day for name, day in dates.items() if not day or day < row["end_date"]}


def window_created_max(row: dict) -> str:
    """The latest creation day that the sampling window of the retrieval with the index row
    `row` admits (its `created_max`). The step `gone` compares it with GitHub's creation dates.
    A value that is not a date YYYY-MM-DD raises `CacheError`, because the step could not tell
    which repositories GitHub created after the window, and a blank compared as a day would
    remove every row."""
    value = row.get("created_max") or ""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(value)
        date.fromisoformat(value)
    except ValueError:
        raise CacheError(f"the index row of retrieval {row['retrieval_id']} records the "
                         f"created_max {value!r}, not a date YYYY-MM-DD") from None
    return value


# =============================================================================
# Output
# =============================================================================

def sampling_frame_metadata(row: dict, config: FilterConfig, results: list, initial: int,
                   final: int) -> dict:
    """The provenance of a sampling frame: the retrieval and GitHub's responses, the config and
    the filter steps."""
    def number(value):
        return int(value) if str(value).isdigit() else value

    return {
        "filtered_at": ghs_cache.iso(ghs_cache.utc_now()),
        "input_source": {
            "retrieval_id": row["retrieval_id"],
            # When the GHS search result was downloaded, which
            # `4_data-analysis/rq2/_repo_metadata.py` takes as the day that the GHS counts
            # describe.
            "downloaded_at": row["started_at"],
            "item_count": initial,
            "window": {key: row[key] for key in ("end_date", "end_date_source", "created_max",
                                                 "committed_min")},
            "query": json.loads(row["query"]) if row["query"] else {},
            "ghs_version": row["ghs_version"],
            # `not_found` counts the repositories that GitHub did not find or blocks, and
            # `unresolved` the rows that no response settled, which the step `gone` removes too.
            "github": {"started_at": row["github_started_at"],
                       "finished_at": row["github_finished_at"],
                       "found": number(row["github_found"]),
                       "not_found": number(row["github_not_found"]),
                       "unresolved": number(row["github_unresolved"])},
        },
        "filter_config": {k: v for k, v in asdict(config).items() if k != "dry_run"},
        "stages": [asdict(result) for result in results],
        "final_count": final,
    }


def save_output(path: Path, metadata: dict, items: list) -> None:
    """Write the sampling frame in one step (`caches.replace_file`), so that a power loss
    cannot leave an empty sampling frame, which would stop the classification."""
    path = Path(path)
    caches.replace_file(path, json.dumps({"_metadata": metadata, "items": items}, indent=1,
                                         ensure_ascii=False))
    logs.info(COMPONENT, f"Wrote {path} ({len(items):,} repositories).")


def read_license_report(path: Path) -> list:
    """The rows of a license table, or an empty list when there is none yet."""
    if not Path(path).is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_license_report(path: Path, table: list) -> None:
    """Write the license table, with each license's count in the table that it replaces, in one
    step (`caches.replace_file`)."""
    path = Path(path)
    previous = {row["license"]: row["repositories"] for row in read_license_report(path)}
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(REPORT_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in table:
        writer.writerow({**row, "osi_approved": "TRUE" if row["osi_approved"] else "FALSE",
                         "previous_repositories": previous.get(row["license"], ""),
                         "examples": ";".join(row["examples"])})
    caches.replace_file(path, buffer.getvalue())
    logs.info(COMPONENT, f"Wrote {path} ({len(table):,} licenses).")


def check_licenses(report_path: Path, licenses_path: Path, osi: set[str]) -> list:
    """What a person has to read before the licenses of the sampling frame are reviewed: a
    license that the sampling frame keeps and that the review does not list as software, a
    license that the review lists as software and that the SPDX list does not mark
    OSI-approved, and a license with an OSI approval in the table that the tracked SPDX list no
    longer gives it."""
    reviewed = load_reviewed_licenses(licenses_path)
    findings = []
    for row in read_license_report(report_path):
        name, approved = row["license"], row["osi_approved"] == "TRUE"
        decision = (reviewed.get(ghs_cache.license_key(name)) or {}).get("decision", "")
        if row["decision"] == "kept" and decision != "software":
            findings.append(f"The sampling frame keeps {row['repositories']} repositories under "
                            f"{name}, which {licenses_path.name} does not list as software")
        elif row["decision"] == "excluded" and decision != "excluded":
            # The sampling frame excludes the license by a review that no longer excludes it.
            # A license changed to `software` after the table was written stays out of the
            # sampling frame until the filter runs again.
            findings.append(f"The sampling frame excludes {row['repositories']} repositories "
                            f"under {name}, which {licenses_path.name} no longer excludes. Filter "
                            f"the retrieval again")
        if name and approved != is_osi_approved(name, osi):
            findings.append(f"The license table records the OSI approval of {name} as "
                            f"{approved}, but the tracked SPDX list gives {not approved}. Filter "
                            f"the retrieval again")
    for row in reviewed.values():
        if row["decision"] == "software" and not is_osi_approved(row["license"], osi):
            findings.append(f"{licenses_path.name} lists {row['license']} as software, but the "
                            f"SPDX list does not mark it as OSI-approved")
    return findings


def parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = cli.ArgumentParser(description="Filter the GHS retrieval that the sampling "
                                                 "stage reads into the sampling frame.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="The YAML config (default: %(default)s)")
    parser.add_argument("--cache-dir", type=Path, default=ghs_cache.CACHE_DIR,
                        help="The cache of retrievals (default: %(default)s)")
    parser.add_argument("--pointer", type=Path, default=ghs_cache.POINTER,
                        help="The file that names the retrieval (default: %(default)s)")
    parser.add_argument("--output", type=str, dest="output_path",
                        help="The path of the sampling frame (default: the config's output_path)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run every filter step and write nothing")
    parser.add_argument("--top-languages", type=int, dest="top_n_languages",
                        help="The number of languages kept (overrides the config)")
    parser.add_argument("--commits-min", type=int, dest="commits_min",
                        help="An absolute commit threshold (overrides the quantile)")
    parser.add_argument("--watchers-min", type=int, dest="watchers_min",
                        help="An absolute watcher threshold (overrides the quantile)")
    parser.add_argument("--check-licenses", action="store_true",
                        help="Log the licenses of the last sampling frame that need a review, "
                             "exit 2 if there are any, and filter nothing")
    parser.add_argument("--refresh-spdx", action="store_true",
                        help="Replace the tracked SPDX list with the current one, log every "
                             "change of an OSI approval, exit 2 if there is one, and filter "
                             "nothing")
    return parser.parse_args(argv)


def main(argv: list | None = None) -> int:
    args = parse_args(argv)
    try:
        config = FilterConfig.from_yaml(args.config)
    except (OSError, ValueError, getattr(yaml, "YAMLError", ValueError)) as error:
        logs.error(COMPONENT, f"Could not read the config {args.config} ({error}).")
        return 1
    for key in ("output_path", "top_n_languages", "commits_min", "watchers_min"):
        if getattr(args, key) is not None:
            setattr(config, key, getattr(args, key))
    config.dry_run = args.dry_run
    licenses_path = HERE / config.licenses_path
    report_path = SAMPLING_DIR / config.license_report_path
    spdx_path = HERE / config.spdx_list_path
    if args.refresh_spdx:
        return refresh_spdx_list(spdx_path)
    if args.check_licenses:
        return run_license_check(report_path, licenses_path, spdx_path)
    try:
        reviewed = load_reviewed_licenses(licenses_path) if config.filter_licenses else {}
        spdx = read_spdx_list(spdx_path) if config.filter_licenses else None
    except (OSError, ValueError) as error:
        logs.error(COMPONENT, f"Could not read the reviewed licenses or the SPDX list ({error}).")
        return 1
    try:
        items, row = load_rows(args.cache_dir, args.pointer)
        left_out = left_out_languages(row)
        created_max = window_created_max(row)
    except CacheError as error:
        logs.error(COMPONENT, f"The retrieval cannot be read. {upper_first(error)}.")
        return 1
    logs.info(COMPONENT, f"Retrieval {row['retrieval_id']} has {len(items):,} repositories, "
                         f"{int(row['github_found'] or 0):,} of them found on GitHub.")
    osi = set(spdx["osi_approved"]) if spdx else set()
    sampling_frame, results = run_steps(items, config, osi, reviewed, left_out,
                                        created_max=created_max)
    # What the license step decided by, for the sampling frame's record: the SPDX list's version
    # and the SHA-256 of the review, neither of which the config names.
    for result in results:
        if result.key == "licenses" and spdx:
            result.values["spdx_list_version"] = spdx["license_list_version"]
            result.values["review_sha256"] = hashlib.sha256(licenses_path.read_bytes()).hexdigest()
    logs.info(COMPONENT, f"The sampling frame keeps {len(sampling_frame):,} of the "
                         f"{len(items):,} repositories.")
    if config.dry_run:
        logs.info(COMPONENT, "Dry run, nothing written.")
        return 0
    save_output(SAMPLING_DIR / config.output_path,
                sampling_frame_metadata(row, config, results, len(items), len(sampling_frame)), sampling_frame)
    table = next((result.values.get("licenses") for result in results
                  if result.key == "licenses"), None)
    if table is not None:
        write_license_report(report_path, table)
    return 0


def run_license_check(report_path: Path, licenses_path: Path, spdx_path: Path) -> int:
    """`--check-licenses`: 0 when every license of the last sampling frame is reviewed, 2 when a
    license needs a person's review, and 1 when the table, the review or the SPDX list cannot be
    read, or when the table names no license, because the table of every sampling frame names
    at least one."""
    if not report_path.is_file():
        logs.error(COMPONENT, f"{report_path} does not exist. Filter a retrieval first.")
        return 1
    try:
        if not read_license_report(report_path):
            raise ValueError(f"{report_path.name} names no license")
        osi = set(read_spdx_list(spdx_path)["osi_approved"])
        findings = check_licenses(report_path, licenses_path, osi)
    except (OSError, ValueError, KeyError) as error:
        logs.error(COMPONENT, f"The licenses cannot be checked ({error}).")
        return 1
    for finding in findings:
        logs.warning(COMPONENT, f"{finding}.")
    if findings:
        logs.warning(COMPONENT, f"{len(findings):,} license finding(s) to review. Record each "
                                f"license in {licenses_path} with its reason and source.")
        return 2
    logs.info(COMPONENT, f"Every license of {report_path.name} is reviewed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
