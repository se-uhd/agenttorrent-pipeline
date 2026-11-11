# Dataset explorer website

This directory builds the static dataset explorer website, where you can browse published repositories, configuration artifacts, commits, and charts, and download the CSV files. The deployment target is a separate GitHub Pages repository, `se-uhd/agenttorrent`, published at https://se-uhd.de/agenttorrent/. `se-uhd/agentcontext-dataset` and `se-uhd/ai-config-dataset`, the repositories of the earlier addresses, contain only pages that forward https://se-uhd.de/agentcontext-dataset/, https://se-uhd.de/ai-config-dataset/ and every path below them to the same path at https://se-uhd.de/agenttorrent/.

From the repository root, install the dependency lock `locks/development.txt`, which locks `requirements.txt`, and the `agentcontext` package, which `build_site.py` imports for the log format, into `.venv`:

```shell
mise exec -- bash dev/setup.sh
```

## Files

- `index.html`: the HTML, CSS and JavaScript template of the single-page site. It contains the `<!-- USAGE_EXAMPLE_CODE -->` placeholder that `build_site.py` replaces with a syntax-highlighted copy of `usage_example.py`, and the `release-notes` section, which describes the current dataset, the changes of the release and the evidence limits.
- `requirements.txt`: the one dependency, pandas.
- `usage_example.py`: the pandas usage example that the site shows and offers for download beside the data. It studies a question with each CSV of the dataset that it reads, such as whether agents co-author their own instructions.
- `build_site.py`: builds the static site.
- `deploy.sh`: copies the built site into a GitHub Pages repository.
- `upload_assets.sh`: uploads the download files of each release to the asset host.
- `test_build_site.py`: the suite. It checks that the module imports, that explicit configuration paths and unrelated references receive the categories that the test specifies independently of the code, that only the rows of `context-files.csv` that a Codex, Gemini or OpenCode configuration names fall under Named in Configuration, and that each cell of the mechanism-by-tool heatmap counts the repositories with a configuration artifact attributed to its tool. The site imports the context file classifier of `4_data-analysis/_shared.py` rather than a copy of it. A copy falls behind the classifier, and a row of a category that the copy lacks then falls into no category and is missing from every chart on the site, while the analysis figures count it. Run the suite as `python3 -m unittest test_build_site` from this directory. The CI workflow `.github/workflows/pipeline-tests.yml` and `python -m agentcontext verify` run it too.

## Release notes

Update the `release-notes` section of `index.html` when the dataset or its evidence limits
change. After each dataset release, regenerate the site and the analysis tables and figures.
The checks read the current inputs and the generated outputs, and none of them compares a
value with the count of an earlier publication. Keep citations out of the release notes. The
header above them cites the release on Zenodo with the authors of its `CITATION.cff` and the
paper that describes release 1.0.0. The header, the Downloads section and the Usage Example
section link the concept DOIs of the Zenodo records of the dataset and of the pipeline.

## Building

Build the release first with `python3 -m agentcontext collected-files-archive && python3 -m agentcontext release`. Then run `build_site.py` from this directory. It reads `../data/collection/release/`, the files that Zenodo users download, and builds the site in `_site/`: paginated JSON for repositories, commits, and configuration artifacts, chart data, download metadata, the processed `index.html`, and `usage_example.py`.

GitHub Pages publishes the site and its data, with a 1 GB limit. The build exits 1 above that limit, so commit pages contain only message subjects; full messages are in `commits.csv.7z`. Repository details link configuration artifacts to the scanned branch on GitHub, and `repos_data.7z` contains the files. Release downloads go to `_assets/<version>/`, using the version in `provenance.json` and hard links where supported. The site links to that directory on the asset host.

```shell
python3 build_site.py
```

Inputs:
- `../data/collection/release/*.csv`, i.e., `repos.csv`, the commit CSVs and the per-mechanism CSVs
- `../data/collection/release/provenance.json`, for the version of the release
- the download files of the release, for their sizes and for `_assets/`
- `../4_data-analysis/_shared.py`, for the tool and mechanism lists and the cells of the mechanism-by-tool heatmap, imported rather than restated

Output:
- `_site/`: the complete static site (`index.html`, `usage_example.py`, `data/`). It is gitignored and rebuilt whole on every run, because `build_site.py` deletes the directory before it starts.
- `_assets/<version>/`: the download files of the release for the asset host, gitignored and rebuilt for the version of the release on every run.

The build exits 1 in each of these cases:

- The release lacks `repos.csv`, `provenance.json`, its `dataset_version` or a file that the
  site offers for download.
- `usage_example.py` fails against the release.
- The site is larger than GitHub Pages publishes.

Rebuild the release and then the site to resolve these failures.

The build reads the release's CSVs with a blank cell as the only missing value. pandas reads
`n/a` as missing by default, but `n/a` is a value of the release, in `memory` of
`subagents.csv` and in `language` of the mechanism CSVs, which the site would otherwise show
empty.

## Deploying

Upload the download files first with `upload_assets.sh`, so that the links of the site work when it goes live. The script mirrors `_assets/` to `data/agenttorrent/` on the asset host with lftp, as `scripts/sync-assets.sh` of se-uhd.github.io uploads the teaching assets, with the login that `~/.config/se-uhd/assets-ftp.conf` gives, and never deletes anything on the server. `--dry-run` lists the transfers.

```shell
./upload_assets.sh
```

Then run `deploy.sh` with the checkout of the GitHub Pages repository as its target. It copies `_site/` into the target with rsync, which deletes stale files, and creates `.nojekyll`, a `.gitignore` and the GitHub Actions deployment workflow. Commit and push in the target repository to publish the site.

```shell
./deploy.sh ../../agenttorrent
```

## Site contents

The configuration browser has a tab for each mechanism, Plugins and LSP included, and one
each for planning documents and imports, which are not configuration artifacts. Each
tab renders its path column as a link: the configuration artifact, the planning document, or
the line of the import. The regression test of the browser runs the page's JavaScript with
Node.js and checks the tabs against the mechanism definitions and the two extensions.

The commit browser lists the commits of `commits.csv` and `other_commits.csv`, a commit in both
files once with the tools of both. The tool filter of the commits matches a commit under each
tool that the commit credits, and its option Other tools matches a commit that credits a tool
outside the six. The dashboard counts the commits that credit one of the six tools. The
search reads the subject line, which is the part of the message that the commit pages hold. Filtered searches
cover at most the newest <!--s:parameters._website.index.commit_search_limit-->5,000<!--/s--> commits, and the result label states how many commits
were searched and the dataset's total. Unfiltered pagination covers all commits.

The site shows the subject line of each commit as the commit CSV stores it.
The dashboard counts the six tools and shows the repositories with no specific
tool as a separate category. That category is the `no_specific_tool` flag of
`repos.csv`, TRUE for a repository with only shared configuration artifacts,
such as `AGENTS.md`, `.agents/skills` or a root `.mcp.json`. Downloads include
`other_commits.csv` for attributions outside the six tools, `planning-documents.csv`, and
`imports.csv` with `imported_files.7z`. The build runs its
usage example with the interpreter that runs `build_site.py`.

The timeline of the creation of the earliest configuration artifacts plots the families of
the figure of `4_data-analysis/rq3/adoption_timeline.py` from the same constants of `_shared.py`:
the five kinds of context file and the skills and subagents of the timeline of release 1.0.0,
and the planning documents. It leaves out the files created before `ADOPTION_START`, which
were created for another purpose and only later renamed into a configuration or named by one.

Each cell of the mechanism-by-tool heatmap counts the repositories with a
configuration artifact of the mechanism that the `tool` column of the mechanism CSV
attributes to the tool. A file that several tools read, written `claude|cursor`, counts under each of them,
and a shared file, written `any`, counts under none. The last column counts the
repositories with no specific tool that have the mechanism. `build_site.py` takes the
cells from `_shared.mechanism_tool_counts`, which also computes the cells of
`4_data-analysis/rq2/config_tool_heatmap.py`. The explorer and the analysis figure therefore
show the same matrix. The UpSet of tool combinations has the `agents.md` context file flag,
labeled AGENTS.md, as a seventh set beside the six tools, as the analysis figure does.
