"""Shared constants and helpers for the analysis scripts.

One module for what `rq2/`, `rq3/` and `dataset/` share: the paths of the pipeline's outputs, the
tool and mechanism constants that the section "Constants" of CLAUDE.md states, the context file
categories derived from the heuristics, the plot styling, the escaping of text for LaTeX, the
check of an extracted input against the archive that git tracks beside it, the loader of the
commit CSV and the UpSet renderer. A script imports it with

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # 4_data-analysis/
    from _shared import DATA_DIR, TOOLS, output_dir

`DATA_DIR` is the collection stage's `output/` under the data root unless
`DATA_COLLECTION_OUTPUT` names another directory. `output_dir(__file__)` is the directory named
for the script's own directory under the analysis stage's data directory, unless
`ANALYSIS_OUTPUT_DIR` names a root to write under instead. Together the two variables let a script run over the fixture under
`testdata/` without changing the tracked figures.

Nothing third-party is imported at module level, so that the constants and the categories load
under a stdlib test. pandas, matplotlib and seaborn are imported inside the functions that
need them.
"""

from __future__ import annotations

import contextlib
import csv
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Mapping, Optional, Sequence, Tuple
from agentcontext import (ANALYSIS_DIR, CLASSIFICATION_DIR, REPO_ROOT, SAMPLING_DIR, SEVEN_ZIP,
                          STAGE_DIR)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_COLLECTION_DIR = STAGE_DIR
DEFAULT_DATA_DIR = DATA_COLLECTION_DIR / "output"
# The output CSVs that every analysis reads. An environment variable rather than a flag on each
# script, because most of the scripts have no command line.
DATA_DIR = Path(os.environ.get("DATA_COLLECTION_OUTPUT") or DEFAULT_DATA_DIR)
# A field of the output CSVs has no bound on its length: The detection step joins every file that
# a context file references into `referenced_file`, and a commit message can be longer than the
# default limit of the csv module, which would stop every script that reads such a field under
# that limit.
csv.field_size_limit(sys.maxsize)
VALIDATED_DIR = DATA_COLLECTION_DIR / "data" / "validated"
CONFIG_PATTERNS_CSV = REPO_ROOT / "heuristics" / "config_path_patterns.csv"


def output_dir(script_file: str | os.PathLike) -> Path:
    """Where the script writes: the directory named for its own directory, `rq2/`, `rq3/` or
    `dataset/`, under the analysis stage's data directory, or under `ANALYSIS_OUTPUT_DIR` when
    that is set. The name is kept, so that two scripts of the same name in different directories
    do not overwrite each other's files.
    """
    script = Path(script_file).resolve()
    root = os.environ.get("ANALYSIS_OUTPUT_DIR")
    target = (Path(root) if root else ANALYSIS_DIR) / script.parent.name
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# The tool and mechanism constants, as the section "Constants" of CLAUDE.md states them
# ---------------------------------------------------------------------------

TOOLS = ["claude", "copilot", "no_specific_tool", "cursor", "gemini", "codex", "opencode"]
TOOL_LABELS = {
    "claude": "Claude",
    "copilot": "Copilot",
    "no_specific_tool": "No specific tool",
    "cursor": "Cursor",
    "gemini": "Gemini",
    "codex": "Codex",
    "opencode": "OpenCode",
}

# The flag of a repository with only shared configuration artifacts, which no specific tool
# owns, such as AGENTS.md or `.agents/skills`. Tool-specific analyses exclude it, because such
# a repository has no tool-specific configuration by definition (see CLAUDE.md).
NO_SPECIFIC_TOOL = "no_specific_tool"
SPECIFIC_TOOLS = [t for t in TOOLS if t != NO_SPECIFIC_TOOL]

MECHANISMS = ["context_file", "skills", "subagents", "commands", "rules", "settings",
              "hooks", "mcp", "plugins", "lsp"]
MECHANISM_LABELS = {
    "context_file": "Context Files",
    "skills": "Skills",
    "subagents": "Subagents",
    "commands": "Commands",
    "rules": "Rules",
    "settings": "Settings",
    "hooks": "Hooks",
    "mcp": "MCP",
    "plugins": "Plugins",
    "lsp": "LSP",
}

# The mechanism CSV of each mechanism under `output/`. The release publishes the context
# files as `context_files.csv` and the others under these names.
MECHANISM_CSV_NAMES = {
    "context_file": "context-files.csv", "skills": "skills.csv",
    "subagents": "subagents.csv", "commands": "commands.csv", "rules": "rules.csv",
    "settings": "settings.csv", "hooks": "hooks.csv", "mcp": "mcp.csv",
    "plugins": "plugins.csv", "lsp": "lsp.csv",
}


def artifact_tools(value: str) -> set:
    """The tools that a value of a mechanism CSV's `tool` column names: none for `any`, a file
    that no specific tool owns, and several for a value joined with `|`."""
    return set(str(value).split("|")) & set(SPECIFIC_TOOLS)


def mechanism_tool_counts(repos: "pd.DataFrame",  # noqa: F821
                          dataframes: Mapping[str, "pd.DataFrame"]) -> Tuple[dict, dict]:
    """The cells and the column totals of the mechanism-by-tool heatmap.

    A cell `(mechanism, tool)` counts the repositories in `repos` with a
    configuration artifact of the mechanism attributed to the tool, read off the `tool` column
    of `dataframes[mechanism]`, the mechanism CSV. The `no_specific_tool` column counts the
    repositories with that flag that have the mechanism, and their shared files appear once,
    in that column. A total is the number of repositories in `repos` with the tool's flag.
    `repos` has `repo_name` and the seven flags of `TOOLS` as booleans.
    """
    sample = set(repos["repo_name"])
    shared_only = set(repos.loc[repos[NO_SPECIFIC_TOOL], "repo_name"])
    columns = SPECIFIC_TOOLS + [NO_SPECIFIC_TOOL]
    counts = {}
    for mechanism in MECHANISMS:
        dataframe = dataframes[mechanism]
        found = {tool: set() for tool in columns}
        for repo, value in zip(dataframe["repo_name"], dataframe["tool"]):
            if repo not in sample:
                continue
            for tool in artifact_tools(value):
                found[tool].add(repo)
            if repo in shared_only:
                found[NO_SPECIFIC_TOOL].add(repo)
        for tool in columns:
            counts[(mechanism, tool)] = len(found[tool])
    totals = {tool: int(repos[tool].sum()) for tool in columns}
    return counts, totals


# ---------------------------------------------------------------------------
# The adjustment for multiple comparisons
# ---------------------------------------------------------------------------

# The false discovery rate at which an adjusted p-value is significant.
FDR_ALPHA = 0.05


def adjust_tested(p_raw: "pd.Series") -> Tuple["pd.Series", "pd.Series"]:  # noqa: F821
    """The Benjamini-Hochberg adjusted p-values and their rejections at FDR_ALPHA, over the
    comparisons that were tested.

    A comparison without a p-value was not tested: One group of it was empty. It keeps a blank
    adjusted p-value, is not significant, and is not one of the comparisons that the
    adjustment counts. Handed to `multipletests`, one blank would make every adjusted
    p-value blank.
    """
    import pandas as pd
    from statsmodels.stats.multitest import multipletests

    tested = p_raw.notna()
    p_adjusted = pd.Series(float("nan"), index=p_raw.index, dtype="float64")
    significant = pd.Series(False, index=p_raw.index, dtype="bool")
    if tested.any():
        reject, adjusted, _, _ = multipletests(p_raw[tested], alpha=FDR_ALPHA, method="fdr_bh")
        p_adjusted[tested] = adjusted
        significant[tested] = reject
    return p_adjusted, significant


# ---------------------------------------------------------------------------
# Context file categories, derived from the heuristics
# ---------------------------------------------------------------------------

# The figure label of every `context_file` row of `config_path_patterns.csv`, by the row's path.
# The keys are the heuristics' paths. A row added there and not here makes the import of this
# module fail, and a context file therefore cannot be left out of a timeline unnoticed. The
# labels are the figures' series, and two rows that describe one family of files share a label.
CATEGORY_OF_HEURISTIC_PATH = {
    "CLAUDE.md": "CLAUDE.md",
    "CLAUDE.local.md": "CLAUDE.md",
    # Read by Claude Code Review and by Copilot code review, and therefore a series of its own.
    "REVIEW.md": "REVIEW.md",
    "AGENTS.md": "AGENTS.md",
    "AGENTS.override.md": "AGENTS.md",
    # Claude Code's own AGENTS.md, an AGENTS.md like any other in the figures.
    ".claude/AGENTS.md": "AGENTS.md",
    "GEMINI.md": "GEMINI.md",
    # The same three spelled in another case at the repository root.
    "claude.md": "CLAUDE.md",
    "agents.md": "AGENTS.md",
    "gemini.md": "GEMINI.md",
    ".github/copilot-instructions.md": "Copilot Instructions",
    ".github/instructions": "Copilot Instructions",
    ".cursorrules": ".cursorrules",
    ".gemini/styleguide.md": "Gemini Style Guide",
    ".cursor/BUGBOT.md": "Cursor BUGBOT",
    # The rows with files that declare context files: a Gemini settings file, a Gemini
    # extension manifest, a Codex config and OpenCode's config in its four places. Their
    # contents determine what they declare.
    ".gemini/settings.json": "Named in Configuration",
    "gemini-extension.json": "Named in Configuration",
    ".codex/config.toml": "Named in Configuration",
    "opencode.json": "Named in Configuration",
    "opencode.jsonc": "Named in Configuration",
    ".opencode/opencode.json": "Named in Configuration",
    ".opencode/opencode.jsonc": "Named in Configuration",
}
# The category of a context file that a configuration names and no path pattern matches.
NAMED_IN_CONFIGURATION = "Named in Configuration"
# The series in figure order.
CATEGORY_ORDER = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "Copilot Instructions",
                  ".cursorrules", "Gemini Style Guide", "Cursor BUGBOT", "REVIEW.md",
                  NAMED_IN_CONFIGURATION)


def _context_file_rows() -> List[dict]:
    with open(CONFIG_PATTERNS_CSV, newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if row["mechanism"] == "context_file"]


def _matcher(row: dict) -> Callable[[str], bool]:
    """Whether a `context_file` value is this heuristic's, as the detection step matches it:
    a basename at any depth, a fixed path at any depth, a suffix under a directory, or for a
    `nested:` row the place of its inner mode below the repository root. Case does not count,
    because the row records the name on disk, and a case-insensitive volume may spell it in
    another case. The row of a file that declares context files matches no value, because a
    path does not show which configuration named it, and `context_file_row_category` gives
    these rows their category."""
    path = row["path"].lower()
    mode = row["match_mode"]
    if mode.startswith("nested:"):
        # The inner mode's place in a directory below the root: the value after one or more of
        # its leading directories.
        within = _matcher({**row, "match_mode": mode[len("nested:"):]})
        return lambda f: any(within(f.split("/", depth)[-1]) for depth in range(1, f.count("/") + 1))
    if mode == "basename":
        return lambda f: f.lower().rsplit("/", 1)[-1] == path
    if mode == "root_file":
        return lambda f: f == row["path"]
    if mode == "root_any_case":
        return lambda f: "/" not in f and f.lower() == path
    if mode == "file":
        return lambda f: f.lower() == path or f.lower().endswith("/" + path)
    if mode.startswith("tree_suffix:"):
        suffix = mode.split(":", 1)[1].lower()
        return lambda f: (f.lower().startswith(path + "/") or ("/" + path + "/") in f.lower()) \
            and f.lower().endswith(suffix)
    if mode.startswith(("declared_names:", "declared_fallback:", "declared_path:", "declared_local:",
                        "declared_globs:")):
        return lambda f: False
    raise ValueError(f"No matcher for the match mode {mode!r} of the context file {row['path']}")


def _build_categories() -> dict:
    by_label: dict = {label: [] for label in CATEGORY_ORDER}
    for row in _context_file_rows():
        label = CATEGORY_OF_HEURISTIC_PATH.get(row["path"])
        if label is None:
            raise RuntimeError(f"_shared.CATEGORY_OF_HEURISTIC_PATH has no category for the "
                               f"context file {row['path']!r} of config_path_patterns.csv. "
                               f"Add the path there with its category.")
        by_label[label].append(_matcher(row))
    unused = set(CATEGORY_OF_HEURISTIC_PATH) - {row["path"] for row in _context_file_rows()}
    if unused:
        raise RuntimeError(f"_shared.CATEGORY_OF_HEURISTIC_PATH names paths that "
                           f"config_path_patterns.csv does not list as context files "
                           f"({', '.join(sorted(unused))})")
    return {label: (lambda f, ms=matchers: any(m(f) for m in ms))
            for label, matchers in by_label.items()}


# `{label: predicate}` in figure order. A `context_file` value matches at most one label.
CONTEXT_FILE_CATEGORIES = _build_categories()


def context_file_category(path: str) -> Optional[str]:
    """The category that a path pattern gives a path, or None for a path that no path pattern
    matches.

    The function takes any path, including a referenced file. A row of `context-files.csv` gets its
    category from `context_file_row_category`.
    """
    for label, matches in CONTEXT_FILE_CATEGORIES.items():
        if matches(path):
            return label
    return None


def context_file_row_category(path: str) -> str:
    """The category of a `context-files.csv` row, which every row has.

    The detection step lists a file as a context file because a path pattern matches it or
    because the repository's configuration names it. A row that no path pattern matches is
    therefore a file that a configuration named. A heuristic without a category makes the
    import of this module fail.
    """
    return context_file_category(path) or NAMED_IN_CONFIGURATION


COLORS = {
    "CLAUDE.md": "#1f77b4",
    "AGENTS.md": "#ff7f0e",
    "GEMINI.md": "#9467bd",
    "Copilot Instructions": "#8c564b",
    ".cursorrules": "#e377c2",
    "Gemini Style Guide": "#7f7f7f",
    "Cursor BUGBOT": "#d62728",
    "REVIEW.md": "#2ca02c",
    "Named in Configuration": "#bdbdbd",
    "Skills": "#17becf",
    "Subagents": "#bcbd22",
    "Planning Documents": "#393b79",
}

# The families of the adoption timeline: the five context file categories and the two mechanisms
# of the timeline of AgentConfigs 1.0.0, and the planning documents, which AgentTorrent 1.0.0 added.
ADOPTION_CONTEXT_FILE_CATEGORIES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "Copilot Instructions",
                                    ".cursorrules")
# The first day of the adoption timeline. A file created before it was created for another
# purpose and later renamed into a configuration or named by one, as the development document
# of 2021 that kdeldycke/meta-package-manager renamed to claude.md and then to AGENTS.md, the
# reviewer guidelines of spyder-ide/spyder that Copilot code review now reads as REVIEW.md, or the
# documentation of mathiasertl/django-ca that its configuration names. Its creation is no
# adoption, and the timeline leaves such files out.
ADOPTION_START = "2023-01-01"

# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------


def require_plotting() -> None:
    """Import matplotlib and seaborn, raising a clear error if missing."""
    try:
        import matplotlib  # noqa: F401
        import matplotlib.pyplot as plt  # noqa: F401
        import seaborn as sns  # noqa: F401
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"{exc.name} is not installed. Install the dependency lock locks/development.txt "
            f"into .venv with mise exec -- bash dev/setup.sh from the repository root, and run "
            f"the script with mise exec"
        ) from exc


def configure_plot_style() -> None:
    """Apply consistent plot styling across all figure scripts."""
    require_plotting()
    import seaborn as sns

    sns.set_style("whitegrid")


# Matplotlib's font type for TrueType fonts, under which the text of a PDF remains text.
PDF_FONTTYPE = 42


def save_figure(fig, path, **savefig_args) -> None:
    """Write a figure as a PDF with `bbox_inches="tight"` and with its fonts embedded as TrueType.

    `bbox_inches="tight"` sizes the page to what the figure draws, which keeps labels that reach
    beyond the edges of the figure and trims the empty margin. Matplotlib's PDF
    backend embeds Type 3 fonts unless `pdf.fonttype` is 42, and with 42 it embeds TrueType fonts,
    so that the text in the PDF remains text. The setting applies only while this figure is
    written, and every figure of the stage is written here."""
    import matplotlib

    with matplotlib.rc_context({"pdf.fonttype": PDF_FONTTYPE, "ps.fonttype": PDF_FONTTYPE}):
        fig.savefig(path, bbox_inches="tight", **savefig_args)


# ---------------------------------------------------------------------------
# LaTeX
# ---------------------------------------------------------------------------

# The characters that LaTeX gives a meaning in text, each as it is written to stand for itself.
# A language name such as C# and a filter step's description come from the data.
LATEX_SPECIAL = {"\\": r"\textbackslash{}", "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&",
                 "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                 "^": r"\textasciicircum{}"}


def latex_text(text: str) -> str:
    """`text` as LaTeX that prints it."""
    return "".join(LATEX_SPECIAL.get(char, char) for char in text)


# ---------------------------------------------------------------------------
# The inputs that the repository tracks as archives, and the commit CSV
# ---------------------------------------------------------------------------


def checked_copy(copy: Path, archive: Optional[Path] = None) -> Path:
    """`copy`, after a check against the archive beside it, `csv_archive(copy)` unless `archive`
    names another. Where git tracks that archive, as in a checkout, the script stops when the two
    differ or when 7zz cannot list the archive, because an output drawn from such a copy would not
    be the output of the commit (`agentcontext.archives`). Where git tracks no archive, as in the
    data root of a run or in a working copy, the copy is the input and passes."""
    from agentcontext import archives, csv_archive
    try:
        archives.check_copy(copy, archive if archive is not None else csv_archive(copy))
    except (archives.CopyDiffers, archives.ArchiveUnreadable) as error:
        raise SystemExit(str(error)) from error
    return copy


# The rows of the commit CSV that `read_commits_chunks` reads at a time. The commit messages
# make up most of the file, and a script that reads them holds one chunk of them at a time.
COMMIT_CHUNK_ROWS = 100_000


@contextlib.contextmanager
def commits_csv(data_dir: Path | str = DATA_DIR) -> Iterator[Path]:
    """The path of `all_ai_commits.csv` in `data_dir`, extracted from `all_ai_commits.csv.7z`
    beside it into a temporary directory when the CSV is absent, wherever in the archive the file
    is, for as long as the context lasts. The tracked archive contains it at its root. A CSV that
    differs from the archive that git tracks beside it stops the script (`checked_copy`)."""
    data_dir = Path(data_dir)
    path = data_dir / "all_ai_commits.csv"
    if path.exists():
        yield checked_copy(path)
        return
    from agentcontext import csv_archive
    archive = csv_archive(path)
    if not archive.exists():
        raise SystemExit(f"Neither {path} nor {archive} exists.")
    with tempfile.TemporaryDirectory() as work:
        result = subprocess.run([SEVEN_ZIP, "x", str(archive), f"-o{work}", "-y"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"7zz could not extract {archive} ({result.stderr.strip()})")
        extracted = sorted(p for p in Path(work).rglob(path.name) if p.is_file())
        if not extracted:
            raise SystemExit(f"{archive} does not contain {path.name}")
        yield extracted[0]


def read_commits_csv(data_dir: Path | str = DATA_DIR,
                     columns: Optional[Sequence[str]] = None) -> "pd.DataFrame":  # noqa: F821
    """Read `all_ai_commits.csv` (`commits_csv`).

    `columns` names the columns to read as text, and None reads every column with the type
    that pandas infers. The commit messages make up most of the file. A script that does not
    use them names its columns, and the parser then reads the file in chunks and holds the
    tokens of one chunk rather than the tokens of the whole file. Read as text, a column has
    the same type in every chunk. Without `columns`, every column is read in one pass, so that
    pandas infers one type per column rather than one per chunk."""
    import pandas as pd
    with commits_csv(data_dir) as path:
        if columns is None:
            return pd.read_csv(path, low_memory=False)
        return pd.read_csv(path, usecols=list(columns), dtype=str)


def read_commits_chunks(data_dir: Path | str = DATA_DIR, *, columns: Sequence[str],
                        rows: Optional[int] = None) -> Iterator["pd.DataFrame"]:  # noqa: F821
    """The columns `columns` of `all_ai_commits.csv` (`commits_csv`) as text, `rows` rows at a
    time, by default `COMMIT_CHUNK_ROWS`. A script that reads the commit messages holds one
    chunk of them rather than the whole file, which is larger than the memory of a GitHub
    runner once pandas holds it."""
    import pandas as pd
    with commits_csv(data_dir) as path:
        yield from pd.read_csv(path, usecols=list(columns), dtype=str,
                               chunksize=rows or COMMIT_CHUNK_ROWS)


# ---------------------------------------------------------------------------
# The UpSet renderer, matplotlib alone
# ---------------------------------------------------------------------------
#
# The `upsetplot` package cannot plot under the pinned pandas: It fills four style columns
# with `styles[col].fillna(value, inplace=True)`, which copy-on-write turns into a no-op. The
# edge color stays NaN, and matplotlib rejects it. No newer release fixes the defect, and no
# argument avoids that code path.


@dataclass(frozen=True)
class Intersection:
    mask: int
    count: int
    membership: Tuple[bool, ...]


def compute_intersections(
    bool_df: "pd.DataFrame",  # noqa: F821
    columns: Sequence[str],
    *,
    drop_empty: bool = True,
) -> List[Intersection]:
    """Pack membership into a bitmask per row, then count unique masks."""
    import pandas as pd

    mask = pd.Series(0, index=bool_df.index, dtype="int64")
    for bit_index, col in enumerate(columns):
        mask = mask | (bool_df[col].astype("int64") * (1 << bit_index))

    counts = mask.value_counts(dropna=False)
    intersections: List[Intersection] = []
    for mask_value, count in counts.items():
        mask_int = int(mask_value)
        if drop_empty and mask_int == 0:
            continue
        membership = tuple(bool((mask_int >> i) & 1) for i in range(len(columns)))
        intersections.append(Intersection(mask=mask_int, count=int(count), membership=membership))

    intersections.sort(key=lambda x: (-x.count, x.mask))
    return intersections


# The intersections that an UpSet plot draws, and the fewest that a smaller top_n draws.
UPSET_TOP_N = 25
UPSET_MIN_TOP_N = 1


def plot_upset(
    intersections: Sequence[Intersection],
    set_labels: Sequence[str],
    *,
    title: str,
    output_path,
    top_n: int = UPSET_TOP_N,
    min_count: int = 1,
    show: bool = False,
    y_axis_log_scale: bool = False,
    set_colors: Optional[Mapping[str, str]] = None,
) -> None:
    """Draw one UpSet plot: a bar per intersection over a membership matrix.

    `set_colors` gives one color per set label, which colors that set's dots
    in the matrix. Without it, every dot is black.
    """
    import matplotlib.pyplot as plt
    from matplotlib import gridspec

    filtered = [x for x in intersections if x.count >= min_count]
    if not filtered:
        raise ValueError(f"No intersection has at least {min_count} repositories. Lower --min-count.")

    filtered = filtered[: max(UPSET_MIN_TOP_N, int(top_n))]
    n_sets = len(set_labels)
    n_inters = len(filtered)

    fig_w = max(9.0, min(22.0, 0.55 * n_inters + 6.0))
    fig_h = max(5.5, min(14.0, 0.35 * n_sets + 5.0))

    fig = plt.figure(figsize=(fig_w, fig_h))
    gs = gridspec.GridSpec(2, 1, height_ratios=[2.2, 2.8], hspace=0.03)
    ax_bar = fig.add_subplot(gs[0])
    ax_mat = fig.add_subplot(gs[1], sharex=ax_bar)

    x = list(range(n_inters))
    counts = [it.count for it in filtered]

    ax_bar.bar(x, counts, color="#aaaaaa")
    ax_bar.set_ylabel("Repository Count" + ("\n(log scale)" if y_axis_log_scale else ""), fontsize=12)
    ax_bar.set_title(title, fontsize=14)
    ax_bar.set_xlim(-0.5, n_inters - 0.5)
    ax_bar.grid(axis="y", linestyle=":", alpha=0.3)
    ax_bar.tick_params(axis="x", bottom=False, labelbottom=False)

    if y_axis_log_scale:
        ax_bar.set_yscale("log")

    # Count labels above the bars
    for xi, c in zip(x, counts):
        ax_bar.text(xi, c, f"{c:,}", ha="center", va="bottom", fontsize=8)

    y_positions = list(range(n_sets))
    ax_mat.set_yticks(y_positions)
    ax_mat.set_yticklabels(list(set_labels))
    ax_mat.invert_yaxis()
    ax_mat.set_xlabel("Top Intersections (by Count)", fontsize=12)
    ax_mat.grid(False)

    for col_idx in x:
        ax_mat.scatter([col_idx] * n_sets, y_positions, s=26, color="#dddddd", zorder=1)

    for col_idx, inter in enumerate(filtered):
        members = [row_idx for row_idx, present in enumerate(inter.membership) if present]
        for row_idx in members:
            color = ("#111111" if set_colors is None
                     else set_colors.get(set_labels[row_idx], "#111111"))
            ax_mat.scatter([col_idx], [row_idx], s=36, color=color,
                           edgecolors="#111111", linewidths=0.4, zorder=2)
        if len(members) >= 2:
            ax_mat.plot([col_idx, col_idx], [min(members), max(members)], color="#111111", lw=1.6, zorder=1.5)

    ax_mat.set_xlim(-0.5, n_inters - 0.5)
    ax_mat.tick_params(axis="x", bottom=False, labelbottom=False)
    ax_bar.tick_params(axis="y", labelsize=12)
    ax_mat.tick_params(axis="y", labelsize=12)

    fig.tight_layout()
    save_figure(fig, output_path)
    if show:
        plt.show()
    plt.close(fig)
