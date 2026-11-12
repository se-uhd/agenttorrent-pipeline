"""
Tests that the documents cite a commit only where a record needs its hash.

Strategy:
  - The repository starts from one commit, and the private repository
    se-uhd/agenttorrent-pipeline-internal holds the history of this tree. A hash in a document
    therefore resolves to nothing here, and a reader cannot look it up. A document states the
    fact without the hash, and a record that needs the hash, such as the commit of a build or of
    a set of parameters, keeps it as a plain value and says what it names.
  - The public repository leaves out the paths that `.publicignore` lists, and a record under
    such a path is checked only where the tree contains it.
  - The test reads only the tracked files. It runs no command that needs more than the one commit
    of the checkout, and it resolves no hash.
  - The check covers only eight-character tokens in Markdown files. A token of another length can
    be evidence in another repository or the prefix of a digest, which it cannot tell from a
    commit of this repository.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_doc_commit_refs -v
"""

import re
import subprocess
import unittest
from agentcontext import REPO_ROOT


# Documents that are not this repository's prose: the fixture drawn from collected files, with
# whatever their repositories wrote, such as other repositories' hashes, and the brief, the plan
# and the prepared decisions of the detection accuracy evaluation of 2026-11-04, kept as its agents
# read them, which name commits of other repositories and dates of eight digits.
_NOT_OURS = ("4_data-analysis/testdata/", "docs/history/2026-11-detection-evaluation/")

# The records that keep a hash as a plain value, each of which says what it names.
_ALL_RECORDS = ("2_classification/classify-repos-spec.md", "2_classification/evaluate-labels-spec.md",
                "_plans/msr-data-showcase.md", "_plans/open-weight-models.md",
                "data/classification/evaluation/2026-10-28T214115Z/effort_probe_2026-11-02/0_README.md",
                "data/classification/evaluation/2026-10-28T214115Z/probe_runs_2026-10-30/0_README.md",
                "docs/history/2026-10-classification-calibration.md",
                "docs/history/2026-11-detection-evaluation.md", "docs/history/march-sample.md",
                "docs/validation/README.md")

# The paths that the public repository leaves out, one per line of `.publicignore`, a directory
# with its trailing slash.
_LEFT_OUT = tuple(line.strip() for line in
                  (REPO_ROOT / ".publicignore").read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#"))


def _left_out(name: str) -> bool:
    """Whether the public repository leaves out the path `name`."""
    return any(name == entry or (entry.endswith("/") and name.startswith(entry))
               for entry in _LEFT_OUT)


# The records of this tree: in the public repository, those that it does not leave out.
_RECORDS = tuple(name for name in _ALL_RECORDS
                 if (REPO_ROOT / name).is_file() or not _left_out(name))

# What a record says about its hashes.
_PRIVATE_REPOSITORY = "private repository `se-uhd/agenttorrent-pipeline-internal`"


def _docs() -> tuple:
    """Every tracked Markdown file outside `_NOT_OURS`, including the plans and the dated records
    under `docs/history/`."""
    listed = subprocess.run(["git", "ls-files", "-z", "--", "*.md"], capture_output=True,
                            text=True, cwd=str(REPO_ROOT))
    if listed.returncode != 0:
        return ()
    return tuple(sorted(name for name in listed.stdout.split("\0")
                        if name and not name.startswith(_NOT_OURS)))


DOCS = _docs()

# A short hash as these documents write it: eight hex characters, not part of a longer word,
# so that the pattern skips a checksum or a hex color in the text.
_HASH = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{8}(?![0-9a-zA-Z])")
# The file of a record is named for the commit of its build, which is its name and no citation.
_RECORD_FILE = re.compile(r"(?:production-build|verification|subsample-run-list)-[0-9a-f]{8}")

# Hex-looking strings in the prose that are not commits.
_NOT_COMMITS = {
    "62310815",  # the GitHub account id of github-advanced-security, in docs/heuristics.md
    "37929162",  # the GitHub account id of mergify, in docs/checks/unmatched.md
    # An eight-digit decimal number is a valid hex string, and the documents cite such numbers
    # that are not commits.
    "16899501",  # the Zenodo record of AIDev, the MSR 2026 challenge data, in TODO.md
    "19256968",  # the Zenodo record of agent-impact's data, in TODO.md
    "19375429",  # the concept record of the pipeline of AgentConfigs on Zenodo, in TODO.md and the plans
    "19375880",  # the concept record of AgentConfigs on Zenodo, in TODO.md and the plans
    "20090356",  # the record of AgentConfigs 1.0.0 on Zenodo, in CHANGELOG.md
    "24058026",  # the concept record of AgentTorrent on Zenodo, in README.md
    "24058027",  # the record of AgentTorrent 1.0.0 on Zenodo, in CHANGELOG.md
    "24058028",  # the concept record of the pipeline of AgentTorrent on Zenodo, in README.md
    "20261001",  # the date in the Docker volume's name for the check of the complete handover
    "20250929",  # the date in the model name claude-sonnet-4-5@20250929, in docs/commits.md
    # The start of the address of the shared conversation that revised the batch prompt, in
    # docs/history/2026-03-classification-prompt-iterations.md.
    "696def05",
}


def _hashes(name: str) -> set:
    """The tokens of a document that have the form of a short hash and are no known other value."""
    text = _RECORD_FILE.sub("", (REPO_ROOT / name).read_text(encoding="utf-8"))
    return set(_HASH.findall(text)) - _NOT_COMMITS


class TestTheDocumentsCiteNoCommitOutsideTheRecords(unittest.TestCase):
    """A hash in a document names a commit that a reader of this repository cannot look up."""

    def test_a_document_outside_the_records_has_no_hash(self):
        cited = {name: sorted(_hashes(name)) for name in DOCS
                 if name not in _RECORDS and (REPO_ROOT / name).is_file()}
        self.assertEqual({name: found for name, found in cited.items() if found}, {},
                         "these documents cite a commit: state the fact without the hash, or "
                         "keep the hash in a record that says what it names")

    def test_every_record_says_what_its_hashes_name(self):
        for name in _RECORDS:
            with self.subTest(name=name):
                self.assertIn(_PRIVATE_REPOSITORY, (REPO_ROOT / name).read_text(encoding="utf-8"))

    def test_the_records_are_tracked_documents(self):
        for name in _RECORDS:
            with self.subTest(name=name):
                self.assertIn(name, DOCS)

    def test_the_check_is_not_vacuous(self):
        """A hash in a record is what the check keeps apart from the other documents."""
        self.assertTrue(_hashes("docs/validation/README.md"))
        self.assertIn("docs/deploy.md", DOCS)

    def test_the_file_of_a_record_is_no_citation(self):
        self.assertEqual(_HASH.findall(_RECORD_FILE.sub("", "production-build-22fab80f.json")), [])


if __name__ == "__main__":
    unittest.main()
