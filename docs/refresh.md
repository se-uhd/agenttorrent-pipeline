# Refresh strategy

A complete run of the five stages refreshes the dataset: a new retrieval of the GHS dataset,
GitHub's responses for it, the filter, the classification, the collection, the publication and the
analysis. The development dataset comes from the run of all five stages that started on
2026-10-23 over the sampling frame of its own retrieval and completed on 2026-10-28. A
published dataset comes from a complete run. This document records the decisions of 2026-10-05,
the work that each kind of change requires, and the measurements behind the decisions.

## The decisions

- **The sampling window moves with every collection.** Each collection takes a new retrieval. Its
  window keeps the definition of the sampling frame of March 2026 and ends on the oldest crawl
  date of GHS's languages (`1_sampling/download-ghs-spec.md`). It covers the repositories created
  at least <!--s:parameters.1_sampling.config.ghs_download.window.created_before_months-->18<!--/s-->
  months before the end date, with a commit in the <!--s:parameters.1_sampling.config.ghs_download.window.committed_within_months-->6<!--/s--> months before it. Consecutive sampling frames share most of
  their repositories, and a comparison over time reads the repositories that they share.
- **A collection runs monthly.** A collection that is published as a release is a major version,
  because under the [versioning policy](../CHANGELOG.md#versioning-policy), every new collection
  snapshot is a major version.
- **Every repository in the sample is in each collection.** The cloning step takes over a
  repository with a head commit that has not changed since the previous collection from that
  collection without a clone. It does so as long as the version of git and the records that the
  collection kept are unchanged, and no change to the collector's code, the code of the imports,
  the path heuristics or the import heuristics since that collection reaches the repository
  ([the cloning step](steps/repository_cloning.md)). A change of the collector's code reaches only
  the repositories that its record in `agentcontext/code_versions.py` names
  ([recording a change of the collector](steps/repository_cloning.md#recording-a-change-of-the-collector)),
  and a changed row of the heuristics only the repositories with files that the row matches. The
  tree taken over is the tree that a clone would give. Of the <!--s:collection_2026_10_05.unchanged_heads-->27,487<!--/s--> head commits that did not change between the
  October 2–3 and the October 5–6 collections, <!--s:collection_2026_10_05.unchanged_heads_matching-->27,486<!--/s--> matched in every value of the output. The
  output of the remaining repository changed because of a fix to the collector.
- **When GHS returns no response within the attempts, the run continues over the previous
  sampling frame.** The download records the failure in the cache's index and in its report, and
  the window does not move until the next run. The report counts the consecutive downloads that
  fell back. At the end of each Start in which the download fell back, the entrypoint logs a
  warning that names the reason and the count ([deploy](deploy.md#a-run-every-month)). The README
  of the release states the fallback among its evidence limits ([the release](steps/release.md)).
  The stage exits 0 after a fallback, and the run completes. Without the warning and the evidence
  limit, a GHS that stays smaller, or a crawler that stays stopped, would keep every later month
  on the same sampling frame, and only a warning in the download's log would show it.
- **A stopped crawler fails the download only for a language that the sampling frame keeps.**
  For a language that the previous sampling frame kept, the download fails before it requests the
  GHS search result when the language's crawl date is more than <!--s:parameters.1_sampling.config.ghs_download.window.max_crawl_lag_days-->30<!--/s--> days old, or when it has
  no crawl date after every attempt and round. The download leaves a language with such a crawl
  date that the previous sampling frame did not keep out of the end date, with a warning. Every
  other language counts for the end date. The filter keeps the most common languages of each
  retrieval, and it may rank a language that the end date left out among them. After the download
  of the GHS search result, a warning names such a language, and a monthly download takes the
  retrieval. The filter leaves the language out (below). The previous sampling frame lacks the
  language as well, because its filter did not keep it. A fallback would therefore add no row and
  would only keep the window from moving, every month while the crawler stays stopped. The size
  check compares only the languages that the previous sampling frame kept, because the filter
  removes every row of another language (`1_sampling/download-ghs-spec.md` gives the grounds).
- **The filter ranks only the languages that the end date covers.** A monthly download takes a
  retrieval even when a language that the end date left out ranks among the languages that the
  filter would keep (above). At a first Start, the download decides from the rows of the GHS
  search result whether the filter would keep such a language. The filter instead counts GitHub's
  values after its earlier steps, and with a larger `--top-languages`, it keeps more languages.
  The download and the filter can therefore rank a language near the last place differently. A
  download run again with the same setting decides the same way, and a filter that refused the
  retrieval would stop the run at every Start. Instead, the filter's step `languages` passes over
  such a language with a warning that names the language and its crawl date. It keeps the next
  most common language that the end date covers and records the language that it passed over in
  its values. The sampling frame then has no rows for which GHS lacks the latest pushes, and the
  README of the release names the language among its evidence limits
  (`1_sampling/filter-ghs-spec.md`).
- **The download reads the languages that the filter would keep from the GHS search result.**
  At the first Start on an empty data root, there is no sampling frame and no retrieval to fall
  back to. If every language of GHS were held to the limit, each would need a recent crawl date,
  and a crawler that stopped for any of them would end every Start with exit 1. Without a list of
  the languages that the previous sampling frame kept, the download leaves each language with a
  crawl date more than <!--s:parameters.1_sampling.config.ghs_download.window.max_crawl_lag_days-->30<!--/s--> days old, or without one, out of the end date with a warning. It then
  downloads the GHS search result, as a monthly download does for a language outside that list.
  It counts by `mainLanguage` the rows of the GHS search result with a license that the filter's
  license step keeps, as the filter's step `languages` counts them. It takes as many of the most
  common languages as the `top_n_languages` (<!--s:parameters.1_sampling.config.ghs_filter.top_n_languages-->10<!--/s-->) of `1_sampling/config/ghs_filter.yaml` gives, or the
  filter's `--top-languages` when a step argument gives it, and the name decides a tie in count.
  When a language that the end date left out is among them, the retrieval fails. The filter would
  leave that language out, and a crawler that stopped for it, or a failed search for its crawl
  date, would drop a language that the filter keeps from the first sampling frame. With no
  retrieval to fall back to, the stage exits 1, and the download runs again at the next Start.
  Otherwise the download takes the retrieval. The decision rests on these grounds:
  - GHS states no count per language before the download of the GHS search result. Its language
    list, `/api/l`, names the languages without counts, and a count per language would take one
    search per language with the query and the window. The GHS search result contains every row
    with its `mainLanguage` and its `license`, and counting them costs no request.
  - The filter's step `languages` counts GitHub's primary language after the merge with GitHub's
    responses and after the filter steps before it. The GHS search result holds GHS's values from
    before those steps. Of those steps, the license step removes the most rows, and it is the only
    one that needs no response from GitHub. The download applies it with the filter's own code
    over GHS's `license`. The <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages:words-->ten<!--/s--> most common values of GHS's `mainLanguage` in the retrieval of
    2026-10-12 were the languages that the filter kept in the sampling frame of that retrieval.
    In each usable retrieval in the cache on 2026-10-19, the tenth language led the eleventh by
    at least <!--s:retrieval_sizes_2026_10_19.tenth_language_lead_min-->2,205<!--/s--> rows over all rows and by at least <!--s:retrieval_sizes_2026_10_19.tenth_language_lead_licensed_min-->677<!--/s--> under the license step. In the
    two retrievals with GitHub's responses, it led in the rows that reached the filter's step
    `languages` as well. The same <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages:words-->ten<!--/s--> languages led each time, and the steps that the
    download leaves out did not change which languages lead.
  - When the end date left out a language that the filter would keep, the first download fails,
    because the first sampling frame would lack the language. A monthly download takes the
    retrieval with a warning instead, because the previous sampling frame lacks the language as
    well. A language that the filter removes changes no row of the sampling frame, and the
    download needs no crawl date for it.
- **A retrieval with far fewer rows than the previous one counts as a failed download.** The
  stage falls back to the previous retrieval when a retrieval of the same query has fewer than <!--s:parameters.1_sampling.1_download_ghs.MIN_SHARE_OF_PREVIOUS-->90%<!--/s-->
  of the rows of the retrieval that the stage read before. It also falls back when a language
  that had at least <!--s:parameters.1_sampling.1_download_ghs.LANGUAGE_ROWS_MIN-->1,000<!--/s--> rows there has fewer than <!--s:parameters.1_sampling.1_download_ghs.MIN_LANGUAGE_SHARE_OF_PREVIOUS-->75%<!--/s--> of them. The warning names both
  counts (`1_sampling/download-ghs-spec.md`). GHS computes its counts before and after the
  download from the same database as the GHS search result, and the counts cannot show that GHS
  lost rows, such as after a restore of part of its database. If the stage took such a retrieval,
  the cloning step would delete the trees of every repository that left the sample, and the next
  collection would clone them again. [Retrieval sizes](#retrieval-sizes) gives the measurements
  behind the two shares. A retrieval taken with other parameters or other window lengths is not
  compared. After GHS has lost the rows for good, pass `--accept-smaller` to take the smaller
  retrieval for one run. In the container, add `--step-arg ghs_download:--accept-smaller` to the
  `command:` of `compose.yaml` as an argument for one Start, as
  [deploy](deploy.md#deployment-options) describes. Without it, every later download would be
  compared with the same retrieval and fall back.
- **Every retrieval is kept.** `data/sampling/ghs_retrievals/` keeps each retrieval with GitHub's
  responses.
- **The filter computes the activity thresholds and the languages of each sampling frame
  again.** For each sampling frame, the filter computes the median commits and watchers and the <!--s:parameters.1_sampling.config.ghs_filter.top_n_languages-->10<!--/s--> most common languages that the end date covers over that sampling frame's own
  population, as it did for the sampling frame of 2026-03-17. It records the values under
  `_metadata.stages` in `data/sampling/ghs_filtered.json`, and the release publishes `_metadata`
  as `sampling.json`. A repository near a median can enter one month and leave the next.

## Work per change

| Change | Work | Collection | Version |
|---|---|---|---|
| A commit heuristic | `commit-reclassification` over the per-repository commit records that the collection kept | None | As the versioning policy specifies for the changed attributions |
| A path heuristic | A new image with the changed heuristic. Import it and build the project as [deploy](deploy.md#restarting-and-updating) describes: in Container Manager, open Project in the left menu, select the project, then choose Action, Build. When the Build has finished, open Container in the left menu, select `agentcontext-agentcontext-1`, then choose Action, Start. The runner runs the cloning step, because the path heuristics are among the inputs by which it records that step. The step compares the changed rows with each repository's file list, `data/file_lists/<owner§repo>.ls-tree.xz`. It collects the repositories that a changed row affects again and takes over the others | The affected repositories, and every repository with a new head commit, at its current head. A release between two monthly runs therefore contains rows collected at that Start beside the rows of the monthly run and of the collections that the monthly run took over. Each row records its date in its `head_sha` and `scanned_at`. The next monthly run takes over each repository with an unchanged head, whether the run of that Start collected it or not, unless a row of the path heuristics that changed since the repository was collected matches its files (`reuse.heuristics_reach`). It collects the repositories with a new head commit again. Collecting the affected repositories at the commit of the previous collection would need a clone at a given commit, with the history fetched and read only up to that commit. Such clones were rejected on 2026-10-13 | Minor for a new mechanism, major for a reclassification |
| Repositories change on GitHub | The monthly run | The repositories with a new head commit. The others are taken over | Major |
| The window moves | The monthly run: a new retrieval, GitHub's responses, the filter, the classification of the repositories that are new to the sampling frame or have a changed README or description, and the collection | The repositories new to the sample and the repositories with a new head commit | Major |

## GHS's updates

GHS has no discrete update that a collection could align with. Its source,
`github.com/seart-group/ghs` at v1.17.1, is the build that production reported on 2026-10-05
(`/api/version`, built 2024-08-06). No commit has changed `src/main` since 2024-07-19. The source
shows the following:

- One crawl job covers <!--s:retrieval_2026_10_05.ghs_languages-->44<!--/s--> languages one after another, the oldest checkpoint first. For each
  language, it searches GitHub for repositories with at least <!--s:ghs.crawl_min_stars-->10<!--/s--> stars that were pushed between
  the language's checkpoint and <!--s:ghs.crawl_lag_hours:words-->one<!--/s--> hour before the present. The job pauses for <!--s:ghs.crawl_pause_hours:words-->six<!--/s--> hours after a run
  ends (`CrawlProjectsJob.java:77, 109-133`, `application.properties:169`).
- The crawler refreshes a repository only when it reaches a push of that repository, and it skips
  a row with unchanged `updated_at` and `pushed_at` (`CrawlProjectsJob.java:395-402`). The stars,
  the archived flag and the license therefore stay as they were at the last push.
- A weekly cleanup, Mondays at 00:00 UTC, deletes a row that was not crawled for more than <!--s:ghs.cleanup_uncrawled_days-->35<!--/s--> days when
  `git ls-remote` cannot reach its repository and GitHub returns 404 for it (`CleanUpProjectsJob.java:44, 51-103`,
  `GitRepoRepository.java:40`).
- A renamed or transferred repository gets a new row, and the old row stays.
- No database dump is newer than 2024-08-06.

On 2026-10-05, probes of the live service found that the crawl dates differed between the
languages. The crawler had reached pushes of 2026-10-02 for Shell, of 2026-09-23 for TypeScript and
of 2026-09-22 for Python. GHS records a push between a few hours and more than a week after it
happens. When the window ends on the oldest crawl date, GHS has recorded every push before the
end date.

## The measurements

The measurements below were taken on 2026-10-05 over the tracked GHS search result of March
(`data/sampling/ghs_retrievals/`, retrieval `2026-03-16T180701Z`), the October 2–3 collection
(`3_data-collection/data/collected/repos.csv`, scanned 2026-10-02 and 2026-10-03)
and the live GHS search.

| Measure | Value |
|---|---|
| Sample repositories with a collected HEAD that predates the March download by at least six days: GHS's `lastCommitSHA` is that HEAD | <!--s:refresh_2026_10_05.march_head_predating_matched-->3,534<!--/s--> of <!--s:refresh_2026_10_05.march_head_predating-->3,573<!--/s--> (<!--s:refresh_2026_10_05.march_head_predating_share-->98.9%<!--/s-->). <!--s:refresh_2026_10_05.march_head_predating_branch_changed-->12<!--/s--> of the other <!--s:refresh_2026_10_05.march_head_predating_other-->39<!--/s--> changed their default branch |
| Sample repositories with an October HEAD that differs from GHS's March `lastCommitSHA` | <!--s:refresh_2026_10_05.moved_since_march-->33,001<!--/s--> of <!--s:collection_2026_10_02.repositories_with_head-->36,617<!--/s--> (<!--s:refresh_2026_10_05.moved_since_march_share-->90.1%<!--/s-->), and <!--s:refresh_2026_10_05.branch_changed_since_march-->986<!--/s--> changed their default branch |
| Last commit within N days of the October scan | 7 days <!--s:refresh_2026_10_05.last_commit_within_7_days-->44.0%<!--/s-->, 30 days <!--s:refresh_2026_10_05.last_commit_within_30_days.d1-->65.3%<!--/s-->, 91 days <!--s:refresh_2026_10_05.last_commit_within_91_days-->79.9%<!--/s-->, 182 days <!--s:refresh_2026_10_05.last_commit_within_182_days-->88.6%<!--/s--> |
| GHS's count for the March window's query on 2026-10-05, by a probe of the live service | More than the <!--s:release_2026_03.ghs_repositories-->187,304<!--/s--> rows of the GHS search result of March |
| Sample repositories without a commit since 2026-04-02 at the October scan | <!--s:refresh_2026_10_05.no_commit_since_2026_04_02-->4,163<!--/s--> (<!--s:refresh_2026_10_05.no_commit_since_2026_04_02_share-->11.4%<!--/s-->) |
| Collection of the sample | about <!--s:collection_2026_10_02.duration_hours-->21<!--/s--> hours for <!--s:collection_2026_10_02.sample_repositories-->36,710<!--/s--> repositories |
| Clone failures in October | <!--s:collection_2026_10_02.clone_failures-->92<!--/s--> (<!--s:collection_2026_10_02.clone_failure_share-->0.25%<!--/s-->) |

GHS's `lastCommitSHA` reliably shows whether a repository has new commits, because GHS refreshes
a repository when it is pushed. <!--s:refresh_2026_10_05.last_commit_within_30_days.d1-->65.3%<!--/s--> of the sample committed within 30 days, and a threshold on the
share of repositories with new commits would trigger within weeks of every collection. The
collection therefore runs on a schedule. A fixed window does not keep the population fixed
either. GHS's count for the March window grew from March to 2026-10-05, through repositories that new commits brought into
the window and repositories that GHS crawled for the first time, while <!--s:refresh_2026_10_05.no_commit_since_2026_04_02_share-->11.4%<!--/s--> of its repositories
stopped committing. Taking over the repositories without new commits saves <!--s:refresh_2026_10_05.unmoved_share_monthly-->34.7%<!--/s--> of the clones at
a monthly cadence, and a repository taken over keeps its measurements in the caches keyed by
`head_sha`. For every new retrieval, `1_sampling/3_compare_retrievals.py` writes to
`data/sampling/ghs_changes.json` the share of the collected sample with a new head commit and the
repositories that entered and left the window.

## The first retrieval of the rewritten stage

Retrieval `2026-10-05T101430Z` was taken on 2026-10-05, and GitHub was queried for its
repositories on the same day. The values below were computed from the files that the repository
tracked at the time, with the sampling frame of the retrieval read from the labels of run
`2026-10-10T035814Z`, which labeled every repository in it:

| Measure | Value |
|---|---|
| End date | 2026-09-22, the crawl date of C#. The window covers repositories created by 2025-03-22 with a last commit since 2026-03-22 |
| Repositories | <!--s:retrieval_2026_10_05.repositories-->205,418<!--/s-->. GHS counted <!--s:retrieval_2026_10_05.count_before-->205,418<!--/s--> before the download and <!--s:retrieval_2026_10_05.count_after-->205,420<!--/s--> after it |
| GHS search result | <!--s:retrieval_2026_10_05.export_mib-->66<!--/s--> MiB compressed, after a few minutes of streaming. The step took <!--s:retrieval_2026_10_05.download_step_minutes-->19<!--/s--> minutes with the probes and the compression |
| GitHub's responses | <!--s:retrieval_2026_10_05.github_found-->205,216<!--/s--> found and <!--s:retrieval_2026_10_05.github_not_found-->202<!--/s--> not found. Beside the repositories for which GitHub returned 404, the repositories not found include repositories that GitHub blocked after a DMCA takedown (451) and repositories that it blocked for a violation of the terms of service (403). GraphQL itself resolves a renamed repository under its old name |
| Cost of the GitHub query | GraphQL requests of <!--s:parameters.1_sampling.2_query_github.BATCH_SIZE-->50<!--/s--> repositories, at <!--s:retrieval_2026_10_05.github_points_per_request:words-->one<!--/s--> point each, the least that GitHub charges for a call, with a secondary rate limit every few minutes at <!--s:parameters.1_sampling.2_query_github.WORKERS:words-->four<!--/s--> requests in flight. The index records <!--s:retrieval_2026_10_05.index_query_minutes-->172<!--/s--> minutes for the query, from the start of its first run to the end of its last run. A request of <!--s:parameters.1_sampling.2_query_github.batch_size_max-->100<!--/s--> repositories exceeded GitHub's limit of <!--s:github.graphql_timeout_seconds-->10<!--/s--> seconds |
| GHS against GitHub, the same day | Of the <!--s:retrieval_2026_10_05.github_found-->205,216<!--/s--> repositories found, the HEAD differs for <!--s:retrieval_2026_10_05.head_differs-->50,207<!--/s--> (<!--s:retrieval_2026_10_05.head_differs_share-->24%<!--/s-->). GitHub marks <!--s:retrieval_2026_10_05.archived_on_github_only-->831<!--/s--> as archived that GHS does not. The primary language differs for <!--s:retrieval_2026_10_05.language_differs-->1,316<!--/s-->, and the default branch for <!--s:retrieval_2026_10_05.branch_differs-->504<!--/s-->. The license differs for <!--s:retrieval_2026_10_05.license_differs-->9,929<!--/s-->, of which <!--s:retrieval_2026_10_05.license_quotes_only-->9,549<!--/s--> differ only in the quotes of the BSD names |
| Duplicates | <!--s:retrieval_2026_10_05.duplicate_groups-->2,061<!--/s--> groups of rows that GitHub resolves to one repository. <!--s:retrieval_2026_10_05.duplicate_groups_with_current_name-->2,046<!--/s--> of the groups contain a row under the current name |
| The filter | The filter removed the repositories that GitHub did not find, all rows but one of each group of duplicates, the archived, disabled, locked or forked repositories, the repositories excluded by license, by language and as educational, and the repositories below the medians of commits and watchers, which left a sampling frame of <!--s:retrieval_2026_10_05.frame_repositories-->45,849<!--/s-->. `_metadata.stages` of a sampling frame records the repositories that each step removed |
| Against the sampling frame of 2026-03-17 (<!--s:release_2026_03.sampling_frame_repositories-->40,585<!--/s-->) | <!--s:retrieval_2026_10_05.march_frame_remaining-->33,806<!--/s--> of its repositories remain (<!--s:retrieval_2026_10_05.march_frame_remaining_share-->83.3%<!--/s-->) and <!--s:retrieval_2026_10_05.frame_entering-->12,043<!--/s--> enter |
| Against the sample of 2026-03-17 (<!--s:release_2026_03.sample_repositories-->36,710<!--/s-->) | <!--s:retrieval_2026_10_05.march_sample_remaining-->31,071<!--/s--> remain in the sampling frame (<!--s:retrieval_2026_10_05.march_sample_remaining_share-->84.6%<!--/s-->). <!--s:retrieval_2026_10_05.frame_outside_march_sample-->14,778<!--/s--> repositories of the new sampling frame are outside the sample, <!--s:retrieval_2026_10_05.frame_without_march_label-->12,931<!--/s--> of them without a label from March |
| Against the October collection | <!--s:retrieval_2026_10_05.collection_moved-->7,670<!--/s--> of <!--s:collection_2026_10_02.repositories_with_head-->36,617<!--/s--> collected repositories had a new head commit (<!--s:retrieval_2026_10_05.collection_moved_share-->20.9%<!--/s-->) <!--s:retrieval_2026_10_05.days_after_scan_min:words-->two<!--/s--> to <!--s:retrieval_2026_10_05.days_after_scan_max:words-->three<!--/s--> days after their scan, <!--s:retrieval_2026_10_05.collection_absent-->4,528<!--/s--> are not in the retrieval, <!--s:retrieval_2026_10_05.collection_renamed-->367<!--/s--> were renamed, and <!--s:retrieval_2026_10_05.collection_gone-->1<!--/s--> is gone |
| Against the March retrieval | <!--s:retrieval_2026_10_05.added-->68,277<!--/s--> added, <!--s:retrieval_2026_10_05.added_created_after_window-->13,263<!--/s--> of them created after the March window's bound. <!--s:retrieval_2026_10_05.removed-->52,299<!--/s--> removed, all with their last commit before the new window. <!--s:retrieval_2026_10_05.renamed-->1,517<!--/s--> renamed or transferred. <!--s:retrieval_2026_10_05.archived_changed-->1,743<!--/s--> archived flags and <!--s:retrieval_2026_10_05.license_changed-->1,378<!--/s--> licenses changed, compared without the double quotes that GitHub writes and GHS does not |

The GitHub query takes most of the stage's time. A second GitHub access token of another account
would halve that time, because GitHub applies the secondary rate limit per account.

## Retrieval sizes

On 2026-10-19, the cache of retrievals held three usable retrievals of the same query, each with
more rows than the one before: <!--s:release_2026_03.ghs_repositories-->187,304<!--/s--> in the GHS search result of March, <!--s:retrieval_2026_10_05.repositories-->205,418<!--/s--> on 2026-10-05 and
<!--s:retrieval_2026_10_12.repositories-->205,697<!--/s--> on 2026-10-12. From the GHS search result of March to 2026-10-05, the end date moved <!--s:retrieval_sizes_2026_10_19.march_to_october_end_date_days-->190<!--/s--> days. In
that time, <!--s:retrieval_2026_10_05.removed-->52,299<!--/s--> repositories left the window, <!--s:retrieval_sizes_2026_10_19.removed_share-->27.9%<!--/s--> of the GHS search result of March or about <!--s:retrieval_sizes_2026_10_19.removed_share_per_30_days-->4.4%<!--/s--> in 30 days,
while more entered. A month in which GHS recorded no new repository would therefore still keep
more than <!--s:parameters.1_sampling.1_download_ghs.MIN_SHARE_OF_PREVIOUS-->90%<!--/s--> of the rows, the share below which a retrieval counts as a failed download.

The rows of one language vary more. Among the languages with at least <!--s:parameters.1_sampling.1_download_ghs.LANGUAGE_ROWS_MIN-->1,000<!--/s--> rows, PHP lost the
largest share between the retrievals of 2026-10-05 and 2026-10-12, with end dates <!--s:retrieval_sizes_2026_10_19.language_end_date_days:words-->nine<!--/s--> days apart:
<!--s:retrieval_sizes_2026_10_19.php_drop_share-->3.5%<!--/s--> (<!--s:retrieval_sizes_2026_10_19.php_rows_before-->9,663<!--/s--> to <!--s:retrieval_sizes_2026_10_19.php_rows_after-->9,327<!--/s-->). At that rate, a language would keep <!--s:retrieval_sizes_2026_10_19.php_two_month_share-->79%<!--/s--> of its rows over 60
days, the interval when a run falls back and the download of the next month compares its
retrieval with a retrieval taken two months earlier. The share of <!--s:parameters.1_sampling.1_download_ghs.MIN_LANGUAGE_SHARE_OF_PREVIOUS-->75%<!--/s--> for a language allows that loss.
A language with fewer than <!--s:parameters.1_sampling.1_download_ghs.LANGUAGE_ROWS_MIN-->1,000<!--/s--> rows is not compared, because a language with fewer rows varies
more still. Elm lost <!--s:retrieval_sizes_2026_10_19.elm_drop_share-->5.4%<!--/s--> in the same days (<!--s:retrieval_sizes_2026_10_19.elm_rows_before-->92<!--/s--> to <!--s:retrieval_sizes_2026_10_19.elm_rows_after-->87<!--/s--> rows).

## Implementation in the pipeline

- `1_sampling/config/ghs_download.yaml` defines the sampling window by its end date,
  `crawl_date`, and two lengths, <!--s:parameters.1_sampling.config.ghs_download.window.created_before_months-->18<!--/s--> and <!--s:parameters.1_sampling.config.ghs_download.window.committed_within_months-->6<!--/s--> months. The cache's index records the resolved bounds of every retrieval.
- A monthly run takes a new retrieval with `--from ghs_download`. The runner records the GitHub
  query, the comparison and the filter by the retrieval that `ghs_retrieval.json` names, its rows
  and GitHub's responses. After a new retrieval, the runner runs the three steps again, and after
  a fallback, it runs none of them again. The download's report in `run_logs/state.json` records
  whether the window moved.
- The comparison also reads the collected list. When the comparison of a monthly run compares its
  new retrieval, the list still holds the previous collection, because the sampling stage runs
  before the cloning step. The cloning step rewrites the list later in the same run. The runner
  does not record the comparison by the list, so that a completed run stays complete at the next
  Start. When the comparison replaces reports made from other retrievals or another collected
  list, it keeps the earlier reports beside the new reports under `.previous` names, and
  `ghs_changes.json` records the inputs of each version.
- The sampling frame names its retrieval in `_metadata.input_source.retrieval_id`, with the
  window and the query, and the release publishes that block as `sampling.json`.

The classification of the next sampling frame reuses a label while the wording of the
repository's README, its description, the model and the classification parameters are unchanged
([2_classification/0_README.md](../2_classification/0_README.md)).
