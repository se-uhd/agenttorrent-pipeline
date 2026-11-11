#!/usr/bin/env python3
"""Summarize the measurements of calibration_measure.py in data/classification/calibration/: their
distributions, and the share of the calibration sample that a candidate limit would cut, with its
95 % Wilson interval (the section "The reading rules" of ../0_README.md). The script writes no file
and logs the summary.

    python3 calibration_analyze.py
"""

from __future__ import annotations

import json
import lzma
import math
import re
import statistics
import sys
from collections import Counter
from urllib.parse import urlsplit

from agentcontext import CLASSIFICATION_DIR, logs

COMPONENT = "calibration"
DATA = CLASSIFICATION_DIR / "calibration"


def rows(phase: str) -> list[dict]:
    """The results of a phase: <phase>.jsonl while calibration_measure.py writes it, else the
    tracked <phase>.jsonl.xz."""
    path = DATA / f"{phase}.jsonl"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    elif path.with_suffix(".jsonl.xz").is_file():
        text = lzma.open(path.with_suffix(".jsonl.xz"), "rt", encoding="utf-8").read()
    else:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


def report(*parts: object) -> None:
    """Log the parts, joined by spaces, one line of the log per line of the text."""
    for line in " ".join(str(part) for part in parts).splitlines():
        if line.strip():
            logs.info(COMPONENT, line)


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """The 95 % Wilson interval of the share k of n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def percentiles(values: list, points: tuple = (50, 75, 90, 95, 99, 99.5, 100)) -> str:
    """The values at the percentiles, with their count and mean."""
    if not values:
        return "(none)"
    ordered = sorted(values)
    out = []
    for p in points:
        index = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
        out.append(f"p{p:g}={ordered[index]:,}")
    return ", ".join(out) + f" (n={len(ordered):,}, mean={statistics.fmean(ordered):,.0f})"


def over(values: list, limits: tuple) -> str:
    """For each limit, the share of the values above it with its interval."""
    n = len(values)
    lines = []
    for limit in limits:
        k = sum(1 for v in values if v > limit)
        low, high = wilson(k, n)
        lines.append(f"    Over {limit:>9,}, {k:>5,} of {n:,} = {k / n:6.2%} "
                     f"[{low:.2%}, {high:.2%}]")
    return "\n".join(lines)


def link_kind(url: str) -> str:
    """The kind of page that a link of a README leads to."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|bmp)$", path) or "badge" in path or \
            host in ("img.shields.io", "shields.io", "badge.fury.io", "badgen.net"):
        return "image or badge"
    if host == "raw.githubusercontent.com":
        return "file of a repository (raw)"
    if host in ("github.com", "www.github.com"):
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) >= 3 and segments[2] in ("blob", "tree"):
            return "github.com file or directory page"
        if len(segments) >= 3:
            return f"github.com {segments[2]} page"
        return "github.com repository or account page"
    if host.endswith("github.io"):
        return "github.io site"
    if host.endswith("readthedocs.io") or host.startswith("docs.") or "/docs" in path:
        return "documentation site"
    if host in ("discord.gg", "discord.com", "twitter.com", "x.com", "t.me", "gitter.im",
                "join.slack.com", "www.linkedin.com", "www.youtube.com", "youtube.com",
                "www.reddit.com", "reddit.com", "matrix.to"):
        return "chat, social or video"
    if host in ("pypi.org", "www.npmjs.com", "npmjs.com", "crates.io", "pkg.go.dev",
                "mvnrepository.com", "search.maven.org", "www.nuget.org", "rubygems.org",
                "packagist.org", "hub.docker.com", "anaconda.org", "godoc.org",
                "marketplace.visualstudio.com"):
        return "package index"
    return "other site"


def main() -> int:
    """Log the summary of every phase that has results."""
    readmes = rows("readmes")
    report(f"Measured {len(readmes):,} READMEs.")
    statuses = Counter(r.get("status") or ("error " + r.get("error", "")[:40]) for r in readmes)
    report("  The READMEs by status are", str(dict(statuses)) + ".")
    found = [r for r in readmes if r.get("chars") is not None]
    report("  The READMEs with text by usability are",
           str(dict(Counter(r.get("unusable") or "usable" for r in found))) + ".")
    usable = [r for r in found if not r.get("unusable")]
    chars = [r["chars"] for r in usable]
    report("  The characters of a usable README have the percentiles", percentiles(chars) + ".")
    report(over(chars, (25_000, 50_000, 100_000, 200_000, 400_000)))
    links = [len(r.get("links") or []) for r in usable]
    report("  The links per usable README have the percentiles", percentiles(links) + ".")

    pages = rows("pages")
    if pages:
        by_url = {p["url"]: p for p in pages}
        report(f"\nFetched {len(pages):,} distinct links.")
        kinds = Counter()
        outcomes = Counter()
        for url, page in by_url.items():
            kinds[link_kind(url)] += 1
            outcomes[(link_kind(url), page.get("outcome"))] += 1
        for kind, count in kinds.most_common():
            detail = {o: c for (k, o), c in outcomes.items() if k == kind}
            report(f"  {kind:<40} {count:>7,}  {detail}")
        # github.com pages are read through GitHub's API instead (the github phase).
        read = [p for p in pages if p.get("outcome") == "read"
                and (urlsplit(p["url"]).hostname or "") not in ("github.com", "www.github.com")]
        text = [p["text_chars"] for p in read]
        report("  The characters of a page read have the percentiles", percentiles(text) + ".")
        report(over(text, (5_000, 10_000, 20_000, 50_000, 100_000)))
        for kind in kinds:
            values = [p["text_chars"] for p in read if link_kind(p["url"]) == kind]
            if values:
                report(f"    {kind:<40} {percentiles(values, (50, 90, 99))}")
        refreshed = sum(1 for p in pages if p.get("meta_refreshes"))
        cut = sum(1 for p in pages if p.get("bytes_cut"))
        report(f"  Followed a meta refresh on {refreshed:,} pages.")
        report(f"  The measurement's read limit cut {cut:,} pages.")
        report("  The redirects per page have the percentiles",
               percentiles([len(p.get("chain") or []) - 1 for p in pages if p.get("chain")],
                           (50, 99, 100)) + ".")
        report("  The seconds per page have the percentiles",
               percentiles([round(p.get("seconds", 0)) for p in pages], (50, 90, 99, 100)) + ".")

    github = rows("github")
    if github:
        report(f"\nMeasured {len(github):,} github.com links through GitHub.")
        kinds = Counter(g.get("kind") for g in github)
        for kind, count in kinds.most_common(16):
            items = [g for g in github if g.get("kind") == kind]
            outcomes = Counter(g.get("outcome") for g in items)
            text = [g["text_chars"] for g in items if g.get("outcome") == "read"
                    and g.get("text_chars") is not None]
            report(f"  {kind:<14} {count:>6,}  {dict(outcomes)}"
                  + (f"  chars {percentiles(text, (50, 90, 99, 100))}" if text else ""))
        read = [g["text_chars"] for g in github if g.get("outcome") == "read"
                and g.get("text_chars") is not None]
        report("  The characters of everything read have the percentiles", percentiles(read) + ".")
        report(over(read, (5_000, 10_000, 20_000, 50_000, 100_000)))

    trees = rows("trees")
    if trees:
        ok = [t for t in trees if t.get("tree_status") == 200]
        report(f"\nRequested {len(trees):,} trees, of which GitHub returned {len(ok):,} and "
               f"truncated {sum(1 for t in ok if t.get('truncated')):,}.")
        report("  The entries per tree have the percentiles",
               percentiles([t["entries"] for t in ok]) + ".")
        report("  The files per tree have the percentiles",
               percentiles([t["blobs"] for t in ok]) + ".")
        report("  The depth of a tree has the percentiles",
               percentiles([t["max_depth"] for t in ok]) + ".")
        report("  The languages per repository in GitHub's API have the percentiles",
               percentiles([len(t.get("languages") or {}) for t in ok]) + ".")

    linguist = rows("linguist")
    if linguist:
        ok = [r for r in linguist if "rendered_chars" in r]
        report(f"\nTried Linguist on {len(linguist):,} repositories and analyzed {len(ok):,}, "
               f"with {sum(1 for r in linguist if r.get('clone_failure')):,} clone failures and "
               f"{sum(1 for r in linguist if r.get('linguist_failure')):,} Linguist failures.")
        report("  The rendered characters of the current presentation have the percentiles",
               percentiles([r["rendered_chars"] for r in ok]) + ".")
        report("  The seconds per clone have the percentiles",
               percentiles([round(r["clone_seconds"]) for r in linguist
                            if "clone_seconds" in r]) + ".")
        report("  The seconds per Linguist analysis have the percentiles",
               percentiles([round(r["linguist_seconds"]) for r in ok]) + ".")
        report("  The MB per checkout have the percentiles",
               percentiles([round((r.get("checkout_kb") or 0) / 1024) for r in ok]) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
