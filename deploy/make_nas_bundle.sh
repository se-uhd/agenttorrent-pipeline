#!/usr/bin/env bash
# Assemble the bundle from which the Container Manager project is created: the three files that
# the host itself reads and the two empty mount sources.
#
#   bash deploy/make_nas_bundle.sh [output.zip]     default: deploy/agentcontext-nas-bundle.zip
#
#   agentcontext/
#     compose.yaml            the project file Container Manager reads
#     credentials.csv.example an example of the credentials.csv that the operator writes
#     deploy.md               the runbook, docs/deploy.md
#     data/                   the data root, empty but for a .keep file
#     handover/               the directory for the run's archive, empty but for a .keep file
#
# The image contains the code and the heuristics, and the bundle contains no data: the first
# start writes everything into data/, from the download of the retrieval to the analysis.
#
# Unpacking this over a project folder in use leaves the data root as it is. A clean run
# deletes the project folder and unpacks the bundle into an empty one, as docs/deploy.md describes.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd -P)
out=${1:-$HERE/agentcontext-nas-bundle.zip}
# The directory, not the file: the zip does not exist yet, and BSD readlink refuses a path
# with a missing last component.
out="$(cd "$(dirname "$out")" && pwd -P)/$(basename "$out")"
# `zip` appends `.zip` to a name that has no extension, so the path printed at the end
# would name a file that is not there, and the `du` inside the printf argument would fail
# without triggering `set -e` and print an empty size beside it.
case "$out" in
  *.zip) ;;
  *) echo "The bundle has to be named *.zip, because zip appends the extension ($out)" >&2
     exit 1 ;;
esac
rm -f "$out"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root=$stage/agentcontext
mkdir -p "$root/data" "$root/handover"
cp "$HERE/compose.yaml" "$HERE/credentials.csv.example" "$root/"
cp "$HERE/../docs/deploy.md" "$root/deploy.md"
# The bundle contains the two mount sources as directories with only a .keep file, because
# Container Manager, unlike the docker CLI, refuses to create a missing bind-mount source when
# the project is created. Without them, the unpacked bundle fails at Create project.
touch "$root/data/.keep" "$root/handover/.keep"

# -y stores a symlink rather than what it points at, should one ever be added.
(cd "$stage" && zip -qry "$out" agentcontext)
printf 'Wrote %s (%s)\n' "$out" "$(du -h "$out" | cut -f1)"
