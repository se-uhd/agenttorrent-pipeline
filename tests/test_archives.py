"""
Tests for agentcontext.archives: an extracted file against the archive that git tracks beside it.

Strategy:
  - Each test packs a small archive with the 7zz on PATH, so that the listing is the one that
    the comparison parses in a checkout, and skips without 7zz.
  - Where the archive counts as committed, the test makes a git repository in a temporary
    directory and adds the archive to its index, as a checkout tracks the archive.
  - The data root of a run and a working copy are directories outside any repository, and the
    tests over them expect the check to pass whatever lies beside the extracted file.
  - The message of a differing copy is checked for the sampling frame and the labels: It names
    the command that extracts the archive again and a section of docs/archives.md, which has to
    exist and give the command that packs that archive again.

Requirements: Python 3.14 (stdlib only), 7zz and git on PATH
Run with:  python3 -m unittest tests.test_archives -v
"""

import re
import shutil
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from agentcontext import REPO_ROOT, SEVEN_ZIP, archives

HAVE_7Z = shutil.which(SEVEN_ZIP) is not None


def pack(archive: Path, files: dict, volume_bytes: int = 0) -> None:
    """Pack `files`, a mapping of a path inside the archive to its content, into `archive`,
    split into volumes of `volume_bytes` when that is set."""
    with tempfile.TemporaryDirectory() as staging:
        for name, content in files.items():
            path = Path(staging) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        split = [f"-v{volume_bytes}b"] if volume_bytes else []
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", "-mx=0", *split, str(archive.resolve()),
                        *files], cwd=staging, check=True, capture_output=True)


def git_add(root: Path, *paths: Path) -> None:
    """Make `root` a git repository, if it is none yet, and add `paths` to its index."""
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "--", *(str(path.relative_to(root)) for path in paths)],
                   cwd=str(root), check=True, capture_output=True)


@unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
class TestTheListing(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_every_file_is_listed_with_its_size_and_crc32_and_no_directory(self):
        archive = self.tmp / "labels.7z"
        pack(archive, {"labels.csv": b"a,b\n1,2\n", "nested/record.json": b"{}\n",
                       "empty.csv": b""})
        listed = sorted(archives.listed_files(archive))
        self.assertEqual(listed, [("empty.csv", 0, 0),
                                  ("labels.csv", 8, zlib.crc32(b"a,b\n1,2\n")),
                                  ("nested/record.json", 3, zlib.crc32(b"{}\n"))])

    def test_a_split_archive_is_listed_from_its_first_volume(self):
        content = bytes(range(256)) * 64
        archive = self.tmp / "commits.csv.7z"
        pack(archive, {"commits.csv": content}, volume_bytes=4096)
        first = Path(str(archive) + ".001")
        self.assertTrue(Path(str(archive) + ".002").is_file())
        self.assertEqual(archives.listed_files(first),
                         [("commits.csv", len(content), zlib.crc32(content))])

    def test_an_archive_that_7z_cannot_read_is_named(self):
        archive = self.tmp / "broken.7z"
        archive.write_bytes(b"not an archive")
        with self.assertRaises(archives.ArchiveUnreadable) as caught:
            archives.listed_files(archive)
        self.assertIn(str(archive), str(caught.exception))


@unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
class TestTheComparison(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.copy = self.tmp / "commits.csv"
        self.archive = self.tmp / "commits.csv.7z"
        pack(self.archive, {"commits.csv": b"repo_name\nowner/repo\n"})

    def test_the_same_bytes_match(self):
        self.copy.write_bytes(b"repo_name\nowner/repo\n")
        self.assertTrue(archives.copy_matches(self.copy, self.archive))

    def test_other_bytes_of_the_same_size_differ(self):
        self.copy.write_bytes(b"repo_name\nowner/repa\n")
        self.assertFalse(archives.copy_matches(self.copy, self.archive))

    def test_another_size_differs(self):
        self.copy.write_bytes(b"repo_name\n")
        self.assertFalse(archives.copy_matches(self.copy, self.archive))

    def test_a_file_that_the_archive_lacks_differs(self):
        other = self.tmp / "other.csv"
        other.write_bytes(b"repo_name\nowner/repo\n")
        self.assertFalse(archives.copy_matches(other, self.archive))

    def test_a_file_stored_below_the_root_of_the_archive_matches_by_its_name(self):
        """An earlier commit archive stores the CSV under `3_data-collection/output/`, and the
        loader of the commit CSV finds it at any depth."""
        archive = self.tmp / "earlier.7z"
        pack(archive, {"3_data-collection/output/commits.csv": b"repo_name\nowner/repo\n"})
        self.copy.write_bytes(b"repo_name\nowner/repo\n")
        self.assertTrue(archives.copy_matches(self.copy, archive))


@unittest.skipUnless(HAVE_7Z and shutil.which("git"), "7zz or git is not on PATH")
class TestTheCheck(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.output = self.tmp / "data" / "collection" / "output"
        self.output.mkdir(parents=True)
        self.copy = self.output / "all_ai_commits.csv"
        self.archive = self.output / "all_ai_commits.csv.7z"
        pack(self.archive, {"all_ai_commits.csv": b"repo_name\nowner/new\n"})

    def test_a_copy_that_differs_from_a_tracked_archive_stops(self):
        """The case of a checkout after a pull that replaced the archive."""
        git_add(self.tmp, self.archive)
        self.copy.write_bytes(b"repo_name\nowner/old\n")
        with self.assertRaises(archives.CopyDiffers) as caught:
            archives.check_copy(self.copy, self.archive)
        message = str(caught.exception)
        self.assertIn(str(self.copy), message)
        self.assertIn(f"7zz x {self.archive} -o{self.output} -y", message)

    def test_a_copy_that_matches_a_tracked_archive_passes(self):
        git_add(self.tmp, self.archive)
        self.copy.write_bytes(b"repo_name\nowner/new\n")
        archives.check_copy(self.copy, self.archive)

    def test_a_copy_beside_a_tracked_split_archive_is_checked_against_its_volumes(self):
        content = b"repo_name\n" + b"owner/repo\n" * 1000
        split = self.output / "split.csv.7z"
        pack(split, {"split.csv": content}, volume_bytes=2048)
        volumes = sorted(self.output.glob("split.csv.7z.*"))
        self.assertGreater(len(volumes), 1)
        git_add(self.tmp, *volumes)
        copy = self.output / "split.csv"
        copy.write_bytes(content[:-1] + b"X")
        with self.assertRaises(archives.CopyDiffers):
            archives.check_copy(copy, volumes[0])
        copy.write_bytes(content)
        archives.check_copy(copy, volumes[0])

    def test_outside_a_repository_the_extracted_file_is_the_input(self):
        """The data root of a run and a working copy: A step wrote the extracted file, and an
        archive beside it, if any, is older and decides nothing."""
        self.copy.write_bytes(b"repo_name\nowner/old\n")
        archives.check_copy(self.copy, self.archive)

    def test_an_archive_that_git_does_not_track_decides_nothing(self):
        """A working copy extracted inside a checkout, where git ignores or does not know the
        archive."""
        git_add(self.tmp)
        self.copy.write_bytes(b"repo_name\nowner/old\n")
        self.assertFalse(archives.tracked(self.archive))
        archives.check_copy(self.copy, self.archive)

    def test_an_absent_file_passes(self):
        git_add(self.tmp, self.archive)
        archives.check_copy(self.copy, self.archive)
        self.copy.write_bytes(b"repo_name\nowner/old\n")
        archives.check_copy(self.copy, self.output / "absent.csv.7z")

    def test_a_tracked_archive_that_7z_cannot_read_stops(self):
        broken = self.output / "broken.csv.7z"
        broken.write_bytes(b"not an archive")
        git_add(self.tmp, broken)
        copy = self.output / "broken.csv"
        copy.write_bytes(b"repo_name\n")
        with self.assertRaises(archives.ArchiveUnreadable):
            archives.check_copy(copy, broken)


def github_slug(heading: str) -> str:
    """The anchor that GitHub gives a Markdown heading: lower case, without punctuation other
    than hyphens and underscores, and with each space replaced by a hyphen."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@unittest.skipUnless(HAVE_7Z and shutil.which("git"), "7zz or git is not on PATH")
class TestTheRemedy(unittest.TestCase):
    """The message of a copy that differs names the command that extracts the archive again and
    the section of the documentation with the command that packs that archive again."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def message(self, directory: str, archive_name: str, files: dict, copy_name: str) -> str:
        """The message of `check_copy` for the copy `copy_name`, written with other bytes than
        the file of that name in the tracked archive `archive_name`, which holds `files`."""
        stage = self.tmp / "data" / directory
        stage.mkdir(parents=True)
        archive = stage / archive_name
        pack(archive, files)
        git_add(self.tmp, archive)
        copy = stage / copy_name
        copy.write_bytes(files[copy_name] + b"newer\n")
        with self.assertRaises(archives.CopyDiffers) as caught:
            archives.check_copy(copy, archive)
        message = str(caught.exception)
        self.assertIn(f"7zz x {archive} -o{stage} -y", message)
        return message

    def packing_section(self, message: str) -> str:
        """The text of the section of docs/archives.md that `message` names."""
        found = re.search(r"docs/archives\.md#([\w-]+)", message)
        self.assertIsNotNone(found, message)
        lines = (REPO_ROOT / "docs" / "archives.md").read_text(encoding="utf-8").splitlines()
        starts = [i for i, line in enumerate(lines)
                  if line.startswith("## ") and github_slug(line[3:]) == found.group(1)]
        self.assertEqual(len(starts), 1, f"docs/archives.md has no section #{found.group(1)}")
        end = next((i for i in range(starts[0] + 1, len(lines)) if lines[i].startswith("## ")),
                   len(lines))
        return "\n".join(lines[starts[0]:end])

    def test_the_sampling_frame_is_extracted_again_or_its_archive_packed_again(self):
        message = self.message("sampling", "ghs_filtered.json.7z",
                               {"ghs_filtered.json": b'{"_metadata": {}}\n'},
                               "ghs_filtered.json")
        self.assertNotIn("engineered-project-selection", message)
        self.assertRegex(self.packing_section(message), r"7zz a [^\n]*ghs_filtered\.json\.7z")

    def test_the_labels_are_extracted_again_with_the_sample_or_their_archive_packed_again(self):
        """Extracting the labels again leaves the list of the sample that the earlier labels gave
        until the selection runs again."""
        message = self.message("classification", "repos_annotated.7z",
                               {"repos_annotated.csv": b"repo_name,engineered_project\n",
                                "repos_annotated.json": b'{"models": ["m"]}\n',
                                "repos_excluded.csv": b"repo_name,reason\n"},
                               "repos_annotated.csv")
        self.assertIn("`python3 -m agentcontext engineered-project-selection`", message)
        self.assertRegex(self.packing_section(message), r"7zz a [^\n]*repos_annotated\.7z")


if __name__ == "__main__":
    unittest.main()
