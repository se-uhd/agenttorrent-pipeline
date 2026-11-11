"""
Tests for agentcontext/credentials.py

Strategy:
  - Every file is written in a temporary directory, and every environment is a dict that the
    test builds. No test reads a developer's credentials.csv or exported variables.
  - The values are invented strings in the format of each service. The tests that check that a
    problem never contains the text of a credential compare the problem with these values.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_credentials -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from agentcontext import REPO_ROOT, credentials
from agentcontext.credentials import Credential

HEADER = "service,account,credential\n"
TOKEN_A, TOKEN_B = "github_pat_11AAAA_first", "ghp_second"
KEY = "sk-svcacct-abc_DEF-123"
ROUTER_KEY = "sk-or-v1-0123abcd_EF-89"


class _FileCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "credentials.csv"


class TestRead(_FileCase):

    def test_rows_of_both_services_with_comments_and_blank_lines_skipped(self):
        self.path.write_text(HEADER + "\n"
                             f"github,first-account,{TOKEN_A}\n#github,x,ghp_commented\n"
                             f"github,,{TOKEN_B}\nopenai,se-uhd,{KEY}\n")
        found, problems = credentials.read(self.path)
        self.assertEqual(problems, [])
        self.assertEqual(found, [Credential("github", "first-account", TOKEN_A),
                                 Credential("github", "", TOKEN_B),
                                 Credential("openai", "se-uhd", KEY)])

    def test_no_path_and_no_file_are_no_credentials_and_no_problem(self):
        self.assertEqual(credentials.read(None), ([], []))
        self.assertEqual(credentials.read(self.tmp / "absent.csv"), ([], []))

    def test_a_byte_order_mark_and_crlf_do_not_spoil_the_header(self):
        # An editor on Windows writes both, and str.strip does not treat the mark as whitespace.
        self.path.write_bytes(("﻿" + HEADER + f"github,a,{TOKEN_A}\n").replace("\n", "\r\n")
                              .encode("utf-8"))
        self.assertEqual(credentials.read(self.path), ([Credential("github", "a", TOKEN_A)], []))

    def test_a_file_without_the_header_is_refused(self):
        self.path.write_text(f"{TOKEN_A}\n{TOKEN_B}\n")
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [])
        self.assertEqual(problems, [f"{self.path} line 1 is not the header service,account,credential, "
                                    f"which has to be the first line of the file"])

    def test_a_file_that_begins_with_a_comment_is_refused(self):
        """The header is the first line, so that the file has one layout. A file written from an
        earlier example begins with comment lines, and the problem names the line to delete."""
        self.path.write_text("# the operator's credentials\n" + HEADER + f"github,a,{TOKEN_A}\n")
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [])
        self.assertEqual(problems, [f"{self.path} line 1 is not the header service,account,credential, "
                                    f"which has to be the first line of the file"])

    def test_the_example_of_the_bundle_begins_with_the_header_and_holds_no_credential(self):
        example = REPO_ROOT / "deploy" / "credentials.csv.example"
        self.assertEqual(example.read_text(encoding="utf-8").splitlines()[0], HEADER.strip())
        self.assertEqual(credentials.read(example), ([], []))

    def test_a_row_that_is_not_a_credential_is_named_without_its_text(self):
        # The text of a line with a GitHub access token and a comment contains the GitHub access
        # token, and the operator exports and sends the log.
        self.path.write_text(HEADER + f"github,a,{TOKEN_A} # my main account\n"
                             f"github,a,{TOKEN_B}\ngitlab,a,glpat_x\nopenai,a,{TOKEN_A}\n"
                             f"github,a\n")
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [Credential("github", "a", TOKEN_B)])
        self.assertEqual(len(problems), 4)
        self.assertNotIn(TOKEN_A, " ".join(problems))
        self.assertIn("line 2 is not a GitHub access token", problems[0])
        self.assertIn("line 4 names a service other than github, openai or openrouter",
                      problems[1])
        self.assertIn("line 5 is not an OpenAI API key", problems[2])
        self.assertIn("line 6 has 2 fields, not 3", problems[3])

    def test_a_row_with_its_columns_swapped_is_named_without_the_credential(self):
        """The runner logs every problem with the file at every Start, and the handover packs
        the logs."""
        self.path.write_text(HEADER + f"{TOKEN_A},first-account,github\n{KEY},a,openai\n")
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [])
        self.assertNotIn(TOKEN_A, " ".join(problems))
        self.assertNotIn(KEY, " ".join(problems))
        self.assertEqual(problems,
                         [f"{self.path} line {n} names a service other than github, openai or "
                          f"openrouter" for n in (2, 3)])

    def test_an_openrouter_api_key_is_read_and_is_no_key_of_another_service(self):
        """An OpenRouter API key starts with sk-or-, which also has the form of an OpenAI API
        key. In a row of `openai`, OpenAI would reject it at the first request of a run."""
        self.path.write_text(HEADER + f"openrouter,se-uhd,{ROUTER_KEY}\nopenai,a,{ROUTER_KEY}\n"
                             f"openrouter,a,{KEY}\n")
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [Credential("openrouter", "se-uhd", ROUTER_KEY)])
        self.assertNotIn(ROUTER_KEY, " ".join(problems))
        self.assertNotIn(KEY, " ".join(problems))
        self.assertIn("line 3 is not an OpenAI API key", problems[0])
        self.assertIn("line 4 is not an OpenRouter API key", problems[1])

    def test_another_encoding_is_a_problem_not_a_traceback(self):
        self.path.write_bytes((HEADER + f"github,a,{TOKEN_A}\n").encode("utf-16"))
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [])
        self.assertTrue(problems)

    def test_a_directory_is_named_as_what_docker_made_of_a_missing_file(self):
        self.path.mkdir()
        found, problems = credentials.read(self.path)
        self.assertEqual(found, [])
        self.assertIn("is a directory, not a file. Docker created it", problems[0])

    def test_a_credential_has_no_value_in_its_repr_or_label(self):
        credential = Credential("github", "a", TOKEN_A)
        self.assertNotIn(TOKEN_A, repr(credential))
        self.assertEqual(credential.label(), "github credential of a")


class TestTheEnvironmentAndTheFile(_FileCase):

    def setUp(self):
        super().setUp()
        self.path.write_text(HEADER + f"github,file-account,{TOKEN_B}\ngithub,file-account,{TOKEN_A}\n"
                             f"openai,se-uhd,{KEY}\n")

    def test_the_environment_comes_first_and_nothing_repeats(self):
        env = {"GITHUB_TOKENS": f" {TOKEN_A},,ghp_env", "GITHUB_TOKEN": "ghp_single"}
        found = credentials.github_tokens(self.path, env)
        self.assertEqual([c.value for c in found], [TOKEN_A, "ghp_env", "ghp_single", TOKEN_B])
        self.assertEqual([c.account for c in found[1:]], ["", "", "file-account"])

    def test_a_github_access_token_in_both_keeps_the_account_the_file_names(self):
        """The runner's pre-checks set GITHUB_TOKEN to a GitHub access token of the file,
        and the later steps inherit the variable. Without its account, that GitHub access token
        would not be paused with the other GitHub access tokens of its account."""
        found = credentials.github_tokens(self.path, {"GITHUB_TOKEN": TOKEN_A})
        self.assertEqual([(c.value, c.account) for c in found],
                         [(TOKEN_A, "file-account"), (TOKEN_B, "file-account")])

    def test_a_value_of_the_environment_without_its_services_form_is_named_by_position(self):
        """Sent to GitHub, a value with a line break makes the `requests` library refuse the
        header and quote the value in its error. The GitHub query logs that error for every
        batch."""
        env = {"GITHUB_TOKENS": f"{TOKEN_A}\n{TOKEN_B}", "GITHUB_TOKEN": f"ghp_single,{TOKEN_B} x",
               "OPENAI_API_KEY": TOKEN_A}
        self.assertEqual([c.value for c in credentials.github_tokens(None, env)], ["ghp_single"])
        self.assertEqual(credentials.openai_keys(None, env), [])
        problems = credentials.environment_problems(env)
        for value in (TOKEN_A, TOKEN_B):
            self.assertNotIn(value, " ".join(problems))
        self.assertEqual(problems, [
            "Value 1 of 1 in GITHUB_TOKENS does not have the form of a GitHub access token and "
            "is not used. Separate several values with commas.",
            "Value 2 of 2 in GITHUB_TOKEN does not have the form of a GitHub access token and "
            "is not used. Separate several values with commas.",
            "Value 1 of 1 in OPENAI_API_KEY does not have the form of an OpenAI API key and is "
            "not used. Separate several values with commas."])
        self.assertEqual(credentials.environment_problems(
            {"GITHUB_TOKENS": f" {TOKEN_A},,{TOKEN_B} ", "OPENAI_API_KEY": KEY}), [])

    def test_a_service_reads_only_its_own_rows(self):
        self.assertEqual([c.value for c in credentials.openai_keys(self.path, {})], [KEY])
        self.assertEqual([c.value for c in credentials.openai_keys(None, {"OPENAI_API_KEY": "sk-env"})],
                         ["sk-env"])
        self.assertEqual(credentials.openrouter_keys(self.path, {}), [])

    def test_the_openrouter_api_key_of_the_environment_comes_before_the_file(self):
        self.path.write_text(HEADER + f"openrouter,se-uhd,{ROUTER_KEY}\nopenai,se-uhd,{KEY}\n")
        env = {"OPENROUTER_API_KEY": "sk-or-v1-env", "OPENAI_API_KEY": "sk-env"}
        self.assertEqual([(c.value, c.account) for c in credentials.openrouter_keys(self.path, env)],
                         [("sk-or-v1-env", ""), (ROUTER_KEY, "se-uhd")])
        self.assertEqual(credentials.SERVICE_OF["OPENROUTER_API_KEY"], "openrouter")

    def test_a_value_of_openrouter_api_key_without_its_form_is_named_by_position(self):
        env = {"OPENROUTER_API_KEY": f"{ROUTER_KEY},{KEY}"}
        self.assertEqual([c.value for c in credentials.openrouter_keys(None, env)], [ROUTER_KEY])
        problems = credentials.environment_problems(env)
        self.assertNotIn(KEY, " ".join(problems))
        self.assertEqual(problems, [
            "Value 2 of 2 in OPENROUTER_API_KEY does not have the form of an OpenRouter API key "
            "and is not used. Separate several values with commas."])

    def test_without_a_path_the_file_is_not_read(self):
        self.assertEqual(credentials.github_tokens(None, {}), [])

    def test_the_default_path_is_the_repository_root_or_the_variable(self):
        self.assertEqual(credentials.default_path({}), REPO_ROOT / "credentials.csv")
        self.assertEqual(credentials.default_path({"AGENTCONTEXT_CREDENTIALS": "/app/credentials.csv"}),
                         Path("/app/credentials.csv"))


class TestTheFileIsNeverCommitted(unittest.TestCase):

    def test_credentials_csv_is_ignored_wherever_it_is(self):
        import subprocess
        for place in ("credentials.csv", "deploy/credentials.csv", "1_sampling/credentials.csv"):
            with self.subTest(place=place):
                ignored = subprocess.run(["git", "check-ignore", "-q", place], cwd=REPO_ROOT)
                self.assertEqual(ignored.returncode, 0, f"{place} is not gitignored")


if __name__ == "__main__":
    unittest.main()
