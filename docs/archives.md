# Tracked archives and the files extracted from them

The repository tracks the large inputs below only as 7-Zip archives, and git ignores the file
that is extracted from each beside it:

| Extracted file | Tracked archive |
|---|---|
| `data/collection/output/all_ai_commits.csv` | the volumes `all_ai_commits.csv.7z.001` and on |
| `data/collection/output/other_ai_commits.csv` | `other_ai_commits.csv.7z` |
| `data/sampling/ghs_filtered.json` | `ghs_filtered.json.7z` |
| `data/classification/repos_annotated.csv` | `repos_annotated.7z`, which also holds `repos_annotated.json` and `repos_excluded.csv` |

Readers use an extracted file when present without opening its archive. A merge, pull, or branch
switch can replace the archive while leaving an earlier extraction on disk. The reader would then
use data that the commit does not contain.
`agentcontext/archives.py` compares the two files, and `check_copy` raises `CopyDiffers` where the
extracted file is not the committed input.

## The authoritative file in each data root

No step of the pipeline writes one of these archives. You pack them by hand in a checkout
([Packing the archives again](#packing-the-archives-again)). The steps of a run write the
extracted files: the filter writes the sampling frame, the labeling the labels, and the cloning
step and the detection step the commit CSVs. Both files are therefore on disk together only where
someone put an archive beside a file:

- In a checkout, git tracks the archive, and the archive is the committed input. The stated
  statistics read the archive (`agentcontext/statistics/commits.py`). An extracted file that
  differs from it is either stale or newer and not yet archived. In both cases, a figure drawn
  from it is not the figure of the commit, and the stated commit counts describe another table.
  A step that runs in a checkout, such as the filter, writes a newer file, and so does a copy of
  the CSVs of a regeneration into the checkout. The analysis stops on such a file until you pack
  the archive again or extract the file again. A run of the whole pipeline in a checkout would
  therefore stop in the analysis stage, and the development container runs the stages over a
  data root outside the checkout ([development](development.md#linux-x86-64-development-container)).
- The data root of a run, such as the volume of the container on the NAS, starts empty, because
  the image and the bundle contain no data. The steps write the extracted files there, and no
  step writes an archive.
- A working copy is extracted from a handover, which contains the extracted files and none of
  the archives. A working copy can still hold an older commit archive that was copied in, and the
  regeneration then writes a newer CSV beside it.
- The checkout of a [subsample run](tools/mirror_run.md) is a checkout too. Its collection
  writes the commit CSVs of the subsample beside the tracked archives of the full collection,
  and its publication builds the release from those CSVs. The analysis stage does not run there.
- The release reads the data root that it runs in, any of these, and reads the extracted files
  without the check. In the checkout of a subsample run, the check would stop the release on
  the commit CSVs that the run has just written.

The check therefore compares the files only where git tracks the archive
(`git ls-files --error-unmatch`). There, a difference means that the extracted file is not the
committed input, and the check raises. Where git tracks nothing, as in the data root of a run and
in a working copy, the extracted file is the input, and the check passes whatever lies beside it.
It cannot stop a run on the NAS or the processing of a working copy, and in a checkout it stops a
stale file before any output is drawn from it.

## Packing the archives again

Pack an archive again when the extracted file is the newer input and the commit is to contain
it, such as after you copy the CSVs of a regeneration into the checkout. When the archive is the
newer file, after a merge, a pull or a switch of branches, extract the file again instead, with
the `7zz x` command that the message of `CopyDiffers` names. Run these commands from the root of
the checkout. Each removes the old archive first, because `7zz a` adds to an existing archive
rather than replacing it:

```shell
(cd data/sampling &&
  rm -f ghs_filtered.json.7z && 7zz a -mx=9 ghs_filtered.json.7z ghs_filtered.json)
(cd data/classification && rm -f repos_annotated.7z &&
  7zz a -mx=9 repos_annotated.7z repos_annotated.csv repos_annotated.json repos_excluded.csv)
(cd data/collection/output &&
  rm -f all_ai_commits.csv.7z.* && 7zz a -mx=9 -v80m all_ai_commits.csv.7z all_ai_commits.csv &&
  rm -f other_ai_commits.csv.7z && 7zz a -mx=9 other_ai_commits.csv.7z other_ai_commits.csv)
```

Each command packs the files at the root of the archive, and the readers extract them beside
it. `repos_annotated.7z` holds the labels together with their record, `repos_annotated.json`,
and the list of the repositories in the sampling frame without a published label,
`repos_excluded.csv`. The release reads all three, and the subsample run refuses an archive that
lacks one of them (`INPUT_ARCHIVES` in `agentcontext/tools/mirror_run.py`). The archive of the
commit CSV of the six tools exceeds GitHub's limit for a file and is therefore split into volumes,
and git tracks every volume. When the new archive has fewer volumes than the old one, commit the
removal of the volumes that it no longer has.

## The comparison

`listed_files` reads the path, the size and the CRC32 of every file in an archive from
`7zz l -slt`, without extracting the archive. A split archive is named by its first volume, and 7zz
reads the other volumes beside it. `copy_matches` compares the extracted file with the files of
its name at any depth of the archive, because an earlier commit archive stores the CSV under
`3_data-collection/output/`. A size that no such file has decides the difference without reading
the extracted file. Otherwise, the CRC32 of the extracted file decides. A tracked archive that
7zz cannot list, such as an archive with a missing volume, raises `ArchiveUnreadable`, because
the extracted file cannot be checked against it.

## The callers

- `_shared.checked_copy` in the analysis stage turns either exception into an exit with the
  message. The message of `CopyDiffers` names both files, the `7zz x` command that extracts the
  archive again and [the section](#packing-the-archives-again) with the command that packs it
  again. For the labels, it also names `python3 -m agentcontext engineered-project-selection`,
  which writes the list of the sample from the extracted labels, as
  [development](development.md#handover-working-copy) describes. The loader of the commit CSV,
  `rq2/_repo_metadata.py` for the labels and the sampling frame,
  `rq2/language_tool_association.py` and `dataset/filter_steps_table.py` read their inputs
  through it.
- `run_analysis.py` declares the commit CSV, the labels and the sampling frame each with its
  archive as an alternative. The fingerprint of an entry covers a 7-Zip archive among its
  alternatives as well as the first alternative on disk ([runner](runner.md#step-list)), so that a
  changed archive runs the steps that read the file again.
- `dev/setup.sh` keeps a commit CSV that matches its archive and extracts a commit CSV that
  differs from it again. It extracts each missing file of the archives of the sampling frame and
  the labels and keeps a file of them that differs, because the filter and the labeling write
  newer files there. It then names the file and extracts no file of that archive. From labels
  that match their archive, it writes the list of the sample again with
  `engineered-project-selection` when the list is missing or differs.
- `evaluation sample` checks the two commit CSVs against their archives before it draws, and
  exits 1 with the message of either exception
  ([detection accuracy evaluation](tools/evaluation.md)). No runner runs the evaluation, and
  neither does the subsample run. It draws in a checkout, where the check applies, or in a working copy,
  where git tracks no archive and the check passes. In the checkout of a subsample run, the commit
  CSVs beside the tracked archives are those of the subsample, and the check stops a draw from
  them.
