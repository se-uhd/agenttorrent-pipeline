#!/usr/bin/env python3
"""Measure what the classification reads, over one random sample of the sampling frame, without
calling a model: the README, every page that the README links to, and the file tree from
GitHub's API. The limits of the classification are set from these measurements
(the section "The reading rules" of ../0_README.md).

    python3 calibration_measure.py draw --size N --seed S
    python3 calibration_measure.py readmes | trees | pages | github

Each phase appends one JSON line per item to <phase>.jsonl in data/classification/calibration/
and skips the items that are already there, so that a stopped phase resumes. The fetched READMEs,
trees and page texts are kept compressed and untracked under raw/ in that directory, and the
tracked results are the <phase>.jsonl.xz files. The sample of 2026-10-06 was drawn from the
sampling frame filtered from retrieval 2026-10-05T101430Z. `linguist.jsonl.xz` records Linguist's
results over a clone of each repository. The outline of rule 11 takes the place of Linguist in the
classification, and this script has no phase that measures Linguist.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import hashlib
import importlib
import json
import lzma
import random
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "2_classification"))
import readmes  # noqa: E402
import classification_tools as tools  # noqa: E402
classify = importlib.import_module("1_classify_repos")

import requests  # noqa: E402
from agentcontext import CLASSIFICATION_DIR, SAMPLING_DIR, credentials, logs  # noqa: E402
from agentcontext.github_api import TokenPool  # noqa: E402

COMPONENT = "calibration"
DATA = CLASSIFICATION_DIR / "calibration"
FRAME = SAMPLING_DIR / "ghs_filtered.json"
SAMPLE = DATA / "calibration_sample.csv"
RAW = DATA / "raw"
lock = threading.Lock()

# The measurement reads past the stage's limits so that the limits can be set from its results.
PAGE_BYTES = 20_000_000
PAGE_TIMEOUT = (15, 60)
REDIRECTS = 10
META_REFRESH = re.compile(rb"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']?\s*\d*\s*;?\s*url\s*=\s*['"]?([^"'>\s]+)""",
                          re.I)


def append(phase: str, record: dict) -> None:
    with lock, open(DATA / f"{phase}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def done(phase: str, key: str = "id") -> set:
    path = DATA / f"{phase}.jsonl"
    if not path.is_file():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            keys.add(json.loads(line)[key])
        except (ValueError, KeyError):
            continue
    return keys


def sample() -> list[dict]:
    with open(SAMPLE, newline="", encoding="utf-8") as f:
        return [{"id": int(r["id"]), "name": r["name"], "revision": r["revision"]}
                for r in csv.DictReader(f)]


def github_client() -> readmes.Client:
    tokens = readmes.accepted_tokens(credentials.github_tokens(credentials.default_path()))
    pool = TokenPool(tokens)
    logs.info(COMPONENT, f"Measuring with {len(tokens):,} GitHub access token(s) of "
                         f"{pool.accounts():,} account(s).")
    return readmes.Client(pool)


def draw(args) -> None:
    data = json.loads(FRAME.read_text(encoding="utf-8"))
    items = data["items"]
    chosen = random.Random(args.seed).sample(items, args.size)
    with open(SAMPLE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["id", "name", "revision"])
        for item in sorted(chosen, key=lambda i: i["id"]):
            writer.writerow([item["id"], item["name"], item.get("lastCommitSHA") or ""])
    (DATA / "calibration_sample.json").write_text(json.dumps({
        "seed": args.seed, "size": args.size, "population": len(items),
        "retrieval_id": data["_metadata"]["input_source"]["retrieval_id"],
        "drawn_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2) + "\n")
    logs.info(COMPONENT, f"Drew {args.size:,} of {len(items):,} repositories with seed "
                         f"{args.seed}.")


def measure_readme(client, repository: dict) -> dict:
    readme = readmes.fetch(client, repository["name"], repository["id"], repository["revision"])
    record = {"id": repository["id"], "name": repository["name"], "status": readme.status,
              "answered_name": readme.name, "revision": readme.revision, "path": readme.path,
              "sha": readme.sha, "size": readme.size}
    if readme.text is not None:
        text = readme.text
        (RAW / "readmes").mkdir(parents=True, exist_ok=True)
        with lzma.open(RAW / "readmes" / f"{repository['id']}.md.xz", "wt", encoding="utf-8") as f:
            f.write(text)
        reason = readmes.unusable(readme)
        language = {}
        if not reason:
            language = readmes.language(text)
            if not readmes.english(language):
                reason = readmes.NOT_ENGLISH
        links = classify.readme_links(text, readme.name, readme.path)
        record.update({"chars": len(text), "prose_chars": len(readmes.prose(text)),
                       "unusable": reason, "language": language, "links": sorted(links)})
    return record


def readmes_phase(args) -> None:
    client = github_client()
    todo = [r for r in sample() if r["id"] not in done("readmes")]
    with cf.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(measure_readme, client, r): r for r in todo}
        for number, future in enumerate(cf.as_completed(futures), 1):
            repository = futures[future]
            try:
                append("readmes", future.result())
            except Exception as error:  # noqa: BLE001 - recorded, the phase goes on
                append("readmes", {"id": repository["id"], "name": repository["name"],
                                   "error": f"{type(error).__name__}: {error}"})
            if number % 100 == 0:
                logs.info(COMPONENT, f"Measured the READMEs of {number:,} of {len(todo):,} "
                                     f"repositories.")


def measure_tree(client, repository: dict) -> dict:
    record = {"id": repository["id"], "name": repository["name"]}
    tree = client.get(f"https://api.github.com/repos/{repository['name']}/git/trees/"
                      f"{repository['revision']}?recursive=1")
    record["tree_status"] = tree.status_code
    if tree.status_code == 200:
        body = tree.json()
        (RAW / "trees").mkdir(parents=True, exist_ok=True)
        with gzip.open(RAW / "trees" / f"{repository['id']}.json.gz", "wt", encoding="utf-8") as f:
            json.dump(body, f)
        entries = body.get("tree") or []
        record.update({"truncated": bool(body.get("truncated")), "entries": len(entries),
                       "blobs": sum(1 for e in entries if e.get("type") == "blob"),
                       "trees": sum(1 for e in entries if e.get("type") == "tree"),
                       "submodules": sum(1 for e in entries if e.get("type") == "commit"),
                       "max_depth": max((e["path"].count("/") + 1 for e in entries), default=0),
                       "bytes": sum(int(e.get("size") or 0) for e in entries)})
    languages = client.get(f"https://api.github.com/repos/{repository['name']}/languages")
    record["languages_status"] = languages.status_code
    if languages.status_code == 200:
        record["languages"] = languages.json()
    return record


def trees_phase(args) -> None:
    client = github_client()
    todo = [r for r in sample() if r["id"] not in done("trees")]
    with cf.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(measure_tree, client, r): r for r in todo}
        for number, future in enumerate(cf.as_completed(futures), 1):
            repository = futures[future]
            try:
                append("trees", future.result())
            except Exception as error:  # noqa: BLE001
                append("trees", {"id": repository["id"], "name": repository["name"],
                                 "error": f"{type(error).__name__}: {error}"})
            if number % 100 == 0:
                logs.info(COMPONENT, f"Measured the trees of {number:,} of {len(todo):,} "
                                     f"repositories.")


# GitHub's web pages rate-limit a client that sends requests too fast. A 429 there would count as a
# page that the stage cannot read, although the stage can read it.
HOST_LIMITS = {"github.com": threading.Semaphore(4), "raw.githubusercontent.com": threading.Semaphore(8)}


def _get(session, url: str):
    host = urlsplit(url).hostname or ""
    gate = HOST_LIMITS.get(host)
    for attempt in range(2):
        if gate:
            gate.acquire()
        try:
            response = session.get(url, timeout=PAGE_TIMEOUT, allow_redirects=False, stream=True,
                                   headers={"User-Agent": tools.USER_AGENT})
        finally:
            if gate:
                gate.release()
        wait = response.headers.get("Retry-After", "")
        if response.status_code == 429 and attempt == 0 and wait.isdigit() and int(wait) <= 120:
            response.close()
            time.sleep(int(wait) + 1)
            continue
        return response
    return response


def fetch_page(url: str) -> dict:
    """The page at `url` as the stage would read it, without its limits: every redirect and
    meta refresh followed to a public address, up to PAGE_BYTES."""
    record = {"url": url, "chain": []}
    current, started = url, time.monotonic()
    session = requests.Session()
    for _ in range(REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            record["outcome"] = "not_http"
            break
        refused = tools.public_address(parts.hostname)
        if refused:
            record.update({"outcome": "not_public", "note": refused})
            break
        try:
            response = _get(session, current)
        except requests.RequestException as error:
            record.update({"outcome": "request_failed", "note": type(error).__name__})
            break
        record["chain"].append([current, response.status_code])
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            current = urljoin(current, response.headers["Location"])
            response.close()
            continue
        content_type = response.headers.get("Content-Type", "").lower()
        record.update({"status": response.status_code, "content_type": content_type,
                       "final_url": current})
        if response.status_code != 200:
            response.close()
            record["outcome"] = "status"
            break
        body, cut = b"", False
        try:
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) >= PAGE_BYTES:
                    cut = True
                    break
        except requests.RequestException as error:
            record.update({"outcome": "read_failed", "note": type(error).__name__})
            break
        finally:
            response.close()
        record.update({"bytes": len(body), "bytes_cut": cut})
        refresh = META_REFRESH.search(body[:20000])
        if refresh and len(record["chain"]) <= REDIRECTS:
            target = urljoin(current, refresh.group(1).decode("utf-8", "replace"))
            if target != current:
                record.setdefault("meta_refreshes", []).append(target)
                current = target
                continue
        main_type = content_type.split(";")[0].strip()
        if main_type and main_type not in tools.PAGE_TYPES:
            record["outcome"] = "not_text"
            break
        text = tools.page_text(body, content_type)
        record.update({"outcome": "read", "text_chars": len(text),
                       "noscript": b"<noscript" in body[:200000].lower()})
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        (RAW / "pages").mkdir(parents=True, exist_ok=True)
        with lzma.open(RAW / "pages" / f"{key}.txt.xz", "wt", encoding="utf-8") as f:
            f.write(text)
        record["raw"] = key
        break
    else:
        record["outcome"] = "too_many_redirects"
    record["seconds"] = round(time.monotonic() - started, 2)
    return record


def pages_phase(args) -> None:
    urls = set()
    for line in (DATA / "readmes.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        urls.update(record.get("links") or [])
    todo = sorted(u for u in urls - done("pages", "url") if not is_github(u))
    logs.info(COMPONENT, f"{len(todo):,} of the {len(urls):,} distinct links of the READMEs are "
                         f"outside github.com and still to fetch.")
    with cf.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(fetch_page, url): url for url in todo}
        for number, future in enumerate(cf.as_completed(futures), 1):
            try:
                append("pages", future.result())
            except Exception as error:  # noqa: BLE001
                append("pages", {"url": futures[future], "outcome": "error",
                                 "note": f"{type(error).__name__}: {error}"})
            if number % 500 == 0:
                logs.info(COMPONENT, f"Fetched {number:,} of {len(todo):,} links.")


def is_github(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in ("github.com", "www.github.com")


def github_kind(url: str) -> tuple[str, list]:
    """What a github.com link names, and its path segments."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) <= 1:
        return "account", segments
    if len(segments) == 2:
        return "repository", segments
    return segments[2].lower(), segments


def measure_github_link(client, url: str) -> dict:
    """A github.com link read through raw.githubusercontent.com or GitHub's API: a file as its
    content, a directory as its listing, a repository as its README, releases as their list.
    Every other kind is recorded with its kind and not read."""
    kind, segments = github_kind(url)
    record = {"url": url, "kind": kind}
    owner_repo = "/".join(segments[:2]).removesuffix(".git") if len(segments) >= 2 else ""
    try:
        if kind in ("blob", "raw") and len(segments) >= 5:
            raw = f"https://raw.githubusercontent.com/{owner_repo}/{'/'.join(segments[3:])}"
            page = fetch_page(raw)
            record.update({"read_as": "raw file", "outcome": page.get("outcome"),
                           "status": page.get("status"), "text_chars": page.get("text_chars"),
                           "content_type": page.get("content_type")})
            if page.get("status") == 404:
                # A blob link to a directory, which GitHub's web page redirects to the tree.
                kind = "tree"
        if kind == "tree" and len(segments) >= 4:
            ref, path = segments[3], "/".join(segments[4:])
            answer = client.get(f"https://api.github.com/repos/{owner_repo}/contents/{path}?ref={ref}")
            record.update({"read_as": "directory listing", "status": answer.status_code})
            body = answer.json() if answer.status_code == 200 else None
            if isinstance(body, list):
                listing = "\n".join(f"{e.get('name')} ({e.get('type')}, {e.get('size', 0)} bytes)"
                                     for e in body)
                record.update({"outcome": "read", "entries": len(body),
                               "text_chars": len(listing)})
            elif isinstance(body, dict) and body.get("type") == "file":
                # A tree link to a file, which GitHub's web page redirects to the file.
                raw = client.get(f"https://api.github.com/repos/{owner_repo}/contents/{path}?ref={ref}",
                                 accept=readmes.RAW)
                record.update({"read_as": "raw file", "status": raw.status_code})
                if raw.status_code == 200:
                    record.update({"outcome": "read", "text_chars": len(raw.content.decode(
                        "utf-8", "replace"))})
                else:
                    record["outcome"] = "status"
            else:
                record["outcome"] = "status"
        if kind in ("blob", "raw", "tree"):
            pass
        elif kind == "repository":
            answer = client.get(f"https://api.github.com/repos/{owner_repo}/readme",
                                accept=readmes.RAW)
            record.update({"read_as": "README", "status": answer.status_code})
            if answer.status_code == 200:
                record.update({"outcome": "read", "text_chars": len(answer.content.decode(
                    "utf-8", "replace"))})
            else:
                record["outcome"] = "status"
        elif kind == "releases":
            answer = client.get(f"https://api.github.com/repos/{owner_repo}/releases?per_page=100")
            record.update({"read_as": "release list", "status": answer.status_code})
            if answer.status_code == 200:
                releases = answer.json()
                listing = "\n".join(f"{r.get('tag_name')} {r.get('published_at')} {r.get('name') or ''}"
                                     for r in releases)
                record.update({"outcome": "read", "releases": len(releases),
                               "text_chars": len(listing)})
            else:
                record["outcome"] = "status"
        else:
            record.update({"read_as": "", "outcome": "not_read"})
    except readmes.NoResponse as error:
        record.update({"outcome": "unanswered", "note": str(error)[:120]})
    return record


def github_phase(args) -> None:
    client = github_client()
    urls = set()
    for line in (DATA / "readmes.jsonl").read_text(encoding="utf-8").splitlines():
        urls.update(u for u in json.loads(line).get("links") or [] if is_github(u))
    todo = sorted(urls - done("github", "url"))
    logs.info(COMPONENT, f"{len(todo):,} of the {len(urls):,} github.com links of the READMEs are "
                         f"still to read.")
    with cf.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(measure_github_link, client, url): url for url in todo}
        for number, future in enumerate(cf.as_completed(futures), 1):
            try:
                append("github", future.result())
            except Exception as error:  # noqa: BLE001
                append("github", {"url": futures[future], "outcome": "error",
                                  "note": f"{type(error).__name__}: {error}"})
            if number % 1000 == 0:
                logs.info(COMPONENT, f"Read {number:,} of {len(todo):,} github.com links.")


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    drawing = commands.add_parser("draw")
    drawing.add_argument("--size", type=int, required=True)
    drawing.add_argument("--seed", type=int, required=True)
    for name in ("readmes", "trees", "pages", "github"):
        phase = commands.add_parser(name)
        phase.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    {"draw": draw, "readmes": readmes_phase, "trees": trees_phase, "pages": pages_phase,
     "github": github_phase}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
