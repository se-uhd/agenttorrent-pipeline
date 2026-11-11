#!/usr/bin/env bash
# Build the pipeline's image.
#
#   bash deploy/build_image.sh             linux/amd64 for the NAS, written to
#                                              deploy/agentcontext-pipeline.tar.gz as a
#                                              classic docker-save v1.2 tarball, with
#                                              BUILD_INFO.txt beside it
#   bash deploy/build_image.sh --local     this machine's platform, loaded into the
#                                              local Docker, no tarball: for a container on
#                                              this machine
#   bash deploy/build_image.sh --context DIR
#                                              assemble the build context into DIR and stop,
#                                              which is what the suite reads
#
# The context is not the working directory. It is the tracked files that the image needs,
# listed by git and unpacked into a temporary directory, so that an untracked file, an
# extracted collected tree or a built release beside the code cannot get into the image, and
# the commit that BUILD_INFO records names what was built.
#
# TRACKED is the code of the five stages, the heuristics, mise.toml, which pins the interpreter,
# git, rsync and 7-Zip, install_tools.sh, which installs those three tools, and the two
# dependency locks that the Dockerfile installs, and no data:
# everything that a run reads and writes is under the data root, which is mounted from the
# project folder, as the mining project mounts its pinned `input/`.
#
# EXCLUDED removes the files that the image would contain and never run: the suites beside the
# scripts of the other stages, and the fixture of the analysis suites.
#
# The Dockerfile names its base image by digest, and install_tools.sh fails the build when the
# interpreter of that image is not the release of Python that mise.toml pins. BUILD_INFO.txt names
# the release that the interpreter of the built image reports.
#
# On a containerd image store both `docker save` and `buildx --output type=docker` write an
# OCI-layout tarball, which DSM's Docker imports untagged, and compose with
# `pull_policy: never` then fails with "No such image". The script rewrites the tarball into
# the classic layout, which records the tag, loads the tarball again and checks that the tag
# exists.
#
# After either build, the zone check runs the image once with the zone file of Europe/Berlin
# mounted where compose.yaml mounts the NAS's /etc/localtime. The container's own time has to
# stay UTC, because the cloning step writes the commit times with `git log` under TZ=UTC, the
# entrypoint's own check of that time has to pass, and the entrypoint has to read the mounted
# zone, in which a Start at 23:00 UTC on November 30 falls on December 1. The zone file comes
# from the zone database of this machine, under $TZDIR or /usr/share/zoneinfo.
#
# The tarball and BUILD_INFO.txt are written into deploy/ after the zone check, and the files of
# an earlier build are removed before the build starts, so a build that fails leaves neither.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd -P)
ROOT=$(cd "$HERE/.." && pwd -P)
IMAGE=agentcontext-pipeline
TARBALL=$HERE/$IMAGE.tar.gz
PLATFORM=linux/amd64
# Every path that git lists under these, minus EXCLUDED.
TRACKED=(mise.toml pyproject.toml run_pipeline.py agentcontext heuristics deploy/entrypoint.py
         deploy/install_tools.sh locks/build.txt locks/pipeline.txt
         1_sampling 2_classification 4_data-analysis)
EXCLUDED='(^|/)test_[^/]*\.py$|^4_data-analysis/testdata/|^2_classification/config/parameter_digests\.csv$'

assemble_context() {  # assemble_context DIR
  local ctx=$1
  mkdir -p "$ctx"
  (cd "$ROOT" && git ls-files -- "${TRACKED[@]}" | grep -Ev "$EXCLUDED" | tar -cf - -T -) \
    | tar -xf - -C "$ctx"
  cp "$HERE/Dockerfile" "$ctx/Dockerfile"
}

# The release of Python of the interpreter of the built image, which BUILD_INFO.txt names.
image_python() {  # image_python [docker run option...]
  docker run --rm --cpus 1 "$@" --entrypoint python3 "$IMAGE:latest" \
    -c 'import platform; print(platform.python_version())'
}

# The data root revision of the entrypoint in the context, which BUILD_INFO.txt names, so that you
# can tell before an import whether the image resumes the run in a project folder, whose
# run_logs/data_root.json names the revision of the image that began it. The constant is read
# from the source without importing the entrypoint.
data_root_revision() {  # data_root_revision CONTEXT
  python3 -c 'import ast, sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
found = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
         and [getattr(target, "id", None) for target in node.targets] == ["DATA_ROOT_REVISION"]
         and isinstance(node.value, ast.Constant) and type(node.value.value) is int]
if len(found) != 1:
    sys.exit(f"{sys.argv[1]} does not set DATA_ROOT_REVISION to an integer exactly once")
print(found[0])' "$1/deploy/entrypoint.py"
}

# What the image prints in the zone check: the offset of `date` under TZ=UTC and without TZ, the
# commit time of a commit made at 12:00 UTC as the cloning step's `git log` writes it, the local
# time in the entrypoint's zone of the decision on the next monthly run of a Start at 23:00 UTC
# on November 30, and the verdict of the entrypoint's check of the container's own zone.
ZONE_CHECK_EXPECTED=$'+0000\n+0000\n2026-07-01T12:00:00Z\n2026-12-01 00:00 CET\nthe container\'s own zone is UTC'
ZONE_CHECK_SCRIPT=$(cat <<'PY'
import importlib.util, os, subprocess, tempfile
from datetime import datetime, timezone
for zone in ({"TZ": "UTC"}, {}):
    env = {key: value for key, value in os.environ.items() if key != "TZ"} | zone
    print(subprocess.run(["date", "+%z"], env=env, capture_output=True, text=True,
                         check=True).stdout.strip())
with tempfile.TemporaryDirectory() as repo:
    at = {"GIT_AUTHOR_DATE": "2026-07-01T12:00:00+00:00",
          "GIT_COMMITTER_DATE": "2026-07-01T12:00:00+00:00"}
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=z", "-c", "user.email=z@example.com",
                    "commit", "-q", "--allow-empty", "-m", "z"], env=os.environ | at, check=True)
    print(subprocess.run(["git", "-C", repo, "log", "-1", "--format=%ad",
                          "--date=format-local:%Y-%m-%dT%H:%M:%SZ"], env=os.environ | {"TZ": "UTC"},
                         capture_output=True, text=True, check=True).stdout.strip())
spec = importlib.util.spec_from_file_location("entrypoint", "/opt/pipeline/deploy/entrypoint.py")
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)
start = datetime(2026, 11, 30, 23, tzinfo=timezone.utc)
print(f"{start.astimezone(entrypoint.month_zone()):%Y-%m-%d %H:%M %Z}")
print(entrypoint.container_zone_problem(os.environ, start) or "the container's own zone is UTC")
PY
)

zone_check() {  # zone_check [docker run option...]
  local zone=${TZDIR:-/usr/share/zoneinfo}/Europe/Berlin target out
  target=$(python3 -c 'import re, sys
found = re.search(r"source: /etc/localtime\n\s+target: (\S+)", open(sys.argv[1]).read())
print(found.group(1) if found else "")' "$HERE/compose.yaml")
  if [ -z "$target" ]; then
    echo "compose.yaml mounts no /etc/localtime in the long form. The zone check needs that mount as its target." >&2
    return 1
  fi
  mkdir -p "$tmp/zone"
  cp "$zone" "$tmp/zone/Berlin"
  out=$(docker run --rm --cpus 1 "$@" -v "$tmp/zone/Berlin:$target:ro" --entrypoint python3 \
    "$IMAGE:latest" -c "$ZONE_CHECK_SCRIPT")
  if [ "$out" != "$ZONE_CHECK_EXPECTED" ]; then
    printf 'The zone check of %s with %s at %s printed\n%s\ninstead of\n%s\n' \
      "$IMAGE:latest" "$zone" "$target" "$out" "$ZONE_CHECK_EXPECTED" >&2
    return 1
  fi
  echo "Zone check: UTC stays UTC, and the entrypoint reads Europe/Berlin at $target"
}

mode=tarball
context_only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --local) mode=local; shift ;;
    --context) context_only=$2; shift 2 ;;
    --platform) PLATFORM=$2; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [ -n "$context_only" ]; then
  assemble_context "$context_only"
  echo "Assembled the context in $context_only"
  exit 0
fi

# A tarball build that fails leaves no tarball and no BUILD_INFO.txt, so the files of an earlier
# build are removed first, and the new ones are written into deploy/ only after every check
# passed.
if [ "$mode" = tarball ]; then
  rm -f "$TARBALL" "$HERE/BUILD_INFO.txt"
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
ctx=$tmp/context
assemble_context "$ctx"

revision=$(data_root_revision "$ctx")
commit=$(git -C "$ROOT" rev-parse HEAD)
if [ -n "$(git -C "$ROOT" status --porcelain -- "${TRACKED[@]}" deploy/Dockerfile)" ]; then
  commit="$commit-dirty"
fi
date=$(date -u +%FT%TZ)
build_args=(--build-arg "GIT_COMMIT=$commit" --build-arg "BUILD_DATE=$date"
            --provenance=false --sbom=false)

if [ "$mode" = local ]; then
  docker buildx build "${build_args[@]}" -t "$IMAGE:latest" --load "$ctx"
  echo "Built $IMAGE:latest for this machine from $commit"
  zone_check
  exit 0
fi

built=$tmp/$IMAGE.tar.gz
docker buildx build --platform "$PLATFORM" "${build_args[@]}" -t "$IMAGE:latest" \
  --output "type=docker,dest=$tmp/oci.tar" "$ctx"

python3 - "$tmp" "$IMAGE" "$built" <<'PY'
import json, pathlib, shutil, sys, tarfile
tmp, image, out_path = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
src = tmp / "oci"; src.mkdir()
with tarfile.open(tmp / "oci.tar") as t:
    t.extractall(src, filter="data")
manifest = json.load(open(src / "manifest.json"))[0]
out = tmp / "classic"; out.mkdir()
cfg = manifest["Config"].split("/")[-1]
shutil.copy(src / manifest["Config"], out / f"{cfg}.json")
layers, last = [], None
for layer in manifest["Layers"]:  # the layer bytes are kept as they are
    sha = layer.split("/")[-1]
    (out / sha).mkdir()
    shutil.copy(src / layer, out / sha / "layer.tar")
    layers.append(f"{sha}/layer.tar")
    last = sha
json.dump([{"Config": f"{cfg}.json", "RepoTags": [f"{image}:latest"], "Layers": layers}],
          open(out / "manifest.json", "w"))
json.dump({image: {"latest": last}}, open(out / "repositories", "w"))
with tarfile.open(out_path, "w:gz") as t:
    t.add(out, arcname=".")
PY

echo "Checking that the tarball loads with its tag"
docker rmi -f "$IMAGE:latest" >/dev/null 2>&1 || true
docker load -i "$built"
# The check is whether the tag exists after the load. `docker images` on a name that it does
# not know prints nothing and exits 0, so the empty listing rather than the exit status shows
# an image that did not load.
loaded=$(docker images "$IMAGE:latest" --format '  {{.Repository}}:{{.Tag}} ({{.Size}})')
if [ -z "$loaded" ]; then
  echo "Docker loaded the tarball, but the local Docker has no $IMAGE:latest" >&2
  exit 1
fi
printf '%s\n' "$loaded"
version=$(image_python --platform "$PLATFORM")
zone_check --platform "$PLATFORM"

mv "$built" "$TARBALL"
printf 'image %s:latest\nplatform %s\ncommit %s\nbuilt %s\npython %s\nsha256 %s\ndata_root_revision %s\n' \
  "$IMAGE" "$PLATFORM" "$commit" "$date" "$version" \
  "$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)" "$revision" > "$HERE/BUILD_INFO.txt"
echo "Wrote $TARBALL"
cat "$HERE/BUILD_INFO.txt"
