# Build records and collection evidence

This directory contains build records for the images below, the images used for the development
dataset and the October 5–6 collection, and evidence from earlier collections cited in the
documents and heuristic notes.

| Source commit | Image |
|---|---|
| `5d29edef` | Data root revision 3, able to continue the NAS run started on 2026-11-03 |
| `8f4c6aa8` | Backup providers for the same run |
| `bca78459` | Resumed that run |
| `b649efd9` | Started that run |
| `80b321b9` | Superseded by later changes on `main` |

Decompress a `.json.xz` record with `xz -dc` or Python's `lzma` module. A hash in these
records, such as the source commit of a build, names a commit of the private repository `se-uhd/agenttorrent-pipeline-internal`, which holds the history
of this tree, and no check resolves it. A verification report names a file of the checkout by its
path in the checkout, as [the verification](../tools/verify.md) writes it. The SHA-256 of the
tracked report is the `report_sha256` of a build record, which the record of `22fab80f` names
`full_verification_report_sha256`. The private repository holds the reports with the absolute
paths that the earlier runs printed.

## Build of data root revision 3

[Actions run 41954329315](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41954329315)
built the Linux x86-64 image and the NAS bundle from `5d29edef80b62cf81cf75d1e5a48ee0464a36c25` on
`main`. Its image can replace [the image of `8f4c6aa8`](#build-with-the-backup-providers) during or
after the collection of the run that started on the NAS on 2026-11-03. It collects with the code,
the version of git and the heuristics of that image, and it classifies the commits and the
repositories under the same digests, so it collects nothing again and labels nothing again. Its
first start converts the data root to revision
<!--s:build_5d29edef.data_root_revision-->3<!--/s--> ([deploy](../deploy.md)), after which the image
of `8f4c6aa8` refuses it. After a collection has ended, the image keeps its record, and a later
change of the path or import heuristics or of the collector's code collects again only the
repositories that the change reaches ([repository cloning](../steps/repository_cloning.md)). The
release README reads the labels of `engineered_project` as text in its usage examples and cites the
MSR 2026 paper with its DOI ([the release](../steps/release.md)).
[production-build-5d29edef.json](production-build-5d29edef.json) records the build date, the
checksums, the image identity, the versions of Python, git, rsync and 7-Zip in the image, the
image's list of steps, the checks and the comparison of the subsample run with the tracked
outputs. [CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41951901438)
and [full local verification](verification-5d29edef.txt) passed.

| Download | SHA-256 |
|---|---|
| `agentcontext-pipeline.tar.gz` in `agentcontext-image` | `77dccc6683be8cb4652e1a45a6799e11306af4e603f612a9a083e6021f33d010` |
| `agentcontext-nas-bundle.zip` in `agentcontext-nas-bundle` | `575441f1fe4174552c9fd3e945482b0cacc0034b7412c9770a36b45ff111feec` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit, linux/amd64,
Python 3.14.10 and data root revision 3. The loaded image keeps the tag
`agentcontext-pipeline:latest`, runs git 2.55.0, rsync 3.5.1 and 7-Zip 25.01, has the versions of
the distributions that `locks/pipeline.txt` names for Linux, and `--with-commit-reclassification
--list` lists the steps that `run_pipeline.py --list` gives at the source commit. Every file of the
bundle is byte for byte the file of the source commit. The bundle contains no credentials and no
data.

The subsample run at the source commit over
[subsample-run-list-5d29edef.csv](subsample-run-list-5d29edef.csv), the list of the build of
`8f4c6aa8`, failed no step, and its comparison with the run at `bca78459` found no difference. Its
comparison with the tracked outputs gives the same rows as the subsample run of the build of
`8f4c6aa8`. The release step and the analysis stage also ran over the working copy of the latest
handover ([the dataset](../dataset.md)), in a container of the development image, with the code of
the source commit. The release built the dataset, and the reproduction check ran every script twice
and got the same bytes from both runs.

## Build with the backup providers

[Actions run 41819195195](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41819195195)
built the Linux x86-64 image and the NAS bundle from `8f4c6aa83fba90a1ff68b33afa719a176277a3a4` on
`main`. Its image continues the run of all five stages that started on the NAS on 2026-11-03,
after the provider of both models, Morph, rate-limited the labeling and returned server errors on
2026-11-04. When the pinned provider keeps failing after the pauses, the classifying model, the
summary model and the second model are served by a backup provider with the same model,
quantization and settings ([the classification](../../2_classification/classify-repos-spec.md)).
The provider is no classification parameter, so the run keeps the decisions that it recorded
before. The image installs every Python distribution from the dependency locks on the release of
Python that `mise.toml` pins ([locks](../../locks/0_README.md)), and the labeling decides
<!--s:parameters.2_classification.1_classify_repos.WORKERS-->32<!--/s--> repositories at once by default.
[production-build-8f4c6aa8.json](production-build-8f4c6aa8.json) records the build date, the
checksums, the image identity, the versions of Python, git, rsync and 7-Zip in the image, the
image's list of steps, the checks and the comparison of the subsample run with the tracked
outputs. [CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41819155307)
and [full local verification](verification-8f4c6aa8.txt) passed. The full verification ran after the
image was handed over, at the maintainer's request because of the deadline of the paper.

| Download | SHA-256 |
|---|---|
| `agentcontext-pipeline.tar.gz` in `agentcontext-image` | `815bd4bb959872f2edf7373c978c70248a0659342f2e775a7dd1e07bbc2342de` |
| `agentcontext-nas-bundle.zip` in `agentcontext-nas-bundle` | `6ec478255c63786a7d95ef5c653be76eaa134d3b83078a8fd91a323e68b5a55a` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit, linux/amd64,
Python 3.14.10 and data root revision <!--s:build_8f4c6aa8.data_root_revision-->2<!--/s-->. The loaded image keeps the tag
`agentcontext-pipeline:latest`, runs git 2.55.0, rsync 3.5.1 and 7-Zip 25.01, has the versions of
the distributions that `locks/pipeline.txt` names, and `--with-commit-reclassification --list`
lists the steps that `run_pipeline.py --list` gives at the source commit. Every file of the bundle
is byte for byte the file of the source commit. The bundle contains no credentials and no data.

The subsample run at the source commit over
[subsample-run-list-8f4c6aa8.csv](subsample-run-list-8f4c6aa8.csv), the list of the build of
`bca78459`, failed no step, and its comparison with the run at `bca78459` found no difference. The
release step and the analysis stage also ran over the working copy of the latest handover
([the dataset](../dataset.md)), in a container of the development image, with the code of the
source commit. The release built the dataset, and the reproduction check ran every script twice and
got the same bytes from both runs. The table of the repository metadata comparison differs from
the copy that an earlier analysis left in the working copy, because the source commit renames its
caption, and it now equals the tracked table byte for byte.

## Build that resumes the run of 2026-11-03

[Actions run 41718511091](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41718511091)
built the Linux x86-64 image and the NAS bundle from `bca784597a48c4b0aa8a5bdfdc98e272cf8bd31c` on
`main`. Its image resumes the run of all five stages that
[the image of `b649efd9`](#build-of-the-run-of-2026-11-03) started on the NAS on 2026-11-03. That
image stopped the labeling twice, each time after a short streak of final responses of the
classifying model's endpoint that failed the response format. The image of `bca78459` pauses
every worker on such a streak, and it stops the run only when the endpoint keeps failing through
all the pauses ([the classification](../../2_classification/classify-repos-spec.md)). Its
classification parameters are those of `b649efd9`, so the resumed run keeps the decisions of its
partial file. The image also classifies the commits with the rows that read the tools that
`Authored-by:`, `Created-by:`, `Written-by:` and `Made-with:` lines name.
[production-build-bca78459.json](production-build-bca78459.json) records the build date, the
checksums, the image identity, the versions of git and rsync in the image, the image's list of
steps, the checks and the comparison of the subsample run with the tracked outputs.
[Full local verification](verification-bca78459.txt) passed.
[CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41718457211)
failed in two tests of the classification that imported `httpx`, which the CI job does not
install, because openai installs `httpx2`. The local verification passed them because the
development machine had `httpx` from another package. `ff9933fe` changes only those two tests,
and [CI on `cf975df7`](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41734745443),
which contains it, passed. The code that the image runs is the same in the source commit and in
`ff9933fe`.

| Download | SHA-256 |
|---|---|
| `agentcontext-pipeline.tar.gz` in `agentcontext-image` | `fcb72bd783e034eb3691520f71d5217ae251677e8392c27faadd46e59ac26e70` |
| `agentcontext-nas-bundle.zip` in `agentcontext-nas-bundle` | `29dacb776efe3362da58e713df529840671683a16d589d9fabf428d99f7caa93` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit, linux/amd64
and data root revision <!--s:build_bca78459.data_root_revision-->2<!--/s-->. The
loaded image keeps the tag `agentcontext-pipeline:latest` and runs git 2.55.0, rsync 3.5.1 and
7-Zip 25.01, the versions that `mise.toml` pins, and `--with-commit-reclassification --list`
lists the steps that `run_pipeline.py --list` gives at the source commit. Every file of the bundle
is byte for byte the file of the source commit. The bundle contains no credentials and no data.

The subsample run at the source commit over
[subsample-run-list-bca78459.csv](subsample-run-list-bca78459.csv) failed no step. It ran with the
git and the rsync that `mise.toml` pins. The list starts with the repository with the history
fetch that the run makes fail, followed by the development subsample that the commit
reclassification of `f9a2279a` selected. The mirrors of the repositories that are new to the list
were cloned and, as every other mirror, set to the head that the collection recorded. Apart from
the scan times, the output CSVs of the run agree row for row with the tracked rows of the same
repositories, except for the repositories that the build of `b649efd9` names below and two
repositories that are new to the list: `AGENTS.md` of nafiesl/free-pmo and
`dotfiles/.claude/settings.sandbox.json` of vinta/hal-9000 were each added, deleted and added
again, and a new clone dates them from their first commit, as [the dataset](../dataset.md) states.
The comparison with the reference run at `b649efd9` finds differences because the list and the
commit heuristics changed. The run at the source commit is the reference of the next subsample
run.

The release step and the analysis stage also ran over the working copy of the latest handover
([the dataset](../dataset.md)), in a container of the development image, with the code of the
source commit. The release built the dataset, and the reproduction check ran every script twice
and got the same bytes from both runs. Four outputs differ from the copies that an earlier
analysis left in the working copy: the table of the content quality, which now equals the tracked
table byte for byte, and three figures, which are Linux outputs with other PDF bytes than the
tracked figures of macOS.

## Build of the run of 2026-11-03

[Actions run 41534208344](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41534208344)
built the Linux x86-64 image and the NAS bundle from `b649efd956bd5b8c31c9e7b32596da2c9ddf1ada` on
`main`. Its image started the run of all five stages on the NAS on 2026-11-03, and
[the image of `bca78459`](#build-that-resumes-the-run-of-2026-11-03) resumes it.
[production-build-b649efd9.json](production-build-b649efd9.json) records the build date, the
checksums, the image identity, the versions of git and rsync in the image, the image's list of
steps, the checks and the comparison of the subsample run with the tracked outputs.
[CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41534208255)
and [full local verification](verification-b649efd9.txt) passed.

| Download | SHA-256 |
|---|---|
| `agentcontext-pipeline.tar.gz` in `agentcontext-image` | `168d4532729dd440766b55052ea3e46a06b670c742b11413d87bcdbe14d7e1ae` |
| `agentcontext-nas-bundle.zip` in `agentcontext-nas-bundle` | `519a0c4a503e414935e81f70ed670d092efc401b12b5cfa31918ee97b2412cdb` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit, linux/amd64
and data root revision <!--s:build_b649efd9.data_root_revision-->2<!--/s-->. The
loaded image keeps the tag `agentcontext-pipeline:latest` and runs git 2.55.0 and rsync 3.5.1, the
versions that `mise.toml` pins, and `--with-commit-reclassification --list` lists the steps that
`run_pipeline.py --list` gives at the source commit. Every file of the bundle is byte for byte the
file of the source commit. The bundle contains no credentials and no data.

The subsample run at the source commit over
[subsample-run-list-b649efd9.csv](subsample-run-list-b649efd9.csv) failed no step. It ran with the
git and the rsync that `mise.toml` pins. The list starts with the repository with the history
fetch that the run makes fail, followed by the development subsample of the current collection,
`data/collection/dev_subsample.csv`. One repository of the list has no mirror, because GitHub no
longer serves it, and it fails to clone in the run as it did in the collection. Each mirror was set
to the head that the collection recorded for its repository, so that the run collects the commits
that the tracked outputs describe. Apart from the scan times, the output CSVs of the run agree row
for row with the tracked rows of the same repositories, except where the record names the
repository:

- the commit history that the run makes fail on purpose
- the commits of upstash/context7 and wonderwhy-er/DesktopCommanderMCP, whose files were fetched
  into the working copy after the collection, and which the run collected with their full history
- the rows of a path with a commit older than its recorded creation commit, which a new clone
  dates from that commit, as [the dataset](../dataset.md) states, among them `AGENTS.md` of
  HKUDS/LightRAG, whose history git 2.55.0 and Debian's git 2.47.3 list alike

The unmatched commits and the consistency report cover the whole collection and have no rows by
repository to compare. The comparison with the reference run at `80b321b9` finds differences
because the list and the version of git changed. The run at the source commit is the reference of
the next subsample run.

The release step and the analysis stage also ran over the working copy of the latest handover
([the dataset](../dataset.md)), in a container of the development image, with the code of
`7e2739f2`. The source commit differs from it in the pinned tools, the output limits of the
classification and the documents, and not in the release or the analysis. The release built the
dataset, and the reproduction check ran every script twice and got the same bytes from both runs.
Two figures over the AI commits differ from the copies that an earlier analysis left in the
working copy before its commit CSV gained the merges that `codex-full-body-12` credits, and from
the tracked figures, which are macOS outputs with other PDF bytes than those of Linux.

## Superseded build of `80b321b9`

[Actions run 41366302992](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41366302992)
built the Linux x86-64 image and the NAS bundle from `80b321b911ee503685e6a46f1e2b5799f7c9f575` on
`main` for a run of all five stages. The changes of the classification on `main` after
`80b321b9` supersede this build, and its image is not for deployment. Since `39104a7b`, the second
model labels each repository that the classifying model leaves without a decision after its retry
rounds and a failed response, and each request of the classifying model sends an output limit.
Since `eb8584b1`, the requests send the settings that each vendor documents, and the classifying
model labels at a higher temperature before the second model. The image of `80b321b9` labels
without these changes, and it also classifies the commits without `codex-full-body-12`, which
`389dedc2` added to the commit heuristics. [The build of `b649efd9`](#build-of-the-run-of-2026-11-03)
replaces it.
[production-build-80b321b9.json](production-build-80b321b9.json) records the build date, the
checksums, the image identity, the image's list of steps, the checks and the comparison of the
subsample run with the tracked outputs.
[CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/41366283755)
and [full local verification](verification-80b321b9.txt) passed.

| Download | SHA-256 |
|---|---|
| `agentcontext-pipeline.tar.gz` in `agentcontext-image` | `0b50f14565d910cd4f67d10f9cf1582e6fb7c33bc23d64e067df14b6219342c2` |
| `agentcontext-nas-bundle.zip` in `agentcontext-nas-bundle` | `39dae1ab92fcf768a657ce85847a744f69d9a5446e135aa455cc9157049bce8e` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit, linux/amd64
and data root revision <!--s:build_80b321b9.data_root_revision-->2<!--/s-->. The loaded image keeps the tag
`agentcontext-pipeline:latest`, and `--with-commit-reclassification --list` lists the steps that
`run_pipeline.py --list` gives at the source commit. Every file of the bundle is byte for byte the
file of the source commit. The bundle contains no credentials and no data.

The subsample run at the source commit over
[subsample-run-list-80b321b9.csv](subsample-run-list-80b321b9.csv) failed no step. The list starts
with the repository with the history fetch that the run makes fail, followed by the development
subsample of the current collection, `data/collection/dev_subsample.csv`. One repository of the list has no
mirror, because GitHub no longer serves it, and it fails to clone in the run as it did in the
collection. Each mirror was set to the head that the collection recorded for its repository, so
that the run collects the commits that the tracked outputs describe. Apart from the scan times,
the output CSVs of the run agree row for row with the tracked rows of the same repositories,
except where the record names the repository:

- the commit history that the run makes fail on purpose
- the commits of upstash/context7 and wonderwhy-er/DesktopCommanderMCP, whose files were fetched
  into the working copy after the collection, and which the run collected with their full history
- the rows of a path with a commit older than its recorded creation commit, which a new clone
  dates from that commit, as [the dataset](../dataset.md) states

The unmatched commits and the consistency report cover the whole collection and have no rows by
repository to compare. The comparison with the reference run at `e99d5614` finds differences
because the list changed: the earlier list has repositories that the current sampling frame no
longer contains, and the release refuses them. The run at the source commit is the reference of
the next subsample run.

The release step and the analysis stage also ran over the working copy of the latest handover
([the dataset](../dataset.md)), in a container of the development image. The release ran over the
code of `41161c2f`, which differs from the source commit only in the analysis stage, and built the
dataset. The analysis ran over the code of the source commit, and its reproduction check ran every
script twice without a failure.

## Production build

[Actions run 40552633184](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/40552633184)
built the Linux x86-64 image and the NAS bundle from
`953988cf596a9dc877109ce3a326c9087632f89d` on the branch `e99d5614-sync-written-files`. The run of
all five stages that began on the NAS on 2026-10-23 with the image of `e99d5614` resumed with
this image and completed on 2026-10-28. The branch adds two commits to `e99d5614`. The first makes
the runner sync the declared outputs and the report of a step, instead of the whole volume, before
it records the step ([the runner](../runner.md)), and the second makes the `--mark-complete` tests
of the runner suite write the sample list inside their own temporary directory. The code of
the collector and of the imports, and the script of every step that the run recorded as complete,
are those of `e99d5614`, so the run could resume its collection.
[production-build.json](production-build.json) records the build date, the checksums, the image
identity, the image's list of steps and the checks.
[CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/40552597428)
and [full local verification](verification-953988cf.txt) passed, with every suite, the
reproduction of the analysis scripts, the outputs against the regression manifest, the snapshot
sections and the consistency checked, and a clean working tree. The image of `e99d5614`,
with which the run began, was built by
[Actions run 39856182261](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/39856182261),
and [production-build-e99d5614.json](production-build-e99d5614.json) and
[its full local verification](verification-e99d5614.txt) record that build.

| Download | SHA-256 |
|---|---|
| `agentcontext-pipeline.tar.gz` in `agentcontext-image` | `37961540c6aa274b53b8250bf0067af156cea9052f5df2e8c14848bf6b83cdd8` |
| `agentcontext-nas-bundle.zip` in `agentcontext-nas-bundle` | `350d859bf8b2956d8f4486860d20946844badfaac7e34a4694d3f3b832e84d80` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit, linux/amd64
and data root revision 1. The loaded image keeps the tag `agentcontext-pipeline:latest`, and
`--with-commit-reclassification --list` lists the steps of all five stages, the steps that
`run_pipeline.py --list` gives at the source commit. The steps and their commands are the same as
those that the image of `e99d5614` lists. Every file of the bundle is byte for byte the file of the
source commit, and the same as in the bundle of `e99d5614`. The bundle contains no credentials and
no data.

The subsample run at `95dfa3ed` over [subsample-run-list.csv](subsample-run-list.csv) failed no
step. The list is the development subsample of `a6940512` plus the repository with the history
fetch that the run makes fail. A repository of the list has no mirror and fails to clone in the run.
The run has no difference against the run at `e99d5614` over the same list, and no repository was
added or removed. The source commit differs from
`95dfa3ed` only in the runner suite, which the subsample run does not run.

The run at `e99d5614` failed no step either, and it is the reference of the subsample run. Against the
run at `07885829` over the same list, no repository was added or removed, and the differences are
the reworded reasons of the collected file cleanup on the
same paths, in `deletion.csv` and `output/deletions.csv`, and the new `classified_by` in
`output/snapshot.json` and `output/commit-reclassification.json`, which changed because the error
texts of the commit heuristics were reworded. The commit CSVs are byte for byte the same.

## Build of the clean collection

[Actions run 37249871852](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/37249871852)
built the Linux x86-64 image and the NAS bundle of the October 5–6 collection from
`22fab80fbb9715fee446a01adfaecb0506312fd2`. [production-build-22fab80f.json](production-build-22fab80f.json)
records the build date, the checksums, the input checksums, the image identity, the runtime
versions and the checks. [CI on the build source](https://github.com/se-uhd/agenttorrent-pipeline-internal/actions/runs/37249854112)
and [full local verification](verification-22fab80f.txt) passed, and the subsample run over the
mirrors of [local-run-list.csv](local-run-list.csv) at the source commit failed no stage.

| Download | SHA-256 |
|---|---|
| `agentconfigs-pipeline.tar.gz` in `agentconfigs-image` | `05f0b76eab8ce57ad8b0f8fd668c4251b60d944fb364303438a804cc1ef91355` |
| `agentconfigs-nas-bundle.zip` in `agentconfigs-nas-bundle` | `0cbf9f0649ceee7faf8f41aaff3a7e4e895354cd18b35b58cc282488b5e77c18` |

The checksum of the tarball matches `BUILD_INFO.txt`, which names the source commit. The loaded
image keeps the tag `agentconfigs-pipeline:latest`, and `--with-commit-reclassification --list`
lists the intended stages. Every file of the bundle is byte for byte the file of the source
commit, and the bundle contains no credentials, collected trees or resume state.

## Clean collection review

The October 5–6 collection is `agentconfigs-handover-20261006T052554Z-complete.7z`, SHA-256
`a1a10cd046f2ec0ca4e7289650e1febebf4bf8880eb6ec7b825c0e5a81d34a2a`, collected by image `22fab80f`
from 2026-10-05 05:57 to 2026-10-06 05:26 UTC.
[clean-collection-review.json.xz](clean-collection-review.json.xz) contains its clone failures,
the repositories that joined and left the configured set, the repository collected again and the
findings with the commits that fixed them. The detection accuracy evaluation in
`data/collection/evaluation/2026-10-05/` drew its samples from this collection.

## Earlier collections

The documents and the notes of the heuristics also cite two earlier collections of the March
sample. The September collection is `agentconfigs-handover-20260928T162417Z-complete.7z`, SHA-256
`00d6117bb8bc50a8d27de0fa3863411e92a935441a77787d652f6c19722d595a`, from the run that started on
2026-09-27 with the image of `893f51bd` and ended on 2026-09-28. The October 2–3 collection is
`agentconfigs-handover-20261003T074056Z-complete.7z`, SHA-256
`7ffb721fb887119d06b52bb72a7a31956ac7d36c992ed6f0fa0b362444264f94`, collected by image `87c26a45`
on 2026-10-02 and 2026-10-03.

[october-review.json.xz](october-review.json.xz) contains the evidence of the review of the commit
logs of the October 2–3 collection, with repository and commit references: every identity that
the identity heuristics `anthropic\.com` and `\bcopilot\b` matched before they were narrowed to the
tools' own identities, written `(a person)` when it names a person, the disclosure lines from which commit heuristics were widened or added,
the Assisted by lines of the rows widened on 2026-10-05, and the commit counts of the
`daangn/seed-design` rows in the September and the October 2–3 collections. The record is the
evidence of the notes of the commit heuristics that describe the October 2–3 collection, and of
the statistic of [docs/commits.md](../commits.md) that counts the personal anthropic.com
addresses that the identity heuristic `anthropic\.com` matched.

[docs/dataset.md](../dataset.md) names the tracked dataset, and
[development](../development.md#continuous-improvement-from-handovers) gives the processing and
verification steps.
