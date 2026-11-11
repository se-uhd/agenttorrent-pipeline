"""
Tests for forks.py

Strategy:
  - GitHub is a fake session behind the real `readmes.Client`. The fake stores each repository
    as a history of commits, newest first, and responds as GitHub's REST API does to the
    requests that the heuristic makes: a repository, its commits filtered by `sha`, `since` and
    `until` in pages with a Link header, and the comparison of two commits.
  - A fork and its upstream share the commits that the fork took over or merged. Whether the
    fork contains a commit of its upstream therefore follows from the histories rather than
    from a flag.

Requirements: Python 3.14, requests (requirements.txt)
Run with:  python3 -m unittest test_forks -v
"""

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests  # noqa: E402

import forks  # noqa: E402
import readmes  # noqa: E402

from agentcontext.credentials import Credential  # noqa: E402
from agentcontext.github_api import TokenPool  # noqa: E402

THRESHOLDS = forks.Thresholds(author_minimum=2, window_start="2026-03-22")
CREATED = "2025-01-01T00:00:00Z"


def _commit(sha, date, login="dev", kind="User"):
    return {"sha": sha, "commit": {"author": {"name": login, "email": f"{login}@example.org"},
                                   "committer": {"date": date}},
            "author": {"login": login, "type": kind}}


# The commits that a comparison sends before the fake closes the connection, as GitHub closes it
# for a fork as far past its upstream's commit as ferrocene/ferrocene.
COMPARED_COMMITS = 20


class _Response:

    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        return self.body


class _GitHub:
    """Repositories by name, each with an id, a creation time and a history, newest first.
    `renamed` maps an old name to the current name, as GitHub's redirect does."""

    def __init__(self, repositories, renamed=None):
        self.repositories, self.renamed = repositories, renamed or {}
        self.sent = []

    def history(self, repository, head):
        commits = repository["commits"]
        start = next((i for i, c in enumerate(commits) if c["sha"] == head), None)
        if head in ("HEAD", repository.get("default_branch", "main")):
            start = 0
        return [] if start is None else commits[start:]

    def get(self, url, headers=None, timeout=None):
        self.sent.append(url)
        parts = urlsplit(url)
        path = parts.path.removeprefix("/repos/").split("/")
        name = self.renamed.get("/".join(path[:2]), "/".join(path[:2]))
        repository = self.repositories.get(name)
        if repository is None:
            return _Response(404, {"message": "Not Found"})
        if len(path) == 2:
            return _Response(200, {"id": repository["id"], "full_name": name,
                                   "created_at": repository["created_at"],
                                   "default_branch": "main"})
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if path[2] == "compare":
            # As on GitHub, the commits of `head` that `base` lacks are ahead, and the commits
            # of `base` that `head` lacks are behind. The comparison sends the commits ahead,
            # from the merge base to `head`, and closes the connection past COMPARED_COMMITS.
            base, head = path[3].split("...")
            known = {c["sha"] for c in repository["commits"]} | {"HEAD", "main"}
            if base not in known or head not in known:
                return _Response(404, {"message": "Not Found"})
            from_base = [c["sha"] for c in self.history(repository, base)]
            from_head = [c["sha"] for c in self.history(repository, head)]
            ahead = [sha for sha in from_head if sha not in from_base]
            behind = [sha for sha in from_base if sha not in from_head]
            if len(ahead) > COMPARED_COMMITS:
                raise requests.exceptions.ChunkedEncodingError(
                    "Connection broken: IncompleteRead(0 bytes read, 2 more expected)")
            status = ("identical" if not ahead and not behind else "behind" if not ahead
                      else "ahead" if not behind else "diverged")
            return _Response(200, {"status": status, "ahead_by": len(ahead),
                                   "behind_by": len(behind)})
        commits = self.history(repository, query.get("sha", "HEAD"))
        if "since" in query:
            commits = [c for c in commits if c["commit"]["committer"]["date"] >= query["since"]]
        if "until" in query:
            commits = [c for c in commits if c["commit"]["committer"]["date"] <= query["until"]]
        size, page = int(query.get("per_page", 30)), int(query.get("page", 1))
        pages = max(1, -(-len(commits) // size))
        links = []
        base = url.split("&page=")[0]
        if page < pages:
            links.append(f'<{base}&page={page + 1}>; rel="next"')
            links.append(f'<{base}&page={pages}>; rel="last"')
        return _Response(200, commits[(page - 1) * size:page * size],
                         headers={"Link": ", ".join(links)} if links else {})


def _client(session):
    return readmes.Client(TokenPool([Credential("github", "a", "t")]), session=session,
                          sleep=lambda s: None)


def _upstream(*more):
    """An active upstream: two commits before the window, then `more` in it, newest first."""
    return {"id": 1, "created_at": "2020-01-01T00:00:00Z",
            "commits": [*more, _commit("o2", "2026-01-10T00:00:00Z", "up"),
                        _commit("o1", "2024-06-01T00:00:00Z", "up")]}


def _fork(*own, took=("o1",), base=None):
    """A fork created on CREATED with its `own` commits, newest first, over the commits of its
    upstream that it took over."""
    base = base or _upstream()
    taken = [c for c in base["commits"] if c["sha"] in took]
    return {"id": 2, "created_at": CREATED, "commits": [*own, *taken]}


def _own(count, authors=("ana", "bo"), start_month=4, year=2026):
    """`count` commits made after the fork's creation, alternating `authors`, the newest in the
    sampling window."""
    return [_commit(f"f{i}", f"{year}-{start_month:02d}-{28 - i % 27:02d}T00:00:00Z",
                    authors[i % len(authors)]) for i in range(count)]


class TestIndependence(unittest.TestCase):

    def decide(self, repositories, upstream, revision="", renamed=None):
        github = _GitHub(repositories, renamed)
        client = _client(github)
        fork = readmes.repository(client, "f/fork")
        return forks.independence(client, fork, revision, upstream, THRESHOLDS), github

    def test_a_fork_that_contains_its_active_upstreams_commit_tracks_it_and_is_a_downstream(self):
        """ferrocene/ferrocene and clover-moe/lilium-voyager keep merging their upstreams, and
        most of the many commits since their creation are their upstreams' commits. A fork can
        have more commits since the upstream's commit than a comparison of GitHub sends, as
        ferrocene/ferrocene has, and the comparison therefore has to start at the fork's
        revision."""
        upstream = _upstream(_commit("o3", "2026-05-01T00:00:00Z", "up"))
        fork = _fork(*_own(50), took=("o2", "o1"), base=upstream)
        result, _ = self.decide({"up/orig": upstream, "f/fork": fork},
                                 "https://github.com/up/orig")
        self.assertEqual((result["tracks_upstream"], result["independent"], result["reasons"]),
                         (True, False, ["tracks its upstream"]))
        self.assertEqual(result["upstream_commit_before_window"], "o2")

    def test_a_fork_that_only_started_from_its_upstreams_history_does_not_track_it(self):
        """closeio/sync-engine, "a fork of the Nylas sync-engine project", was created on GitHub in
        2020 and contains the newest commit of nylas/sync-engine from before the window, which is
        from 2017, because it started from that history. Nylas's only commit in the window is its
        archive notice. A fork tracks its upstream only when it contains a commit that the
        upstream made after GitHub created the fork, and otherwise its own development decides,
        so the comparison is not requested. openshift/sriov-network-device-plugin, created in
        2019, contains the newest commit of k8snetworkplumbingwg from before the window, made in
        2026, and tracks its upstream."""
        nylas = {"id": 1, "created_at": "2014-01-01T00:00:00Z",
                 "commits": [_commit("n3", "2026-06-25T18:21:11Z", "nylas"),
                             _commit("n2", "2017-03-08T18:34:10Z", "nylas"),
                             _commit("n1", "2016-01-01T00:00:00Z", "nylas")]}
        closeio = {"id": 2, "created_at": "2020-07-09T17:19:42Z",
                   "commits": [*_own(6), *[c for c in nylas["commits"] if c["sha"] != "n3"]]}
        result, github = self.decide({"up/orig": nylas, "f/fork": closeio},
                                      "https://github.com/up/orig")
        self.assertEqual((result["upstream_active"], result["upstream_commit_before_window"],
                          result["upstream_commit_before_window_date"], result["tracks_upstream"],
                          result["independent"]),
                         (True, "n2", "2017-03-08T18:34:10Z", None, True))
        self.assertFalse(any("/compare/" in url for url in github.sent))
        upstream = _upstream(_commit("o3", "2026-05-01T00:00:00Z", "up"))
        downstream = {"id": 2, "created_at": "2019-01-14T22:33:39Z",
                      "commits": [*_own(4), *[c for c in upstream["commits"] if c["sha"] != "o3"]]}
        result, _ = self.decide({"up/orig": upstream, "f/fork": downstream},
                                 "https://github.com/up/orig")
        self.assertEqual((result["tracks_upstream"], result["independent"]), (True, False))

    def test_an_upstream_named_as_owner_and_repository_is_resolved_through_github(self):
        """The model names openjdk/jdk without a link as the upstream of openjdk/lilliput, a
        repository of an OpenJDK project that has the JDK's README and keeps merging the JDK. The
        heuristic resolves the name through GitHub, as it resolves a link, and finds that the
        fork tracks its upstream. A name that GitHub does not have is an upstream off GitHub,
        and the fork's own development decides."""
        upstream = _upstream(_commit("o3", "2026-05-01T00:00:00Z", "up"))
        downstream = _fork(*_own(30), took=("o2", "o1"), base=upstream)
        result, github = self.decide({"up/orig": upstream, "f/fork": downstream}, "up/orig")
        self.assertEqual((result["upstream_repository"], result["tracks_upstream"],
                          result["independent"]), ("up/orig", True, False))
        result, github = self.decide({"f/fork": _fork(*_own(4))}, "gone/orig")
        self.assertEqual((result["upstream_repository"], result["tracks_upstream"],
                          result["independent"]), (None, None, True))
        self.assertIn("https://api.github.com/repos/gone/orig", github.sent)

    def test_a_failed_request_for_the_upstreams_commit_before_the_window_is_githubs_failure(
            self):
        """A status other than 200 for the upstream's last commit before the window says nothing
        about the upstream's commits. Read as no such commit, the status would skip the test
        whether the fork tracks the upstream, and a downstream would be recorded as
        independent."""
        upstream = _upstream(_commit("o3", "2026-05-01T00:00:00Z", "up"))
        fork = _fork(*_own(50), took=("o2", "o1"), base=upstream)

        class Refusing(_GitHub):
            def get(self, url, headers=None, timeout=None):
                if "until=" in url:
                    self.sent.append(url)
                    return _Response(422, {"message": "Validation Failed"})
                return super().get(url, headers, timeout)

        github = Refusing({"up/orig": upstream, "f/fork": fork})
        client = _client(github)
        with self.assertRaises(forks.Unexpected):
            forks.independence(client, readmes.repository(client, "f/fork"), "",
                               "https://github.com/up/orig", THRESHOLDS)

    def test_a_fork_that_diverged_with_its_own_development_is_independent(self):
        upstream = _upstream(_commit("o3", "2026-05-01T00:00:00Z", "up"))
        result, _ = self.decide({"up/orig": upstream, "f/fork": _fork(*_own(4), base=upstream)},
                                 "https://github.com/up/orig")
        self.assertEqual({k: result[k] for k in ("tracks_upstream", "own_authors",
                                                 "own_commits_in_window", "independent")},
                         {"tracks_upstream": False, "own_authors": 2,
                          "own_commits_in_window": 4, "independent": True})

    def test_a_fork_with_few_own_commits_and_its_own_development_is_independent(self):
        """jaroslawhartman/withings-sync is "Based on withings-garmin", does not merge it and
        has two authors and commits in the window, but fewer commits since its creation than
        the sampling frame requires of a repository. A fork with independent development is a
        project of its own however few its own commits are."""
        result, _ = self.decide({"up/orig": _upstream(), "f/fork": _fork(*_own(2))},
                                 "https://github.com/up/orig")
        self.assertEqual((result["own_authors"], result["own_commits_in_window"],
                          result["independent"], result["reasons"]), (2, 2, True, []))

    def test_one_author_besides_bots_is_too_few(self):
        own = [*_own(4, authors=("ana",)), _commit("b1", "2026-05-02T00:00:00Z",
                                                    "dependabot[bot]", "Bot")]
        result, _ = self.decide({"up/orig": _upstream(), "f/fork": _fork(*own)},
                                 "https://github.com/up/orig")
        self.assertEqual((result["own_authors"], result["independent"]), (1, False))
        self.assertIn("1 own authors, fewer than the sampling frame's minimum of 2",
                      result["reasons"])

    def test_the_own_authors_are_counted_past_a_thousand_commits_of_one_author(self):
        """psi-probe/psi-probe has 9,460 own commits, and its newest 1,000 commits have one
        author. Its second author is further back, and the fork is judged by all of its own
        commits."""
        older = [_commit(f"b{i}", "2026-01-10T00:00:00Z", "bo") for i in range(5)]
        result, _ = self.decide({"f/fork": _fork(*_own(1050, authors=("ana",)), *older,
                                                 took=())}, "Lambda Probe")
        self.assertEqual((result["own_authors"], result["independent"]), (2, True))

    def test_a_commit_without_its_git_author_counts_by_its_github_account(self):
        """GitHub's REST schema declares the author in a commit's git data as
        `nullable-git-user`, so a page of commits can contain a commit with a null author
        there. Such a commit counts by its GitHub account."""
        unparsed = {"sha": "n1", "author": {"login": "bo", "type": "User"},
                    "commit": {"author": None, "committer": {"date": "2026-05-03T00:00:00Z"}}}
        result, _ = self.decide({"up/orig": _upstream(),
                                  "f/fork": _fork(unparsed, *_own(3, authors=("ana",)))},
                                 "https://github.com/up/orig")
        self.assertEqual((result["own_authors"], result["independent"]), (2, True))

    def test_a_commit_that_names_neither_an_account_nor_an_address_counts_no_author(self):
        """A commit without a GitHub account and without its git author, or without its git
        data, names nobody. Counted as an author, such a commit would bring a fork with one
        author to the minimum of two."""
        anonymous = {"sha": "n2", "author": None,
                     "commit": {"author": None, "committer": {"date": "2026-05-03T00:00:00Z"}}}
        result, _ = self.decide({"up/orig": _upstream(),
                                  "f/fork": _fork(anonymous, *_own(3, authors=("ana",)))},
                                 "https://github.com/up/orig")
        self.assertEqual((result["own_authors"], result["independent"]), (1, False))
        self.assertEqual(result["reasons"],
                         ["1 own authors, fewer than the sampling frame's minimum of 2"])
        bare = {"sha": "n3", "author": None, "commit": None}
        client = _client(_GitHub({"f/fork": _fork(bare, anonymous, *_own(3, authors=("ana",)),
                                                  took=())}))
        self.assertEqual(forks.own_authors(client, "f/fork", "HEAD", "", 2), 1)

    def test_no_own_commit_in_the_window_is_not_a_project_of_its_own(self):
        result, _ = self.decide({"up/orig": _upstream(),
                                  "f/fork": _fork(*_own(4, start_month=6, year=2025))},
                                 "https://github.com/up/orig")
        self.assertEqual((result["own_authors"], result["own_commits_in_window"]), (2, 0))
        self.assertEqual(result["reasons"], ["no own commit since 2026-03-22"])

    def test_an_upstream_that_stopped_cannot_be_tracked_and_the_forks_own_development_decides(self):
        """samfundev/KtaneTwitchPlays contains the last commit of its upstream, which stopped in
        2018. Containing the last commit of an upstream without new commits is no sign of
        tracking that upstream."""
        result, github = self.decide({"up/orig": _upstream(),
                                       "f/fork": _fork(*_own(4), took=("o2", "o1"))},
                                      "https://github.com/up/orig")
        self.assertEqual((result["upstream_active"], result["tracks_upstream"],
                          result["independent"]), (False, None, True))
        self.assertFalse(any("/compare/" in url for url in github.sent))

    def test_a_fork_that_shares_no_history_with_its_upstream_counts_every_author(self):
        """parthenon-hpc-lab/athenapk calls itself a version of Athena++, contains none of
        Athena++'s commits, and has commits from before GitHub created its repository. All of
        its commits are therefore its own, and so are their authors. A fork that contains its
        upstream's first commit counts only the authors of its commits since its creation. For
        a fork with enough authors since its creation, no request checks the upstream's first
        commit."""
        before = [_commit(f"p{i}", "2024-03-01T00:00:00Z", "bo") for i in range(3)]
        result, _ = self.decide({"up/orig": _upstream(),
                                  "f/fork": {"id": 2, "created_at": CREATED,
                                             "commits": [*_own(1), *before]}},
                                 "https://github.com/up/orig")
        self.assertEqual({k: result[k] for k in ("shares_history", "own_authors",
                                                 "independent")},
                         {"shares_history": False, "own_authors": 2, "independent": True})
        result, _ = self.decide({"up/orig": _upstream(),
                                  "f/fork": {"id": 2, "created_at": CREATED,
                                             "commits": [*_own(1), *before,
                                                         *_upstream()["commits"][-1:]]}},
                                 "https://github.com/up/orig")
        self.assertEqual((result["shares_history"], result["own_authors"], result["independent"]),
                         (True, 1, False))
        result, github = self.decide({"up/orig": _upstream(), "f/fork": _fork(*_own(4))},
                                      "https://github.com/up/orig")
        self.assertEqual((result["shares_history"], result["independent"]), (None, True))
        self.assertFalse(any("/compare/" in url or url.endswith("/up/orig/commits?per_page=1")
                             for url in github.sent))

    def test_a_fork_of_an_upstream_off_github_that_starts_with_its_own_commit_counts_every_author(
            self):
        """Perlence/PyGuitarPro, "a Python port of AlphaTab", has its history from before GitHub
        created its repository, and its oldest commit is by its own author. A fork that took
        over its upstream's history starts with the upstream's commit, as LukeSmithxyz/dwm
        starts with dwm's, and counts only the authors of its commits since its creation."""
        since = [_commit("s1", "2026-04-28T00:00:00Z", "ana"),
                 _commit("s2", "2025-05-01T00:00:00Z", "ana")]
        for oldest, expected in (("ana", (True, 2, True)), ("up", (False, 1, False))):
            before = [_commit(f"p{i}", "2024-03-01T00:00:00Z", "bo") for i in range(2)]
            before.append(_commit("p2", "2023-01-01T00:00:00Z", oldest))
            for upstream in ("https://www.alphatab.net/", "AlphaTab"):
                with self.subTest(oldest=oldest, upstream=upstream):
                    result, _ = self.decide({"f/fork": {"id": 2, "created_at": CREATED,
                                                        "commits": [*since, *before]}}, upstream)
                    self.assertEqual((result["starts_with_own_commit"], result["own_authors"],
                                      result["independent"]), expected)

    def test_an_upstream_not_named_as_a_repository_on_github_leaves_the_forks_own_development_to_decide(
            self):
        """MirageNet/Mirage names "Mirror" and kivymd/KivyMD a GitLab project."""
        for upstream in ("Mirror", "https://gitlab.com/kivymd/KivyMD", "https://github.com/up",
                         "github.com\uff0fup\uff0fa"):
            with self.subTest(upstream=upstream):
                result, github = self.decide({"f/fork": _fork(*_own(4))}, upstream)
                self.assertEqual((result["upstream_repository"], result["tracks_upstream"],
                                  result["independent"]), (None, None, True))
                self.assertFalse(any("/up/" in url for url in github.sent))

    def test_an_upstream_github_no_longer_has_leaves_the_decision_to_the_forks_own_development(self):
        """GitHub returns 404 for an upstream that it no longer has, 451 for an upstream that it
        blocks after a DMCA takedown, and 403 with the block for an upstream that it blocks for a
        terms-of-service violation. If the heuristic failed for a blocked upstream, the fork
        would be left without a decision in every run, and the classification would pay the
        model for the fork's README again each month."""
        class Blocking(_GitHub):
            def get(self, url, headers=None, timeout=None):
                for owner, response in blocks.items():
                    if f"/repos/{owner}/orig" in url:
                        self.sent.append(url)
                        return response
                return super().get(url, headers, timeout)
        blocks = {"dmca": _Response(451, {"message": "Repository access blocked",
                                          "block": {"reason": "dmca"}}),
                  "tos": _Response(403, {"message": "Repository access blocked",
                                         "block": {"reason": "tos",
                                                   "created_at": "2026-01-05T00:00:00Z"}})}
        for owner in ("gone", "dmca", "tos"):
            with self.subTest(owner=owner):
                client = _client(Blocking({"f/fork": _fork(*_own(2))}))
                result = forks.independence(client, readmes.repository(client, "f/fork"), "",
                                            f"https://github.com/{owner}/orig", THRESHOLDS)
                self.assertEqual((result["upstream_repository"], result["own_authors"],
                                  result["independent"]), (None, 2, True))

    def test_an_upstream_that_is_the_repository_itself_is_no_fork(self):
        """calcom/cal.diy is calcom/cal.com under a new name, and GitHub's redirect resolves the
        old name to calcom/cal.diy."""
        result, _ = self.decide({"f/fork": _fork(*_own(1))}, "https://github.com/f/old-name",
                                 renamed={"f/old-name": "f/fork"})
        self.assertEqual((result["upstream_is_this_repository"], result["independent"]),
                         (True, True))

    def test_the_commits_are_counted_at_the_sampling_frames_revision(self):
        own = _own(5)
        result, _ = self.decide({"up/orig": _upstream(), "f/fork": _fork(*own)},
                                 "https://github.com/up/orig", revision=own[3]["sha"])
        self.assertEqual(result["own_commits_in_window"], 2)

    def test_the_own_commits_are_counted_across_pages(self):
        result, github = self.decide({"f/fork": _fork(*_own(250))}, "Mirror")
        self.assertEqual(result["own_commits_in_window"], 250)
        self.assertTrue(any("per_page=1" in url for url in github.sent))

    def test_an_undocumented_github_response_fails(self):
        class Refusing(_GitHub):
            def get(self, url, headers=None, timeout=None):
                if "/commits" in url:
                    self.sent.append(url)
                    return _Response(500, {"message": "Server Error"})
                return super().get(url, headers, timeout)
        github = Refusing({"f/fork": _fork(*_own(4))})
        client = _client(github)
        with self.assertRaises((forks.Unexpected, readmes.NoResponse)):
            forks.independence(client, readmes.repository(client, "f/fork"), "", "Mirror",
                               THRESHOLDS)


class TestPieces(unittest.TestCase):

    def test_the_heuristics_version_follows_its_code_and_not_its_comments(self):
        """The classification queries GitHub again for a fork's verdict when the version differs,
        at a cost of requests for every fork. A comment or a docstring therefore does not change
        the version."""
        source = Path(forks.__file__).read_text(encoding="utf-8")
        reworded = source.replace('"""The fork heuristic:', '"""The heuristic for forks:', 1)
        commented = source.replace("\ndef independence(", "\n# A comment.\ndef independence(", 1)
        changed = source.replace("if result[\"own_commits_in_window\"] < 1:",
                                 "if result[\"own_commits_in_window\"] < 2:", 1)
        self.assertEqual(len({reworded, commented, changed, source}), 4)
        self.assertEqual(forks.code_digest(reworded), forks.code_digest(source))
        self.assertEqual(forks.code_digest(commented), forks.code_digest(source))
        self.assertNotEqual(forks.code_digest(changed), forks.code_digest(source))

    def test_the_thresholds_come_from_the_sampling_frames_record(self):
        metadata = {"input_source": {"query": {"contributorsMin": "2"},
                                     "window": {"committed_min": "2026-03-22"}},
                    "stages": [{"key": "licenses", "values": {}},
                               {"key": "activity", "values": {"commits_min": 355.0}}]}
        self.assertEqual(forks.thresholds_of(metadata), forks.Thresholds(2, "2026-03-22"))
        del metadata["input_source"]["window"]
        self.assertIsNone(forks.thresholds_of(metadata))

    def test_a_github_link_names_its_repository(self):
        cases = {"https://github.com/yiisoft/yii2-elasticsearch": "yiisoft/yii2-elasticsearch",
                 "github.com/aws/aws-sdk-js.git": "aws/aws-sdk-js",
                 "https://www.github.com/rust-lang/rust/tree/master/src": "rust-lang/rust",
                 "https://github.com/tymondesigns/jwt-auth/wiki": "tymondesigns/jwt-auth",
                 "https://github.com/yiisoft": None, "https://cal.com": None,
                 "https://bitbucket.org/ckolivas/ckpool": None, "Wick Editor": None, "": None,
                 # A segment of dots would leave the repository's path in a request.
                 "https://github.com/../user": None, "https://github.com/o/..": None,
                 "https://github.com/./o": None, "https://github.com/o/..git": None,
                 "https://github.com/o/.github": "o/.github",
                 # Strings that are no URL: a fullwidth slash in the host and an unbalanced
                 # bracket.
                 "github.com\uff0fup\uff0fa": None, "https://github.com[/a/b": None,
                 # A repository on GitHub named without its link, as the model names openjdk/jdk.
                 "openjdk/jdk": "openjdk/jdk", "golang/mock": "golang/mock", "../user": None,
                 "a/b/c": None, "Lambda Probe": None,
                 # A host with a path is no repository on GitHub. A GitHub login has no dot.
                 "gitlab.com/inkscape": None, "example.com/docs": None, "github.com/owner": None,
                 "my.org/tool": None, "o/.github": "o/.github"}
        for link, name in cases.items():
            with self.subTest(link=link):
                self.assertEqual(forks.github_name(link), name)


if __name__ == "__main__":
    unittest.main()
