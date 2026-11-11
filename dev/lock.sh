#!/bin/bash
# Write the dependency locks under locks/ from the requirements files, with the uv that mise.toml
# pins:
#     mise exec -- bash dev/lock.sh [--upgrade | --upgrade-package NAME]
# A dependency lock lists every distribution that its environment installs, with its exact version
# and the SHA-256 of every file of that version on PyPI. `--universal` resolves one version of
# each distribution for every platform, macOS and Linux on arm64 and x86-64 among them. uv keeps
# the versions of an existing dependency lock that the requirements files still allow, so a
# changed pin changes only that distribution and what depends on it. The arguments, such as
# `--upgrade`, go to every `uv pip compile`.
#
# development.txt locks every requirements file. pipeline.txt, which the production image installs,
# and build.txt, which every environment installs first, are subsets, each resolved under the
# versions of development.txt as constraints, so that a distribution has the same version in every
# dependency lock. locks/0_README.md names who installs which dependency lock.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

pinned=$(python3 -c 'import tomllib; print(tomllib.load(open("mise.toml", "rb"))["tools"]["aqua:astral-sh/uv"])')
# major.minor of the pinned release of Python. uv resolves for that version and every later one,
# so a new patch release in mise.toml leaves the dependency locks as they are.
python=$(python3 -c 'import tomllib; print(".".join(tomllib.load(open("mise.toml", "rb"))["tools"]["python"].split(".")[:2]))')
running=$(uv --version | cut -d' ' -f2)
if [ "$running" != "$pinned" ]; then
    echo "This is uv $running, and mise.toml pins $pinned. Run the script with mise exec." >&2
    exit 1
fi

# The build constraint pins the setuptools with which uv builds venn to read its dependencies.
compile=(uv pip compile --quiet --no-config --universal --python-version "$python"
         --generate-hashes --build-constraints build-requirements.txt "$@")
build=(build-requirements.txt)
pipeline=("${build[@]}" 1_sampling/requirements.txt 2_classification/requirements.txt
          agentcontext/requirements.txt 4_data-analysis/dataset/requirements.txt
          4_data-analysis/rq2/requirements.txt 4_data-analysis/rq3/requirements.txt)

"${compile[@]}" --output-file locks/development.txt "${pipeline[@]}" _website/requirements.txt \
    dev-requirements.txt

subset() {  # subset LOCK REQUIREMENTS...
    local lock=$1
    shift
    "${compile[@]}" --constraints locks/development.txt --output-file "locks/$lock.txt" "$@"
}
subset pipeline "${pipeline[@]}"
subset build "${build[@]}"
