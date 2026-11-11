#!/usr/bin/env python3
"""Run the cloning step over local mirrors in a separate checkout, kill it once, resume it,
run every step that the container runs after it, and compare the outputs with a reference run.

The steps after the collection are read from the runner's list, the list that
`run --with-commit-reclassification` executes, the publication included. The publication reads
labels and a sampling frame, which a run of the container produces in its own sampling and
classification stages. This run has neither stage and extracts the labels and the
sampling frame of March 2026 from their tracked archives in the checkout.

This is the subsample run that a change of the collector or the runner must pass before its
image goes to the NAS. It clones the repositories of the run list, `--repo-csv`, from mirrors on
disk through a `url.<mirrors>.insteadOf` rewrite, so that no clone reaches github.com and no
GitHub access token is needed. The run list is the development subsample and the repositories
added to it, among them the repository with the failing history fetch. The run works in a
separate checkout of this repository and writes its files to that checkout's `data/`, never to
this repository's tree. The checkout is first fetched and reset to this repository's commit,
so that the code that runs is the code committed here.

One repository's history fetch fails on purpose, the first of the run list unless
`--history-fails` names another. Its clones in the cloning step come from a copy of its
mirror that contains only the tip. `git fetch --unshallow` then exits 0 and leaves the clone
shallow, which is how the cloning step sees a history fetch that failed on the network, as when
the time bound stops the fetch or GitHub returns HTTP 400. The cloning step records the
history as unavailable and tries the repository again, in the resume or in a round of its own
before it ends, with the pause shortened to seconds. The repository stays within the cloning
step's ceiling, and the collection exits 0. The reclassification must exit 0 and report exactly
the history failure that the run injected, as an incomplete reclassification, because the
container's runner continues with that report to the publication and collects nothing again.
The detection step writes the repository's rows with blank Git columns, and the file history
step measures them from the full mirror. The remaining steps run through the publication, and
the run keeps the coverage report.

Usage:

    python3 -m agentcontext.tools.mirror_run --checkout DIR --repo-csv LIST \\
        --mirrors DIR --reference DIR [--logs DIR] [--kill-after SECONDS] [--workers N] \\
        [--history-fails OWNER/REPO] [--min-free-gb GB]

The comparison is `regressions compare` over the two stage directories, and a reference made
before the tracked caches grew differs from every later run in known ways. With
`--expected-findings FILE`, the comparison's findings, with the paths of both stage directories
removed, have to be exactly the lines of that file. The run passes when they are, and the
differences in either direction are logged when they are not. The file is written once from a
run that a person read and judged, and rewritten when a change is meant to change the outputs.

Exit codes: 0 when the step exits and the coverage match the expected outcomes and the
comparison found nothing beyond the expected findings, 2 when a step failed or the comparison
found something else, and 1 when a guard refused the run.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from agentcontext import REPO_ROOT, SEVEN_ZIP
from agentcontext import cli
from agentcontext import logs

# The largest run list that a subsample run reads. A run over more rows than this is not a
# subsample run, and the guard keeps a mistyped `--repo-csv` from cloning the whole sample. The
# subsample tool draws as many repositories as the heuristics need to be covered, and the run
# list adds the repository with the history fetch that the run makes fail. The ceiling leaves
# room for a subsample that grows with the heuristics.
MAX_ROWS = 200
# The exit code of the cloning step that stopped on SIGTERM: Its handlers end it with
# `os._exit(128 + signum)`. Any other code after the kill is a step that did not stop as it
# should: a traceback, `LowDiskStop`'s 4, or SIGKILL after the grace period.
STOPPED_EXIT = 128 + signal.SIGTERM
# The cloning step's pause before a round that retries the repositories that a later run would
# retry, `guards.RETRY_PAUSE_SECONDS` on the NAS, where the pause lets a burst of failures from
# GitHub end. The failure that this run causes does not end, and waiting for it would only
# lengthen the run.
RETRY_PAUSE_SECONDS = 5
# Each run records a different scan time. All attribution columns are published and
# deterministic and must be compared, matched_heuristics included. The JSON records that the
# comparison reads have the scan time in three keys: the window of the scan times, the window of
# the times at which the collected files were written, and the digest of the collected list,
# with rows that contain the scan times. The list itself is compared row by row.
IGNORED_COLUMNS = ("scanned_at", "scanned_at_window", "artifact_mtime_window",
                   "collection_sha256")
# The archives in the checkout's data root that hold the inputs that the publication reads
# from the sampling and classification stages, each with the files that it contains. The
# archive of the classification stage holds the labels of the development dataset, the record
# of the model that made them and the repositories in the sampling frame that are not in
# repos_annotated.csv, the copies that the classification labeled `true` included. The
# archive of the sampling stage holds the sampling frame of the development dataset.
INPUT_ARCHIVES = (("data/classification/repos_annotated.7z",
                   ("repos_annotated.csv", "repos_annotated.json", "repos_excluded.csv")),
                  ("data/sampling/ghs_filtered.json.7z", ("ghs_filtered.json",)))


def steps_after_collect(history_fails: str | None = None) -> list:
    """The steps that the container runs after collection, in its order, as (name, argv).

    Read from the runner's own list, the list that `run --with-commit-reclassification`
    executes, because a copy typed here would become outdated. The file history step runs
    `--offline`, because the cloning step measured every row except the rows of
    `history_fails`, the repository with the history fetch that the run made fail, and a fetch
    for any other row would hide a cloning step that stopped measuring. The collection summary
    runs `--without-fetched-files`, because the run collects its own tree and fetches nothing
    into it, and the list that the checkout tracks describes the fetches of the development
    dataset.

    Called when the steps are about to run rather than at import: The runner imports
    `commits`, which raises the CSV field size limit for the whole process, and the guard on
    an unreadable repository list reads that list under the default limit.
    """
    from agentcontext import runner
    after = []
    for stage in runner.steps_for(True):
        if stage.key in ('engineered_project_selection', "repository_cloning"):
            continue
        argv = list(stage.args)
        if stage.key == 'file_history':
            argv.append("--offline")
            if history_fails:
                argv += ["--offline-except", history_fails]
        if stage.key == "collection_summary":
            argv.append("--without-fetched-files")
        after.append((stage.key, argv))
    return after



class Refused(Exception):
    """A guard refused the run, and the message names the guard."""


def extract_inputs(checkout: Path) -> None:
    """Extract each file of an archived input of the publication that is not yet beside its
    archive, and leave the files that are.

    The files are extracted by name: A checkout can contain the CSV of the labels and lack the
    record of their model, and extracting the whole archive would overwrite a CSV edited since.
    Raises `Refused` when 7zz fails or the archive lacks a file, so that either case logs one
    line, as every other guard does, rather than a traceback after the whole collection.
    """
    for archive, names in INPUT_ARCHIVES:
        archive = checkout / archive
        missing = [name for name in names if not (archive.parent / name).is_file()]
        if not missing:
            continue
        result = subprocess.run([SEVEN_ZIP, "x", "-y", "-bso0", "-bsp0", f"-o{archive.parent}",
                                 str(archive), *missing], capture_output=True, text=True)
        if result.returncode != 0:
            raise Refused(f"7zz could not extract {archive} "
                          f"({(result.stderr or result.stdout).strip()[-300:]})")
        for name in missing:
            if not (archive.parent / name).is_file():
                raise Refused(f"{archive} does not contain {name}")


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused(f"`git {' '.join(args)}` failed in {cwd} ({result.stderr.strip()})")
    return result.stdout.strip()


def checkout_of_this_repository(checkout: Path, root: Path = REPO_ROOT) -> None:
    """Refuse unless `checkout` is a clone with `root` as its origin and is not `root` itself."""
    checkout = checkout.resolve()
    if checkout == root.resolve() or checkout.is_relative_to(root.resolve()):
        raise Refused(f"{checkout} is this repository or inside it. Name a separate checkout.")
    # Before the first `git`, because `subprocess.run` with a `cwd` that does not exist raises
    # FileNotFoundError, which is not a Refused, and a mistyped path would then give a traceback
    # where every other guard gives one line.
    if not checkout.is_dir():
        raise Refused(f"{checkout} is not a directory. Name the checkout of the subsample run.")
    top = Path(git("rev-parse", "--show-toplevel", cwd=checkout)).resolve()
    if top != checkout:
        raise Refused(f"{checkout} is not the root of a checkout ({top} is)")
    url = git("remote", "get-url", "origin", cwd=checkout)
    if url.startswith("file://"):
        url = url[len("file://"):]
    if "://" in url or url.startswith("git@") or not url:
        raise Refused(f"The origin of {checkout} is {url!r}, not a local directory")
    if Path(url).resolve() != root.resolve():
        raise Refused(f"The origin of {checkout} is {url}, not this repository {root}")


def repositories_without_a_mirror(mirrors: Path, path: Path) -> frozenset:
    """The repositories of the run list that have no mirror under `mirrors`, the clone failures
    of the run."""
    with open(path, newline="", encoding="utf-8") as f:
        return frozenset(row["repo_name"] for row in csv.DictReader(f)
                         if row.get("repo_name")
                         and not (mirrors / f"{row['repo_name']}.git").is_dir())


def run_list_repositories(path: Path) -> frozenset:
    """The repositories of the run list."""
    with open(path, newline="", encoding="utf-8") as f:
        return frozenset(row["repo_name"] for row in csv.DictReader(f) if row.get("repo_name"))


def finished_repositories(stage: Path) -> frozenset:
    """The repositories with a line in the step's list of finished repositories."""
    from agentcontext import resume
    path = stage / resume.FINISHED_NAME
    if not path.is_file():
        return frozenset()
    names = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                names.add(json.loads(line)["repo"])
            except (ValueError, KeyError, TypeError):
                continue
    return frozenset(names)


def first_repository(path: Path) -> str:
    """The first repository of the run list, which the cloning step starts at once."""
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("repo_name"):
                return row["repo_name"]
    raise Refused(f"{path} lists no repository")


def shallow_copy(mirrors: Path, repo: str, into: Path) -> Path:
    """A copy of `repo`'s mirror that contains only each branch's tip.

    A clone from it is shallow, and `git fetch --unshallow` from it exits 0 and leaves the
    clone as shallow as before. The cloning step's history fetch therefore fails through git
    and not through a stand-in. The tip is the mirror's, and the collected commit stays the
    same.
    """
    source = (mirrors / f"{repo}.git").resolve()
    if not source.is_dir():
        raise Refused(f"{repo} has no mirror at {source} to make its history fetch fail. Name a "
                      f"repository with a mirror in --history-fails.")
    target = (into / f"{repo}.git").resolve()
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "-q", "--mirror", "--depth", "1", source.as_uri(), str(target), cwd=into)
    if git("rev-parse", "--is-shallow-repository", cwd=target) != "true":
        raise Refused(f"The copy of {repo}'s mirror at {target} is not shallow")
    if git("rev-parse", "HEAD", cwd=target) != git("rev-parse", "HEAD", cwd=source):
        raise Refused(f"The copy of {repo}'s mirror at {target} is not at the mirror's HEAD")
    return target


def collect_environment(env: dict, repo: str, shallow: Path) -> dict:
    """`env` with `repo` cloned from `shallow` rather than from its mirror.

    git applies the longest `insteadOf` that matches a URL, and the repository's own rewrite
    therefore takes precedence over the rewrite that sends all of github.com to the mirrors.
    """
    env = dict(env)
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = f"url.{shallow.as_uri()}.insteadOf"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"https://github.com/{repo}.git"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def finished_records(stage: Path, repo: str) -> int:
    """How many lines of the step's list of finished repositories name `repo`."""
    from agentcontext import resume
    path = stage / resume.FINISHED_NAME
    if not path.is_file():
        return 0
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                if json.loads(line).get("repo") == repo:
                    count += 1
            except (ValueError, AttributeError):
                continue
    return count


def history_outcome(stage: Path, repo: str) -> str | None:
    """The outcome that the cloning step recorded for reading `repo`'s commit history, or None
    when it recorded none."""
    from agentcontext import commits
    from agentcontext.steps import repository_cloning
    path = commits.shard_paths(stage / repository_cloning.COMMITS_DIR_NAME, repo)[1]
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("outcome")
    except (OSError, ValueError, AttributeError):
        return None


def rows_without_history(stage: Path, repo: str) -> tuple[int, int]:
    """(rows, rows with a blank `created_at`) of `repo` over the output CSVs."""
    from agentcontext import heuristics
    rows = blank = 0
    for filename, _ in heuristics.MECHANISM_CSVS.values():
        path = stage / "output" / filename
        if not path.is_file():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("repo_name") == repo:
                    rows += 1
                    blank += not row.get("created_at")
    return rows, blank


def count_rows(path: Path) -> int:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "repo_name" not in reader.fieldnames:
            raise Refused(f"{path} has no repo_name column")
        return sum(1 for _ in reader)


def child_environment(mirrors: Path, checkout: Path) -> dict:
    """The steps' environment: the checkout's own package and data root, every github.com URL
    rewritten to the mirrors, and no GitHub access token.

    The package is installed editable from this repository, and a script in the checkout would
    otherwise import this repository's package and write into this repository's data root.
    `PYTHONPATH` puts the checkout's package first, and `AGENTCONTEXT_DATA_ROOT` names the
    checkout's data root for it. With a GitHub access token, the clone URL names the user
    `x-access-token`, and the rewrite of the prefix `https://github.com/` does not match a URL
    with a user. The function therefore removes `GITHUB_TOKEN` and `GH_TOKEN` rather than
    leaving them for the collection stage to find.
    """
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    checkout = checkout.resolve()
    env["PYTHONPATH"] = str(checkout) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["AGENTCONTEXT_DATA_ROOT"] = str(checkout / "data")
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"url.{mirrors.resolve().as_uri()}/.insteadOf"
    env["GIT_CONFIG_VALUE_0"] = "https://github.com/"
    return env


def package_in(checkout: Path, env: dict) -> None:
    """Refuse unless a script run under `env` imports the checkout's package and stage directory."""
    probe = ("import agentcontext; print(agentcontext.__file__); print(agentcontext.STAGE_DIR)")
    result = subprocess.run([sys.executable, "-c", probe], cwd=str(checkout / "data" / "collection"),
                            env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused(f"The checkout's package does not import ({result.stderr.strip()[-300:]})")
    where, stage = result.stdout.split("\n")[:2]
    checkout = checkout.resolve()
    if not Path(where).resolve().is_relative_to(checkout):
        raise Refused(f"A script in {checkout} imports the package at {where}, not the checkout's own")
    if Path(stage).resolve() != checkout / "data" / "collection":
        raise Refused(f"The package's stage directory is {stage}, not the checkout's")


def reset_checkout(checkout: Path, root: Path = REPO_ROOT) -> str:
    """Discard the last run's files, put the checkout at this repository's commit, return the sha.

    The checkout is disposable and is reset rather than pulled: An amended or rebased commit
    here is what the run is for, and a pull would refuse it.
    """
    here = git("rev-parse", "HEAD", cwd=root)
    git("checkout", "-q", "--", ".", cwd=checkout)
    git("clean", "-xdfq", cwd=checkout)
    git("fetch", "-q", "origin", cwd=checkout)
    git("reset", "-q", "--hard", here, cwd=checkout)
    there = git("rev-parse", "HEAD", cwd=checkout)
    if here != there:
        raise Refused(f"{checkout} is at {there[:8]} after the reset, but this repository is at "
                      f"{here[:8]}")
    return here


def run_step(name: str, argv: list, cwd: Path, env: dict, log_dir: Path,
              kill_after: float | None = None) -> int:
    """Run one step with its output under `log_dir`, killed after `kill_after` seconds if set."""
    return run_step_killed(name, argv, cwd, env, log_dir, kill_after)[0]


def kill_grace_seconds() -> float:
    """How long the killed cloning step gets to exit after SIGTERM before SIGKILL.

    GROUNDING: the step's own stop is bounded by `WORKER_STOP_SECONDS` for its workers plus
    `REAP_SECONDS` for the git that it runs itself, and five seconds more cover the
    interpreter's exit. A shorter grace period would kill a stop that takes its full time and
    count it as a step that did not stop. The constants are imported here rather than at the
    top, because the cloning step raises the csv module's field limit when it is imported, and
    the guard on the run list should read the list under the default limit that a person who
    types the command has.
    """
    from agentcontext.git.bounds import REAP_SECONDS
    from agentcontext.steps.repository_cloning import WORKER_STOP_SECONDS
    return WORKER_STOP_SECONDS + REAP_SECONDS + 5.0


def run_step_killed(name: str, argv: list, cwd: Path, env: dict, log_dir: Path,
                     kill_after: float | None = None,
                     grace: float | None = None) -> tuple[int, bool]:
    """The exit code, and whether this run killed the step.

    The caller logs "killed after N s", and the statement has to be true: A first collection
    that finishes inside the window makes the run two ordinary collections, which differ from a
    stop and a resume and must not be reported as a stop and a resume.
    """
    with (open(log_dir / f"{name}.log", "w", encoding="utf-8") as out,
          open(log_dir / f"{name}.err", "w", encoding="utf-8") as err):
        proc = subprocess.Popen([sys.executable, *argv], cwd=str(cwd), env=env,
                                stdout=out, stderr=err)
        if kill_after is None:
            return proc.wait(), False
        try:
            return proc.wait(timeout=kill_after), False
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGTERM)
            try:
                return proc.wait(timeout=kill_grace_seconds() if grace is None else grace), True
            except subprocess.TimeoutExpired:
                proc.kill()
                return proc.wait(), True


def normalized_findings(report: str, roots: tuple) -> list:
    """The comparison's finding lines with the paths of the two stage directories replaced by
    a placeholder."""
    lines = report.splitlines()[1:]
    out = []
    for line in lines:
        line = line.strip()
        for root in roots:
            line = line.replace(str(root), "<root>")
        if line:
            out.append(line)
    return out


def unexpected(findings: list, expected: list) -> tuple:
    """The findings not in the expected set, and the expected findings that were not found."""
    return ([f for f in findings if f not in expected], [e for e in expected if e not in findings])


def check_reclassification_report(report: dict, history_fails: str | None = None,
                                   unmirrored: frozenset = frozenset(),
                                   run_list: frozenset | None = None) -> None:
    """Require a complete reclassification, or exactly the history failure that the run
    injected. The repositories of the run list without a mirror, and no others, must be
    `unavailable`, and, given the `run_list`, the report must cover exactly its repositories.

    A repository without a mirror cannot be cloned, which is how the run list includes the clone
    outcomes of the subsample: A repository gone from GitHub has no mirror, and its outcome is
    the `unavailable` that a clone failure of a collection gives.
    """
    from agentcontext.steps import commit_reclassification
    try:
        repositories = report["repositories"]
        outcomes = {repo: result["outcome"] for repo, result in repositories.items()}
        complete = history_fails is None
        valid = (
            type(report["requested_repositories"]) is int
            and report["requested_repositories"] == len(outcomes) > 0
            and report["assembly_complete"] is True
            and report["complete"] is complete
            and report["population_complete"] is (complete and not unmirrored)
            and report["counts"] == commit_reclassification.outcome_counts(outcomes.values())
            and all(outcomes.get(repo) == "unavailable" for repo in unmirrored)
            and all(outcome in ("current", "reclassified", "not_applicable")
                    for repo, outcome in outcomes.items()
                    if repo != history_fails and repo not in unmirrored)
            and (complete or outcomes.get(history_fails) == "history_not_read")
            and (run_list is None or set(outcomes) == set(run_list))
        )
    except (KeyError, TypeError, AttributeError, ValueError):
        valid = False
    if not valid:
        raise Refused("The repository outcomes of the reclassification differ from the expected outcomes")


def run_reclassification(name: str, argv: list, stage: Path, env: dict, log_dir: Path,
                         history_fails: str | None = None,
                         unmirrored: frozenset = frozenset(),
                         run_list: frozenset | None = None) -> int:
    """Run the reclassification, verify its exit and coverage, and preserve the coverage report.

    The step exits 0 with the injected history failure too, which is within the cloning step's
    ceiling, as the runner requires to continue to publication.
    """
    report_path = stage / "output" / "commit-reclassification.json"
    report_path.unlink(missing_ok=True)
    code = run_step(name, argv, stage, env, log_dir)
    if code != 0:
        logs.warning("mirror_run", f"The step {name} exited {code}, not 0.")
        return 2
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        check_reclassification_report(report, history_fails, unmirrored, run_list)
        shutil.copy2(report_path, log_dir / (
            "reclassification-incomplete.json" if history_fails else "reclassification-complete.json"))
    except Refused as why:
        logs.warning("mirror_run", f"{str(why).rstrip('.')}.")
        return 2
    except (OSError, ValueError) as why:
        logs.warning("mirror_run", f"Could not read or keep {report_path}, which the step {name} "
                                   f"writes ({why}).")
        return 2
    logs.info("mirror_run", f"The step {name} exited 0 with the expected repository outcomes.")
    return 0


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description='Collect the subsample from local Git mirrors, stop the collection with SIGTERM and resume it, run the steps after the collection, and compare the outputs with a reference run.')
    parser.add_argument("--checkout", required=True, type=Path,
                        help="A separate checkout of this repository to run in")
    parser.add_argument("--repo-csv", required=True, type=Path,
                        help=f"The run list (at most {MAX_ROWS} rows)")
    parser.add_argument("--mirrors", required=True, type=Path,
                        help="The directory of the local Git mirrors, as <owner>/<repo>.git")
    parser.add_argument("--reference", required=True, type=Path,
                        help="The stage directory of the reference run to compare the "
                             "outputs with")
    parser.add_argument("--logs", type=Path, default=None,
                        help="The directory for the output of each step (default: "
                             "<checkout>-logs-<commit> beside the checkout)")
    parser.add_argument("--kill-after", type=float, default=90.0,
                        help="Seconds into the first collection to send SIGTERM (default: 90)")
    parser.add_argument("--workers", type=int, default=3, help="Clone workers (default: 3)")
    parser.add_argument("--history-fails", default=None, metavar="OWNER/REPO",
                        help="The repository with a history fetch that fails on purpose in "
                             "the cloning step (default: the first repository of the run list)")
    # The cloning step's minimum is sized for the full collection. The mirrors of a subsample
    # run are far smaller, and a development machine can be under that minimum before the
    # first clone.
    parser.add_argument("--min-free-gb", type=float, default=None,
                        help="The free space in GB that the cloning step keeps on the scratch "
                             "volume (default: the cloning step's minimum)")
    parser.add_argument("--expected-findings", type=Path, default=None,
                        help="A file of the expected findings of the comparison, one per "
                             "line. The run passes when the comparison finds exactly these "
                             "findings.")
    args = parser.parse_args(argv)
    try:
        checkout_of_this_repository(args.checkout)
        repo_csv = args.repo_csv.resolve()
        if not repo_csv.is_file():
            raise Refused(f"The run list {repo_csv} does not exist")
        try:
            rows = count_rows(repo_csv)
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            # The same failures as the read of the expected findings, for the same reason: a
            # list that is not text, a list that cannot be read, or a field over the csv
            # module's default limit, which would otherwise end the run with a traceback where
            # every other guard in this block logs one line.
            raise Refused(f"Could not read the run list {repo_csv} ({e})")
        if rows > MAX_ROWS:
            raise Refused(f"{repo_csv} has {rows:,} rows, but a subsample run reads at most "
                          f"{MAX_ROWS:,}")
        history_fails = args.history_fails or first_repository(repo_csv)
        if not args.mirrors.is_dir():
            raise Refused(f"The directory of the local Git mirrors {args.mirrors} does not exist")
        unmirrored = repositories_without_a_mirror(args.mirrors, repo_csv)
        run_list = run_list_repositories(repo_csv)
        if history_fails in unmirrored:
            raise Refused(f"{history_fails} has no mirror to make its history fetch fail. Name "
                          f"a repository with a mirror in --history-fails.")
        if not (args.reference / "output").is_dir():
            raise Refused(f"{args.reference} has no output/ directory and is not a stage "
                          f"directory")
        expected = None
        if args.expected_findings is not None:
            # Read here rather than at the end, after the kill, the resume, five stages and
            # the comparison, where a mistyped path would end all of that with a traceback.
            # Read rather than tested for existence, because the next line runs
            # `git clean -xdfq` in the checkout, and a file inside the checkout passes an
            # `is_file()` and is gone by the end. Read at the end, an expected list at
            # `<logs>/findings.txt` would also compare the run with itself, because the run
            # writes its own findings there first, and the run would exit 0 whatever the
            # comparison found.
            try:
                expected = [l.strip() for l in
                            args.expected_findings.read_text(encoding="utf-8").splitlines()
                            if l.strip()]
            except (OSError, UnicodeDecodeError) as e:
                # A decode failure is a `ValueError`, not an `OSError`, and a file that is not
                # text, such as the archive beside the list, would get past an `OSError` handler
                # and end the run with a traceback where every other guard logs one line.
                raise Refused(f"Could not read the file of expected findings "
                              f"{args.expected_findings} ({e})")
        sha = reset_checkout(args.checkout)
    except Refused as e:
        logs.error("mirror_run", f"{str(e).rstrip('.')}.")
        return 1
    stage = args.checkout.resolve() / "data" / "collection"
    # Outside the checkout: The next run's `git clean -xdfq` removes everything in it,
    # including the coverage reports that this run keeps.
    log_dir = (args.logs or args.checkout.resolve().parent
               / f"{args.checkout.resolve().name}-logs-{sha[:8]}").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    # The history columns depend on git's version. The run records the git version that
    # produced them, and a reference that this run replaces keeps that record.
    try:
        version = git("--version", cwd=log_dir)
    except Refused as e:
        logs.error("mirror_run", f"{str(e).rstrip('.')}.")
        return 1
    (log_dir / "git-version.txt").write_text(version + "\n", encoding="utf-8")
    scratch = stage / "_scratch"
    scratch.mkdir(exist_ok=True)
    env = child_environment(args.mirrors, args.checkout)
    try:
        package_in(args.checkout, env)
        # Beside the checkout's tree rather than under the stage directory, and inside the
        # checkout, whose `git clean` above removed the last run's copy.
        shallow = shallow_copy(args.mirrors, history_fails,
                               args.checkout.resolve() / "_mirror_run_shallow")
    except Refused as e:
        logs.error("mirror_run", f"{str(e).rstrip('.')}.")
        return 1
    collect_env = collect_environment(env, history_fails, shallow)
    collection = ["-m", "agentcontext.steps.repository_cloning", "--repo-csv", str(repo_csv), "--clone-dir", str(scratch),
               "-w", str(args.workers), "--progress-every", "5", "--progress-seconds", "30",
               *(["--min-free-gb", f"{args.min_free_gb:g}"] if args.min_free_gb is not None else []),
               "--retry-pause", str(RETRY_PAUSE_SECONDS)]
    logs.info("mirror_run",
              f"Running the pipeline in {args.checkout} at {sha[:8]} with {version} over "
              f"{rows:,} repositories (logs in {log_dir}).")
    logs.info("mirror_run", f"Making the history fetch of {history_fails} fail on purpose.")
    code, killed = run_step_killed("repository_cloning-killed", collection, stage, collect_env, log_dir,
                                   kill_after=args.kill_after)
    logs.info("mirror_run", f"The first collection "
                            f"{'was killed after' if killed else 'finished within'} "
                            f"{args.kill_after:g} s and exited {code}.")
    if not killed and code != 0:
        # A first collection that ended on its own with a non-zero code, such as `LowDiskStop`'s
        # 4, an argument that the step refused, or a traceback, is a failed step, not a window too
        # wide. Advising a longer window there would point at the one thing that cannot be the
        # cause, and the exit code is that of a failed step, not of a refusal.
        logs.warning("mirror_run", f"The first collection exited {code} before the SIGTERM. "
                                   f"See {log_dir / 'repository_cloning-killed.err'}.")
        return 2
    if killed and code != STOPPED_EXIT:
        # The kill is half of what the run tests. A step that responded to SIGTERM with a
        # traceback, with `LowDiskStop`'s 4, or not at all until the grace period ran out and it
        # was killed, did not stop as it should, and the resume after it shows nothing about a
        # stop.
        logs.warning("mirror_run", f"The first collection exited {code} after SIGTERM, not "
                                   f"{STOPPED_EXIT}. See {log_dir / 'repository_cloning-killed.err'}.")
        return 2
    if not killed:
        # The run is a kill and a resume. A first collection that finished inside the window
        # makes it two ordinary runs, which test neither the stop nor the resume, and the run
        # refuses rather than only warning afterwards.
        logs.warning("mirror_run", f"The first collection finished within {args.kill_after:g} s, "
                                   f"and the stop and the resume went untested. Lower "
                                   f"--kill-after, or add repositories to the run list.")
        return 1
    # The other half: The resume has to take over what the killed collection finished. With
    # nothing finished it is a first collection, and a resume that collected a finished
    # repository again took nothing over.
    finished = finished_repositories(stage) - {history_fails}
    if not finished:
        logs.warning("mirror_run", f"No repository finished in the first {args.kill_after:g} s, "
                                   f"before the SIGTERM. The resume had nothing to take over. "
                                   f"Raise --kill-after.")
        return 1
    failed = []
    code = run_step("repository_cloning", collection, stage, collect_env, log_dir)
    logs.info("mirror_run", f"The resumed collection exited {code}.")
    if code != 0:
        failed.append("the exit code of the resumed collection")
    again = sorted(repo for repo in finished if finished_records(stage, repo) != 1)
    logs.info("mirror_run", f"The resume took over {len(finished) - len(again):,} of the "
                            f"{len(finished):,} repositories that the killed collection finished.")
    if again:
        logs.warning("mirror_run", f"The resumed collection cloned {len(again):,} finished "
                                   f"repositories again ({', '.join(again[:5])}"
                                   f"{' and more' if len(again) > 5 else ''}).")
        failed.append("the takeover of the finished repositories")
    # The injected failure has to have happened, and the step has to have tried the
    # repository again, or the run tested neither and would still pass. A retry leaves two lines
    # in the list of finished repositories, the mark before it starts and its record, and a
    # repository tried more than once has more than one line, whichever collection retried it.
    outcome = history_outcome(stage, history_fails)
    retried = finished_records(stage, history_fails) >= 2
    logs.info("mirror_run", f"The cloning step recorded "
                            f"{f'the outcome {outcome}' if outcome else 'no outcome'} for the "
                            f"commit history of {history_fails} and "
                            f"{'tried' if retried else 'did not try'} the repository again.")
    if outcome != "history_unavailable":
        logs.warning("mirror_run", f"The cloning step recorded "
                                   f"{f'the outcome {outcome}' if outcome else 'no outcome'} for "
                                   f"{history_fails}, not history_unavailable from the failed "
                                   f"history fetch.")
        failed.append(f"the history failure of {history_fails}")
    elif not retried:
        logs.warning("mirror_run", f"The cloning step did not try {history_fails} again, in the "
                                   f"resume or in a retry round.")
        failed.append(f"the history failure of {history_fails}")
    if failed:
        return 2
    try:
        extract_inputs(args.checkout.resolve())
    except Refused as e:
        logs.error("mirror_run", f"{str(e).rstrip('.')}.")
        return 1
    # Run the steps after the collection once, in the runner's order, as the container runs
    # them. The runner collects nothing again after an incomplete reclassification.
    for name, argv_ in steps_after_collect(history_fails):
        if name == 'commit_reclassification':
            code = run_reclassification(name, argv_, stage, env, log_dir, history_fails,
                                        unmirrored, run_list)
        else:
            code = run_step(name, argv_, stage, env, log_dir)
        logs.info("mirror_run", f"The step {name} exited {code}.")
        if code != 0:
            return 2
        if name == 'file_history':
            measured, blank = rows_without_history(stage, history_fails)
            logs.info("mirror_run", f"The mechanism CSVs have {measured:,} rows of "
                                    f"{history_fails}, {blank:,} of them without Git columns.")
            if not measured or blank:
                logs.warning("mirror_run", f"The file history step did not fill the blank Git "
                                           f"columns of {history_fails} ({measured:,} row(s), "
                                           f"{blank:,} of them without Git columns).")
                failed.append(f"the Git columns of {history_fails}")
    ignores = [flag for column in IGNORED_COLUMNS for flag in ("--ignore", column)]
    # The findings are read from the file that the comparison writes them to, not from its log:
    # Every line of the log has a level and a component, which a parser of the log would
    # have to strip from every finding.
    report = log_dir / "compare.txt"
    report.unlink(missing_ok=True)
    code = run_step("compare", ["-m", "agentcontext.checks.regressions", "compare",
                                 str(args.reference.resolve()), str(stage), *ignores,
                                 "--report", str(report)],
                     stage, env, log_dir)
    logs.info("mirror_run", f"The comparison with {args.reference} exited {code}."
                            f"{'' if code == 0 else f' See {report}.'}")
    findings = normalized_findings(report.read_text(encoding="utf-8") if report.is_file() else "",
                                   (args.reference.resolve(), stage))
    (log_dir / "findings.txt").write_text("".join(f + "\n" for f in findings), encoding="utf-8")
    if expected is not None:
        extra, missing = unexpected(findings, expected)
        for f in extra:
            logs.info("mirror_run", f"The comparison found a difference that the expected "
                                    f"findings do not list ({f}).")
        for e in missing:
            logs.info("mirror_run", f"The comparison did not find an expected finding ({e}).")
        # Only a comparison that ran gets its verdict replaced. It exits 0 when the two stage
        # directories agree and 2 when they differ. Any other code, 1 or the negative code of a
        # process killed by a signal, means that the comparison did not finish, and overwriting
        # it would make an empty log look like agreement whenever the expected list covered what
        # the comparison had produced.
        if code in (0, 2):
            code = 2 if extra or missing else 0
        logs.info("mirror_run", f"The comparison found {len(findings):,} differences, "
                                f"{len(extra):,} of them not in {args.expected_findings}. "
                                f"{len(missing):,} expected findings were not found.")
    if failed or code != 0:
        logs.warning("mirror_run", (f"The subsample run failed its checks of "
                                    f"{', '.join(failed)}." if failed
                                    else "The subsample run passed its checks.")
                                   + f" The comparison exited {code}.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
