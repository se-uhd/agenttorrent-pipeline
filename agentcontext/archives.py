"""Compare a file extracted from a 7-Zip archive with the archive beside it, where git tracks
the archive.

The repository tracks a few large inputs only as 7-Zip archives and ignores the file extracted
from each: the commit CSV `output/all_ai_commits.csv` in the volumes `all_ai_commits.csv.7z.001`
and on, `output/other_ai_commits.csv` in `other_ai_commits.csv.7z`, the sampling frame in
`sampling/ghs_filtered.json.7z` and the labels in `classification/repos_annotated.7z`. A reader
that finds the extracted file reads it and leaves the archive alone. When the archive changes
after the extraction, through a merge, a pull or a switch of branches, the extracted file is
stale, and the reader would draw its outputs from data that the commit does not contain.

Which of the two files is authoritative depends on the data root:

- In a checkout, git tracks the archive, which is the committed input. The stated statistics
  read the archive (`statistics.commits`). An extracted file that differs from it is either
  stale or newer and not yet archived, and an output drawn from it would in neither case be the
  output of the commit.
- In the data root of a run, such as the volume of the container on the NAS, and in a working
  copy, a step of the pipeline wrote the extracted file: the filter wrote the sampling frame,
  the labeling the labels and the detection step the commit CSV. No step writes these archives,
  the bundle contains no data, and a handover contains none of the archives. An archive in such
  a data root was copied there, such as an older commit archive in a working copy, and git
  tracks nothing there.

`check_copy` therefore compares the two files only where git tracks the archive. A difference
there means that the extracted file is not the committed input, and the check raises. Elsewhere
the extracted file is the input, and the check passes whatever lies beside it, so that it
cannot stop a run on the NAS or the processing of a working copy.

The comparison reads the size and the CRC32 that `7zz l -slt` lists for each file of the archive
and computes the CRC32 of the extracted file, without extracting the archive.
"""

from __future__ import annotations

import subprocess
import zlib
from pathlib import Path

from agentcontext import SEVEN_ZIP

# What `7zz l -slt` prints between the properties of the archive and those of its first file.
FILES_START = "----------"
# The size of a read in the CRC32 of a file.
CHUNK_BYTES = 1 << 20
# The section of the documentation with the command that packs each tracked archive again.
PACKING_SECTION = "docs/archives.md#packing-the-archives-again"
# The command that derives a file from an extracted input, by the name of the input, and what it
# writes. Extracting the input again leaves the file derived from the earlier input beside it
# until the command runs.
DERIVED_BY = {
    "repos_annotated.csv": ("python3 -m agentcontext engineered-project-selection",
                            "which writes the list of the sample from the labels"),
}


class ArchiveUnreadable(Exception):
    """`7zz` could not list an archive."""


class CopyDiffers(Exception):
    """A file extracted beside an archive that git tracks differs from the file of its name in
    that archive."""

    def __init__(self, copy: Path, archive: Path):
        self.copy = copy
        self.archive = archive
        then = ""
        if copy.name in DERIVED_BY:
            command, what = DERIVED_BY[copy.name]
            then = f", then run `{command}`, {what}"
        super().__init__(
            f"{copy} differs from the file of its name in {archive}, which git tracks beside "
            f"it. It was extracted from an earlier archive, or it is newer and not archived "
            f"yet. If the archive is newer, extract the file again with "
            f"`7zz x {archive} -o{archive.parent} -y`{then}. If the file is newer, pack the "
            f"archive again with the command that {PACKING_SECTION} gives for it.")


def listed_files(archive: Path) -> list:
    """`(path, size, crc32)` for each file in `archive`, with the path under which the archive
    stores it, as `7zz l -slt` lists them. A split archive is named by its first volume, and 7zz
    reads the other volumes beside it. A directory has no CRC and is left out. Raises
    `ArchiveUnreadable` when 7zz cannot start or exits non-zero."""
    try:
        result = subprocess.run([SEVEN_ZIP, "l", "-slt", str(archive)], capture_output=True,
                                text=True)
    except OSError as error:
        raise ArchiveUnreadable(f"Could not run 7zz to list {archive} ({error})") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-300:]
        raise ArchiveUnreadable(f"7zz exited {result.returncode} listing {archive} ({detail})")
    lines = result.stdout.splitlines()
    if FILES_START not in lines:
        raise ArchiveUnreadable(f"7zz listed no files in {archive}")
    files = []
    block = {}
    for line in lines[lines.index(FILES_START) + 1:] + [""]:
        key, separator, value = line.partition(" = ")
        if separator:
            block[key] = value
            continue
        if block.get("Path") is not None and "CRC" in block and not block.get(
                "Attributes", "").startswith("D"):
            # 7zz lists an empty CRC for an empty file, which has the CRC32 0.
            files.append((block["Path"], int(block.get("Size") or 0),
                          int(block["CRC"] or "0", 16)))
        block = {}
    return files


def crc32_of(path: Path) -> int:
    """The CRC32 of the file at `path`, as 7zz records it for a file in an archive."""
    crc = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_BYTES):
            crc = zlib.crc32(chunk, crc)
    return crc


def copy_matches(copy: Path, archive: Path) -> bool:
    """Whether `copy` has the size and the CRC32 of a file of its name in `archive`, at any
    depth of the archive. Raises `ArchiveUnreadable` when 7zz cannot list the archive."""
    listed = {(size, crc) for path, size, crc in listed_files(archive)
              if path.replace("\\", "/").rsplit("/", 1)[-1] == copy.name}
    size = copy.stat().st_size
    # The size decides most differences without reading the file.
    if not any(listed_size == size for listed_size, _ in listed):
        return False
    return (size, crc32_of(copy)) in listed


def tracked(path: Path) -> bool:
    """Whether git tracks `path` in the repository that contains it. Outside a repository, and
    where git cannot run, nothing is tracked."""
    try:
        result = subprocess.run(["git", "ls-files", "--error-unmatch", "--", path.name],
                                cwd=str(path.parent), capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0


def check_copy(copy: Path, archive: Path) -> None:
    """Raise `CopyDiffers` when git tracks `archive` and `copy` differs from the file of its
    name in it. Pass when either file is absent or git does not track the archive, because the
    extracted file is then the input. Raises `ArchiveUnreadable` when 7zz cannot list a tracked
    archive."""
    if not (copy.is_file() and archive.is_file() and tracked(archive)):
        return
    if not copy_matches(copy, archive):
        raise CopyDiffers(copy, archive)
