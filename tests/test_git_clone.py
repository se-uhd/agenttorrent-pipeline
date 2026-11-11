"""Tests for agentcontext.git.clone: the clone URL, the retry and how it classifies git's
errors, the GitHub access token in a message, the HEAD readers, the submodule rows, and that a
clone checks no submodule out. Every clone is of a repository built with `git init` and reached
through a `file://` redirect, and an ambient GITHUB_TOKEN is therefore removed for the run.

Requirements: Python 3.14, git on PATH (stdlib only)
Run with:  python3 -m unittest tests.test_git_clone -v
"""

import errno
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._fixtures import (_add_commit, _add_noise, _init_git_repo, ext_redirect,
                             git_version_fails, github_on_loopback, sampled_every, upload_pack)
from agentcontext.git import clone as _clone
from agentcontext.git.clone import (
    build_clone_url,
    clone_with_retry,
    is_filter_unsupported,
    read_snapshot_metadata,
    is_permanent_failure,
    permanent_failure_reason,
    redact_token,
    submodule_rows,
)
from agentcontext.paths import collect_repo
from agentcontext import collector as _mod
from tests import _ambient_env


_saved_credentials = {}


def setUpModule():
    # An ambient GITHUB_TOKEN changes the clone URL that these tests redirect, and the result of
    # the suite would then depend on the machine rather than on the code. _ambient_env names the
    # variables and the reason.
    _ambient_env.take(_saved_credentials)


def tearDownModule():
    _ambient_env.restore(_saved_credentials)


def _git(repo: Path, *args: str) -> str:
    """What git itself prints, the expectation of the tests below."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class TestReadSnapshotMetadata(unittest.TestCase):
    """The branch, the committer date and the commit of a clone's HEAD, which identify the
    snapshot. A repository without a commit is three blanks, and a directory that git cannot
    read raises rather than returning a guess."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        _init_git_repo(self.repo)

    def test_the_branch_the_date_and_the_commit_of_head(self):
        _add_commit(self.repo, "second.md", "# second")
        self.assertEqual(read_snapshot_metadata(self.repo),
                         (_git(self.repo, "branch", "--show-current"),
                          _git(self.repo, "show", "-s", "--format=%cI", "HEAD"),
                          _git(self.repo, "rev-parse", "HEAD")))

    def test_a_tag_with_the_branchs_name_leaves_the_branch_its_name(self):
        """`rev-parse --abbrev-ref HEAD` prints `heads/main` when a tag has the branch's
        name."""
        branch = _git(self.repo, "branch", "--show-current")
        subprocess.run(["git", "-C", str(self.repo), "tag", branch], check=True)
        self.assertEqual(read_snapshot_metadata(self.repo)[0], branch)

    def test_a_tracked_file_named_head_does_not_hide_the_commit(self):
        """`git show HEAD` without `--` is ambiguous when the tree contains a file named `HEAD`,
        as protoman/rockbot's does, and exits 128."""
        _add_commit(self.repo, "HEAD", "not a ref\n")
        self.assertEqual(read_snapshot_metadata(self.repo)[1:],
                         (_git(self.repo, "show", "-s", "--format=%cI", "HEAD", "--"),
                          _git(self.repo, "rev-parse", "HEAD")))

    def test_a_repository_without_commits_is_three_blanks(self):
        empty = self.tmp / "empty"
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        self.assertEqual(read_snapshot_metadata(empty), ("", "", ""))

    def test_a_directory_git_cannot_read_raises(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(RuntimeError, "Could not read the branch"):
            read_snapshot_metadata(plain)


class TestTheVersionOfGit(unittest.TestCase):
    """`git_version`, which every line of the list of finished repositories records. A fork
    can fail for a moment on the NAS, and "" in place of the version would make the cloning
    step collect every repository again."""

    def test_a_git_that_fails_to_start_once_gives_the_version(self):
        version = _clone.git_version()
        self.assertTrue(version.startswith("git version "))
        with git_version_fails(times=1) as failures:
            self.assertEqual(_clone.git_version(), version)
        self.assertEqual(len(failures), 1)

    def test_a_git_that_never_starts_raises_after_every_try(self):
        with git_version_fails() as failures, \
                self.assertRaises(_clone.GitVersionUnavailable) as caught:
            _clone.git_version()
        self.assertEqual(len(failures), _clone.GIT_VERSION_TRIES)
        self.assertIn("BlockingIOError", str(caught.exception))

    def test_a_git_that_prints_nothing_raises(self):
        empty = subprocess.CompletedProcess(["git", "--version"], 0, "", "")
        with mock.patch.object(_clone, "GIT_VERSION_WAIT", 0), \
                mock.patch("subprocess.run", return_value=empty), \
                self.assertRaises(_clone.GitVersionUnavailable):
            _clone.git_version()


class TestRemoteHead(unittest.TestCase):
    """remote_head queries the branch and the commit of a repository's HEAD without a clone,
    through the URL and the credential helper that a clone uses, and reads them as
    read_snapshot_metadata reads a clone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        self.saved = {k: v for k, v in os.environ.items() if k.startswith("GIT_CONFIG_")}

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:
            os.environ.pop(k)
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect(self, target: str) -> None:
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.{target}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/o/a.git"

    def test_the_branch_and_the_commit_of_head(self):
        _init_git_repo(self.remote)
        self._redirect(f"file://{self.remote}")
        self.assertEqual(_clone.remote_head("o/a"),
                         (_git(self.remote, "branch", "--show-current"),
                          _git(self.remote, "rev-parse", "HEAD")))

    def test_a_repository_without_a_commit_is_two_blanks(self):
        """As read_snapshot_metadata reads a clone of one, so that the two compare equal."""
        subprocess.run(["git", "init", "--bare", str(self.remote)], capture_output=True, check=True)
        self._redirect(f"file://{self.remote}")
        self.assertEqual(_clone.remote_head("o/a"), ("", ""))

    def test_a_repository_that_is_not_there_is_the_reason_of_the_refusal(self):
        """git's own message for a remote path with no repository behind it, which the local Git
        mirrors of the subsample run give for a repository for which GitHub returns Repository
        not found."""
        self._redirect(f"file://{self.tmp / 'gone'}")
        self.assertEqual(_clone.remote_head("o/a"), _clone.NOT_FOUND_OR_NO_ACCESS)

    def test_a_network_that_does_not_respond_is_no_response(self):
        """A port that nothing listens on gives no response, not a repository that is gone,
        because the cloning step clones every repository with a head that it cannot query."""
        self._redirect("http://127.0.0.1:9/nowhere")
        self.assertIsNone(_clone.remote_head("o/a"))


class TestTheHeadQuerySendsTheGitHubAccessToken(unittest.TestCase):
    """With `authenticate` and a GitHub access token, the head query sends the GitHub access
    token with its first request, rather than after a 401 that GitHub does not send for a public
    repository, so that GitHub counts the query as authenticated. A loopback server in place of
    github.com records the credential of every request."""

    TOKEN = "ghp_notarealtoken0000000000000000000000"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_git_repo(self.tmp / "source")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.tmp / "source"),
                        str(self.tmp / "o" / "a.git")], capture_output=True, check=True)
        self.head = (_git(self.tmp / "source", "branch", "--show-current"),
                     _git(self.tmp / "source", "rev-parse", "HEAD"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_first_request_sends_the_github_access_token(self):
        with github_on_loopback(self.tmp) as server:
            self.assertEqual(_clone.remote_head("o/a", self.TOKEN, authenticate=True), self.head)
        self.assertEqual(server.requests[0], ("x-access-token", self.TOKEN))

    def test_without_a_github_access_token_no_request_sends_a_credential(self):
        """`http.proactiveAuth` without the askpass helper makes git prompt for a username, and
        with prompts off it ends before its first request with a message that reads as a
        repository that is gone."""
        with github_on_loopback(self.tmp) as server:
            self.assertEqual(_clone.remote_head("o/a", None, authenticate=True), self.head)
        self.assertEqual(set(server.requests), {None})

    def test_a_refused_github_access_token_is_no_response_rather_than_a_repository_that_is_gone(
            self):
        """GitHub refuses a GitHub access token that has expired or been revoked, also for a
        public repository, and git's message is the one that a clone without access gets."""
        with github_on_loopback(self.tmp, refuse="token"):
            self.assertIsNone(_clone.remote_head("o/a", self.TOKEN, authenticate=True))

    def test_no_credential_helper_keeps_the_github_access_token(self):
        """git approves a credential after each request that succeeds, and a configured helper,
        such as osxkeychain or store, would keep the GitHub access token."""
        store = self.tmp / "credentials.store"
        with github_on_loopback(self.tmp):
            Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(
                f"[credential]\n\thelper = store --file={store}\n")
            self.assertEqual(_clone.remote_head("o/a", self.TOKEN, authenticate=True), self.head)
        self.assertNotIn(self.TOKEN, store.read_text() if store.exists() else "")


class TestARefusedGitHubAccessToken(unittest.TestCase):
    """GitHub may require authentication for the clone of a public repository: "During periods
    of unusual traffic, GitHub may temporarily require authentication for some clones and
    fetches of public repositories over HTTPS" (changelog of 2026-10-19). git then sends the
    GitHub access token, and a GitHub access token that GitHub refuses fails the clone of a
    repository that exists. A loopback server in place of github.com requires a credential and
    refuses it."""

    TOKEN = "ghp_notarealtoken0000000000000000000000"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_git_repo(self.tmp / "source")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.tmp / "source"),
                        str(self.tmp / "o" / "a.git")], capture_output=True, check=True)
        self.dest, self.log = self.tmp / "clone", self.tmp / "logs" / "o_a.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_clone_fails_on_the_network_after_one_attempt(self):
        """`clone_failed` keeps a repository's earlier collection, counts toward
        --max-clone-failures and the failures in a row, and a resumed run clones it again,
        where a refusal of the repository would record it as gone for the collection."""
        with github_on_loopback(self.tmp, "anonymous and token"):
            status = clone_with_retry("o/a", self.dest, self.log, token=self.TOKEN,
                                      max_tries=3, base_delay=0)
        self.assertEqual(status, "clone_failed")
        log = self.log.read_text()
        self.assertIn("CREDENTIAL_REFUSED", log)
        self.assertNotIn("attempt 2/3", log)
        self.assertNotIn(self.TOKEN, log)

    def test_without_the_requirement_the_clone_stays_anonymous(self):
        with github_on_loopback(self.tmp, "token") as server:
            status = clone_with_retry("o/a", self.dest, self.log, token=self.TOKEN,
                                      max_tries=1, base_delay=0)
        self.assertEqual(status, "ok")
        self.assertEqual(set(server.requests), {None})

    def test_the_anonymous_head_query_gets_no_response_rather_than_a_refusal(self):
        """The head check's second query is anonymous and sends the GitHub access token after
        GitHub's 401. Read as a refusal of the repository, it would make the step collect the
        repository again, and its earlier collection would be at stake."""
        with github_on_loopback(self.tmp, "anonymous and token"):
            self.assertIsNone(_clone.remote_head("o/a", self.TOKEN))


class TestANameTooLongForTheFileSystem(unittest.TestCase):
    """A path component longer than the file system allows, 255 bytes on ext4, btrfs and APFS,
    makes git end with "Clone succeeded, but checkout failed" on every attempt, at every Start."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        bare = self.tmp / "o" / "long.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

        def plumbing(*args: str, text: str = "") -> str:
            return subprocess.run(["git", "-C", str(bare), *args], input=text,
                                  capture_output=True, text=True, check=True).stdout.strip()

        blob = plumbing("hash-object", "-w", "--stdin", text="# Rules\n")
        tree = plumbing("mktree", text=f"100644 blob {blob}\t{'a' * 300}.md\n")
        commit = plumbing("-c", "user.name=t", "-c", "user.email=t@example.com",
                          "commit-tree", tree, "-m", "initial")
        plumbing("update-ref", "refs/heads/main", commit)
        plumbing("symbolic-ref", "HEAD", "refs/heads/main")
        self.dest, self.log = self.tmp / "clone", self.tmp / "logs" / "o_long.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_clone_is_recorded_after_one_attempt(self):
        with github_on_loopback(self.tmp):
            status = clone_with_retry("o/long", self.dest, self.log, max_tries=3, base_delay=0)
        self.assertEqual(status, _clone.FILE_NAME_TOO_LONG)
        log = self.log.read_text()
        self.assertIn("File name too long", log)
        self.assertNotIn("attempt 2/3", log)
        self.assertFalse(self.dest.exists())


class TestEveryRevisionEndsBeforePaths(unittest.TestCase):
    """A git command that takes revisions and paths reads a name as either, and a repository
    that tracks a file of that name makes it ambiguous: with a root file named `HEAD`, as
    protoman/rockbot tracks, `git show -s HEAD` exits 128. Every such command that the package
    builds ends its revisions with `--`. `rev-parse` and `ls-tree` read `HEAD` as a revision
    also in such a repository."""

    TAKES_PATHS = {"log", "show", "rev-list", "diff", "whatchanged", "blame", "shortlog",
                   "checkout"}

    @classmethod
    def offenders(cls, tree) -> list:
        """The lines of `tree` that build such a command with a revision and no `--`."""
        import ast
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            values = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
            command = next((i for i, v in enumerate(values) if v in cls.TAKES_PATHS), None)
            if command is None or "--" in values:
                continue
            if not ("git" in values or command == 0):
                continue
            after = node.elts[command + 1:]
            revision = any(not (isinstance(e, ast.Constant) and isinstance(e.value, str)
                                and e.value.startswith("-")) for e in after)
            if revision:
                found.append(node.lineno)
        return found

    def test_the_package_ends_every_revision_with_a_double_dash(self):
        import ast
        root = Path(__file__).resolve().parents[1] / "agentcontext"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            offenders.extend(f"{path.relative_to(root.parent)}:{line}"
                             for line in self.offenders(ast.parse(path.read_text())))
        self.assertEqual(offenders, [])

    def test_the_check_detects_a_revision_without_a_double_dash(self):
        import ast
        tree = ast.parse('a = ["git", "show", "-s", "HEAD"]\n'
                         'b = ["show", "-s", "HEAD", "--"]\n'
                         'c = ["git", "-C", d, "rev-list", "--all", "--count"]\n'
                         'e = ["git", "log", *revisions]\n'
                         'f = ["git", "rev-parse", "HEAD"]\n')
        self.assertEqual(self.offenders(tree), [1, 4])


class TestBuildCloneUrl(unittest.TestCase):

    def test_https_anonymous(self):
        self.assertEqual(
            build_clone_url("owner/repo", use_ssh=False, token=None),
            "https://github.com/owner/repo.git",
        )

    def test_https_with_github_access_token(self):
        # The placeholder user, which makes git run the credential helper. The GitHub access
        # token itself is never in the URL, and never in argv or in the clone's
        # config.
        self.assertEqual(
            build_clone_url("owner/repo", use_ssh=False, token="mytoken"),
            "https://x-access-token@github.com/owner/repo.git",
        )

    def test_ssh(self):
        self.assertEqual(
            build_clone_url("owner/repo", use_ssh=True, token=None),
            "git@github.com:owner/repo.git",
        )

    def test_ssh_ignores_github_access_token(self):
        self.assertEqual(
            build_clone_url("owner/repo", use_ssh=True, token="mytoken"),
            "git@github.com:owner/repo.git",
        )

    def test_empty_github_access_token_treated_as_anonymous(self):
        self.assertEqual(
            build_clone_url("owner/repo", use_ssh=False, token=""),
            "https://github.com/owner/repo.git",
        )


class TestIsFilterUnsupported(unittest.TestCase):

    def test_filter_spec_matches(self):
        self.assertTrue(is_filter_unsupported("fatal: invalid filter-spec 'blob:none'"))

    def test_does_not_support_filter_matches(self):
        self.assertTrue(is_filter_unsupported("server does not support --filter"))

    def test_unknown_option_filter_matches(self):
        self.assertTrue(is_filter_unsupported("error: unknown option `filter=blob:none'"))

    def test_case_insensitive(self):
        self.assertTrue(is_filter_unsupported("FATAL: INVALID FILTER-SPEC"))

    def test_normal_output_does_not_match(self):
        self.assertFalse(is_filter_unsupported("Cloning into 'repo'...\ndone."))

    def test_empty_string_does_not_match(self):
        self.assertFalse(is_filter_unsupported(""))


class TestIsPermanentFailure(unittest.TestCase):

    def test_error_repository_not_found(self):
        self.assertTrue(is_permanent_failure("ERROR: Repository not found."))

    def test_an_authentication_challenge_without_a_github_access_token_is_permanent(self):
        # Private, or gone with an authentication challenge in GitHub's response: without a
        # GitHub access token, git has no credential to send, and a retry cannot change that.
        self.assertTrue(is_permanent_failure(
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled"))
        self.assertFalse(is_permanent_failure("fatal: unable to access 'https://github.com/o/r.git/': "
                                              "Could not resolve host: github.com"))
        self.assertFalse(is_permanent_failure("error: RPC failed; HTTP 429 curl 22"))

    def test_a_refused_github_access_token_is_not_permanent(self):
        """GitHub's message for a GitHub access token that it refuses, and git's own line
        without it. GitHub may require authentication for a public repository, and the
        messages concern the GitHub access token and not the repository."""
        self.assertFalse(is_permanent_failure(
            "remote: Invalid username or token. Password authentication is not supported "
            "for Git operations.\nfatal: Authentication failed for 'https://github.com/o/r.git/'"))
        self.assertFalse(is_permanent_failure(
            "fatal: Authentication failed for 'https://github.com/o/r.git/'"))

    def test_a_repository_github_closed_is_permanent(self):
        # GitHub's messages for a repository that exists and is not served, which a retry
        # cannot change.
        self.assertTrue(is_permanent_failure(
            "remote: Access to this repository has been disabled by GitHub Staff due to "
            "excessive resource use."))
        self.assertTrue(is_permanent_failure(
            "remote: Repository unavailable due to DMCA takedown."))

    def test_remote_repository_not_found(self):
        self.assertTrue(is_permanent_failure("remote: Repository not found"))

    def test_fatal_repository_not_found(self):
        self.assertTrue(is_permanent_failure("fatal: repository 'https://...' not found"))

    def test_not_authorized(self):
        self.assertTrue(is_permanent_failure("fatal: not authorized"))

    def test_permission_denied_publickey(self):
        self.assertTrue(is_permanent_failure("git@github.com: Permission denied (publickey)."))

    def test_access_denied(self):
        self.assertTrue(is_permanent_failure("remote: access denied"))

    def test_case_insensitive(self):
        self.assertTrue(is_permanent_failure("REMOTE: REPOSITORY NOT FOUND"))

    def test_transient_error_does_not_match(self):
        self.assertFalse(is_permanent_failure("fatal: unable to access: Could not resolve host"))

    def test_a_path_with_no_repository_behind_it_is_a_repository_that_is_gone(self):
        # The file history step also reads this message as a repository that is gone. A local
        # Git mirror missing from the subsample run gives it where GitHub returns Repository
        # not found. It is therefore not a network failure to retry and count toward the limit
        # on repositories to retry.
        self.assertTrue(is_permanent_failure("fatal: '/tmp/x' does not appear to be a git repository"))

    def test_a_refused_connection_does_not_match(self):
        self.assertFalse(is_permanent_failure(
            "fatal: unable to access 'http://127.0.0.1:9/o/r.git/': Failed to connect to "
            "127.0.0.1 port 9 after 0 ms: Couldn't connect to server"))

    def test_empty_string_does_not_match(self):
        self.assertFalse(is_permanent_failure(""))


class TestRedactToken(unittest.TestCase):

    def test_github_access_token_stripped_from_https_url(self):
        self.assertEqual(
            redact_token("https://ghp_secret123@github.com/owner/repo.git"),
            "https://github.com/owner/repo.git",
        )

    def test_plain_https_unchanged(self):
        self.assertEqual(
            redact_token("https://github.com/owner/repo.git"),
            "https://github.com/owner/repo.git",
        )

    def test_ssh_url_unchanged(self):
        # git@github.com is an SSH username, not a secret, and stays intact
        self.assertEqual(
            redact_token("git@github.com:owner/repo.git"),
            "git@github.com:owner/repo.git",
        )

    def test_github_access_token_value_absent_from_result(self):
        self.assertNotIn("ghp_secret123",
                         redact_token("https://ghp_secret123@github.com/owner/repo.git"))


class TestPermanentFailureReason(unittest.TestCase):
    """The reason under which a refusal is recorded, from GitHub's message in git's output."""

    def test_each_refusal_and_a_network_failure(self):
        cases = (("remote: Repository not found.\nfatal: repository 'https://github.com/o/r.git/' not found",
                  "not_found_or_no_access"),
                 ("remote: Repository unavailable due to DMCA takedown.", "dmca_takedown"),
                 ("remote: Access to this repository has been disabled by GitHub staff.\n"
                  "fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 403",
                  "disabled_by_github"),
                 ("fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com",
                  None),
                 ("", None))
        for text, reason in cases:
            with self.subTest(text=text[:40]):
                self.assertEqual(permanent_failure_reason(text), reason)


class TestCloneWithRetry(unittest.TestCase):
    """Integration tests using a local file:// remote redirected via GIT_CONFIG insteadOf."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.dest = self.tmp / "clone"
        self.log = self.tmp / "logs" / "owner§repo.log"

    def tearDown(self):
        self._clear_redirect()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect(self, base_url: str,
                  original: str = "https://github.com/owner/repo.git") -> None:
        """Redirect the clone URL `original` to base_url through the environment's config."""
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.{base_url}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = original

    def _through(self, script: Path) -> None:
        """Redirect the clone URL through `script`, a stand-in for upload-pack."""
        os.environ.update(ext_redirect(script, self.remote, "https://github.com/owner/repo.git"))

    def _clear_redirect(self) -> None:
        for k in ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                  "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"]:
            os.environ.pop(k, None)

    def test_missing_local_remote_returns_a_permanent_failure_without_retry(self):
        self._redirect(f"file://{self.tmp}/absent")
        status = clone_with_retry("owner/repo", self.dest, self.log,
                                  max_tries=3, base_delay=0)
        self.assertEqual(status, "not_found_or_no_access")
        log = self.log.read_text()
        self.assertNotIn("attempt 2/3", log)
        self.assertIn("PERMANENT_FAIL_NOT_FOUND_OR_NO_ACCESS", log)
        self.assertFalse(self.dest.exists())

    def test_github_s_reason_for_a_refused_repository_is_recorded(self):
        """git passes GitHub's reason through, and the refusal is recorded under it, so that a
        DMCA takedown or a repository that GitHub disabled is not recorded as
        not_found_or_no_access. A refusal is not retried, whatever its reason."""
        for message, reason in (
                ("remote: Repository unavailable due to DMCA takedown.", "dmca_takedown"),
                ("remote: Access to this repository has been disabled by GitHub staff.",
                 "disabled_by_github"),
                ("remote: Repository not found.", "not_found_or_no_access")):
            with self.subTest(reason=reason):
                script = self.tmp / f"refuse_{reason}.py"
                script.write_text(f"import sys\nsys.stderr.write({message!r} + '\\n')\n"
                                  "sys.exit(128)\n", encoding="utf-8")
                self._through(script)
                status = clone_with_retry("owner/repo", self.dest, self.log,
                                          max_tries=3, base_delay=0)
                self.assertEqual(status, reason, self.log.read_text())
                log = self.log.read_text()
                self.assertNotIn("attempt 2/3", log)
                self.assertIn(f"PERMANENT_FAIL_{reason.upper()}", log)
                self._clear_redirect()

    def test_success_returns_ok(self):
        self._redirect(f"file://{self.remote}")
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=1, base_delay=0)
        self.assertEqual(status, "ok")

    def test_success_clones_files(self):
        self._redirect(f"file://{self.remote}")
        clone_with_retry("owner/repo", self.dest, self.log, max_tries=1, base_delay=0)
        self.assertTrue((self.dest / "README.md").exists())

    def test_success_writes_log(self):
        self._redirect(f"file://{self.remote}")
        clone_with_retry("owner/repo", self.dest, self.log, max_tries=1, base_delay=0)
        self.assertTrue(self.log.exists())
        self.assertIn("attempt 1/1", self.log.read_text())

    def test_transient_failure_returns_clone_failed(self):
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=2, base_delay=0)
        self.assertEqual(status, "clone_failed")

    def test_transient_failure_logs_all_attempts(self):
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        clone_with_retry("owner/repo", self.dest, self.log, max_tries=2, base_delay=0)
        text = self.log.read_text()
        self.assertIn("attempt 1/2", text)
        self.assertIn("attempt 2/2", text)

    def test_a_clone_past_the_size_bound_is_a_third_outcome(self):
        """A clone that goes over the size bound is stopped, recorded under its own outcome,
        and not retried, because the bound is deterministic and a retry would
        download the same bytes again."""
        _add_commit(self.remote, "big.bin", "x" * (3 * 1024 * 1024))
        self._redirect(f"file://{self.remote}")
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=3,
                                  base_delay=0, max_clone_bytes=1024 * 1024)
        self.assertEqual(status, "size_bound_exceeded")
        self.assertFalse(self.dest.exists(), "the clone over the size bound was left on disk")
        text = self.log.read_text()
        # This clone ends before the watcher's first measurement, and the check after the
        # clone finds the breach. In the test below, the watcher stops a running
        # clone.
        self.assertIn("after the clone", text)
        self.assertNotIn("attempt 2/3", text)

    def test_a_clone_the_watcher_stops_is_the_same_outcome(self):
        """The watcher stops a clone that is still receiving its pack. The other clones here
        end before the watcher's first measurement, and the breach that the watcher raises must
        not leave the retry loop as an exception, which the cloning step would record as
        `collector_error` and collect again on every run."""
        _add_noise(self.remote, "big.bin", 3 * 1024 * 1024)
        self._through(upload_pack(self.tmp, pause=0.05))
        with sampled_every(0.05):
            status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=3,
                                      base_delay=0, max_clone_bytes=1024 * 1024)
        self.assertEqual(status, "size_bound_exceeded")
        self.assertFalse(self.dest.exists(), "the clone over the size bound was left on disk")
        text = self.log.read_text()
        self.assertIn("SIZE_BOUND_EXCEEDED", text)
        self.assertNotIn("after the clone", text, "the clone was not stopped while it ran")
        self.assertNotIn("attempt 2/3", text)

    def test_a_clone_within_the_size_bound_is_ok(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=1,
                                  base_delay=0, max_clone_bytes=64 * 1024 * 1024)
        self.assertEqual(status, "ok")

    def test_a_max_clone_bytes_of_zero_turns_the_size_bound_off(self):
        """A clone that lasts several intervals is left alone. A watcher started anyway, which
        would treat any tree as a breach of a size bound of zero, would be detected."""
        _add_noise(self.remote, "big.bin", 1024 * 1024)
        self._through(upload_pack(self.tmp, pause=0.05))
        with sampled_every(0.05), \
                mock.patch.object(_clone.bounds, "directory_size",
                                  wraps=_clone.bounds.directory_size) as sampled:
            status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=1,
                                      base_delay=0, max_clone_bytes=0)
        self.assertEqual(status, "ok")
        sampled.assert_not_called()

    def test_a_clone_past_the_timeout_twice_is_time_bound_exceeded(self):
        """One timeout is a failed attempt, retried like a network failure. A second one
        records the clone as `time_bound_exceeded` rather than holding a worker for the rest of
        the attempts, each one time bound long, and the log records which bound stopped each
        attempt."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=6,
                                  base_delay=0, clone_timeout=0.001)
        self.assertEqual(status, "time_bound_exceeded")
        text = self.log.read_text()
        self.assertEqual(text.count("TIMEOUT after"), 2, text)
        self.assertIn("attempt 2/6", text)
        self.assertNotIn("attempt 3/6", text)
        self.assertIn("TIME_BOUND_EXCEEDED", text)
        self.assertFalse(self.dest.exists(), "the clone that the time bound ended was left")

    def test_one_timeout_among_failed_attempts_is_a_failed_clone(self):
        """A timeout and then failures of the network are `clone_failed`, which the next run
        retries, as a run of network failures alone is."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        real = _clone.bounds.run_bounded
        calls = []

        def first_times_out(cmd, *args, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: unable to access")

        with mock.patch.object(_clone.bounds, "run_bounded", first_times_out):
            status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=3,
                                      base_delay=0)
        self.assertEqual(status, "clone_failed", self.log.read_text())
        self.assertEqual(len(calls), 3)
        self.assertIs(_clone.bounds.run_bounded, real)

    def test_the_attempt_after_one_killed_mid_transfer_starts_from_nothing(self):
        """A clone killed at its time bound has part of the pack on disk, and git refuses to clone
        into a directory that is not empty. The time bound in the test above kills git before it
        writes anything, and that test cannot detect a retry that clones over what the
        first attempt left. Here the first attempt's upload-pack stops once the pack has started."""
        _add_noise(self.remote, "big.bin", 2_000_000)
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        script = upload_pack(self.tmp, stall="first")
        self._through(script)
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=2,
                                  base_delay=0, clone_timeout=2)
        self.assertTrue(script.with_suffix(".stalled").exists(), "the first attempt did not stall")
        self.assertEqual(status, "ok", self.log.read_text())
        self.assertIn("TIMEOUT", self.log.read_text())
        self.assertTrue((self.dest / "CLAUDE.md").is_file())

    def test_github_access_token_not_written_to_log(self):
        # The clone uses the URL with the placeholder user, but the log must record only the
        # URL without credentials.
        self._redirect(
            f"file://{self.remote}",
            original="https://x-access-token@github.com/owner/repo.git",
        )
        status = clone_with_retry(
            "owner/repo", self.dest, self.log,
            token="SECRETTOKEN", max_tries=1, base_delay=0,
        )
        self.assertEqual(status, "ok")
        text = self.log.read_text()
        self.assertNotIn("SECRETTOKEN", text)
        self.assertIn("url=https://github.com/owner/repo.git", text)

    def test_the_github_access_token_is_in_no_url_passed_to_git(self):
        """The clone's own config records the remote URL. A GitHub access token in the URL would
        be recorded there, and `ps` would show it in git's arguments for the whole
        clone."""
        self._redirect(f"file://{self.remote}",
                       original="https://x-access-token@github.com/owner/repo.git")
        with mock.patch.object(_mod.bounds, "run_bounded",
                               wraps=_mod.bounds.run_bounded) as bounded:
            status = clone_with_retry("owner/repo", self.dest, self.log,
                                      token="SECRETTOKEN", max_tries=1, base_delay=0)
        self.assertEqual(status, "ok")
        argv = bounded.call_args.args[0]
        self.assertNotIn("SECRETTOKEN", " ".join(argv))
        self.assertIn("https://x-access-token@github.com/owner/repo.git", argv)
        env = bounded.call_args.kwargs["env"]
        self.assertEqual(env["GIT_ASKPASS_TOKEN"], "SECRETTOKEN")
        self.assertIn("GIT_ASKPASS", env)
        self.assertNotIn("SECRETTOKEN", (self.dest / ".git" / "config").read_text())

    def test_a_git_that_cannot_start_is_a_failed_attempt(self):
        """A refused fork() is a transient failure, and the retry loop therefore retries it
        rather than letting the OSError end the loop after a single attempt."""
        self._redirect(f"file://{self.remote}")
        # What fork() sets. EAGAIN is 11 on Linux and 35 on macOS, and Python raises it as
        # BlockingIOError. The log names that class rather than OSError.
        refusal = OSError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusal):
            status = clone_with_retry("owner/repo", self.dest, self.log,
                                      max_tries=2, base_delay=0)
        self.assertEqual(status, "clone_failed")
        text = self.log.read_text()
        self.assertIn("attempt 2/2", text)
        self.assertEqual(text.count("NOT STARTED BlockingIOError"), 2)

    def test_no_sleep_after_the_last_attempt(self):
        """The backoff is between attempts, and none follows the last one, because no attempt
        comes after it."""
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        started = time.monotonic()
        status = clone_with_retry("owner/repo", self.dest, self.log,
                                  max_tries=1, base_delay=2.0)
        self.assertEqual(status, "clone_failed")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(self.log.read_text().rstrip().endswith("FAILED"))
        self.assertNotIn("sleep", self.log.read_text())

    def test_the_log_contains_this_collection_alone(self):
        """One collection's attempts. Appended across runs, the log would grow with every
        run. The earlier run's attempts are in the earlier run's handover."""
        self._redirect(f"file://{self.remote}")
        for _ in range(2):
            shutil.rmtree(self.dest, ignore_errors=True)
            clone_with_retry("owner/repo", self.dest, self.log, max_tries=1, base_delay=0)
        self.assertEqual(self.log.read_text().count("attempt 1/1"), 1)

    def test_an_lfs_repository_clones_without_a_word_from_the_filter(self):
        """`filter.lfs.process=cat` would make git start `cat` as a long-running filter,
        which does not implement the filter protocol, and every LFS file would then cost error
        lines and a kill. With no process filter, git uses the two one-shot filters, for
        which `cat` works."""
        self.assertIn("filter.lfs.process=", _clone.GIT_LFS_DISABLE_ARGS)
        self.assertNotIn("filter.lfs.process=cat", _clone.GIT_LFS_DISABLE_ARGS)
        (self.remote / ".gitattributes").write_text("*.bin filter=lfs diff=lfs -text\n")
        pointer = "version https://git-lfs.github.com/spec/v1\noid sha256:ab\nsize 4\n"
        (self.remote / "x.bin").write_text(pointer)
        subprocess.run(["git", "-C", str(self.remote), "-c", "filter.lfs.required=false",
                        "-c", "filter.lfs.clean=cat", "-c", "filter.lfs.process=",
                        "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.remote), "commit", "-q", "-m", "lfs"],
                       check=True, capture_output=True)
        self._redirect(f"file://{self.remote}")
        status = clone_with_retry("owner/repo", self.dest, self.log, max_tries=1, base_delay=0)
        self.assertEqual(status, "ok")
        self.assertNotIn("error:", self.log.read_text())
        self.assertEqual((self.dest / "x.bin").read_text(), pointer)


class TestSubmoduleRows(unittest.TestCase):
    """The clone records which directories are gitlinks, and no query to GitHub is
        needed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        _init_git_repo(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_gitlink(self, path: str, sha: str = "1" * 40) -> None:
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--add",
                        "--cacheinfo", "160000", sha, path], capture_output=True, check=True)

    def _commit(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "gitlinks"],
                       capture_output=True, check=True)

    def test_a_repository_without_submodules(self):
        self.assertEqual(submodule_rows("o/r", self.repo), [])

    def test_the_file_list_keeps_every_entry_and_every_byte_of_its_name(self):
        """A path in the tree is bytes. A Latin-1 name is written as git gave it, and read
        back as a string that encodes to those bytes again, so that no name is lost or changed.
        The name is staged with git rather than created on disk, which APFS refuses."""
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                              input=b"x\n", capture_output=True, check=True).stdout.strip()
        subprocess.run([b"git", b"-C", os.fsencode(self.repo), b"update-index", b"--add",
                        b"--cacheinfo", b"100644", blob, b"caf\xe9.md"],
                       capture_output=True, check=True)
        self._add_gitlink("lib/dep")
        self._commit()
        listing = _clone.tree_listing(self.repo)
        path = self.tmp / "o§r.ls-tree.xz"
        self.assertEqual(_clone.write_file_list(listing, path), 3)
        entries = _clone.read_file_list(path)
        self.assertEqual(sorted(mode for mode, _ in entries), ["100644", "100644", "160000"])
        latin = next(name for _, name in entries if name.startswith("caf"))
        self.assertEqual(latin.encode("utf-8", "surrogateescape"), b"caf\xe9.md")
        self.assertEqual([r["path"] for r in submodule_rows("o/r", self.repo, listing)],
                         ["lib/dep"])

    def test_failed_listing_preserves_git_exit_status_and_stderr(self):
        missing = self.tmp / "not-a-repository"
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            _clone.tree_listing(missing)
        self.assertNotEqual(caught.exception.returncode, 0)
        self.assertIn(str(missing).encode(), caught.exception.stderr)
        self.assertEqual(caught.exception.cmd,
                         ["git", "-C", str(missing), "ls-tree", "-r", "-z", "HEAD"])

    def test_a_gitlink_is_reported_with_the_repository_its_url_names(self):
        self._add_gitlink("lib/vscode")
        (self.repo / ".gitmodules").write_text(
            '[submodule "v"]\n\tpath = lib/vscode\n'
            '\turl = https://github.com/microsoft/vscode.git\n')
        self._commit()
        self.assertEqual(submodule_rows("o/r", self.repo),
                         [{"repo_name": "o/r", "path": "lib/vscode",
                           "submodule_repo": "microsoft/vscode"}])

    def test_a_gitlink_with_no_gitmodules_entry_still_reports_its_path(self):
        self._add_gitlink("vendor/dep")
        self._commit()
        self.assertEqual([r["path"] for r in submodule_rows("o/r", self.repo)],
                         ["vendor/dep"])
        self.assertEqual(submodule_rows("o/r", self.repo)[0]["submodule_repo"], "")

    def test_an_ssh_url_names_the_same_repository(self):
        self._add_gitlink("dep")
        (self.repo / ".gitmodules").write_text(
            '[submodule "d"]\n\tpath = dep\n\turl = git@github.com:owner/dep.git\n')
        self._commit()
        self.assertEqual(submodule_rows("o/r", self.repo)[0]["submodule_repo"], "owner/dep")

    def test_an_ordinary_directory_is_not_a_gitlink(self):
        _add_commit(self.repo, "packages/web/.claude/CLAUDE.md", "x\n")
        self.assertEqual(submodule_rows("o/r", self.repo), [])

    def test_a_symlink_is_not_a_gitlink(self):
        """Mode 120000 is a symlink, 160000 a gitlink."""
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                              input=b"../elsewhere", capture_output=True, check=True).stdout.strip()
        subprocess.run([b"git", b"-C", os.fsencode(self.repo), b"update-index", b"--add",
                        b"--cacheinfo", b"120000", blob, b"link"], capture_output=True, check=True)
        self._add_gitlink("lib/dep")
        self._commit()
        self.assertEqual([r["path"] for r in submodule_rows("o/r", self.repo)], ["lib/dep"])

    def test_the_paths_are_sorted(self):
        for path in ("z/dep", "a/dep"):
            self._add_gitlink(path)
        self._commit()
        self.assertEqual([r["path"] for r in submodule_rows("o/r", self.repo)],
                         ["a/dep", "z/dep"])

    def test_a_directory_that_is_not_a_repository_reports_nothing(self):
        self.assertEqual(submodule_rows("o/r", self.tmp / "missing"), [])


class TestSubmodulesAreNotCheckedOut(unittest.TestCase):
    """A submodule is a pointer, and the files behind it belong to the repository that the
    gitlink names. A clone therefore checks no submodule out and collects none of another
    project's configuration."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_clone_checks_no_submodule_out(self):
        """A submodule that the clone could reach, with a CLAUDE.md in it, leaves nothing in
        the collection. A search of the source for `submodule update`, and the test below,
        which runs over an empty directory, would both pass a clone with
        `--recurse-submodules`."""
        sub = self.tmp / "sub"
        _init_git_repo(sub)
        _add_commit(sub, "CLAUDE.md", "# the submodule's own\n")
        _init_git_repo(self.repo)
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
                        "submodule", "add", "-q", f"file://{sub}", "vendor/sub"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "submodule"],
                       check=True, capture_output=True)
        # A file:// submodule is refused unless allowed, where GitHub's are not.
        config = {"GIT_CONFIG_COUNT": "2",
                  "GIT_CONFIG_KEY_0": f"url.file://{self.repo}.insteadOf",
                  "GIT_CONFIG_VALUE_0": "https://github.com/owner/repo.git",
                  "GIT_CONFIG_KEY_1": "protocol.file.allow",
                  "GIT_CONFIG_VALUE_1": "always"}
        for key, value in config.items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        control = self.tmp / "control"
        subprocess.run(["git", "clone", "-q", "--recurse-submodules",
                        "https://github.com/owner/repo.git", str(control)],
                       check=True, capture_output=True)
        self.assertTrue((control / "vendor" / "sub" / "CLAUDE.md").is_file(),
                        "the submodule cannot be reached, so the test proves nothing")
        cfg = _mod.CollectionConfig(collected_dir=self.out, clone_dir=self.tmp / "scratch",
                                 log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
                                 measure_git=False)
        _, _, fail_row, _, submodules, _measures = _mod.process_repo(cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual([r["path"] for r in submodules], ["vendor/sub"])
        self.assertEqual([p for p in self.out.rglob("*") if p.is_file()], [])

    def test_an_uninitialized_submodule_directory_contributes_nothing(self):
        _init_git_repo(self.repo)
        (self.repo / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\turl = file:///nonexistent/x.git\n')
        subprocess.run(
            ["git", "-C", str(self.repo), "update-index", "--add", "--cacheinfo",
             "160000", "1234567890123456789012345678901234567890", "sub"],
            capture_output=True)
        (self.repo / "sub").mkdir()
        flags = collect_repo(self.repo, self.out, set())
        self.assertFalse((self.out / "sub").exists())
        self.assertEqual(flags, set())


class TestTheSuiteIgnoresAmbientCredentials(unittest.TestCase):
    """The result must not depend on whether the machine exported a GitHub access token."""

    def test_no_github_access_token_is_visible_to_the_collector(self):
        for name in _ambient_env.CREDENTIAL_VARS:
            with self.subTest(variable=name):
                self.assertIsNone(os.environ.get(name))

    def test_a_github_access_token_would_have_changed_the_clone_url(self):
        # The reason for the hook above, stated as the behavior that it prevents.
        plain = _clone.build_clone_url("o/a", use_ssh=False, token=None)
        with_token = _clone.build_clone_url("o/a", use_ssh=False, token="t")
        self.assertEqual(plain, "https://github.com/o/a.git")
        self.assertNotEqual(with_token, plain)


if __name__ == "__main__":
    unittest.main()
