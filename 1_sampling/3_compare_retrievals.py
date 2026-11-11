"""Report what changed between two GHS retrievals, and how the collected sample compares with
the newer one.

Compares the retrieval that `ghs_retrieval.json` names with the usable retrieval before it in
the index. A repository is matched by GitHub's id where both retrievals have GitHub's responses,
and otherwise by its GHS name or its current name, without regard to case, so that a renamed
repository is matched rather than reported as removed and added. Two rows with different GitHub
ids are never matched by name. Each repository is then:

  added      in the new retrieval only; `created_after_previous_window` when it was created
             after the previous window's creation bound, else `revived_or_newly_matched`
  removed    in the previous retrieval only; `inactive_before_window` when its last commit
             there falls before the new window's commit bound, else `deleted_or_no_longer_matching`
  changed    in both, with the fields that differ
  unchanged  in both, with no field different

The compared fields are the fields that the filter, the classification and the collection
read: the name, the default branch and its HEAD commit, the day of the last
push, the archived, disabled, locked, fork, mirror and private flags, the license, the primary
language, the commit, contributor, issue, pull request, watcher and star counts, the topics,
the description, the creation day and whether GitHub has the repository. Each field comes from
GitHub's response where there is one, and from GHS's row otherwise. GHS has no description and
no mirror or private flag, so these fields are compared only between two retrievals with
GitHub's responses. The description is compared without its surrounding white space, as the
classification reads it. Timestamps are compared by day, because GHS writes the hour on a
12-hour clock without AM/PM.

With the collected list, `data/collection/data/collected/repos.csv` by default, every
collected repository is looked up in the new retrieval: whether GitHub's HEAD differs from the
collected commit, whether the default branch or the name changed, whether GitHub still has the
repository, and whether the new retrieval contains it at all.

Writes `ghs_changes.csv` (one row per added, removed or changed repository),
`ghs_collection_changes.csv` (one row per collected repository that is not `unchanged`) and
`ghs_changes.json` (the counts), and logs the counts. The script reports differences. It exits
0 whatever it found, and 1 only when it could not read its inputs.

Usage:
  python3 3_compare_retrievals.py
  python3 3_compare_retrievals.py --old 2026-03-16T180701Z --collected ../data/collection/data/collected/repos.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from agentcontext import SAMPLING_DIR, STAGE_DIR, caches, logs

import ghs_cache
from ghs_cache import CacheError, upper_first

COMPONENT = "retrieval_comparison"

DEFAULT_COLLECTED = STAGE_DIR / "data" / "collected" / "repos.csv"
CHANGES = SAMPLING_DIR / "ghs_changes.csv"
COLLECTION_CHANGES = SAMPLING_DIR / "ghs_collection_changes.csv"
SUMMARY = SAMPLING_DIR / "ghs_changes.json"

# The compared fields: the name in the view, GitHub's field, GHS's field, and whether the value
# is a timestamp compared by day.
FIELDS = (
    ("name", "name", "name", False),
    ("defaultBranch", "defaultBranch", "defaultBranch", False),
    ("headSha", "headSha", "lastCommitSHA", False),
    ("pushedAt", "pushedAt", "pushedAt", True),
    ("createdAt", "createdAt", "createdAt", True),
    ("isArchived", "isArchived", "isArchived", False),
    ("isDisabled", "isDisabled", "isDisabled", False),
    ("isLocked", "isLocked", "isLocked", False),
    ("isFork", "isFork", "isFork", False),
    ("isMirror", "isMirror", None, False),
    ("isPrivate", "isPrivate", None, False),
    ("license", "license", "license", False),
    ("language", "primaryLanguage", "mainLanguage", False),
    ("stargazers", "stargazers", "stargazers", False),
    ("commits", None, "commits", False),
    ("contributors", None, "contributors", False),
    ("totalIssues", None, "totalIssues", False),
    ("totalPullRequests", None, "totalPullRequests", False),
    ("watchers", None, "watchers", False),
    ("topics", None, "topics", False),
    ("description", "description", None, False),
    ("onGitHub", None, None, False),
)
# The GHS field that a view also has, for the reason of an addition or a removal.
LAST_COMMIT = "lastCommit"


def view(ghs: dict, github: dict | None) -> dict:
    """One repository as the comparison reads it: GitHub's value where GitHub returned one,
    GHS's value otherwise, and a timestamp truncated to its day. Whether GitHub has the
    repository is unknown without a response, for a retrieval that GitHub was not queried for
    and for a row that the query left `unresolved`."""
    repository = (github or {}).get("repository") if (github or {}).get("status") == "found" else None
    record = {"ghsName": ghs.get("name"), "ghsId": ghs.get("id"),
              "githubId": repository.get("id") if repository else None,
              LAST_COMMIT: str(ghs.get(LAST_COMMIT) or "")[:10]}
    for name, github_field, ghs_field, is_time in FIELDS:
        if name == "onGitHub":
            value = (None if github is None or github.get("status") == "unresolved"
                     else github.get("status") == "found")
        elif repository is not None and github_field is not None:
            value = repository.get(github_field)
            # The classification reads the description stripped, and a description that GitHub
            # returns as null as an empty one, so a removed description is a change and a
            # changed surrounding space is none. A response recorded before the query requested
            # the description lacks the field, and the field stays unknown.
            if name == "description" and github_field in repository:
                value = str(value or "").strip()
        else:
            value = ghs.get(ghs_field) if ghs_field else None
        if is_time and value:
            value = str(value)[:10]
        if name == "topics" and isinstance(value, list):
            value = sorted(value)
        record[name] = value
    return record


def views(cache_dir: Path, row: dict) -> list:
    """The views of every row of a retrieval, with GitHub's responses when the retrieval has
    them."""
    rid = row["retrieval_id"]
    ghs_rows = ghs_cache.read_rows(cache_dir, rid, "ghs", row["ghs_file_sha256"])
    if row["github_file_sha256"]:
        github_rows = ghs_cache.read_rows(cache_dir, rid, "github", row["github_file_sha256"])
        return [view(ghs, github) for ghs, github in zip(ghs_rows, github_rows, strict=True)]
    return [view(ghs, None) for ghs in ghs_rows]


def by_repository(records: list) -> dict:
    """The views keyed by GitHub's id, else by GHS's name. GHS has several rows that GitHub
    resolves to one repository when it crawled the repository under a new name and kept the old
    row. Of those rows, the row under the current name represents the repository, and `aliases`
    keeps every name."""
    keyed = {}
    for record in records:
        key = (("id", record["githubId"]) if record["githubId"] is not None
               else ("name", ghs_cache.name_key(record["ghsName"])))
        held = keyed.get(key)
        names = {ghs_cache.name_key(n) for n in (record["ghsName"], record["name"]) if n}
        current = ghs_cache.name_key(record["name"] or "") == ghs_cache.name_key(record["ghsName"])
        if held is None or (current and ghs_cache.name_key(held["name"] or "")
                            != ghs_cache.name_key(held["ghsName"])):
            keyed[key] = dict(record, aliases=names | (held["aliases"] if held else set()))
        else:
            held["aliases"] |= names
    return keyed


def match(old: dict, new: dict) -> tuple:
    """The pairs of old and new keys of one repository, the unmatched new keys and the unmatched
    old keys."""
    old_names = {}
    for key, record in old.items():
        for name in sorted(record["aliases"]):
            old_names.setdefault(name, key)
    # Rows are matched by GitHub's id first. Otherwise a row matched by a name that it shares
    # with an old row could take that old row from a later row with the same GitHub id, and the
    # later row would be reported as added. A name matches only where one of the two rows has no
    # GitHub id: a repository keeps its id when it is renamed, so two rows with different ids
    # are two repositories, of which the newer took the name of the older.
    found_of = {key: key for key in new if key in old and key[0] == "id"}
    matched = set(found_of.values())
    for key, record in new.items():
        if key in found_of:
            continue
        for name in sorted(record["aliases"]):
            candidate = old_names.get(name)
            if (candidate is not None and candidate not in matched
                    and not (key[0] == "id" and candidate[0] == "id")):
                found_of[key] = candidate
                matched.add(candidate)
                break
    pairs = [(found_of[key], key) for key in new if key in found_of]
    added = [key for key in new if key not in found_of]
    removed = [key for key in old if key not in matched]
    return pairs, added, removed


def differences(old: dict, new: dict) -> list:
    """The compared fields with values that differ, skipping a field that either side lacks. A
    name is compared without regard to case, because GHS records names in another case than
    GitHub's (compare-retrievals-spec.md). A license is compared without its double quotes,
    which GitHub writes and GHS does not."""
    def same(name, a, b):
        if name == "name":
            return a.lower() == b.lower()
        if name == "license":
            return ghs_cache.license_key(a) == ghs_cache.license_key(b)
        return a == b

    return [name for name, *_ in FIELDS
            if old.get(name) is not None and new.get(name) is not None
            and not same(name, old[name], new[name])]


def compare_retrievals(old_row: dict, old: dict, new_row: dict, new: dict) -> tuple:
    """The change rows and the counts between two retrievals."""
    pairs, added, removed = match(old, new)
    rows, reasons, fields = [], Counter(), Counter()
    unchanged = 0
    for old_key, new_key in pairs:
        changed = differences(old[old_key], new[new_key])
        if not changed:
            unchanged += 1
            continue
        fields.update(changed)
        record = new[new_key]
        rows.append({"repository": record["name"] or record["ghsName"],
                     "github_id": record["githubId"], "change": "changed", "reason": "",
                     "fields": ";".join(changed),
                     "old": {f: old[old_key][f] for f in changed},
                     "new": {f: record[f] for f in changed}})
    for key in added:
        record = new[key]
        created = record.get("createdAt") or ""
        reason = ("created_after_previous_window"
                  if created and old_row["created_max"] and created >= old_row["created_max"]
                  else "revived_or_newly_matched")
        reasons[reason] += 1
        rows.append({"repository": record["name"] or record["ghsName"],
                     "github_id": record["githubId"], "change": "added", "reason": reason,
                     "fields": "", "old": {}, "new": {}})
    for key in removed:
        record = old[key]
        last = record.get(LAST_COMMIT) or ""
        reason = ("inactive_before_window"
                  if last and new_row["committed_min"] and last < new_row["committed_min"]
                  else "deleted_or_no_longer_matching")
        reasons[reason] += 1
        rows.append({"repository": record["name"] or record["ghsName"],
                     "github_id": record["githubId"], "change": "removed", "reason": reason,
                     "fields": "", "old": {}, "new": {}})
    counts = {"added": len(added), "removed": len(removed),
              "changed": len(pairs) - unchanged, "unchanged": unchanged,
              "reasons": dict(sorted(reasons.items())), "fields": dict(sorted(fields.items()))}
    return rows, counts


def compare_collection(collected: Path, new: dict) -> tuple:
    """Every collected repository compared with the new retrieval: the rows of the repositories
    that are not `unchanged`, and the counts."""
    by_name = {}
    for record in new.values():
        for name in sorted(record["aliases"]):
            by_name.setdefault(name, record)
    with open(collected, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"repo_name", "branch", "head_sha"} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{collected} has no column {', '.join(sorted(missing))}")
        collected_rows = list(reader)
    rows, counts = [], Counter()
    for entry in collected_rows:
        name = entry["repo_name"]
        record = by_name.get(ghs_cache.name_key(name))
        if not entry["head_sha"]:
            changes = ["not_collected"]
        elif record is None:
            changes = ["absent_from_retrieval"]
        elif record["onGitHub"] is False:
            changes = ["gone_on_github"]
        else:
            changes = []
            if record["headSha"] and record["headSha"] != entry["head_sha"]:
                changes.append("moved")
            if record["defaultBranch"] and record["defaultBranch"] != entry["branch"]:
                changes.append("branch_changed")
            if record["name"] and ghs_cache.name_key(record["name"]) != ghs_cache.name_key(name):
                changes.append("renamed")
        counts.update(changes or ["unchanged"])
        if changes:
            rows.append({"repository": name, "github_id": record["githubId"] if record else None,
                         "changes": ";".join(changes),
                         "collected_sha": entry["head_sha"],
                         "current_sha": record["headSha"] if record else "",
                         "collected_branch": entry["branch"],
                         "current_branch": record["defaultBranch"] if record else "",
                         "current_name": record["name"] if record else ""})
    collected_count = len(collected_rows) - counts["not_collected"]
    summary = {"path": str(collected), "repositories": len(collected_rows),
               "collected": collected_count, **{k: counts[k] for k in (
                   "moved", "branch_changed", "renamed", "gone_on_github",
                   "absent_from_retrieval", "not_collected", "unchanged")},
               "moved_share": round(counts["moved"] / collected_count, 4) if collected_count else None}
    return rows, summary


def write_csv(path: Path, columns: list, rows: list) -> None:
    """Write rows as CSV in one step (`caches.replace_file`), with dict cells as JSON."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: json.dumps(v, ensure_ascii=False, sort_keys=True)
                         if isinstance(v, (dict, list)) else ("" if v is None else v)
                         for k, v in row.items()})
    caches.replace_file(Path(path), buffer.getvalue())


# What a report was made from: the two retrievals and the collected list.
INPUTS = (("new_retrieval", "the new retrieval"), ("old_retrieval", "the earlier retrieval"),
          ("collected_sha256", "the collected list"))


def keep_previous(args: argparse.Namespace, summary: dict) -> None:
    """Rename the reports on disk to `.previous` names when they were made from other inputs
    than `summary`'s, so that the version before this run stays beside the new version. That
    version is the report on the previous retrieval, or, after a collection, the report that a
    run with `--from retrieval_comparison` replaces. A run over the same inputs replaces its own
    reports."""
    try:
        recorded = json.loads(args.summary.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = [why for key, why in INPUTS if recorded.get(key) != summary.get(key)]
    if not changed:
        return
    for path in (args.changes, args.collection_changes, args.summary):
        if path.is_file():
            os.replace(path, path.with_name(f"{path.stem}.previous{path.suffix}"))
    inputs = " and ".join(filter(None, [", ".join(changed[:-1]), changed[-1]]))
    logs.info(COMPONENT, f"Renamed the files of the comparison of retrieval "
                         f"{recorded.get('new_retrieval')} with {recorded.get('old_retrieval')} "
                         f"to .previous names. {upper_first(inputs)} changed.")


def main(argv: list | None = None) -> int:
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(description="Compare two GHS retrievals, and compare the "
                                                 "collected repositories with the newer one.",
                                     allow_abbrev=False)
    parser.add_argument("--cache-dir", type=Path, default=ghs_cache.CACHE_DIR)
    parser.add_argument("--pointer", type=Path, default=ghs_cache.POINTER,
                        help="The file that names the new retrieval (default: %(default)s)")
    parser.add_argument("--old", help="The retrieval to compare against (default: the usable "
                                      "retrieval before the new one)")
    parser.add_argument("--collected", type=Path, default=DEFAULT_COLLECTED,
                        help="The CSV of collected repositories, skipped when absent "
                             "(default: %(default)s)")
    parser.add_argument("--changes", type=Path, default=CHANGES)
    parser.add_argument("--collection-changes", type=Path, default=COLLECTION_CHANGES)
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    args = parser.parse_args(argv)
    try:
        new_row = ghs_cache.pointed_retrieval(args.pointer, args.cache_dir)
        old_row = (ghs_cache.index_row(args.old, args.cache_dir) if args.old
                   else ghs_cache.previous_retrieval(new_row["retrieval_id"], args.cache_dir))
        new = by_repository(views(args.cache_dir, new_row))
        summary = {"new_retrieval": new_row["retrieval_id"],
                   "old_retrieval": old_row["retrieval_id"] if old_row else None}
        change_rows = []
        if old_row is not None and old_row["retrieval_id"] != new_row["retrieval_id"]:
            old = by_repository(views(args.cache_dir, old_row))
            change_rows, summary["retrievals"] = compare_retrievals(old_row, old, new_row, new)
            summary["window_moved"] = (old_row["created_max"], old_row["committed_min"]) != (
                new_row["created_max"], new_row["committed_min"])
        collection_rows = []
        if args.collected.is_file():
            collection_rows, summary["collection"] = compare_collection(args.collected, new)
            summary["collected_sha256"] = hashlib.sha256(args.collected.read_bytes()).hexdigest()
    except (CacheError, OSError, ValueError) as error:
        logs.error(COMPONENT, f"Could not compare the retrievals ({error}).")
        return 1
    keep_previous(args, summary)
    write_csv(args.changes, ["repository", "github_id", "change", "reason", "fields", "old", "new"],
              change_rows)
    write_csv(args.collection_changes, ["repository", "github_id", "changes", "collected_sha",
                                        "current_sha", "collected_branch", "current_branch",
                                        "current_name"], collection_rows)
    caches.replace_file(args.summary, json.dumps(summary, indent=1, sort_keys=True) + "\n")
    if "retrievals" in summary:
        counts = summary["retrievals"]
        logs.info(COMPONENT, f"Compared retrieval {summary['new_retrieval']} with "
                             f"{summary['old_retrieval']} ({counts['added']:,} added, "
                             f"{counts['removed']:,} removed, {counts['changed']:,} changed, "
                             f"{counts['unchanged']:,} unchanged). The sampling window "
                             f"{'moved' if summary['window_moved'] else 'did not move'}.")
    elif old_row is None:
        logs.info(COMPONENT, f"Retrieval {summary['new_retrieval']} is the first in the cache. "
                             f"No earlier retrieval to compare it with.")
    else:
        logs.info(COMPONENT, f"Retrieval {summary['new_retrieval']} is both the new and the old "
                             f"retrieval. Nothing to compare.")
    if "collection" in summary:
        c = summary["collection"]
        share = f"{c['moved_share']:.1%}" if c["moved_share"] is not None else "n/a"
        logs.info(COMPONENT, f"Compared the collected repositories with retrieval "
                             f"{summary['new_retrieval']} ({c['moved']:,} of {c['collected']:,} "
                             f"or {share} with a new HEAD commit, {c['branch_changed']:,} with a "
                             f"new default branch, {c['renamed']:,} renamed, "
                             f"{c['gone_on_github']:,} no longer on GitHub, "
                             f"{c['absent_from_retrieval']:,} not in the retrieval).")
    else:
        logs.info(COMPONENT, f"No CSV of collected repositories at {args.collected}. Skipped "
                             f"their comparison.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
