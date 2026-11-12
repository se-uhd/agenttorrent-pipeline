#!/usr/bin/env python3
"""
Build script for the dataset explorer static website.

Reads the publication in data/collection/release/, generates paginated JSON
data files, pre-computes chart data, and assembles the site in _site/, which deploy.sh copies
into the GitHub Pages repository. The download files of the release go to _assets/<version>/,
which upload_assets.sh mirrors to the asset host.

The site is built from the release, the bytes that a Zenodo user downloads, rather than from
the pipeline's output CSVs.
"""

import json
import math
import os
import shutil
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

# --- Paths -------------------------------------------------------------------
from agentcontext import STAGE_DIR  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = STAGE_DIR / "release"
SITE_DIR = SCRIPT_DIR / "_site"
# The download files of each release, in a directory per version, which upload_assets.sh mirrors
# to DOWNLOAD_HOST_URL.
ASSETS_DIR = SCRIPT_DIR / "_assets"

# --- Constants ---------------------------------------------------------------
# Imported from the analysis stage's one shared module, at its root, rather than
# redefined, because another copy of the tool and mechanism lists could diverge from it.
sys.path.insert(0, str(SCRIPT_DIR.parent / "4_data-analysis"))
from _shared import (  # noqa: E402
    ADOPTION_CONTEXT_FILE_CATEGORIES, ADOPTION_START, MECHANISM_LABELS, MECHANISMS,
    CONTEXT_FILE_CATEGORIES, NO_SPECIFIC_TOOL, SPECIFIC_TOOLS, TOOL_LABELS, TOOLS,
    context_file_category, context_file_row_category, mechanism_tool_counts,
)

from agentcontext import logs

# The component of this script's log messages in the pipeline's one log format,
# `LEVEL component: message`. agentcontext/logs.py and docs/logs.md define the format.
COMPONENT = "build_site"

# The columns of config_tool_heatmap, as in `4_data-analysis/rq2/config_tool_heatmap.py`: the
# six tools, then the repositories with no specific tool, which have only shared
# configuration artifacts.
HEATMAP_TOOLS = SPECIFIC_TOOLS + [NO_SPECIFIC_TOOL]

# The site's key for a mechanism's configuration artifacts, by `_shared.MECHANISMS` key. Only
# the context files differ, because the release names their CSV `context_files.csv`.
ARTIFACT_KEYS = {mechanism: ("context_files" if mechanism == "context_file" else mechanism)
                 for mechanism in MECHANISMS}

# Keyed by `TOOL_LABELS`, the seven labels that the dashboard and the tool charts show.
TOOL_COLORS = {
    "Claude": "#87ceeb", "Copilot": "#ef9a9a", "Cursor": "#ffcc80",
    "Gemini": "#fff176", "Codex": "#90ee90", "OpenCode": "#80cbc4",
    "No specific tool": "#ce93d8",
}
# The sets of the tool UpSet: the six tools and the `agents.md` context-file flag, in the
# colors of `4_data-analysis/rq2/upset_plot.py`.
UPSET_COLORS = {
    **{TOOL_LABELS[t]: TOOL_COLORS[TOOL_LABELS[t]] for t in SPECIFIC_TOOLS},
    "AGENTS.md": "#ce93d8",
}

# The context-file categories come from `_shared` too, which derives them from
# `heuristics/config_path_patterns.csv` and fails when the heuristics and the map disagree in
# either direction. A private copy here could miss a category, and the `category.notna()`
# filters would then drop its rows from every chart on the site while the analysis figures
# count them.

# The chart colors of the context file categories, the skills and the subagents.
CONTEXT_FILE_COLORS = {
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
    "Context Files": "#1f77b4",
}

CONFIG_TYPE_COLORS = {
    "Context Files": "#78716c",
    "Skills": "#5ba3cf",
    "Subagents": "#7cc47c",
    "Commands": "#b0a0d0",
    "Rules": "#e6a756",
    "Settings": "#a8a29e",
    "Hooks": "#c4a882",
    "MCP": "#8fb8c4",
    "Plugins": "#c9a0b4",
    "LSP": "#9fb4a0",
}

CONTEXT_FILE_MARKERS = {
    "CLAUDE.md": "circle", "AGENTS.md": "square", "GEMINI.md": "triangle",
    "Copilot Instructions": "diamond", ".cursorrules": "triangleDown",
    "Skills": "pentagon", "Subagents": "cross", "Planning Documents": "star",
}

# Pagination
REPOS_PER_PAGE = 25
COMMITS_PER_PAGE = 25

# GitHub Pages publishes a site of at most 1 GB, and the build refuses a larger site.
PAGES_LIMIT_BYTES = 1_000_000_000
# The column of each tab of the configuration browser that names the configuration artifact,
# which the details of a repository list with its link to GitHub.
ARTIFACT_PATH_COLUMNS = {"context_files": "context_file", "settings": "settings",
                         "skills": "skills.md", "subagents": "subagent", "commands": "command",
                         "hooks": "hook", "rules": "rule", "mcp": "mcp", "plugins": "plugin",
                         "lsp": "lsp"}

# The address of ASSETS_DIR on the asset host, with a directory per release. The downloads of
# AgentConfigs 1.0.0 stay at data/ai-config-dataset, where its links point.
DOWNLOAD_HOST_URL = "https://assets.empirical-software.engineering/data/agenttorrent"
# The release files that the site offers for download, with a description of each.
DOWNLOAD_FILES = {
    "repos.csv.7z": "The repositories in the sampling frame (repos.csv) with their GitHub metadata, the label of "
                    "whether each hosts an engineered software project, and the flags of the tools "
                    "and mechanisms",
    "commits.csv.7z": "Commits that credit one of the six tools (commits.csv), in the configured "
                      "repositories",
    "other_commits.csv": "Commits that credit a tool outside the six, in the configured repositories",
    "repos_data.7z": "The configuration artifacts and planning documents of the configured repositories",
    "context_files.csv": "Context files, such as CLAUDE.md and AGENTS.md",
    "settings.csv": "Settings files",
    "skills.csv": "Skill definitions",
    "subagents.csv": "Subagent definitions",
    "commands.csv": "Custom command files",
    "hooks.csv": "Hook configurations",
    "rules.csv": "Rule files",
    "mcp.csv": "MCP configurations",
    "plugins.csv": "Plugin and extension manifests",
    "lsp.csv": "LSP server configurations",
    "planning-documents.csv": "Agentic planning documents, such as the specs and plans of Spec Kit and OpenSpec, which the "
                              "dataset does not count as configuration artifacts",
    "imports.csv": "Imports of the configuration artifacts, such as @docs/style.md in a CLAUDE.md, with the file that each reaches",
    "imported_files.7z": "The files that configuration artifacts import, such as the docs/style.md "
                         "that a CLAUDE.md imports with @docs/style.md. An imported file that is "
                         "itself a configuration artifact is in repos_data.7z.",
}
# The two extensions beside the configuration artifacts, which the configuration browser shows
# in tabs of their own and which no chart of the mechanisms reads.
EXTENSION_FILES = {
    "planning_documents": "planning-documents.csv",
    "imports": "imports.csv",
}


# --- Helpers -----------------------------------------------------------------
def to_bool(v) -> bool:
    return str(v).upper() == "TRUE"


def read_release_csv(name: str) -> pd.DataFrame:
    """One CSV of the release, with a blank cell as the only missing value. pandas reads `n/a`,
    `NA`, `null` and other strings as missing by default, and `n/a` is a value of the release:
    the `memory` of a subagent of a tool without per-agent memory, and the `language` of a file
    too short for language detection. With the defaults, the site would show these cells
    empty."""
    return pd.read_csv(DATA_DIR / name, keep_default_na=False, na_values=[""])


def write_json(path: Path, data, indent=None):
    """Write JSON with compact separators when not indented."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if indent:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        else:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def df_to_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to list of dicts, converting NaN to None."""
    return json.loads(df.to_json(orient="records"))


def classify_context_file(path: str) -> str:
    """The category of a `context-files.csv` row."""
    return context_file_row_category(path)


def classify_reference(path: str) -> str | None:
    """The category of a referenced file, by path alone, or None."""
    return context_file_category(path)


# --- Phase 1: Load CSVs -----------------------------------------------------
def load_data():
    logs.info(COMPONENT, f"Loading the CSVs of the published dataset from {DATA_DIR}...")
    repos = read_release_csv("repos.csv")
    logs.info(COMPONENT, f"  Read {len(repos):,} rows of repos.csv.")

    # Convert boolean columns: the seven of TOOLS, `no_specific_tool` among them, the
    # mechanisms and the context file flags
    bool_cols = list(set(
        TOOLS
        + MECHANISMS
        + ["agents.md", "claude.md", "gemini.md", "copilot_instructions", "cursorrules"]
    ))
    for col in bool_cols:
        if col in repos.columns:
            repos[col] = repos[col].apply(to_bool)

    configured = repos[repos["scanned_at"].notna()].copy()
    logs.info(COMPONENT, f"  {len(configured):,} of them are configured repositories.")

    commits = read_release_csv("commits.csv")
    logs.info(COMPONENT, f"  Read {len(commits):,} rows of commits.csv.")
    other_commits = read_release_csv("other_commits.csv")
    logs.info(COMPONENT, f"  Read {len(other_commits):,} rows of other_commits.csv.")
    commits = listed_commits(commits, other_commits)
    logs.info(COMPONENT, f"  The commit browser lists {len(commits):,} commits.")

    artifact_files = {
        "context_files": "context_files.csv",
        "settings": "settings.csv",
        "skills": "skills.csv",
        "subagents": "subagents.csv",
        "commands": "commands.csv",
        "hooks": "hooks.csv",
        "rules": "rules.csv",
        "mcp": "mcp.csv",
        "plugins": "plugins.csv",
        "lsp": "lsp.csv",
    }
    artifacts = {}
    for key, filename in {**artifact_files, **EXTENSION_FILES}.items():
        df = read_release_csv(filename)
        artifacts[key] = df
        logs.info(COMPONENT, f"  Read {len(df):,} rows of {filename}.")

    return repos, configured, commits, artifacts


# --- Phase 2: Index JSON ----------------------------------------------------
def build_index(configured: pd.DataFrame):
    logs.info(COMPONENT, "Building index.json...")
    index_cols = [
        "repo_name", "github_link", "mainLanguage",
        "stargazers", "forks", "contributors", "commits", "codeLines",
        "createdAt",
    ] + TOOLS + MECHANISMS

    index_df = configured[index_cols].copy()
    # Add page number for detail lookup
    index_df = index_df.sort_values("stargazers", ascending=False).reset_index(drop=True)
    index_df["_page"] = index_df.index // REPOS_PER_PAGE

    records = df_to_records(index_df)
    write_json(SITE_DIR / "data" / "index.json", records)
    logs.info(COMPONENT, f"  Wrote {len(records):,} entries.")
    return index_df


# --- Phase 3: Summary JSON --------------------------------------------------
def build_summary(repos: pd.DataFrame, configured: pd.DataFrame,
                  commits: pd.DataFrame, artifacts: dict):
    logs.info(COMPONENT, "Building summary.json...")

    engineered_count = len(repos[repos["engineered_project"].apply(
        lambda v: str(v).lower() == "true"
    )])

    tool_counts = {TOOL_LABELS[t]: int(configured[t].sum()) for t in TOOLS}
    mechanism_counts = {MECHANISM_LABELS[c]: int(configured[c].sum()) for c in MECHANISMS}

    # Top languages
    lang_counts = (
        configured["mainLanguage"].value_counts().head(10)
        .to_dict()
    )

    # Commit stats by tool. `commits` lists the commits of both commit CSVs, and the dashboard
    # counts those that credit one of the six tools, which are the rows of commits.csv.
    commit_tool_counts = commits["ai_tool"].value_counts().to_dict()
    six_labels = {TOOL_LABELS[t] for t in SPECIFIC_TOOLS}
    credited = commits["ai_tool"].fillna("").str.split(",")
    six_tool_commits = int(credited.apply(
        lambda tools: any(tool.strip() in six_labels for tool in tools)).sum())

    summary = {
        "total_repos": len(repos),
        "classified_repos": int(repos["engineered_project"].notna().sum()),
        "engineered_repos": engineered_count,
        "configured_repos": len(configured),
        "total_commits": six_tool_commits,
        "total_listed_commits": len(commits),
        "total_context_files": len(artifacts["context_files"]),
        "total_planning_documents": len(artifacts["planning_documents"]),
        "total_imports": len(artifacts["imports"]),
        "tool_counts": tool_counts,
        "tool_colors": TOOL_COLORS,
        "mechanism_counts": mechanism_counts,
        "config_colors": CONFIG_TYPE_COLORS,
        "top_languages": lang_counts,
        "commit_tool_counts": commit_tool_counts,
    }
    write_json(SITE_DIR / "data" / "summary.json", summary, indent=2)
    logs.info(COMPONENT, f"  Wrote summary.json.")
    return summary


# --- Phase 4: Paginated repos -----------------------------------------------
def artifact_links(artifacts: dict) -> dict:
    """The configuration artifacts of each repository, each with its tab, its path and its link to
    the file on GitHub on the scanned branch, which the details of the repository list."""
    links: dict = {}
    for key, column in ARTIFACT_PATH_COLUMNS.items():
        df = artifacts.get(key)
        if df is None or df.empty:
            continue
        for repo, path, link in zip(df["repo_name"], df[column], df["github_link"]):
            links.setdefault(repo, []).append({"tab": key, "path": path, "link": link})
    return {repo: sorted(files, key=lambda f: (f["tab"], f["path"])) for repo, files in links.items()}


def paginate_repos(configured: pd.DataFrame, artifacts: dict):
    logs.info(COMPONENT, "Paginating repositories...")
    df = configured.sort_values("stargazers", ascending=False).reset_index(drop=True)
    links = artifact_links(artifacts)
    # Every column goes into the pages, the large JSON columns (languages, topics,
    # justification) as the strings that the CSV stores
    n_pages = math.ceil(len(df) / REPOS_PER_PAGE)
    for page_idx in range(n_pages):
        start = page_idx * REPOS_PER_PAGE
        end = start + REPOS_PER_PAGE
        page_df = df.iloc[start:end]
        records = df_to_records(page_df)
        for record in records:
            record["files"] = links.get(record["repo_name"], [])
        write_json(SITE_DIR / "data" / "repos" / f"page_{page_idx}.json", records)
    logs.info(COMPONENT, f"  Wrote {n_pages:,} pages.")


# --- Phase 5: Paginated commits ---------------------------------------------
def listed_commits(commits: pd.DataFrame, other_commits: pd.DataFrame) -> pd.DataFrame:
    """The commits of `commits.csv` and `other_commits.csv`, one row per commit, which the commit
    browser lists. A commit that credits one of the six tools and another tool has a row in each
    file, and its row here joins the tools of both in `ai_tool`, the six tools first."""
    key = ["repo_name", "commit_sha"]
    six = commits.set_index(key)
    other = other_commits.set_index(key)
    both = six.index.intersection(other.index)
    six.loc[both, "ai_tool"] = six.loc[both, "ai_tool"] + "," + other.loc[both, "ai_tool"]
    return pd.concat([six, other.drop(both)]).reset_index()


def paginate_commits(commits: pd.DataFrame):
    logs.info(COMPONENT, "Paginating commits...")
    df = commits.sort_values("commit_timestamp", ascending=False).reset_index(drop=True)
    # The pages hold the subject line of each message. With the whole messages, the pages are
    # larger than GitHub Pages publishes, and the messages are in commits.csv.7z.
    df["commit_subject"] = df["commit_message"].fillna("").str.split("\n", n=1).str[0]
    n_pages = math.ceil(len(df) / COMMITS_PER_PAGE)
    for page_idx in range(n_pages):
        start = page_idx * COMMITS_PER_PAGE
        end = start + COMMITS_PER_PAGE
        page_df = df.iloc[start:end]
        records = df_to_records(page_df[
            ["repo_name", "commit_timestamp", "branch", "commit_sha",
             "github_link", "commit_subject", "ai_tool"]
        ])
        write_json(SITE_DIR / "data" / "commits" / f"page_{page_idx}.json", records)
    logs.info(COMPONENT, f"  Wrote {n_pages:,} pages ({len(df):,} commits).")


# --- Phase 6: Configuration artifact JSONs ----------------------------------
def build_artifact_jsons(artifacts: dict):
    logs.info(COMPONENT, "Building the JSON files of the configuration browser's tabs...")
    for key, df in artifacts.items():
        records = df_to_records(df)
        write_json(SITE_DIR / "data" / "artifacts" / f"{key}.json", records)
        logs.info(COMPONENT, f"  Wrote {len(records):,} entries to {key}.json.")


# --- Phase 7: Chart data ----------------------------------------------------
def build_chart_config_tool_heatmap(configured: pd.DataFrame, artifacts: dict):
    """Mechanisms x tools heatmap, one row per mechanism and one column per tool.

    A cell counts the configured repositories with a configuration artifact of the mechanism
    attributed to the tool by the `tool` column of its mechanism CSV, and the last column the
    repositories with no specific tool that have the mechanism. The cells and the totals come
    from `_shared.mechanism_tool_counts`, the function that the analysis figure uses. A cell
    does not cross the repository flags, which would count a repository with a Claude context
    file and a Cursor command under Claude x Commands.
    """
    dataframes = {mechanism: artifacts[ARTIFACT_KEYS[mechanism]] for mechanism in MECHANISMS}
    cells, totals = mechanism_tool_counts(configured, dataframes)
    rows = [MECHANISM_LABELS[c] for c in MECHANISMS]
    cols = [TOOL_LABELS[t] for t in HEATMAP_TOOLS]
    counts = []
    percentages = []
    tool_totals = {TOOL_LABELS[t]: totals[t] for t in HEATMAP_TOOLS}

    for cfg in MECHANISMS:
        row_counts = []
        row_pcts = []
        for tool in HEATMAP_TOOLS:
            c = cells[(cfg, tool)]
            total = totals[tool]
            pct = round(c / total * 100, 1) if total > 0 else 0
            row_counts.append(c)
            row_pcts.append(pct)
        counts.append(row_counts)
        percentages.append(row_pcts)

    return {
        "rows": rows, "cols": cols,
        "counts": counts, "percentages": percentages,
        "tool_totals": tool_totals,
        "n_repos": len(configured),
    }


def build_chart_language_tool_heatmap(configured: pd.DataFrame):
    """Languages x tools heatmap (the top 10 languages x the 7 columns of TOOLS)."""
    lang_counts = configured["mainLanguage"].value_counts()
    top10 = lang_counts.head(10).index.tolist()

    rows = top10
    cols = [TOOL_LABELS[t] for t in TOOLS]
    tool_totals = {TOOL_LABELS[t]: int(configured[t].sum()) for t in TOOLS}

    counts = []
    percentages = []
    for lang in top10:
        subset = configured[configured["mainLanguage"] == lang]
        row_c = [int(subset[t].sum()) for t in TOOLS]
        row_p = [round(c / tool_totals[TOOL_LABELS[t]] * 100, 1) if tool_totals[TOOL_LABELS[t]] > 0 else 0
                 for c, t in zip(row_c, TOOLS)]
        counts.append(row_c)
        percentages.append(row_p)

    return {
        "rows": rows, "cols": cols,
        "counts": counts, "percentages": percentages,
        "tool_totals": tool_totals,
        "n_repos": len(configured),
    }


def build_chart_tool_cooccurrence(configured: pd.DataFrame):
    """7x7 symmetric tool co-occurrence heatmap over the columns of TOOLS."""
    tool_n = {t: int(configured[t].sum()) for t in TOOLS}
    count_matrix = [[0] * len(TOOLS) for _ in TOOLS]
    pct_matrix = [[0.0] * len(TOOLS) for _ in TOOLS]

    for i, t in enumerate(TOOLS):
        count_matrix[i][i] = tool_n[t]
        pct_matrix[i][i] = 100.0

    for (ai, a), (bi, b) in combinations(enumerate(TOOLS), 2):
        both = int((configured[a] & configured[b]).sum())
        count_matrix[ai][bi] = both
        count_matrix[bi][ai] = both
        min_n = min(tool_n[a], tool_n[b])
        p = round(both / min_n * 100, 1) if min_n > 0 else 0.0
        pct_matrix[ai][bi] = p
        pct_matrix[bi][ai] = p

    return {
        "tools": [TOOL_LABELS[t] for t in TOOLS],
        "count_matrix": count_matrix,
        "pct_matrix": pct_matrix,
        "tool_totals": {TOOL_LABELS[t]: tool_n[t] for t in TOOLS},
        "n_repos": len(configured),
    }


def build_chart_upset(configured: pd.DataFrame):
    """UpSet plot intersection data."""
    col_map = {
        "Claude": "claude", "Copilot": "copilot", "Cursor": "cursor",
        "Gemini": "gemini", "Codex": "codex", "OpenCode": "opencode", "AGENTS.md": "agents.md",
    }
    all_cats = list(col_map.keys())

    # Category totals, sorted descending, so that the largest is first (rendered at bottom)
    category_totals = {cat: int(configured[col_map[cat]].sum()) for cat in all_cats}
    cats = sorted(all_cats, key=lambda c: -category_totals[c])

    # Compute membership for each repo
    memberships = []
    for _, row in configured.iterrows():
        members = [cat for cat in cats if row[col_map[cat]]]
        if members:
            memberships.append(tuple(sorted(members)))

    # Count intersections
    from collections import Counter
    counts = Counter(memberships)

    # Filter min_subset_size >= 5, sort by size descending (matches analysis PDF)
    intersections = []
    for members, size in counts.items():
        if size >= 5:
            intersections.append({"members": list(members), "size": size})
    intersections.sort(key=lambda x: -x["size"])

    return {
        "categories": cats,
        "category_colors": UPSET_COLORS,
        "category_totals": category_totals,
        "intersections": intersections,
    }


def build_chart_count_distribution(artifacts: dict):
    """The distribution of the context files, skills and subagents per repository."""
    max_individual = 7

    categories = []
    for label, key, col in [
        ("Context Files", "context_files", "context_file"),
        ("Skills", "skills", "skills.md"),
        ("Subagents", "subagents", "subagent"),
    ]:
        df = artifacts[key]
        counts = df.groupby("repo_name").size()
        n = len(counts)
        clipped = counts.clip(upper=max_individual + 1)
        dist = clipped.value_counts().sort_index()
        dist = dist.reindex(range(1, max_individual + 2), fill_value=0)
        values = [round(v / n * 100, 1) for v in dist.values]
        categories.append({
            "label": label,
            "color": CONTEXT_FILE_COLORS[label] if label in CONTEXT_FILE_COLORS else "#999",
            "n": n,
            "values": values,
        })

    bins = [str(i) for i in range(1, max_individual + 1)] + ["8+"]
    return {"bins": bins, "categories": categories}


def build_chart_creation_order(artifacts: dict):
    """The five most frequent creation order sequences of the context file categories."""
    df = artifacts["context_files"].copy()
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["category"] = df["context_file"].apply(classify_context_file)
    df = df[df["category"].notna()]

    # Earliest created_at per (repo, category)
    first = df.groupby(["repo_name", "category"])["created_at"].min().reset_index()
    first["date"] = first["created_at"].dt.normalize()

    sequences = []
    for repo, group in first.groupby("repo_name"):
        cats = group[["date", "category"]].sort_values("date")
        if cats["category"].nunique() < 2:
            continue
        date_groups = (
            cats.groupby("date")["category"]
            .apply(lambda x: tuple(sorted(x)))
            .tolist()
        )
        sequences.append(tuple(date_groups))

    seq_counts = pd.Series(sequences).value_counts()
    top = seq_counts.head(5)
    other_count = seq_counts.iloc[5:].sum() if len(seq_counts) > 5 else 0

    def fmt_seq(seq):
        parts = []
        for group in seq:
            if len(group) == 1:
                parts.append(group[0])
            else:
                parts.append("{" + ", ".join(group) + "}")
        return " then ".join(parts)

    result = []
    for seq, count in top.items():
        result.append({"label": fmt_seq(seq), "count": int(count), "color": "#a8a29e"})
    if other_count > 0:
        result.append({"label": "Other", "count": int(other_count), "color": "#a8a29e"})

    return {"sequences": result, "n_repos": len(sequences)}


def build_chart_cumulative_adoption(artifacts: dict):
    """The cumulative number of repositories by the creation month of their earliest file in each
    family of the timeline: the context file categories of `_shared.ADOPTION_CONTEXT_FILE_CATEGORIES`,
    the skills, the subagents and the planning documents, as `4_data-analysis/rq3/adoption_timeline.py`
    plots them. A file is dated by `created_at`, the date of the commit that created it under
    whatever name it had then, and a file created before `_shared.ADOPTION_START` was created for
    another purpose and is left out."""
    start = pd.Timestamp(ADOPTION_START, tz="UTC")

    def first_months(df: pd.DataFrame) -> pd.Series:
        """The month of each repository's earliest file created from ADOPTION_START on."""
        dates = pd.to_datetime(df["created_at"], utc=True)
        kept = df.assign(created_at=dates)[dates >= start]
        first = kept.groupby("repo_name")["created_at"].min()
        return first.dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()

    cf = artifacts["context_files"]
    categories = cf["context_file"].apply(context_file_row_category)
    firsts = {label: first_months(cf[categories == label])
              for label in ADOPTION_CONTEXT_FILE_CATEGORIES}
    for label, key in (("Skills", "skills"), ("Subagents", "subagents"),
                       ("Planning Documents", "planning_documents")):
        firsts[label] = first_months(artifacts[key])
    firsts = {label: first for label, first in firsts.items() if not first.empty}

    series_data = {label: first.value_counts().sort_index().cumsum()
                   for label, first in firsts.items()}
    all_dates = sorted(set().union(*(s.index for s in series_data.values())))
    all_repos = set().union(*(set(first.index) for first in firsts.values()))

    series = []
    for label, s in series_data.items():
        aligned = s.reindex(all_dates).ffill().fillna(0).astype(int)
        first_nonzero = aligned[aligned > 0]
        if first_nonzero.empty:
            continue
        aligned = aligned.loc[first_nonzero.index[0]:]
        data = [
            {"date": d.strftime("%Y-%m"), "value": int(v)}
            for d, v in zip(aligned.index, aligned.values)
        ]
        series.append({
            "label": label,
            "color": CONTEXT_FILE_COLORS.get(label, "#999"),
            "marker": CONTEXT_FILE_MARKERS.get(label, "circle"),
            "data": data,
        })

    return {"series": series, "n_repos": len(all_repos), "start": ADOPTION_START}


def build_chart_reference_network(artifacts: dict):
    """Directed heatmap of context file references."""
    df = artifacts["context_files"].copy()
    df["category"] = df["context_file"].apply(classify_context_file)
    df = df[df["category"].notna()]

    refs = df[df["is_reference"].apply(to_bool)].copy()

    edges = []
    for _, row in refs.iterrows():
        source = row["category"]
        ref_str = str(row["referenced_file"])
        for ref in ref_str.split(";"):
            ref = ref.strip()
            if ref:
                # Classify target
                target = classify_reference(ref) or "Other"
                edges.append({"source": source, "target": target})

    sources = list(CONTEXT_FILE_CATEGORIES.keys())
    targets = sources + ["Other"]

    # Build matrix
    matrix = [[0] * len(targets) for _ in sources]
    for e in edges:
        si = sources.index(e["source"])
        ti = targets.index(e["target"])
        matrix[si][ti] += 1

    # Shorten labels
    short = {"Copilot Instructions": "Instructions"}
    display_sources = [short.get(s, s) for s in sources]
    display_targets = [short.get(t, t) for t in targets]

    return {
        "sources": display_sources,
        "targets": display_targets,
        "matrix": matrix,
        "n_pairs": len(edges),
    }


def build_all_charts(configured: pd.DataFrame, artifacts: dict):
    logs.info(COMPONENT, "Pre-computing chart data...")
    charts = {
        "config_tool_heatmap": build_chart_config_tool_heatmap(configured, artifacts),
        "language_tool_heatmap": build_chart_language_tool_heatmap(configured),
        "tool_cooccurrence": build_chart_tool_cooccurrence(configured),
        "upset_tool_usage": build_chart_upset(configured),
        "count_distribution": build_chart_count_distribution(artifacts),
        "creation_order_sequences": build_chart_creation_order(artifacts),
        "cumulative_repo_adoption": build_chart_cumulative_adoption(artifacts),
        "reference_network": build_chart_reference_network(artifacts),
    }
    for name, data in charts.items():
        write_json(SITE_DIR / "data" / "charts" / f"{name}.json", data, indent=2)
        logs.info(COMPONENT, f"  Wrote {name}.json.")


# --- Phase 8: Downloads info -------------------------------------------------
def release_version() -> str:
    """The `dataset_version` that the release's provenance.json records, which names the
    directory of the release on the asset host."""
    path = DATA_DIR / "provenance.json"
    try:
        version = json.loads(path.read_text(encoding="utf-8")).get("dataset_version")
    except (OSError, ValueError, AttributeError) as error:
        logs.error(COMPONENT, f"{path} cannot be read ({error}). Build the release first "
                              f"(python3 -m agentcontext release).")
        raise SystemExit(1)
    if not version:
        logs.error(COMPONENT, f"{path} records no dataset_version. Build the release again "
                              f"(python3 -m agentcontext release).")
        raise SystemExit(1)
    return version


def stage_downloads(version: str):
    """Put the download files of the release into the directory of the release under
    ASSETS_DIR, which upload_assets.sh mirrors to the asset host, as hard links where the file
    system allows them."""
    target = ASSETS_DIR / version
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in DOWNLOAD_FILES:
        try:
            os.link(DATA_DIR / name, target / name)
        except OSError:
            shutil.copyfile(DATA_DIR / name, target / name)
    logs.info(COMPONENT, f"  Put {len(DOWNLOAD_FILES):,} download files into {target}.")


def build_downloads_json(version: str):
    """Write a JSON file that lists the download files with their sizes and their addresses in
    the directory of the release on the asset host."""
    logs.info(COMPONENT, "Building downloads.json...")
    # A file that the release lacks ends the build rather than being listed with size 0 and a
    # link, which would offer a download that the site cannot describe.
    missing = [name for name in DOWNLOAD_FILES if not (DATA_DIR / name).is_file()]
    if missing:
        logs.error(COMPONENT, f"{DATA_DIR} lacks the download files {', '.join(missing)}. "
                              f"Rebuild the release with python3 -m agentcontext release.")
        raise SystemExit(1)

    downloads = []
    for filename, description in DOWNLOAD_FILES.items():
        filepath = DATA_DIR / filename
        size = filepath.stat().st_size
        downloads.append({
            "filename": filename,
            "description": description,
            "size": size,
            "url": f"{DOWNLOAD_HOST_URL}/{version}/{filename}",
        })

    write_json(SITE_DIR / "data" / "downloads.json", downloads, indent=2)
    logs.info(COMPONENT, f"  Wrote downloads.json ({len(downloads):,} files).")


# --- Test usage example ------------------------------------------------------
def test_usage_example():
    """Run usage_example.py against the release, and end the build when it fails."""
    logs.info(COMPONENT, "Testing usage_example.py...")
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "usage_example.py")],
        capture_output=True, text=True, cwd=str(DATA_DIR),
    )
    if result.returncode != 0:
        logs.error(COMPONENT, f"usage_example.py exited {result.returncode}. The last lines of its "
                              f"stderr follow.")
        for line in result.stderr.splitlines()[-20:]:
            logs.error(COMPONENT, f"  {line}")
        raise SystemExit(1)
    logs.info(COMPONENT, "  usage_example.py passed.")


# --- Phase 9: Copy HTML template ---------------------------------------------
def highlight_python(code: str) -> str:
    """Simple syntax highlighting of Python code as HTML spans."""
    import re

    KEYWORDS = {
        "import", "as", "from", "def", "class", "return", "if", "else",
        "elif", "for", "in", "not", "and", "or", "True", "False", "None",
        "with", "try", "except", "raise", "pass", "lambda",
    }

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def highlight_line(line: str) -> str:
        # Find comment start (outside strings)
        in_str = False
        comment_idx = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' and not in_str:
                in_str = True
            elif ch == '"' and in_str:
                in_str = False
            elif ch == '\\' and in_str:
                i += 1  # skip escaped char
            elif ch == '#' and not in_str:
                comment_idx = i
                break
            i += 1

        if comment_idx is not None:
            code_part = line[:comment_idx]
            comment_part = line[comment_idx:]
        else:
            code_part = line
            comment_part = ""

        # Tokenize code part into strings and non-strings
        parts = re.split(r'("(?:[^"\\]|\\.)*")', code_part)
        out = []
        for part in parts:
            if part.startswith('"') and part.endswith('"'):
                out.append(f'<span class="string">{esc(part)}</span>')
            else:
                # Highlight keywords in non-string text
                words = re.split(r'(\b\w+\b)', esc(part))
                for w in words:
                    if w in KEYWORDS:
                        out.append(f'<span class="keyword">{w}</span>')
                    else:
                        out.append(w)

        if comment_part:
            out.append(f'<span class="comment">{esc(comment_part)}</span>')

        return "".join(out)

    return "\n".join(highlight_line(line) for line in code.split("\n"))


def copy_html():
    logs.info(COMPONENT, "Copying index.html...")
    src = SCRIPT_DIR / "index.html"
    dst = SITE_DIR / "index.html"

    html = src.read_text(encoding="utf-8")

    # Inject usage example from usage_example.py
    example_path = SCRIPT_DIR / "usage_example.py"
    if example_path.exists():
        code = example_path.read_text(encoding="utf-8").rstrip("\n")
        highlighted = highlight_python(code)
        html = html.replace("<!-- USAGE_EXAMPLE_CODE -->", highlighted)
        logs.info(COMPONENT, "  Inserted usage_example.py into index.html.")

    # Copy the .py file itself to _site for download
    shutil.copy2(example_path, SITE_DIR / "usage_example.py")

    dst.write_text(html, encoding="utf-8")
    logs.info(COMPONENT, f"  Wrote {dst}.")


def check_site_size(total_size: int):
    """End the build when the site is larger than GitHub Pages publishes."""
    if total_size > PAGES_LIMIT_BYTES:
        logs.error(COMPONENT, f"The site has {total_size:,} bytes, more than the "
                              f"{PAGES_LIMIT_BYTES:,} bytes that GitHub Pages publishes.")
        raise SystemExit(1)


# --- Main --------------------------------------------------------------------
def main():
    if not (DATA_DIR / "repos.csv").is_file():
        logs.error(COMPONENT, f"{DATA_DIR} contains no repos.csv. Build the release first "
                              f"(python3 -m agentcontext collected-files-archive && "
                              f"python3 -m agentcontext release).")
        raise SystemExit(1)

    version = release_version()
    # Clean output directory
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)

    test_usage_example()
    repos, configured, commits, artifacts = load_data()
    build_index(configured)
    build_summary(repos, configured, commits, artifacts)
    paginate_repos(configured, artifacts)
    paginate_commits(commits)
    build_artifact_jsons(artifacts)
    build_all_charts(configured, artifacts)
    build_downloads_json(version)
    stage_downloads(version)
    copy_html()

    # Log the summary
    total_size = sum(
        f.stat().st_size for f in SITE_DIR.rglob("*") if f.is_file()
    )
    file_count = sum(1 for f in SITE_DIR.rglob("*") if f.is_file())
    logs.info(COMPONENT, f"Wrote {file_count:,} files ({total_size / 1024 / 1024:,.1f} MiB) to "
                         f"{SITE_DIR}.")
    check_site_size(total_size)


if __name__ == "__main__":
    main()
