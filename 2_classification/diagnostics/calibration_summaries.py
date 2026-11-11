#!/usr/bin/env python3
"""Summarize, with a model, a random set of the linked pages that calibration_measure.py measured,
to compare a page's text with its summary as the input that the classification model would read
instead (rule 10 of the section "The reading rules" of ../0_README.md). The script calls OpenAI,
and the person who pays for the requests has to approve each run. It reads the page texts that
calibration_measure.py keeps, untracked, under raw/pages/ in data/classification/calibration/.

    python3 calibration_summaries.py --size N --seed S [--model gpt-6-luna]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import lzma
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "2_classification"))

from agentcontext import CLASSIFICATION_DIR, credentials, logs  # noqa: E402

DATA = CLASSIFICATION_DIR / "calibration"

COMPONENT = "calibration"
# A summary request stays under the 272,000 input tokens above which OpenAI charges the long
# context price (developers.openai.com/api/docs/pricing). At the 3.57 characters per token of
# rule 1, the 10th percentile over READMEs of the calibration sample, 960,000 characters of page
# text with the prompt stay under that limit for at least nine in ten texts.
MAX_INPUT_CHARS = 960_000
PROMPT = """Summarize this web page, which the README of the GitHub repository "{repository}" links \
to, for someone who decides whether the repository hosts an engineered software project. Say \
what the page is, what it states about the software, and any evidence of software engineering \
practice it gives, such as documentation, releases, tests, maintenance or users. Say if the page \
holds nothing about the software. State only what the page says. At most five sentences.

Page: {url}

{text}"""


def readable_pages() -> list[dict]:
    """Every page outside github.com that calibration_measure.py read as text, with the first
    repository that links the page from its README."""
    linked_by = {}
    for line in (DATA / "readmes.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        for url in record.get("links") or []:
            linked_by.setdefault(url, record["name"])
    pages = []
    for line in (DATA / "pages.jsonl").read_text(encoding="utf-8").splitlines():
        page = json.loads(line)
        host = page["url"].split("/")[2].lower() if "://" in page["url"] else ""
        if page.get("outcome") == "read" and page.get("raw") and host not in ("github.com",
                                                                               "www.github.com"):
            pages.append({"url": page["url"], "raw": page["raw"],
                          "repository": linked_by.get(page["url"], "")})
    return pages


async def summarize(client, model: str, page: dict, semaphore) -> dict:
    text = lzma.open(DATA / "raw" / "pages" / f"{page['raw']}.txt.xz", "rt",
                     encoding="utf-8").read()
    cut = len(text) > MAX_INPUT_CHARS
    async with semaphore:
        response = await client.responses.create(
            model=model, reasoning={"effort": "low"},
            input=PROMPT.format(repository=page["repository"], url=page["url"],
                                text=text[:MAX_INPUT_CHARS]))
    usage = response.usage
    summary = response.output_text
    return {**page, "text_chars": len(text), "cut": cut, "summary": summary,
            "summary_chars": len(summary),
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "model": response.model}


async def run(args) -> None:
    import openai
    keys = credentials.openai_keys(credentials.default_path())
    client = openai.AsyncOpenAI(api_key=keys[0].value, max_retries=6, timeout=300)
    pages = readable_pages()
    chosen = random.Random(args.seed).sample(pages, args.size)
    semaphore = asyncio.Semaphore(8)
    results = await asyncio.gather(*(summarize(client, args.model, p, semaphore) for p in chosen),
                                   return_exceptions=True)
    with open(DATA / "summaries.jsonl", "w", encoding="utf-8") as f:
        for page, result in zip(chosen, results):
            if isinstance(result, Exception):
                result = {**page, "error": f"{type(result).__name__}: {result}"}
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    logs.info(COMPONENT, f"Summarized {args.size} of {len(pages):,} readable pages.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    asyncio.run(run(parser.parse_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
