#!/usr/bin/env python3
"""Write the tree of a commit as the tree of the public repository se-uhd/agenttorrent-pipeline.

The export leaves out every path that `.publicignore` of the commit lists, and it removes from each
Markdown document the blocks between a line `<!--internal-->` and a line `<!--/internal-->`, with
both lines. A document of the public tree may name a left-out path only in such a block, so no
document of the public tree links a file that the tree lacks.

Usage, from the checkout: python3 dev/export-public.py <empty or new directory> [<commit>]

The commit defaults to HEAD of the repository of the working directory.

Exit 0 when the export is written, and 1 when the directory is not empty, the commit does not
exist or has no `.publicignore`, a block is not closed, or a link to a left-out path stands
outside a block.
"""

import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentcontext import logs  # noqa: E402

COMPONENT = "public_export"
IGNORE_FILE = ".publicignore"
# The lines that open and close a block that only the private repository keeps.
OPEN, CLOSE = "<!--internal-->", "<!--/internal-->"
# An inline Markdown link: the text and the target, which can end in an anchor.
LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def left_out_paths(text: str) -> tuple:
    """The entries of `.publicignore`: one path per line, a directory with its trailing slash."""
    return tuple(line.strip() for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith("#"))


def is_left_out(path: str, entries: tuple) -> bool:
    """Whether the repository-relative `path` is an entry or lies in a directory entry."""
    return any(path == entry.rstrip("/") or (entry.endswith("/") and path.startswith(entry))
               for entry in entries)


def link_target(document: str, target: str) -> str | None:
    """The repository-relative path that a link of `document` points to, or None for a link to
    another site or to an anchor of the same document."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
        return None
    path = target.split("#", 1)[0]
    parts = []
    for part in (Path(document).parent / path).as_posix().split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def without_internal_blocks(text: str) -> tuple[str, int, list]:
    """`text` without its internal blocks, the number of blocks removed, and the problems of its
    markers: a block opened inside a block, a block not closed, or a close without an open. A
    block between two blank lines leaves one of them."""
    kept, blocks, problems, opened, closed = [], 0, [], None, False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        marker = line.strip()
        if marker == OPEN:
            if opened is not None:
                problems.append(f"line {number} opens a block inside the block of line {opened}")
            opened = number
        elif marker == CLOSE:
            if opened is None:
                problems.append(f"line {number} closes no block")
            else:
                blocks += 1
            opened, closed = None, True
        elif opened is None:
            if not (closed and not marker and (not kept or not kept[-1].strip())):
                kept.append(line)
            closed = False
    if opened is not None:
        problems.append(f"the block of line {opened} is not closed")
    return "".join(kept), blocks, problems


def links_to_left_out_paths(document: str, text: str, entries: tuple) -> list:
    """The targets of the links of `text` that name a left-out path: inline links, reference
    definitions and HTML links."""
    targets = [target for _, target in LINK.findall(text)]
    targets += re.findall(r"^\s*\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", text, re.M)
    targets += re.findall(r"""href=["']([^"']+)["']""", text)
    return [target for target in targets
            if (path := link_target(document, target)) is not None and is_left_out(path, entries)]


def public_text(document: str, text: str, entries: tuple) -> tuple[str, int, list]:
    """The text of `document` in the public tree, the number of internal blocks removed, and the
    problems that stop the export."""
    kept, blocks, problems = without_internal_blocks(text)
    problems += [f"links {target}" for target in links_to_left_out_paths(document, kept, entries)]
    return kept, blocks, [f"{document}: {problem}" for problem in problems]


def export(target: Path, commit: str = "HEAD") -> int:
    """Write the public tree of `commit` into `target` and return the exit status."""
    if target.exists() and any(target.iterdir()):
        logs.error(COMPONENT, f"{target} is not empty. Name a new or empty directory.")
        return 1
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        logs.error(COMPONENT, "The working directory is in no git repository.")
        return 1
    archive = subprocess.run(["git", "archive", commit], cwd=top.stdout.strip(),
                             capture_output=True)
    if archive.returncode != 0:
        logs.error(COMPONENT, f"git archive {commit} failed "
                              f"({archive.stderr.decode(errors='replace').strip()}).")
        return 1
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        tar.extractall(target, filter="data")
    ignore = target / IGNORE_FILE
    if not ignore.is_file():
        logs.error(COMPONENT, f"{commit} has no {IGNORE_FILE}.")
        return 1
    entries = left_out_paths(ignore.read_text(encoding="utf-8"))
    removed = 0
    for entry in entries:
        path = target / entry.rstrip("/")
        if path.is_dir() and not path.is_symlink():
            removed += sum(1 for item in path.rglob("*") if not item.is_dir())
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
            removed += 1
    blocks, problems = 0, []
    for path in sorted(target.rglob("*.md")):
        rel = path.relative_to(target).as_posix()
        text = path.read_text(encoding="utf-8")
        kept, count, found = public_text(rel, text, entries)
        if count:
            path.write_text(kept, encoding="utf-8")
            blocks += count
        problems.extend(found)
    if problems:
        logs.error(COMPONENT, f"{len(problems):,} problems stop the export "
                              f"({'; '.join(problems)}).")
        return 1
    logs.info(COMPONENT, f"Wrote the public tree of {commit} to {target}, which left out "
                         f"{removed:,} files under {len(entries):,} entries of {IGNORE_FILE} and "
                         f"removed {blocks:,} internal blocks.")
    return 0


def main(argv: list) -> int:
    """Parse the arguments and run the export."""
    if len(argv) not in (1, 2) or argv[0] in ("-h", "--help"):
        logs.info(COMPONENT, __doc__.strip().splitlines()[7])
        return 0 if argv and argv[0] in ("-h", "--help") else 1
    return export(Path(argv[0]).resolve(), argv[1] if len(argv) == 2 else "HEAD")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
