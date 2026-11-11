"""The commit_reclassification step: applies the current commit heuristics to every commit
that the cloning step read.

The cloning step classifies each repository's commits under the heuristics of its image as it
reads them, and it keeps the commit log beside the shard (`commits.stored_log_path`). This step
applies the heuristics of the checkout that runs it to those commit logs. A change to
`ai_commit_patterns.csv` or `other_ai_commit_patterns.csv` therefore reaches a finished
collection without a new clone. From the commit logs, the step rebuilds the commit CSV, the
commits of the tools outside the six, the unmatched attributions, the mentions,
`ai_commits-repos.csv` and the AI columns of every configuration artifact.

A repository that is already classified under the current heuristics still needs validated
evidence, and the step reuses its shards once the evidence is validated. The shards of a
repository with evidence that failed validation stay out of the commit CSVs and
`ai_authorship.csv` (`left_out_shards`), as do the shards of a repository that other commit
heuristics classified and that this step could not classify again. The detection step leaves
the same shards out. The step counts the `#commits` of `file_history.csv` again from the path
histories of the records with validated evidence. A collection measured under an earlier
definition of the column gets the current definition in this way, without a clone.

Inputs:  collect_commits/ (the shards, the per-repository commit records and the commit logs),
         the list of finished repositories, data/collected/repos.csv and
         data/collected/clone_failed.csv
Outputs: output/all_ai_commits.csv and the four CSVs beside it, the AI rows of
         ai_authorship.csv and the #commits of file_history.csv

Exit code 0 when the assembly completed and the repositories with incomplete evidence are within
what the cloning step accepts: the commit histories that the time bound or the size bound kept it
from reading, and at most `guards.clone_failure_ceiling` others. Exit code 2 when the other
repositories with incomplete evidence exceed that ceiling, and 1 when an input is missing or the
assembly failed. `complete` in output/commit-reclassification.json records whether the evidence is
complete.
"""

import concurrent.futures
import csv
import hashlib
import io
import json
import multiprocessing
import os
import sys
import traceback
from collections import Counter
from pathlib import Path

from agentcontext import STAGE_DIR, caches, commits, guards, logs, paths, resume
from agentcontext import cli
from agentcontext.repos import repo_name_to_dir

COMPONENT = 'commit_reclassification'

# The outputs of the cloning step that the reclassification reads and writes, with the names that
# `repository_cloning` gives them. A test checks that the names agree. The runner records this
# step under the code of its module closure, and importing the names from the cloning step would
# add the collector and the modules that only schedule the collection
# (`reuse.SCHEDULING_MODULES`) to that closure. An edit to one of those modules would then run
# this step and every later step again without collecting anything.
COMMITS_DIR_NAME = "collect_commits"
COLLECTED_CSV_NAME = "data/collected/repos.csv"
FAIL_CSV_NAME = "data/collected/clone_failed.csv"
AI_COMMITS_CSV_NAME = "output/all_ai_commits.csv"

# The outcomes of a required commit history with missing or incomplete evidence. `failed`, an
# evidence or processing error of one repository, also leaves the reclassification incomplete.
# `unavailable`, a recorded clone failure, leaves only the population incomplete.
INCOMPLETE_OUTCOMES = ("no_log", "no_scope", "no_path_history", "history_incomplete",
                       "history_not_read", "missing_record", "missing_tree")
# Every outcome of a requested repository: the incomplete outcomes, `failed`, a recorded clone
# failure, a repository without files, and the two outcomes of a classification under the
# current heuristics.
OUTCOMES = tuple(sorted((*INCOMPLETE_OUTCOMES, "failed", "unavailable", "not_applicable",
                         "reclassified", "current")))


def outcome_counts(outcomes) -> dict:
    """The number of repositories with each outcome of `OUTCOMES`, from `outcomes`, the outcome
    of each repository. An outcome that no repository has counts 0, and the report names it."""
    counts = Counter(outcomes)
    unknown = sorted(set(counts) - set(OUTCOMES))
    if unknown:
        raise ValueError(f"The reclassification outcomes {', '.join(unknown)} are unknown")
    return {name: counts[name] for name in OUTCOMES}


def coverage(counts: dict, assembled: bool) -> tuple:
    """(complete, population_complete) for the outcome counts of a reclassification report.

    Both fields follow from this one rule. `snapshot write` applies it again to the repository
    outcomes of a report before it records the report's summary.
    """
    complete = (assembled and not counts.get("failed")
                and not any(counts.get(name) for name in INCOMPLETE_OUTCOMES))
    return complete, complete and not counts.get("unavailable")


def bounded_history(commits_dir: Path, repo: str) -> bool:
    """Whether the cloning step left the repository's commit history unread for a reason that
    recurs under the bounds that its per-repository commit record names.

    A fetch or a reading that the time bound or the size bound stopped recurs, and so does a
    reading that git refused. Only a run under larger bounds reads such a history
    (`resume.history_needs_reread`). The cloning step accepts the repository without counting it
    toward its ceiling, and this step accepts it too. A history that the cloning step reads
    again on the next run, such as one that a network failure or a machine failure kept it from
    reading, is not bounded.
    """
    try:
        record = json.loads(commits.shard_paths(commits_dir, repo)[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (not isinstance(record, dict)
            or record.get("outcome") not in ("history_unavailable", "history_read_failed")):
        return False
    # Pass the bounds that the record names, and the history is bounded when a run under the same
    # bounds would not read it again. A bound of 0 or none means no bound, in the record and in
    # the arguments of `history_needs_reread` alike.
    return not resume.history_needs_reread(commits_dir, repo, record.get("time_bound") or 0,
                                           record.get("size_bound") or 0)


def collection_repos(collected_csv: Path, fail_csv: Path) -> list:
    """The repositories that the collection covered, from the collected list and then from the
    failed clones, which together make up its input list."""
    repos = []
    for path in (collected_csv, fail_csv):
        if path.is_file():
            with open(path, newline="", encoding="utf-8") as f:
                repos.extend(row["repo_name"] for row in csv.DictReader(f) if row.get("repo_name"))
    return list(dict.fromkeys(repos))


def left_out_shards(commits_dir: Path, read_repos: list, repository_results: dict) -> dict:
    """{repo: per-repository commit record} for the repositories with shards that stay out of
    the commit CSVs and `ai_authorship.csv`. These are the repositories with evidence that failed
    validation (`failed`) and the repositories with a commit history that other commit
    heuristics classified.

    The commit attribution validation matches the current heuristics against every row of the
    commit CSVs. A row classified by heuristics that have changed since, such as a row that
    names a heuristic id that was removed, would stop the run there. The evidence of a `failed`
    repository does not support its rows. A left-out repository has one of `INCOMPLETE_OUTCOMES`
    or `failed` as its outcome, which leaves the reclassification incomplete, and its shards stay
    on disk for a later collection. Its result records `left_out` and the SHA-256 of its record.
    From these two fields, the detection step leaves out the same shards while the record is
    unchanged, and the cloning step collects a `failed` repository again
    (`resume.reclassification_failed`).

    A record that does not parse, such as one that a power loss left empty, is `failed`. It is
    left out with the SHA-256 of its bytes and the outcome `shard_unreadable`, which its row of
    `ai_commits-repos.csv` records.
    """
    left_out = {}
    for repo in read_repos:
        try:
            text = commits.shard_paths(commits_dir, repo)[1].read_bytes()
        except OSError:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            left_out[repo] = commits.unavailable_record(repo, "", "", "shard_unreadable")
            repository_results[repo].update(left_out=True,
                                            record_sha256=hashlib.sha256(text).hexdigest())
            continue
        if (repository_results[repo]["outcome"] == "failed"
                or (record.get("outcome") == "history_read"
                    and record.get("classified_by") != commits.CLASSIFIED_BY)):
            left_out[repo] = record
            repository_results[repo].update(left_out=True,
                                            record_sha256=hashlib.sha256(text).hexdigest())
    return left_out


def published_count(value) -> "int | str":
    """The count of a left-out repository's commits in the commit CSVs, which contain none of
    them. The count is 0 where the per-repository commit record has a count, and blank where the
    record leaves the count blank because the commit history was not read."""
    return "" if value in ("", None) else 0


def record_left_out(repos_csv: Path, left_out: dict) -> None:
    """Write for each repository of `left_out` the row of `ai_commits-repos.csv` that its
    per-repository commit record gives. The row has the outcome, the commits read and the commits
    without an attribution in the commit history that the cloning step read. `commits_matched`
    and `commits_other_tools` count the repository's commits in the commit CSVs, which leave its
    shards out. Both are 0 (`published_count`), and a reader who sums them gets the number of
    published rows.

    `commits.assemble_ai_commits` reads the shards of the repositories that it is given. Without
    a record, it writes `no_files` for a repository from which files were copied, and this
    function replaces that row.
    """
    with repos_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        columns, rows = list(reader.fieldnames or []), list(reader)
    for row in rows:
        record = left_out.get(row["repo_name"])
        if record is not None:
            row.update(outcome=record.get("outcome", ""),
                       commits_read=record.get("commits_read", ""),
                       commits_matched=published_count(record.get("commits_matched")),
                       commits_other_tools=published_count(record.get("commits_other_tools")),
                       no_signal_shas=";".join(record.get("no_signal_shas") or []))
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    caches.replace_file(repos_csv, text.getvalue())


def drop_ai_rows(path: Path, left_out: dict) -> int:
    """Remove from `ai_authorship.csv` the AI rows that the per-repository commit records of
    `left_out` hold and that the cloning step's assembly put there. A row with other values
    stays, because the file history step measured it in a clone of its own.

    A record that does not name its AI rows, such as a record that does not parse
    (`shard_unreadable`), gives no values to compare. The rows that the repository's earlier,
    readable record held are still in the file. The function removes every row of such a
    repository, including the rows that the file history step measured, because only the record
    could tell which rows the assembly wrote. A record that does not parse is `failed`, and the
    next collection collects its repository again (`resume.reclassification_failed`). Returns
    the number of rows removed."""
    recorded, unnamed = {}, set()
    for repo, record in left_out.items():
        rows = record.get("ai_rows")
        if (record.get("outcome") == "shard_unreadable" or not isinstance(rows, list)
                or not all(isinstance(row, dict) for row in rows)):
            unnamed.add(repo)
            continue
        for row in rows:
            key = caches.AiAuthorshipKey(*(row.get(column, "")
                                           for column in caches.AI_AUTHORSHIP_KEY_COLUMNS))
            recorded[key] = caches.AiAuthorship.from_row(row)
    cache = caches.AiAuthorshipCache.load(path)
    kept = {key: values for key, values in cache.items()
            if key.repo_name not in unnamed and recorded.get(key) != values}
    if len(kept) < len(cache):
        caches.AiAuthorshipCache(kept, path=path).save()
    return len(cache) - len(kept)


def _reclassify(job: tuple) -> tuple:
    """Reclassify one repository in a worker. Returns (repo, the outcome of
    `commits.reclassify` or "failed", the error or "")."""
    commits_dir, repo, head_sha, artifacts = job
    try:
        return repo, commits.reclassify(commits_dir, repo, head_sha, artifacts), ""
    except Exception as error:
        return repo, "failed", f"{error.__class__.__name__} ({error})"


def finished_copied(path: Path) -> dict:
    """{repo: the latest explicit count of copied files} from the list of finished
    repositories, skipping lines that were only partly written.

    A line that repeats the row of the line before it keeps that line's count. The cloning step
    writes such a line without a count when a head query marks the line `head_no_response` or
    clears the mark, before a retry, after a retry that the pool refused, and when a failed
    retry keeps the earlier collection. The row then still describes the collection that the
    count was taken from. Read as a missing count, such a line would give every repository
    without files and with a failed head query the outcome `missing_record`.
    """
    copied, rows = {}, {}
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    repo = record["repo"]
                    if "row" not in record:
                        continue
                    count = (record.get("measures") or {}).get("copied")
                    if isinstance(count, int):
                        copied[repo] = count
                    elif record["row"] != rows.get(repo):
                        copied[repo] = None
                    rows[repo] = record["row"]
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    return copied


def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Apply the current commit heuristics to the commit logs that the cloning "
                    "step kept, and assemble the commit CSVs and the AI columns from them.")
    parser.add_argument("--commits-dir", default=str(STAGE_DIR / COMMITS_DIR_NAME),
                        help="Directory of the shards, per-repository commit records and commit "
                             f"logs (default: {COMMITS_DIR_NAME}/ in the stage "
                             f"directory)")
    parser.add_argument("--finished", default=resume.FINISHED_NAME,
                        help=f"The cloning step's list of finished repositories "
                             f"(default: {resume.FINISHED_NAME})")
    parser.add_argument("--collected-csv", default=COLLECTED_CSV_NAME)
    parser.add_argument("--fail-csv", default=FAIL_CSV_NAME)
    parser.add_argument("--ai-commits-csv", default=str(STAGE_DIR / AI_COMMITS_CSV_NAME))
    parser.add_argument("--ai-authorship", default=str(caches.AI_AUTHORSHIP_PATH))
    parser.add_argument("--file-history", default=str(caches.FILE_HISTORY_PATH))
    parser.add_argument("-w", "--workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)

    commits_dir = Path(args.commits_dir).resolve()
    collected_csv, fail_csv = Path(args.collected_csv), Path(args.fail_csv)
    if not commits_dir.is_dir():
        logs.error(COMPONENT, f"{commits_dir} does not exist. Run repository cloning first to "
                              f"write it.")
        return 1
    if not collected_csv.is_file():
        logs.error(COMPONENT, f"{collected_csv} does not exist. Run repository cloning first to "
                              f"write it.")
        return 1
    if not fail_csv.is_file():
        logs.error(COMPONENT, f"{fail_csv} does not exist (the clone failures of the sample). "
                              f"Run repository cloning first to write it.")
        return 1
    repos = collection_repos(collected_csv, fail_csv)
    wanted = set(repos)
    finished = {repo: value for repo, value in resume.read_finished(Path(args.finished)).items()
                if repo in wanted}
    copied = finished_copied(Path(args.finished))
    with collected_csv.open(newline="", encoding="utf-8") as stream:
        collected = {row["repo_name"]: row for row in csv.DictReader(stream)}
    failed_clones = {}
    if fail_csv.is_file():
        with fail_csv.open(newline="", encoding="utf-8") as stream:
            failed_clones = {row["repo_name"]: row["reason"] for row in csv.DictReader(stream)}
    # A recorded clone failure is `unavailable`, whatever an older record beside it contains.
    # The cloning step removes a repository's record when its clone fails, and a record that
    # still exists describes an earlier clone, not this collection.
    read_repos = [repo for repo in repos if repo not in failed_clones
              and commits.shard_paths(commits_dir, repo)[1].is_file()]
    artifacts = {}
    for repo in repos:
        tree = collected_csv.parent / repo_name_to_dir(repo)
        if tree.is_dir():
            artifacts[repo] = paths.tree_files(tree)

    outcomes, repository_results = Counter(), {}
    failures = []
    # The cloning step writes a per-repository commit record only for a repository from which it
    # copied files, and the reclassification checks the recorded paths against the repository's
    # collected tree. Without the tree, the step keeps the record unchanged and the
    # reclassification is incomplete, as for a missing commit log.
    missing_trees = [repo for repo in read_repos if repo not in artifacts]
    for repo in missing_trees:
        repository_results[repo] = {"outcome": "missing_tree"}
        outcomes["missing_tree"] += 1
    if missing_trees:
        first = collected_csv.parent / repo_name_to_dir(missing_trees[0])
        logs.warning(COMPONENT, f"{len(missing_trees):,} repositories with a per-repository commit "
                                f"record have no directory in the collected tree, such as {first}. "
                                f"Kept their records unchanged. Restore the directories from the "
                                f"original handover, or collect the repositories again.")
    pace = logs.Pace()
    with concurrent.futures.ProcessPoolExecutor(
            max(1, args.workers), mp_context=multiprocessing.get_context("spawn")) as pool:
        jobs = [(commits_dir, repo, collected.get(repo, {}).get("head_sha", ""), artifacts[repo])
                for repo in read_repos if repo in artifacts]
        for done, (repo, outcome, error) in enumerate(pool.map(_reclassify, jobs, chunksize=8), 1):
            outcomes[outcome] += 1
            repository_results[repo] = {"outcome": outcome, **({"error": error} if error else {})}
            if error:
                failures.append((repo, error))
            if pace.due():
                logs.info(COMPONENT, f"{done:,}/{len(jobs):,} repositories read.")
    for repo in repos:
        if repo in repository_results:
            continue
        row = collected.get(repo, {})
        if repo in failed_clones:
            result = {"outcome": "unavailable", "reason": failed_clones[repo]}
        elif (artifacts.get(repo)
              or finished.get(repo, (None, None, []))[2]
              or copied.get(repo) != 0
              or repo not in finished):
            result = {"outcome": "missing_record"}
        else:
            result = {"outcome": "not_applicable", "reason": "no_files"}
        repository_results[repo] = result
        outcomes[result["outcome"]] += 1
    # A failure of one repository becomes that repository's outcome, as missing evidence does.
    # The step still assembles the other repositories, and the run continues to the later steps.
    for repo, error in failures:
        logs.warning(COMPONENT, f"Reclassifying the commits of {repo} raised {error}. Recorded "
                                f"outcome failed and kept its shards on disk, out of the commit "
                                f"CSVs and ai_authorship.csv.")
    logs.info(COMPONENT, f"Reclassified {outcomes['reclassified']:,} of {len(read_repos):,} "
                         f"per-repository commit records under the current heuristics. "
                         f"{outcomes['current']:,} were already classified under them, and "
                         f"{outcomes['history_not_read']:,} record a commit history that was not "
                         f"read.")
    if outcomes["no_log"]:
        logs.warning(COMPONENT, f"{outcomes['no_log']:,} repositories have no commit log. Kept "
                                f"their recorded classifications where the current heuristics "
                                f"made them. Collect them again to reclassify them.")
    left_out = left_out_shards(commits_dir, read_repos, repository_results)
    if left_out:
        logs.warning(COMPONENT, f"{len(left_out):,} repositories, such as {min(left_out)}, have "
                                f"evidence that failed validation or commits that other commit "
                                f"heuristics classified. Left their shards out of the commit CSVs "
                                f"and ai_authorship.csv (reclassification incomplete). Their "
                                f"outcomes in commit-reclassification.json name what to repair.")
    # The cloning step's ceiling limits the repositories with incomplete evidence that it would
    # collect again. A bounded history (`bounded_history`) does not count toward the ceiling.
    bounded = sorted(repo for repo, result in repository_results.items()
                     if result["outcome"] == "history_not_read"
                     and bounded_history(commits_dir, repo))
    for repo in bounded:
        repository_results[repo]["bounded"] = True
    incomplete = sum(outcomes[name] for name in (*INCOMPLETE_OUTCOMES, "failed"))
    left = incomplete - len(bounded)
    ceiling = guards.clone_failure_ceiling(len(repos))
    report = Path(args.ai_commits_csv).parent / resume.RECLASSIFICATION_REPORT_NAME
    report.parent.mkdir(parents=True, exist_ok=True)
    report_fields = {
        "classified_by": commits.CLASSIFIED_BY,
        "collection_sha256": commits._file_sha256(collected_csv),
        "failures_sha256": commits._file_sha256(fail_csv),
        "complete": False,
        "population_complete": False,
        "assembly_complete": False,
        "requested_repositories": len(repos),
        "bounded_histories": len(bounded),
        "left_to_retry": left,
        "retry_ceiling": ceiling,
        "counts": outcome_counts(outcomes.elements()),
        "repositories": dict(sorted(repository_results.items())),
    }
    def write_report():
        caches.replace_file(report, json.dumps(report_fields, indent=2) + "\n")

    write_report()
    try:
        # The record of a failed clone belongs to an earlier clone, as for its outcome above, and
        # its commits stay out of the CSVs. `finished` still gives the repository a row of
        # `ai_commits-repos.csv` that records the failed clone. The shards left out above stay
        # out too, as do the AI rows that the cloning step's assembly took from their records.
        counts = commits.assemble_ai_commits(
            commits_dir, finished, Path(args.ai_commits_csv), Path(args.ai_authorship),
            repos=[repo for repo in repos if repo not in failed_clones and repo not in left_out])
        if left_out:
            record_left_out(Path(args.ai_commits_csv).with_name("ai_commits-repos.csv"),
                            left_out)
            dropped = drop_ai_rows(Path(args.ai_authorship), left_out)
            logs.info(COMPONENT, f"Removed {dropped:,} AI rows of the left-out repositories from "
                                 f"{Path(args.ai_authorship).name}.")
    except Exception:
        report_fields["complete"] = report_fields["population_complete"] = False
        report_fields["assembly_error"] = traceback.format_exc()
        write_report()
        logs.error(COMPONENT, f"Could not assemble the commit CSVs from {commits_dir}.\n"
                              f"{traceback.format_exc()}")
        return 1
    logs.info(COMPONENT, f"Assembled {counts['rows']:,} AI commits and "
                         f"{counts['other_rows']:,} commits attributed to tools outside the six.")
    # Count again only from the records with validated evidence. The other rows keep their
    # measured values.
    validated = [repo for repo, result in repository_results.items()
                 if result["outcome"] in ("reclassified", "current")]
    history_cache = caches.FileHistoryCache.load(args.file_history)
    recounted = history_cache.recount(commits.recorded_commit_counts(commits_dir, validated))
    if recounted:
        history_cache.save()
    logs.info(COMPONENT, f"Recounted #commits from the path histories and changed "
                         f"{recounted:,} rows of {Path(args.file_history).name}.")
    complete, population_complete = coverage(outcomes, assembled=True)
    report_fields.update(assembly_complete=True, complete=complete,
                         population_complete=population_complete)
    write_report()
    if incomplete:
        logs.info(COMPONENT, f"{incomplete:,} repositories have incomplete evidence. "
                             f"{report.name} records the reclassification as incomplete.")
        logs.info(COMPONENT, f"The time bound or the size bound stopped {len(bounded):,} commit "
                             f"histories. Repository cloning under larger bounds reads them.")
        logs.info(COMPONENT, f"A later collection retries {left:,} repositories (at most "
                             f"{ceiling:,} for a sample of {len(repos):,}).")
    if left > ceiling:
        logs.warning(COMPONENT, f"{left:,} repositories with incomplete evidence are more than "
                                f"the {ceiling:,} that repository cloning may leave to retry "
                                f"(exit code 2). Their outcomes in {report} name what to repair.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
