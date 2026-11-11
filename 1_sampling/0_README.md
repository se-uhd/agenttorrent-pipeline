# Sampling

This stage builds the sampling frame. It takes a retrieval of the [SEART GitHub Search (GHS)](https://seart-ghs.si.usi.ch/) dataset, queries GitHub for every repository in the retrieval, compares the retrieval with the previous one, and filters it. The scripts and their configuration are in this directory. Everything that they read and write is in `../data/sampling/`.

The cache of retrievals, `../data/sampling/ghs_retrievals/`, is tracked in git and contains every retrieval: the GHS search result, GitHub's response for each of its repositories, and an index row with the query, the sampling window, the counts and every attempt. Its first entry is the GHS search result of March, `2026-03-16T180701Z`, imported from the download of that day. The tracked sampling frame, `../data/sampling/ghs_filtered.json.7z`, which the development dataset uses, is the sampling frame of retrieval `2026-11-02T223052Z`. The sampling window of that retrieval ends on 2026-10-22 ([docs/dataset.md](../docs/dataset.md#sampling-and-collection-dates)). The sampling window moves with every collection. [docs/refresh.md](../docs/refresh.md) describes when a run takes a new retrieval and the work that each kind of change requires.

## Prerequisites

Set up the development environment as [docs/development.md](../docs/development.md) describes
for [macOS](../docs/development.md#macos) or [Linux](../docs/development.md#linux-ubuntu). The
[x86-64 development container](../docs/development.md#linux-x86-64-development-container) runs
every stage and the complete verification suite.

- Python 3.14, the version that `mise.toml` pins for the whole pipeline
- At least one GitHub access token for the GitHub query, in `credentials.csv` at the repository root ([docs/credentials.md](../docs/credentials.md)), in `GITHUB_TOKENS` (comma-separated) or in `GITHUB_TOKEN`. A GitHub access token without scopes can read public repositories. GitHub's rate limit applies per account, and with the GitHub access tokens of several accounts, the query spreads its requests over the accounts

Every sampling script imports the `agentcontext` package for logging, and `run_sampling.py` imports `agentcontext/runner.py` from it. `requirements.txt` pins the direct Python dependencies, and the dependency lock `locks/development.txt` locks them with those of every other stage. From the repository root, install the dependency lock and the package into `.venv` ([Python environments](../docs/development.md#python-environments)):

```shell
mise exec -- bash dev/setup.sh
```

## Running the sampling stage

`run_sampling.py` runs the steps of the four sections below in order, as one command. It passes them to `../agentcontext/runner.py` as a list, and `../run_pipeline.py` runs the same steps first when it runs the whole pipeline. The analysis stage renders the paper's table of the filter steps in a step of its own (`../4_data-analysis/dataset/filter_steps_table.py`). The steps run under the step interpreter of `../agentcontext/runner.py`, which is `DATA_COLLECTION_PYTHON` when that variable is set, else the interpreter that runs the runner.

```shell
python3 run_sampling.py
python3 run_sampling.py --from ghs_download    # take a new retrieval
python3 run_sampling.py --from repository_filtering   # filter the same retrieval again
python3 run_sampling.py --list                            # show the steps and exit
```

Each step logs to `../data/sampling/run_logs/<step>.log`. The runner writes the pipeline execution state to `../data/sampling/run_logs/state.json`. The file lists the steps that completed with the fingerprint of each, the git commit, the exact command, the step interpreter, the status and duration of each step, and the download's report. The next run skips a completed step.

A retrieval taken again would differ from the first, because GHS updates its rows in place. The pointer, `../data/sampling/ghs_retrieval.json`, names the retrieval that the stage reads. When the pointer is on disk, the runner records the download as complete without running it and logs `INFO runner: ghs_download is irreproducible, and its output is on disk`. To take a new retrieval, run `python3 run_sampling.py --from ghs_download`.

The runner runs each of the three steps after the download again when the pointer changes or when the rows of the retrieval or GitHub's responses in the cache change. After a new retrieval, it runs all three again. After a download that fell back to the previous retrieval, it runs none of them. After a GitHub query that wrote new responses, it runs the comparison and the filter again. It also runs the filter again after an edit of `config/ghs_filter.yaml`, `config/licenses.csv` or `config/spdx_licenses.json`.

The runner also runs the comparison again when the retrieval before the pointed one changes. A change of the collected list, `../data/collection/data/collected/repos.csv`, does not run it again, because the cloning step rewrites that list later in the same run. The comparison therefore compares each new retrieval with the collection before it, and the runner does not run it again at the next Start after a completed run. To compare the retrieval with the collected list on disk, run `python3 run_sampling.py --from retrieval_comparison`.

`run-sampling-spec.md` describes the flags, the complete behavior and the known limitations of the runner.

## 1. Take a GHS retrieval (`ghs_download`)

`1_download_ghs.py` requests from GHS the repositories that match the query in `config/ghs_download.yaml`: a license, no forks, at least <!--s:parameters.1_sampling.config.ghs_download.query.contributorsMin-->2<!--/s--> contributors and <!--s:parameters.1_sampling.config.ghs_download.query.pullsMin-->1<!--/s--> pull request, created at least <!--s:parameters.1_sampling.config.ghs_download.window.created_before_months-->18<!--/s--> months before the window's end date, with a commit in the <!--s:parameters.1_sampling.config.ghs_download.window.committed_within_months-->6<!--/s--> months before it. The config uses GHS's own parameter names. The download rejects a name that GHS does not define, because GHS would ignore the name and return the results of a wider query.

By default, the end date is the oldest crawl date of GHS's languages. A language's crawl date is the latest `lastCommit` date that GHS has in that language. GHS crawls one language at a time and updates a repository only when it crawls a push to it, so GHS has recorded every push before the oldest crawl date. The download takes a crawl date more than <!--s:parameters.1_sampling.config.ghs_download.window.max_crawl_lag_days-->30<!--/s--> days old as a sign that the crawler has stopped for that language. For a language that the previous sampling frame kept, the download fails the retrieval on such a crawl date, and also when the language has no crawl date after every attempt. The download leaves any other language with such a crawl date out of the end date and logs a warning. When no sampling frame on disk names the languages that it kept, as at the first Start on an empty data root, the download leaves out every language with such a crawl date or with none in the same way. With a date as the end date, the download requests no crawl date.

The filter leaves out every language that the end date left out. Because the filter keeps the most common languages of each retrieval, the download can tell from the rows of the GHS search result whether the filter would otherwise keep such a language. After the download, it counts the repositories of the GHS search result under a license that the filter keeps. When a language that the end date left out is among the <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages-->10<!--/s--> most common languages of these repositories, the download names it in a warning and takes the retrieval. When no sampling frame names the languages that it kept, the download fails the retrieval instead ([docs/refresh.md](../docs/refresh.md)). It counts with the filter's own config and license step, and with the filter's `--top-languages` when a step argument sets it.

```shell
python3 1_download_ghs.py
python3 1_download_ghs.py --dry-run   # the sampling window and the count
```

Before it downloads the GHS search result, `1_download_ghs.py` sends short requests for GHS's version, its languages, their crawl dates and the number of repositories that match the query, so that a failure of GHS shows before the stream of the GHS search result, which took <!--s:release_2026_03.ghs_export_minutes-->74<!--/s--> minutes in March. Each request and the download have a bounded number of attempts, and an attempt at the language list reads at most <!--s:parameters.1_sampling.1_download_ghs.LANGUAGE_PAGES_MAX:words-->ten<!--/s--> pages. The download treats a response that is not the JSON object that it expects as no response. It does the same with a client error in response to a request that carries no parameter of the config, such as the request for the version page. When a short request gets no response in its attempts, the download makes the attempts again after each of the waits of `retries.probe_round_wait_minutes`, which grow to hours. GHS can end a failed download with a GHS search result that is short but valid. The GHS search result therefore has to echo every parameter, and its number of repositories has to lie between the counts before and after the download. The count after the download is a short request with the same attempts, and a count that fails does not download the GHS search result again.

An attempt at the crawl dates requests only the languages that earlier attempts left without a date, starting with the languages requested least often. A language with a search that fails every time then cannot keep the other languages from being requested, even after each of them has failed once. The attempt ends after <!--s:parameters.1_sampling.1_download_ghs.CRAWL_FAILURES_IN_A_ROW:words-->three<!--/s--> failed requests in a row, and a response with HTTP 200 resets that count. With this bound, an attempt costs a few requests rather than one per language when the search fails for every language. When no sampling frame names its kept languages, a language for which the search finds no repository in the attempts of a round gets no further round. The download then decides from the rows of the GHS search result whether the filter would keep the language.

When GHS returns no response within the attempts and rounds, the download falls back: It writes the pointer to the retrieval that the stage read before, so that the stage continues over the previous sampling frame, and it records the fallback in its report, `../data/sampling/ghs_retrieval_report.json`. A dry run does not fall back. It writes nothing and exits 1. The download falls back in the same way when a retrieval of the same query has fewer than <!--s:parameters.1_sampling.1_download_ghs.MIN_SHARE_OF_PREVIOUS-->90%<!--/s--> of the rows of the retrieval that the stage read before. It also falls back when the retrieval has fewer than <!--s:parameters.1_sampling.1_download_ghs.MIN_LANGUAGE_SHARE_OF_PREVIOUS-->75%<!--/s--> of the rows of a language that the previous sampling frame kept and that had at least <!--s:parameters.1_sampling.1_download_ghs.LANGUAGE_ROWS_MIN-->1,000<!--/s--> rows in the earlier retrieval. Such a drop means that GHS has lost rows, and the collection would delete the trees of the repositories that left the sample ([docs/refresh.md](../docs/refresh.md#retrieval-sizes)). The download does not compare a retrieval taken with other parameters or other window lengths. The report counts the consecutive downloads that fell back. At the end of every Start in which the download fell back, the container's entrypoint logs a warning with the reason and that count, and the README of the release states the fallback among its evidence limits. When GHS has lost the rows for good, take the smaller retrieval for one run with `--accept-smaller`. In this directory, run `python3 run_sampling.py --from ghs_download --step-arg ghs_download:--accept-smaller`. In the container, put `--step-arg ghs_download:--accept-smaller` in the `command:` of `compose.yaml` for one Start ([docs/deploy.md](../docs/deploy.md)).

The download treats a pointer that cannot be read, such as an empty file after a power loss, as no pointer, logs a warning and writes the pointer again. It syncs every file of the cache, the pointer and the report to disk before the rename. Before it downloads the GHS search result, it removes every `<id>.json.gz.part` that a Stop of the container or a killed process left in the cache. When the download cannot write a file on this host, such as on a full disk, the step exits 1 without a fallback, because GHS has not failed. The error message names the file and the free space.

**Output:** a retrieval in `../data/sampling/ghs_retrievals/`, `../data/sampling/ghs_retrieval.json` and `../data/sampling/ghs_retrieval_report.json`

The SEART server sends its certificate without the Let's Encrypt intermediate that issued it. The download therefore verifies the certificate against the trust store of `requests` and the intermediates in `certs/lets-encrypt-intermediates.pem`. When Let's Encrypt starts issuing from an intermediate that the file does not contain, the retrieval fails with a reason that names the file. Print the URL of the issuer, the `CA Issuers - URI`, from the server's certificate:

```shell
openssl s_client -connect seart-ghs.si.usi.ch:443 -servername seart-ghs.si.usi.ch </dev/null 2>/dev/null \
  | openssl x509 -noout -text | grep 'CA Issuers'
```

Append the issuer to the file with a header in the format of the other entries, and check that it chains to a root of the trust store:

```shell
URL='<the CA Issuers URI that the command above printed>'
{ echo; echo "# $URL"
  curl -s "$URL" | openssl x509 -inform DER -noout -subject -issuer -enddate -fingerprint -sha256 | sed 's/^/# /'
  curl -s "$URL" | openssl x509 -inform DER; } >> certs/lets-encrypt-intermediates.pem
python3 -m unittest test_download_ghs.TestTheShippedIntermediates -v
```

Only a certificate that does not verify ends the attempts at once. A TLS connection that the server or the network dropped gets another attempt.

## 2. Query GitHub (`github_query`)

`2_query_github.py` queries GitHub's GraphQL API for every repository in the retrieval, <!--s:parameters.1_sampling.2_query_github.BATCH_SIZE-->50<!--/s--> repositories a request. It requests GitHub's id, the current name, the description, the timestamps in UTC, the archived, disabled, locked, fork, private and mirror flags, the license, the primary language, the stars and the default branch with its HEAD commit. GHS cannot supply these values, because its flags are as old as the repository's last push, a renamed repository has a row under each name, and GHS writes every timestamp on a 12-hour clock without AM/PM. When GitHub does not find a repository under GHS's name, the query looks it up once more through REST, which follows a rename, and queries GraphQL again for the name that REST returns. A run that was stopped resumes, because the next run queries only the rows without a response.

The query sends the rows that still lack a response or a REST check after the first pass again in further rounds, after waits that grow from minutes to hours (`ROUND_WAITS`), because GitHub's errors and an outage of the network come in spells. It splits a batch in half, down to single repositories, only when GitHub ended the batch because the query took too long: with HTTP 502 or 504, or with HTTP 200 and a timeout error of the whole query. A repository that GitHub cannot return in time then stays unresolved alone. The query sends the repositories that came back with an error in a batch again in a batch of their own. A batch that fails in another way gets its attempts and then waits for the next round. A pass ends early after `NO_RESPONSE_BATCHES` batches or REST checks in a row without a response, so that a failure that every request meets ends the query after the rounds rather than holding the run for months. `QUERY_SECONDS` after its start, a run sends no further request and starts no further round. A row with no response or no REST check after the last round is unresolved. When the unresolved rows are at most `UNRESOLVED_MAX_SHARE` of the retrieval, rounded up, or a single row, the query records them and exits 0, and the filter removes them as unresolved. Above that share, the query exits 1, and the next run queries the rows again.

The query drops a GitHub access token only for HTTP 401. Before the first request, it checks every GitHub access token against `/rate_limit`, and it repeats a check that fails on the network or with a server error. When a request gets HTTP 401, the query checks the GitHub access token again and drops it only when the check returns HTTP 401 too. When GitHub rejects every GitHub access token, at the start of the query or later, the query checks them again after each wait of `ROUND_WAITS`. It exits 1 only when GitHub accepts none after the last wait. A spell of such rejections after GitHub has served a request starts the waits from the first again, and the query makes no check after `QUERY_SECONDS`.

```shell
python3 2_query_github.py
```

**Output:** GitHub's responses in `../data/sampling/ghs_retrievals/`

## 3. Compare retrievals (`retrieval_comparison`)

`3_compare_retrievals.py` lists the repositories that entered, left or changed since the previous retrieval. It also counts the collected repositories with a HEAD commit that differs from their collected commit.

```shell
python3 3_compare_retrievals.py
```

**Output:** `../data/sampling/ghs_changes.csv`, `../data/sampling/ghs_collection_changes.csv`, `../data/sampling/ghs_changes.json`, and the files of the previous comparison under `.previous` names when its retrievals or its collected list differ

## 4. Filter the retrieval (`repository_filtering`)

`4_filter_ghs.py` applies the filter steps in `config/ghs_filter.yaml` and reads GitHub's values where GitHub returned them. The steps remove:

- repositories that GitHub no longer has, that the GitHub query left unresolved or that GitHub found without its id
- repositories that GitHub created under the name after the window's bound on the creation date, which GHS applied to an earlier repository under that name
- duplicates that GitHub resolves to one repository
- archived, disabled, locked and forked repositories
- repositories with missing counts, with licenses that are not OSI-approved or that `config/licenses.csv` excludes, or with languages outside the top <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages-->10<!--/s-->
- educational and resource repositories
- repositories with activity below the median commits and watchers

The language step ranks only the languages that the end date of the retrieval covers. A language with a crawl date before the end date, or without a crawl date, is a language that the end date left out (section 1), and GHS lacks its pushes after its crawl date. When the filter ranks such a language among the languages that it keeps, it leaves the language out with a warning that names the language and its crawl date, and it keeps the next most common language instead. The download made the same check (section 1), and the two counts can disagree. The download counted GHS's rows, while the filter counts GitHub's values after the steps before the language step, and a larger `--top-languages` ranks more languages than the download counted. The filter records the language that it left out in the values of the step. The table of the filter steps names the language in the label of the step, and the README of the release names it among its evidence limits. The filter does not stop the run over such a language, because a download run again with the same setting would decide the same way.

```shell
python3 4_filter_ghs.py
python3 4_filter_ghs.py --dry-run
python3 4_filter_ghs.py --check-licenses
```

Beside the sampling frame, the filter writes `../data/sampling/ghs_licenses.csv`. The file lists
every license that reached the license step, with its repositories, its count in the table of the
previous sampling frame, the decision and the review. `config/licenses.csv` contains a review of
each license that the sampling frame keeps or excludes, with the reason, the page that it was read
on and the date. The filter keeps a license that no row of the file covers and names it in a
warning, so that an unattended run continues. `--check-licenses`, which `verify` and CI run, exits
2 until a row covers that license. It also exits 2 when a review contradicts the tracked SPDX list.

**Output:** `../data/sampling/ghs_filtered.json`, the sampling frame, with a `_metadata` block that names the retrieval and contains the config and the record of each filter step

`config/spdx_licenses.json` contains the OSI-approved licenses of the SPDX list that `config/licenses.csv` was reviewed against, with the version of the list and the date on which it was read. The filter only reads the file, and a run in an empty data root therefore uses the list that the image contains. Before each NAS run, refresh and review the list in this directory, as the pre-deployment checks of [docs/deploy.md](../docs/deploy.md) require:

1. Run `python3 4_filter_ghs.py --refresh-spdx`. It replaces the file with the current list, names every license that gained or lost its OSI approval, and exits 2 when it names one.
2. Filter the newest retrieval of `../data/sampling/ghs_retrievals/index.csv` in a scratch data root. In the checkout, the filter would replace the extracted sampling frame and rewrite the tracked `../data/sampling/ghs_licenses.csv`, and the verification would then fail: The analysis stops on a sampling frame that differs from its archive, and `verify` exits 2 on a working tree that is not clean. Replace `<id>` with the id of the newest retrieval:

   ```shell
   scratch=$(mktemp -d)
   mkdir "$scratch/sampling"
   cp ../data/sampling/ghs_licenses.csv "$scratch/sampling/"
   printf '{"retrieval_id": "%s"}\n' "<id>" > "$scratch/sampling/ghs_retrieval.json"
   AGENTCONTEXT_DATA_ROOT="$scratch" python3 4_filter_ghs.py --cache-dir ../data/sampling/ghs_retrievals
   ```

   The filter reads the retrieval from the cache of the checkout and writes the sampling frame and the license table into `$scratch/sampling/`. With the copy of the tracked table, the new table gives each license its count in the tracked table as `previous_repositories`.
3. Run `AGENTCONTEXT_DATA_ROOT="$scratch" python3 4_filter_ghs.py --check-licenses`, which reads the table in the scratch data root. For each license that it names, add a row to `config/licenses.csv` with its SPDX id, the decision `software` or `excluded`, the reason, the page that you read the license on and the date. Then delete the scratch data root.
4. Build the image and the NAS bundle only after these steps.

To extract the tracked sampling frame of retrieval `2026-11-02T223052Z` from its 7z archive, run in this directory:

```shell
7zz x ../data/sampling/ghs_filtered.json.7z -o../data/sampling -y
```

The filter writes the sampling frame to the same path. A run of the filter therefore replaces the extracted sampling frame, which the classification, the analysis, the publication and `subsample` read. Run the command above again to restore it, or pack the archive again when the new sampling frame is to replace the tracked one ([archives](../docs/archives.md#packing-the-archives-again)). Until you do either, the analysis in a checkout stops on the sampling frame, because it differs from the archive that git tracks beside it. A run of the stage over a data root outside the checkout leaves the sampling frame of the checkout alone ([development](../docs/development.md#linux-x86-64-development-container)).

## Tests

`run_sampling.py` has a `unittest` suite that checks the list of steps against the scripts and configs on disk and runs the list over stand-in scripts in a temporary directory. The suite reads neither the real cache nor the real configs, and it reaches no network. `../tests/test_runner.py` tests the runner that the list runs on. The runner and the suite need the standard library and `agentcontext` alone, so CI and the verification run the suite with `python3 -S`, which imports no site packages, with the checkout on `PYTHONPATH`. CI runs it before it installs the dependency lock, and the verification with the interpreter of an empty virtual environment, so that the child processes of the suite find no distribution in either. The tests that compare the step of the filter with `config/ghs_filter.yaml` read it with `yaml` and are in `test_filter_ghs.py`. Run the suite in this directory:

```shell
mise exec -- python3 -m unittest test_run_sampling -v
```

Each script and the cache module has a suite and a spec beside it: `test_ghs_cache.py` with `ghs-cache-spec.md`, `test_download_ghs.py` with `download-ghs-spec.md`, `test_query_github.py` with `query-github-spec.md`, `test_compare_retrievals.py` with `compare-retrievals-spec.md`, and `test_filter_ghs.py` with `filter-ghs-spec.md`. The suites load the scripts by path through `_script_loader.py`, because a name that starts with a digit is not an identifier. Where `requests` and `yaml` are not installed, the suites stub them. The tests that check that a file is synced to disk before its rename read the syncs and renames that `_script_loader.synced_writes` records. No test reaches the network, because fakes of GHS and GitHub respond and fail as the services do.

```shell
python3 -m unittest test_ghs_cache test_download_ghs test_query_github \
  test_compare_retrievals test_filter_ghs -v
```
