# The March 2026 sample of release 1.0.0

A hash in this record names a commit of the private repository `se-uhd/agenttorrent-pipeline-internal`, which holds the history of this tree, and no check resolves it. The commands below run in that repository, which also holds the tag `agentconfigs_1.0.0`.

The shell collector of release 1.0.0, `3_data-collection/1_collect_config_artifacts.sh`
(`git ls-tree agentconfigs_1.0.0 3_data-collection`), collected the sample on 2026-03-29 and 2026-03-30 and
wrote 4,799 repository directories. Its first validation pass moved 58 of them out. At the tag,
`3_data-collection/repos.7z` holds the other 4,741, with the collector's list as
`repos/input.csv`, which carries `first_commit_sha` and `last_commit_sha` per repository, and
`3_data-collection/repos_excluded.7z` holds the 58. `git show agentconfigs_1.0.0:3_data-collection/repos.7z`
restores an archive from the tag. `72728b14` renamed the two archives, unchanged, to
`3_data-collection/data/collected.7z` and `3_data-collection/data/excluded.7z`, and `37d2ce0d`
removed both. `37d2ce0d^:docs/tools/march.md` describes the retired `march` commands that rebuilt
the collected tree from them.

At the tag, `3_data-collection/deletion_vendored.csv` records the vendored cleanup of 2026-04-21,
and `git show agentconfigs_1.0.0:3_data-collection/deletion_vendored.csv` restores it. The validation pass of
April 2026 also deleted five files that no heuristic recognized then, under `.agents/`,
`.cursor-plugin/` and `.cursorindexingignore`. The published `branch` of twelve repositories had
lost its fractional part, such as `19.0` written as `19`, so that their `github_link` did not
resolve.

`march-snapshot-validation.csv` beside this record checked the recorded commit of each of the
4,998 repositories in the collector's summary list, `input_collected.csv`, against GitHub on
2026-09-09 (`validate_snapshot.py` at `9b860d2a`). Its `outcome` is `OK` for 4,856 of them,
`RENAMED` for 42, `RESCUED_BY_FILE_SHA` for 30, `GONE` for 36, `REPO_GONE` for 17 and `NO_SHA` for
17, and `detail` gives the reason of each other outcome.
