"""Build repo_metadata.csv by merging classification annotations with GHS metadata.

The rq2 scripts that need per-repository metadata, the main language, age, contributors,
commits and size, joined with the engineered/non-engineered classification use this module.
The DataFrame contains every annotated row whatever its label, and `sample_names` names the
repositories in the sample, which are labeled engineered_project=true.

Inputs:
  ../../data/classification/repos_annotated.csv (extract repos_annotated.7z first)
  ../../data/sampling/ghs_filtered.json (extract ghs_filtered.json.7z first)
  Where git tracks the archive beside either file, a file that differs from it stops the script.

Output:
  ../../data/analysis/rq2/repo_metadata.csv
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import CLASSIFICATION_DIR, SAMPLING_DIR, checked_copy, output_dir

from agentcontext import logs
from agentcontext.steps.engineered_project_selection import derive_sample

# The component of this script's log messages in the pipeline's one log format,
# `LEVEL component: message`. agentcontext/logs.py and docs/logs.md define the format.
COMPONENT = "repo_metadata"

OUTPUT_DIR = output_dir(__file__)



# Every annotated row matches the GHS sampling frame, because the annotated list is derived
# from it. A join that loses most of the rows therefore means that a key stopped matching
# rather than that the sample shrank. The names are GitHub's `owner/repo` on both sides, and a
# name in another letter case on either side drops the row without a message. The share is
# checked only for a CSV large enough for the share to mean anything, because a small fixture
# with a row deliberately unmatched is a different case.
MIN_JOINED_SHARE = 0.9
MIN_ROWS_FOR_SHARE = 1000


def reference_date(ghs: dict) -> pd.Timestamp:
    """The day on which the GHS snapshot was downloaded, on which every age is measured.

    `createdAt`, `contributors`, `commits` and `size` describe the repository on that day,
    and an age joined beside them has to end there too.
    """
    downloaded_at = ghs.get("_metadata", {}).get("input_source", {}).get("downloaded_at", "")
    if not downloaded_at:
        raise SystemExit("ghs_filtered.json has no _metadata.input_source.downloaded_at, the "
                         "date at which the age of each repository is measured")
    return pd.Timestamp(downloaded_at[:10])


def labels_csv() -> Path:
    """`repos_annotated.csv`, checked against `repos_annotated.7z` beside it where git tracks that
    archive (`_shared.checked_copy`). The archive also holds the record of the model and the
    repositories in the sampling frame that are not in `repos_annotated.csv`, the copies that the
    classification labeled `true` included, and its name is that of the labels without the
    extension of the CSV."""
    return checked_copy(CLASSIFICATION_DIR / "repos_annotated.csv",
                        CLASSIFICATION_DIR / "repos_annotated.7z")


def sampling_frame_json() -> Path:
    """`ghs_filtered.json`, checked against `ghs_filtered.json.7z` beside it where git tracks
    that archive (`_shared.checked_copy`)."""
    return checked_copy(SAMPLING_DIR / "ghs_filtered.json")


def sample_names() -> set:
    """The repositories in the sample: the rows of repos_annotated.csv that the engineered
    project selection keeps, which are the rows labeled engineered_project=true.

    The selection step's own function decides which rows are in the sample, so that the
    analyses and the collection cannot disagree about it.
    """
    _, rows = derive_sample(labels_csv())
    return {row["repo_name"] for row in rows}


def build_repo_metadata() -> pd.DataFrame:
    """Merge repos_annotated.csv with ghs_filtered.json and return the result.

    Also writes repo_metadata.csv to OUTPUT_DIR.
    """
    annotated = pd.read_csv(labels_csv())

    with open(sampling_frame_json(), encoding="utf-8") as f:
        ghs = json.load(f)
    ghs_df = pd.DataFrame(ghs["items"])[["name", "mainLanguage", "createdAt", "contributors", "commits", "size"]]

    # One row per repository on each side. A name listed twice in either input would give the
    # repository two rows, and every statistic would count it twice.
    try:
        df = annotated.merge(
            ghs_df,
            left_on="repo_name",
            right_on="name",
            how="inner",
            validate="one_to_one",
        )
    except pd.errors.MergeError as e:
        raise SystemExit(f"repos_annotated.csv and ghs_filtered.json do not name each "
                         f"repository once ({e})")

    # In UTC and without the zone, as the reference date is. A sampling frame that the
    # sampling stage writes has GitHub's creation time with a `Z`, an older sampling frame has
    # GHS's time without one, and subtracting a zoned time from a time without a zone raises.
    df["createdAt"] = pd.to_datetime(df["createdAt"], utc=True).dt.tz_localize(None)
    df["repo_age_days"] = (reference_date(ghs) - df["createdAt"]).dt.days

    out = df[["repo_name", "mainLanguage", "repo_age_days", "contributors", "commits", "size"]].copy()
    out = out.rename(columns={"mainLanguage": "main_language"})

    # An inner join over two snapshots that no longer agree keeps whatever matched, and the
    # result replaces a tracked CSV.
    kept = len(out) / len(annotated) if len(annotated) else 0
    if len(annotated) >= MIN_ROWS_FOR_SHARE and kept < MIN_JOINED_SHARE:
        raise SystemExit(f"Only {len(out):,} of {len(annotated):,} rows of repos_annotated.csv "
                         f"have a repository name that ghs_filtered.json lists ({kept:.1%}, "
                         f"below {MIN_JOINED_SHARE:.0%}). Left {OUTPUT_DIR / 'repo_metadata.csv'} "
                         f"unchanged. Check that both files come from the same sampling frame.")

    # Replace the file in one step, because the target is tracked and a kill inside `to_csv`
    # would leave a truncated CSV that no check can distinguish from a short CSV.
    target = OUTPUT_DIR / "repo_metadata.csv"
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        out.to_csv(tmp, index=False)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logs.info(COMPONENT, f"Wrote repo_metadata.csv ({len(out):,} rows).")
    return out


if __name__ == "__main__":
    build_repo_metadata()
