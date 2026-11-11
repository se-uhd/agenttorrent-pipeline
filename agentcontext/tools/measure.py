"""Query GitHub repository sizes for capacity checks and subsample selection.

Inputs: data/collected/repos.csv and GitHub access tokens.
Outputs: output/repo-sizes.csv and logged size percentiles.
Requirements: Python 3.14 (stdlib only).
"""

from __future__ import annotations

import csv
import io
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path

from agentcontext import caches, cli, credentials, STAGE_DIR
from agentcontext.checks import snapshot
from agentcontext import logs


# -----------------------------------------------------------------------------
# Repository sizes
# -----------------------------------------------------------------------------

SIZES_OUTPUT = STAGE_DIR / "output" / "repo-sizes.csv"
PERCENTILES = (50, 90, 99, 99.9, 100)


def percentile(sorted_values: list, p: float):
    """The value at percentile p, nearest-rank, on an already sorted list."""
    if not sorted_values:
        return None
    # `ceil`, because nearest-rank rounds up. `round(x + 0.5)` is banker's rounding and returns
    # one rank too high wherever p/100 times the count is an odd integer, such as for the median
    # of an even-sized list. The rank is computed exactly: In floats, 99.9 / 100 * 1000 is
    # 999.0000000000001, and `ceil` of that is one rank too high as well.
    rank = math.ceil(Fraction(str(p)) * len(sorted_values) / 100)
    index = max(0, min(len(sorted_values) - 1, rank - 1))
    return sorted_values[index]


def fetch_size(client, repo: str) -> dict:
    status, payload = client.get(f"/repos/{repo}")
    if status == 200 and isinstance(payload, dict):
        return {"repo_name": repo, "size_kb": payload.get("size", "")}
    return {"repo_name": repo, "size_kb": ""}


def main_sizes(argv=None) -> int:
    parser = cli.ArgumentParser(description="Write GitHub's packed size of every repository in the sample to output/repo-sizes.csv.")
    parser.add_argument("-i", "--input", type=Path, default=snapshot.DEFAULT_INPUT)
    parser.add_argument("-o", "--output", type=Path, default=SIZES_OUTPUT)
    parser.add_argument("--collected-dir", type=Path, default=snapshot.DEFAULT_COLLECTED_DIR)
    parser.add_argument("--credentials", type=Path, default=credentials.default_path(),
                        help="The credentials file with the GitHub access tokens for the "
                             "requests (default: %(default)s)")
    parser.add_argument("-w", "--workers", type=int, default=0)
    args = parser.parse_args(argv)

    tokens = snapshot.collect_tokens(args.credentials)
    if not tokens:
        logs.error("measure", f"No GitHub access token found. Add one to {args.credentials}, "
                              f"or set GITHUB_TOKENS.")
        return 1
    workers = args.workers or min(16, max(1, 4 * len(tokens)))

    rows = snapshot.load_rows(args.input)
    scoped = snapshot.in_scope(rows, args.collected_dir)
    names = [r["repo_name"] for r in scoped]
    logs.info("measure", f"Querying the sizes of {len(names):,} repositories with "
                         f"{len(tokens):,} GitHub access token(s) and {workers:,} worker(s).")

    client = snapshot.GitHubClient(snapshot.TokenRotator(tokens))
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_size, client, n) for n in names]
        for i, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if i % 500 == 0:
                logs.info("measure", f"Queried the sizes of {i:,} of {len(names):,} repositories.")

    # Replace the file in one step, so that a failure while writing leaves the previous
    # measurement.
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["repo_name", "size_kb"], lineterminator="\n")
    writer.writeheader()
    for row in sorted(results, key=lambda r: r["repo_name"]):
        writer.writerow(row)
    try:
        caches.replace_file(args.output, buffer.getvalue())
    except OSError as e:
        logs.error("measure", f"Could not write {args.output} ({e}).")
        return 1

    sizes = sorted(int(r["size_kb"]) for r in results if str(r["size_kb"]).isdigit())
    missing = len(results) - len(sizes)
    logs.info("measure", f"Read the sizes of {len(sizes):,} of {len(results):,} repositories "
                         f"({missing:,} unavailable).")
    if not sizes:
        # Every summary below divides by a size. With a revoked or absent GitHub access token,
        # GitHub returns 401 for every repository, which leaves no size to summarize.
        logs.error("measure", f"GitHub returned no size, and no summary was logged. "
                              f"{args.output} lists every repository with a blank size.")
        return 1
    logs.info("measure", f"{'percentile':>12}  {'size':>12}")
    for p in PERCENTILES:
        value = percentile(sizes, p)
        logs.info("measure", f"{p:>11}%  {value / 1024 / 1024:>9.2f} GiB")
    logs.info("measure", "The ten largest repositories by packed size.")
    by_size = sorted((r for r in results if str(r["size_kb"]).isdigit()),
                     key=lambda r: int(r["size_kb"]), reverse=True)
    for row in by_size[:10]:
        logs.info("measure", f"  {int(row['size_kb']) / 1024 / 1024:>8.2f} GiB  {row['repo_name']}")
    for limit_gib in (1, 2, 4, 8, 16):
        over = sum(1 for s in sizes if s > limit_gib * 1024 * 1024)
        logs.info("measure", f"{over:,} repositories ({over / len(sizes):.3%}) are larger than "
                             f"{limit_gib} GiB.")
    return 0


SUBCOMMANDS = {"sizes": main_sizes}
SUBCOMMAND_SUMMARY = "sizes writes output/repo-sizes.csv from the GitHub API"


def main(argv=None) -> int:
    return cli.subcommand_main("measure", SUBCOMMANDS, SUBCOMMAND_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
