import pandas as pd

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 10)

# Run this script in the directory of the downloaded release, after extracting repos.csv.7z and
# commits.csv.7z with `7z x <archive>`. The script reads only the CSVs. The files themselves are
# in repos_data.7z, which contains the configuration artifacts and the planning documents, and in
# imported_files.7z, which contains the files that the configuration artifacts import.

# repos.csv lists the GitHub repositories in the sampling frame with their metadata and a label of
# whether each hosts an engineered software project ("true", "false" or "unclear"). Read the label
# as text, because pandas turns a column of only "true" and "false" into booleans. Only the
# configured repositories, which contain a configuration artifact, have a scan date and the TRUE
# and FALSE flags of the tools and mechanisms.
repos = pd.read_csv("repos.csv", dtype={"engineered_project": str})
engineered_repos = repos[repos["engineered_project"] == "true"]
configured_repos = repos[repos["scanned_at"].notna()]

# Which tools do the engineered software projects configure, and does the choice depend on the
# main language of a project?
tools = ["claude", "copilot", "cursor", "gemini", "codex", "opencode", "no_specific_tool"]
uses_tool = configured_repos[tools].eq(True)
print(f"{len(configured_repos):,} of {len(engineered_repos):,} engineered projects are configured")
print(uses_tool.mean().sort_values(ascending=False))
languages = configured_repos["mainLanguage"].value_counts().index[:5]
print(uses_tool.groupby(configured_repos["mainLanguage"]).mean().loc[languages].round(2))

# Do agents co-author their own instructions? context_files.csv has one row per context file, such
# as CLAUDE.md or AGENTS.md. first_commit_ai_created says whether the commit that created the file
# credits one of the six tools, and first_commit_ai_tool names the tools that it credits. Several
# tools read AGENTS.md, so which tools do the commits that created an AGENTS.md credit?
context_files = pd.read_csv("context_files.csv")
names = context_files["context_file"].value_counts().index[:4]
top_files = context_files[context_files["context_file"].isin(names)]
print(top_files.groupby("context_file")["first_commit_ai_created"].apply(lambda s: s.eq(True).mean()))
agents_md = context_files[context_files["context_file"] == "AGENTS.md"]
print(agents_md["first_commit_ai_tool"].str.split(",").explode().value_counts())

# How often does an import reach no file, and what do the other imports reach? imports.csv has one
# row per import in a configuration artifact, such as @docs/style.md in a CLAUDE.md. target_status
# is found for an import that reaches a file of the repository that the tool reads, and otherwise
# names the reason, such as missing. For a found file, target_category names its mechanism. The
# depth is 1 for an import in the configuration artifact itself and 2 or more for an import in a
# file that it imports.
imports = pd.read_csv("imports.csv")
print(imports["target_status"].value_counts(normalize=True))
print(imports.loc[imports["target_status"] == "found", "target_category"].value_counts())
print(imports["depth"].value_counts().sort_index())

# Which spec-driven development frameworks do the projects use, and since when?
# planning-documents.csv lists the specs, plans and task lists that these frameworks and the
# planning features of the tools write. Its framework column names the framework, or the tool for a
# planning feature of a tool.
planning = pd.read_csv("planning-documents.csv")
planning["created_at"] = pd.to_datetime(planning["created_at"], utc=True)
print(planning.groupby("framework").agg(repositories=("repo_name", "nunique"),
                                        first_document=("created_at", "min"))
      .sort_values("repositories", ascending=False).head(8))

# How many commits credit each tool per month, and how do they credit it? commits.csv lists the
# commits that credit one of the six tools, and other_commits.csv those that credit another tool.
# attributions gives the form for each tool: account for a commit by the tool's own account,
# reviewer for the co-author trailer of a committed review suggestion, standard for a line that
# the tool writes itself, and convention for any other line that names the tool.
commits = pd.read_csv("commits.csv", usecols=["repo_name", "commit_timestamp", "ai_tool",
                                              "attributions"])
commits["month"] = pd.to_datetime(commits["commit_timestamp"], utc=True).dt.strftime("%Y-%m")
per_tool = commits.assign(tool=commits["ai_tool"].str.split(",")).explode("tool")
print(per_tool.groupby(["month", "tool"]).size().unstack(fill_value=0).tail(6))
print(commits["attributions"].str.split(";").explode().value_counts().head(10))

# How many skills come with scripts? Each of the other mechanisms has a CSV of its own, such as
# skills.csv, hooks.csv and mcp.csv, with the creation date and the number of commits of each file.
# In skills.csv, scripts says whether the folder of a skill has a scripts directory.
skills = pd.read_csv("skills.csv")
print(f"{skills['scripts'].eq(True).mean():.0%} of the skills have a scripts directory")
