#!/usr/bin/env python3
"""Test variants of the check of a README's language against the model's judgment over the
calibration sample (rule 2 of the section "The reading rules" of ../0_README.md), without a
request to a model.

    python3 calibration_language_variants.py fetch READMES.jsonl.xz   the READMEs at their commits
    python3 calibration_language_variants.py test READMES.jsonl.xz    each check against
                                                                      language_check.csv

`fetch` sends GitHub one request per repository, with the GitHub access tokens of credentials.csv.
"""

from __future__ import annotations

import csv
import json
import lzma
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "2_classification"))
import readmes  # noqa: E402

from agentcontext import CLASSIFICATION_DIR, credentials, logs  # noqa: E402
from agentcontext.github_api import TokenPool  # noqa: E402

COMPONENT = "calibration"
DATA = CLASSIFICATION_DIR / "calibration"
# The languages that Lingua names for the READMEs that the model judged not mainly English, with
# Persian and Bulgarian, the languages of the READMEs among them that Lingua took for English.
SEEN = ["en", "zh", "ja", "fr", "ru", "pt", "de", "pl", "id", "fa", "bg"]


def fetch(out: Path) -> None:
    rows = list(csv.DictReader(open(DATA / "calibration_sample.csv", encoding="utf-8")))
    tokens = readmes.accepted_tokens(credentials.github_tokens(credentials.default_path(),
                                                               os.environ))
    client = readmes.Client(TokenPool(tokens))

    def one(row):
        r = readmes.fetch(client, row["name"], int(row["id"]), row["revision"], "")
        return {"id": int(row["id"]), "name": row["name"], "status": r.status,
                "text": r.text or ""}

    with ThreadPoolExecutor(8) as pool, lzma.open(out, "wt", encoding="utf-8") as f:
        for record in pool.map(one, rows):
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logs.info(COMPONENT, f"Fetched {len(rows):,} READMEs with {client.requests:,} requests.")


def test(source: Path) -> None:
    from lingua import IsoCode639_1, Language, LanguageDetectorBuilder
    model = {int(r["id"]): r["readme_mainly_english"] == "true"
             for r in csv.DictReader(open(DATA / "language_check.csv", encoding="utf-8"))}
    texts = {}
    for line in lzma.open(source, "rt", encoding="utf-8"):
        record = json.loads(line)
        if int(record["id"]) in model:
            texts[int(record["id"])] = (record["name"], readmes.prose(record["text"]))

    def detector_of(codes=None):
        if codes is None:
            return LanguageDetectorBuilder.from_all_languages().build()
        return LanguageDetectorBuilder.from_languages(
            *[Language.from_iso_code_639_1(getattr(IsoCode639_1, c.upper())) for c in codes]
        ).build()

    def single(detector):
        def english(text):
            found = detector.detect_language_of(text) if text else None
            return found is None or found == Language.ENGLISH
        return english

    def share(detector):
        def english(text):
            if not text:
                return True
            parts = detector.detect_multiple_languages_of(text)
            total = sum(p.end_index - p.start_index for p in parts) or 1
            return sum(p.end_index - p.start_index for p in parts
                       if p.language == Language.ENGLISH) / total >= 0.5
        return english

    everything = detector_of()
    checks = {
        "The batch classification's check":
            lambda t: readmes.english(readmes.language(t)) if t else True,
        "The check over all languages without the ASCII rule": single(everything),
        "The check over the languages seen without the ASCII rule": single(detector_of(SEEN)),
        "The check of the share of English over all languages": share(everything),
    }
    for name, english in checks.items():
        table = Counter((english(text), model[i]) for i, (_, text) in texts.items())
        logs.info(COMPONENT, f"{name} excludes {table[(False, True)]:,} English READMEs and "
                             f"passes {table[(True, False)]:,} that are not mainly English.")


if __name__ == "__main__":
    {"fetch": fetch, "test": test}[sys.argv[1]](Path(sys.argv[2]))
