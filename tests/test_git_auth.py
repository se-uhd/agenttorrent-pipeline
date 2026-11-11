"""
Tests for agentcontext.git.auth: how the GitHub access token reaches git.

Strategy:
  - The property that matters is negative: the GitHub access token must not
    appear in any argument that git receives. Several tests assert exactly that.
  - The askpass helper is a real file, and one test runs it and checks that it
    prints the variable, which is all that git relies on it for.
  - No test reaches the network.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_git_auth -v
"""

import importlib
import multiprocessing
import os
import queue
import signal
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

_mod = importlib.import_module("agentcontext.git.auth")

TOKEN = "ghp_notarealtoken0000000000000000000000"


def _stopped_collection_worker(paths) -> None:
    """In a collection worker: create the helper, put its path on the queue, and wait to be
    stopped."""
    from agentcontext import guards
    guards.install_worker_stop()
    paths.put(_mod.askpass_script())
    signal.pause()


class TestCloneUrl(unittest.TestCase):
    def test_without_a_github_access_token_it_is_the_plain_url(self):
        self.assertEqual(_mod.clone_url("owner/repo"),
                         "https://github.com/owner/repo.git")

    def test_with_a_github_access_token_the_url_names_a_placeholder_user(self):
        self.assertEqual(_mod.clone_url("owner/repo", TOKEN),
                         f"https://{_mod.PLACEHOLDER_USER}@github.com/owner/repo.git")

    def test_the_github_access_token_never_appears_in_the_url(self):
        # The purpose of the module: git clone puts its URL in argv.
        self.assertNotIn(TOKEN, _mod.clone_url("owner/repo", TOKEN))

    def test_ssh_wins_over_a_github_access_token(self):
        self.assertEqual(_mod.clone_url("owner/repo", TOKEN, use_ssh=True),
                         "git@github.com:owner/repo.git")

    def test_an_empty_github_access_token_is_treated_as_none(self):
        self.assertEqual(_mod.clone_url("owner/repo", ""),
                         "https://github.com/owner/repo.git")


class TestAskpassScript(unittest.TestCase):
    def tearDown(self):
        _mod.cleanup()

    def test_it_creates_an_executable_file(self):
        path = _mod.askpass_script()
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)

    def test_it_is_not_readable_by_anyone_else(self):
        mode = os.stat(_mod.askpass_script()).st_mode
        self.assertFalse(mode & (stat.S_IRGRP | stat.S_IROTH))

    def test_the_same_path_comes_back_on_a_second_call(self):
        self.assertEqual(_mod.askpass_script(), _mod.askpass_script())

    def _ask(self, prompt):
        return subprocess.run(
            [_mod.askpass_script(), prompt], capture_output=True, text=True,
            env={**os.environ, _mod.TOKEN_ENV_VAR: TOKEN,
                 _mod.HOST_ENV_VAR: _mod.TOKEN_HOST}).stdout.strip()

    def test_it_prints_the_github_access_token_for_the_host_that_it_belongs_to(self):
        # Printing the variable for github.com is the behavior that git depends on.
        self.assertEqual(
            self._ask("Password for 'https://x-access-token@github.com': "), TOKEN)

    def test_it_prints_nothing_for_any_other_host(self):
        # The repository being measured writes its own submodule URLs, and the .gitmodules of
        # a large project can name googlesource, a GitLab instance or a private server.
        # GIT_ASKPASS applies to every prompt that git issues. Without this check, the GitHub
        # access token would be sent as HTTP Basic credentials to any host that a stranger's
        # repository named.
        for host in ("https://chromium.googlesource.com",
                     "https://gitlab.example.com",
                     "https://github.com.evil.example",
                     "https://internal.corp"):
            with self.subTest(host=host):
                self.assertEqual(self._ask(f"Password for '{host}': "), "")

    def test_it_contains_no_github_access_token_of_its_own(self):
        self.assertNotIn(TOKEN, Path(_mod.askpass_script()).read_text())


class TestCleanup(unittest.TestCase):
    def test_it_removes_the_helper(self):
        path = _mod.askpass_script()
        _mod.cleanup()
        self.assertFalse(os.path.exists(path))

    def test_calling_it_twice_is_harmless(self):
        _mod.askpass_script()
        _mod.cleanup()
        _mod.cleanup()

    def test_a_helper_deleted_underneath_is_recreated(self):
        path = _mod.askpass_script()
        os.unlink(path)
        self.assertTrue(os.path.isfile(_mod.askpass_script()))
        _mod.cleanup()

    def test_a_stopped_collection_worker_removes_its_helper(self):
        # A stopped worker ends with `os._exit`, which runs no exit handler.
        context = multiprocessing.get_context("spawn")
        paths = context.Queue()
        worker = context.Process(target=_stopped_collection_worker, args=(paths,))
        worker.start()
        try:
            # The deadline only catches a worker that hangs. The worker starts an interpreter
            # that imports the package before it creates the helper, and a loaded runner can
            # take a long time for that. A worker that exits before then fails the test at once.
            deadline = time.monotonic() + 600
            while True:
                try:
                    path = paths.get(timeout=1)
                    break
                except queue.Empty:
                    self.assertTrue(worker.is_alive(),
                                    "the worker exited before it created the helper")
                    self.assertLess(time.monotonic(), deadline,
                                    "the worker never created the helper")
            self.assertTrue(os.path.isfile(path))
            os.kill(worker.pid, signal.SIGTERM)
            worker.join(60)
        finally:
            if worker.is_alive():
                worker.kill()
        self.assertEqual(worker.exitcode, 128 + signal.SIGTERM)
        self.assertFalse(os.path.exists(path))


class TestApply(unittest.TestCase):
    def tearDown(self):
        _mod.cleanup()

    def test_a_github_access_token_sets_the_helper_its_variable_and_the_host(self):
        env = _mod.apply({}, TOKEN)
        self.assertEqual(env[_mod.TOKEN_ENV_VAR], TOKEN)
        self.assertEqual(env[_mod.HOST_ENV_VAR], _mod.TOKEN_HOST)
        self.assertTrue(os.path.isfile(env["GIT_ASKPASS"]))

    def test_no_github_access_token_sets_nothing(self):
        self.assertEqual(_mod.apply({}, None), {})
        self.assertEqual(_mod.apply({}, ""), {})

    def test_it_returns_the_same_dictionary(self):
        env = {"PATH": "/usr/bin"}
        self.assertIs(_mod.apply(env, TOKEN), env)
        self.assertEqual(env["PATH"], "/usr/bin")


class TestRedact(unittest.TestCase):
    def test_it_strips_credentials_from_a_url(self):
        self.assertEqual(_mod.redact(f"https://{TOKEN}@github.com/o/r.git"),
                         "https://github.com/o/r.git")

    def test_a_url_without_credentials_is_unchanged(self):
        url = "https://github.com/o/r.git"
        self.assertEqual(_mod.redact(url), url)

    def test_it_works_inside_a_longer_message(self):
        message = f"Command '['git', 'clone', 'https://{TOKEN}@github.com/o/r.git']' timed out"
        self.assertNotIn(TOKEN, _mod.redact(message))


class TestTheGitLayersUseIt(unittest.TestCase):
    """The git client of `git.history` must use this module, not a copy of its own."""

    def tearDown(self):
        _mod.cleanup()

    def test_the_clone_url_of_git_history_contains_no_github_access_token(self):
        for name in ("agentcontext.git.history",):
            with self.subTest(script=name):
                cls = importlib.import_module(name).GitFileMetadata
                url = cls(github_token=TOKEN)._build_clone_url("owner/repo")
                self.assertNotIn(TOKEN, url)
                self.assertIn(_mod.PLACEHOLDER_USER, url)

    def test_git_history_passes_the_github_access_token_through_the_environment(self):
        for name in ("agentcontext.git.history",):
            with self.subTest(script=name):
                module = importlib.import_module(name)
                instance = module.GitFileMetadata(github_token=TOKEN)
                captured = {}
                real = module.bounds.run_bounded

                def fake(cmd, cwd, env, timeout, **kwargs):
                    captured["env"] = env
                    captured["cmd"] = cmd
                    return subprocess.CompletedProcess(cmd, 0, "", "")

                module.bounds.run_bounded = fake
                try:
                    instance._run_git_command(["git", "status"], cwd=".")
                finally:
                    module.bounds.run_bounded = real
                self.assertEqual(captured["env"][_mod.TOKEN_ENV_VAR], TOKEN)
                self.assertNotIn(TOKEN, " ".join(captured["cmd"]))


class TestAUsernamePromptNeverGetsTheGitHubAccessToken(unittest.TestCase):
    """A submodule URL in the ordinary form has no username, and git therefore prompts for one
    first. A GitHub access token printed at that prompt would become the username. git would
    build the next prompt from it and pass that prompt to the helper as an argument, and the
    secret would appear in ps. This module exists to prevent that exposure."""

    TOKEN = "SECRET-TOKEN-123"

    def _ask(self, prompt, host=_mod.TOKEN_HOST):
        env = _mod.apply(dict(os.environ), self.TOKEN, host=host)
        return subprocess.run([env["GIT_ASKPASS"], prompt], capture_output=True, text=True,
                              env=env, timeout=30).stdout.strip()

    def test_a_username_prompt_gets_the_placeholder(self):
        self.assertEqual(self._ask("Username for 'https://github.com': "),
                         _mod.PLACEHOLDER_USER)

    def test_a_username_prompt_never_gets_the_github_access_token(self):
        self.assertNotEqual(self._ask("Username for 'https://github.com': "), self.TOKEN)

    def test_a_password_prompt_gets_the_github_access_token(self):
        self.assertEqual(self._ask("Password for 'https://x-access-token@github.com': "),
                         self.TOKEN)

    def test_a_password_prompt_without_a_username_still_gets_the_github_access_token(self):
        self.assertEqual(self._ask("Password for 'https://github.com': "), self.TOKEN)

    def test_the_host_is_compared_exactly_rather_than_matched(self):
        for prompt in ("Password for 'https://a@github.com@evil.example': ",
                       "Password for 'https://a@github.com/@evil.example': ",
                       "Password for 'https://github.com.evil.example': ",
                       "Username for 'https://github.com.evil.example': ",
                       "Password for 'https://evil.example/github.com': "):
            self.assertEqual(self._ask(prompt), "", prompt)

    def test_another_host_gets_nothing_for_either_prompt(self):
        for prompt in ("Username for 'https://gitlab.com': ",
                       "Password for 'https://gitlab.com': "):
            self.assertEqual(self._ask(prompt), "", prompt)

    def test_a_prompt_that_is_neither_gets_nothing(self):
        for prompt in ("Passphrase for key '/home/x/.ssh/id_rsa': ",
                       "Enter passphrase for 'https://github.com': ",
                       ""):
            self.assertEqual(self._ask(prompt), "", prompt)

    def test_the_placeholder_is_what_the_clone_url_names(self):
        """The username that the helper prints has to be the one that the URL already has, or
        git would prompt for the password of a different user."""
        self.assertIn(_mod.PLACEHOLDER_USER, _mod.clone_url("o/r", token=self.TOKEN))

    def test_the_locale_is_pinned_so_the_prompt_keyword_is_english(self):
        env = _mod.apply({}, self.TOKEN)
        self.assertEqual(env["LC_ALL"], "C")

    def test_an_explicit_locale_is_left_alone(self):
        env = _mod.apply({"LC_ALL": "de_DE.UTF-8"}, self.TOKEN)
        self.assertEqual(env["LC_ALL"], "de_DE.UTF-8")


if __name__ == "__main__":
    unittest.main()
