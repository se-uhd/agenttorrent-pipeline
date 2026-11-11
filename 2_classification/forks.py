"""The fork heuristic: whether a repository that its README or description calls a fork of
another project has become a project of its own, decided from GitHub's data.

The sampling frame keeps only repositories that GitHub does not mark as forks. A fork here is
therefore a repository created on its own that, by its README or description, started from
another project. The model names that upstream (`fork_of`). Such a repository is independent
when it does not track its upstream and is developed on its own:

  own authors            at least the `contributorsMin` of the GHS query, bots left out
  own commits in window  at least one since the start of the sampling window

Its own authors are the authors of the commits on its default branch made since GitHub created
it, or of all of its commits when it took over none of its upstream's history. The commits are
counted at the revision that the sampling frame recorded, the same revision at which the
classification read the README. A fork that reaches both thresholds is developed on its own however
few its own commits are, as is jaroslawhartman/withings-sync, "Based on withings-garmin".

A fork tracks its upstream when the upstream is on GitHub and has commits in the sampling window,
the upstream's newest commit from before the window is newer than GitHub's creation of the fork,
and the fork's revision contains that commit. Such a fork keeps merging the upstream, and its
commits since its creation are mostly the upstream's. It is a downstream whatever the number of its
own commits. A fork that contains an upstream commit from before GitHub created the fork may only
have started from that history, as closeio/sync-engine started from the history of
nylas/sync-engine, which made no commit after 2017 until its archive notice. An upstream that is
not on GitHub, that GitHub no longer has or that it blocks cannot be tracked, and neither can an
upstream without commits in the window. For such an upstream, the heuristic decides by the fork's
own development alone. An upstream that GitHub resolves to the repository itself, as after a
rename, means that the repository is the upstream.

forks-spec.md gives the evidence for the thresholds and their sources, and
`evaluation/2026-10-07T021344Z/forks.json` contains the heuristic's results for the forks of that
label evaluation.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from agentcontext.github_api import blocked

REPOSITORY_URL = "https://api.github.com/repos/{name}"
COMMITS_URL = "https://api.github.com/repos/{name}/commits"
COMPARE_URL = "https://api.github.com/repos/{name}/compare/{base}...{head}"
# The commits per page read to find the fork's own authors, the most that GitHub's list of
# commits returns. The authors are counted until the minimum is reached or the commits end. A
# fork with its newest commits from one author is therefore judged by all of its own commits.
AUTHOR_PAGE_SIZE = 100

_LAST_PAGE = re.compile(r'[?&]page=(\d+)>;\s*rel="last"')
_NEXT_PAGE = re.compile(r'<([^>]+)>;\s*rel="next"')
_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
GITHUB_HOSTS = ("github.com", "www.github.com")


class Unexpected(ValueError):
    """A response of GitHub that is neither the data nor the absence that GitHub documents."""


def code_digest(source: str) -> str:
    """The SHA-256 of Python source without its comments, docstrings and layout, so that it
    changes only with what the code does."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return hashlib.sha256(ast.dump(tree).encode("utf-8")).hexdigest()


# The version of the heuristic. The classification records it with each verdict and compares it
# when it reuses a label, and it decides a verdict of an earlier version again from GitHub's data.
HEURISTIC = code_digest(Path(__file__).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Thresholds:
    """The sampling frame's thresholds that a fork's own development has to reach."""

    author_minimum: int
    window_start: str  # the first day of the sampling window, YYYY-MM-DD

    def as_dict(self) -> dict:
        return asdict(self)


def thresholds_of(metadata: dict) -> Thresholds | None:
    """The thresholds that a sampling frame was filtered with: the `contributorsMin` of the GHS
    query and the first day of the window, or None when the sampling frame does not record
    both."""
    source = metadata.get("input_source") or {}
    authors = (source.get("query") or {}).get("contributorsMin")
    start = (source.get("window") or {}).get("committed_min")
    if authors is None or not start:
        return None
    return Thresholds(int(authors), str(start)[:10])


# An upstream that the model names as a repository on GitHub without its link, such as
# openjdk/jdk, apache/iceberg or golang/mock: a GitHub login, which has no dot or underscore,
# then a slash and a repository's name. A host with a path, such as gitlab.com/inkscape or
# example.com/docs, is no such name. A name such as GNU/Linux or OS/2 that has the form is
# resolved like the others, and GitHub's 404 makes it an upstream off GitHub, at the cost of one
# request.
_BARE_REPOSITORY = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")


def github_name(upstream: str) -> str | None:
    """The `owner/repo` of a github.com link to a repository, or of anything under it, or of a
    bare `owner/repo`, which `_upstream` resolves through GitHub as it resolves a link, or None
    for another link, another name or an address that is no URL, such as an address with a
    fullwidth slash in its host, which the model can copy from a README."""
    text = (upstream or "").strip()
    if _BARE_REPOSITORY.match(text):
        text = f"https://github.com/{text}"
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text if text.lower().startswith(GITHUB_HOSTS) else ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.hostname not in GITHUB_HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    owner, name = segments[0], segments[1].removesuffix(".git")
    # A segment of dots alone is no GitHub name, and in a request it would leave the path of
    # the repository: `https://github.com/../user` would query `/user` with the GitHub access
    # token.
    if not (_NAME_PART.match(owner) and _NAME_PART.match(name)
            and owner.strip(".") and name.strip(".")):
        return None
    return f"{owner}/{name}"


def _payload(response) -> list | dict:
    try:
        return response.json()
    except ValueError as error:
        raise Unexpected(f"GitHub returned HTTP {response.status_code} without JSON") from error


def count(client, url: str) -> int:
    """The number of items in one of GitHub's paged lists: the number of the last page when one
    item is requested per page. An empty repository, for which GitHub returns 409, has none."""
    response = client.get(url + ("&" if "?" in url else "?") + "per_page=1")
    if response.status_code == 409:
        return 0
    if response.status_code != 200:
        raise Unexpected(f"GitHub returned HTTP {response.status_code} for {url}")
    last = _LAST_PAGE.search(response.headers.get("Link", ""))
    return int(last.group(1)) if last else len(_payload(response))


def _git_author(item: dict) -> dict:
    """The author that the commit's git data names, which can be null: GitHub's REST schema
    declares it as `nullable-git-user`."""
    return (item.get("commit") or {}).get("author") or {}


def _bot(item: dict) -> bool:
    author, git_author = item.get("author") or {}, _git_author(item)
    names = (author.get("login") or "", git_author.get("name") or "",
             git_author.get("email") or "")
    return author.get("type") == "Bot" or any("[bot]" in name.lower() for name in names)


def _author(item: dict) -> str:
    """The author of a commit: its GitHub account, else the address in its git data, or "" for a
    commit that names neither or that a bot made."""
    if _bot(item):
        return ""
    return ((item.get("author") or {}).get("login")
            or (_git_author(item).get("email") or "").lower())


def _authors(client, name: str, head: str, since: str, minimum: int) -> set[str]:
    """The distinct authors of the commits at `head` since `since`, or of all of them when
    `since` is empty, newest first until `minimum` of them are found or the commits end."""
    url = (f"{COMMITS_URL.format(name=name)}?sha={head}{f'&since={since}' if since else ''}"
           f"&per_page={AUTHOR_PAGE_SIZE}")
    authors: set[str] = set()
    while True:
        response = client.get(url)
        if response.status_code == 409:
            return set()
        if response.status_code != 200:
            raise Unexpected(f"GitHub returned HTTP {response.status_code} for {url}")
        for item in _payload(response):
            # Counted, the commits without an author would add one author, the empty name,
            # which could bring a fork to the author minimum.
            if identity := _author(item):
                authors.add(identity)
                if len(authors) >= minimum:
                    return authors
        following = _NEXT_PAGE.search(response.headers.get("Link", ""))
        if not following:
            return authors
        url = following.group(1)


def own_authors(client, name: str, head: str, since: str, minimum: int) -> int:
    """The number of distinct authors of the commits at `head` since `since`, or of all of them
    when `since` is empty, bots left out, counted until `minimum` is reached or the commits end.
    An author is a commit's GitHub account, else its address, and a commit with neither names no
    author."""
    return len(_authors(client, name, head, since, minimum))


def _contains(client, name: str, sha: str, head: str) -> bool:
    """Whether `head` of `name` has the commit `sha` in its history.

    GitHub compares from the merge base of the two commits to the second, and `head` therefore
    goes first. When `head` contains `sha`, the merge base is `sha` and the comparison is empty.
    In the other order, GitHub compares every commit of the fork since `sha`, and for a fork with
    as many commits as ferrocene/ferrocene, GitHub breaks the connection.
    """
    response = client.get(COMPARE_URL.format(name=name, base=head, head=sha))
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        raise Unexpected(f"GitHub returned HTTP {response.status_code} for the comparison of "
                         f"{head} with {sha}")
    return _payload(response).get("status") in ("behind", "identical")


def oldest_commit(client, name: str, head: str = "") -> dict | None:
    """The oldest commit at `head` of `name`, or on its default branch when `head` is empty, from
    the last page of one commit each, or None for an empty repository."""
    url = f"{COMMITS_URL.format(name=name)}?{f'sha={head}&' if head else ''}per_page=1"
    response = client.get(url)
    if response.status_code == 409:
        return None
    if response.status_code != 200:
        raise Unexpected(f"GitHub returned HTTP {response.status_code} for {url}")
    last = _LAST_PAGE.search(response.headers.get("Link", ""))
    if last:
        response = client.get(f"{url}&page={last.group(1)}")
        if response.status_code != 200:
            raise Unexpected(f"GitHub returned HTTP {response.status_code} for {url}")
    items = _payload(response)
    return items[0] if items else None


def first_commit(client, name: str) -> str | None:
    """The SHA of the oldest commit on the default branch of `name`, or None for an empty
    repository."""
    item = oldest_commit(client, name)
    return item.get("sha") if item else None


def _upstream(client, upstream: str, window: str, repository: dict) -> dict:
    """What GitHub records about the upstream: its name, whether it is the repository itself,
    whether it has commits in the window, and its newest commit from before the window."""
    upstream = github_name(upstream)
    found = {"upstream_repository": None, "upstream_is_this_repository": False,
             "upstream_active": None, "upstream_commit_before_window": None,
             "upstream_commit_before_window_date": None}
    if upstream is None:
        return found
    response = client.get(REPOSITORY_URL.format(name=upstream))
    # GitHub returns 404 for an upstream that it no longer has, 451 for one that it blocks after
    # a DMCA takedown and 403 with the block for one that it blocks for a terms-of-service
    # violation. None of them can be tracked.
    if response.status_code in (404, 451) or blocked(response):
        return found
    if response.status_code != 200:
        raise Unexpected(f"GitHub returned HTTP {response.status_code} for {upstream}")
    body = _payload(response)
    found["upstream_repository"] = body.get("full_name") or upstream
    if body.get("id") == repository.get("id"):
        found["upstream_is_this_repository"] = True
        return found
    commits = COMMITS_URL.format(name=found["upstream_repository"])
    found["upstream_active"] = count(client, f"{commits}?since={window}") > 0
    if found["upstream_active"]:
        before = client.get(f"{commits}?until={window}&per_page=1")
        # Another status is no evidence about the upstream's commits. Read as no commit before the
        # window, it would skip the test whether the fork tracks the upstream.
        if before.status_code not in (200, 409):
            raise Unexpected(f"GitHub returned HTTP {before.status_code} for the commits of "
                             f"{found['upstream_repository']} before the window")
        if before.status_code == 200 and (items := _payload(before)):
            found["upstream_commit_before_window"] = items[0].get("sha")
            found["upstream_commit_before_window_date"] = (
                (items[0].get("commit") or {}).get("committer") or {}).get("date")
    return found


def _later(one: str | None, other: str | None) -> bool:
    """Whether the time `one` is after the time `other`, both as GitHub writes them, such as
    2017-03-08T18:34:10Z. Without `one` it is false. Without `other` it is true, so that a fork
    without a creation time is compared with its upstream."""
    if not one:
        return False
    if not other:
        return True
    return datetime.fromisoformat(one) > datetime.fromisoformat(other)


def independence(client, repository: dict, revision: str, upstream: str,
                 thresholds: Thresholds) -> dict:
    """The counts and the comparison that the heuristic decides on, whether the fork is
    independent, and the reasons when it is not.

    `repository` is GitHub's response for the fork, `GET /repos/{owner}/{repo}`, and `revision` the
    commit that the sampling frame recorded, or empty for the default branch.
    """
    name = repository["full_name"]
    head = revision or repository.get("default_branch") or "HEAD"
    window = f"{thresholds.window_start}T00:00:00Z"
    result = {"upstream": upstream, **_upstream(client, upstream, window, repository),
                "tracks_upstream": None, "shares_history": None,
                "starts_with_own_commit": None, "own_authors": None,
                "own_commits_in_window": None,
                "created_at": repository.get("created_at"), "thresholds": thresholds.as_dict()}
    if result["upstream_is_this_repository"]:
        return {**result, "independent": True, "reasons": []}
    # A fork contains every commit of its upstream that it started from. Only a commit that the
    # upstream made after GitHub created the fork shows that the fork merged the upstream since.
    # closeio/sync-engine, created on GitHub in 2020, contains the newest commit of
    # nylas/sync-engine from before the window, which is from 2017, because it started from that
    # history, and its own development decides as for an upstream that it cannot track.
    if result["upstream_commit_before_window"] and _later(
            result["upstream_commit_before_window_date"], repository.get("created_at")):
        result["tracks_upstream"] = _contains(client, name, result[
            "upstream_commit_before_window"], head)
        if result["tracks_upstream"]:
            return {**result, "independent": False, "reasons": ["tracks its upstream"]}
    commits = f"{COMMITS_URL.format(name=name)}?sha={head}"
    result["own_commits_in_window"] = count(client, f"{commits}&since={window}")
    since = repository["created_at"]
    result["own_authors"] = own_authors(client, name, head, since, thresholds.author_minimum)
    # A fork without its upstream's first commit took over none of its history, and every commit
    # is its own, as for parthenon-hpc-lab/athenapk, a version of Athena++ with commits from
    # before GitHub created it and none of Athena++'s. The requests that check for the upstream's
    # first commit are made only when the authors since the creation are fewer than the minimum.
    if result["own_authors"] < thresholds.author_minimum:
        if result["upstream_repository"]:
            first = first_commit(client, result["upstream_repository"])
            result["shares_history"] = bool(first) and _contains(client, name, first, head)
            own_history = not result["shares_history"]
        # An upstream off GitHub has no first commit to compare with. When one of the authors of
        # a fork's newest commits since its creation also made its oldest commit, the fork
        # started with its own commit and took over none of its upstream's history either.
        # Perlence/PyGuitarPro, a Python port of AlphaTab with its history from before GitHub
        # created its repository, is such a fork. A fork that took over its upstream's history
        # starts with the upstream's commit, as LukeSmithxyz/dwm starts with a commit of dwm's
        # author.
        else:
            oldest = oldest_commit(client, name, head)
            result["starts_with_own_commit"] = own_history = bool(oldest) and bool(
                _author(oldest)) and (_author(oldest) in _authors(
                    client, name, head, since, thresholds.author_minimum))
        if own_history:
            result["own_authors"] = own_authors(client, name, head, "",
                                                thresholds.author_minimum)
    reasons = []
    if result["own_authors"] < thresholds.author_minimum:
        reasons.append(f"{result['own_authors']} own authors, fewer than the sampling frame's "
                       f"minimum of {thresholds.author_minimum}")
    if result["own_commits_in_window"] < 1:
        reasons.append(f"no own commit since {thresholds.window_start}")
    return {**result, "independent": not reasons, "reasons": reasons}
