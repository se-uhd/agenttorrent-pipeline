#!/bin/sh
# Install the git, the rsync and the 7-Zip that mise.toml pins into a Linux image, linked into
# /usr/local/bin: conda-forge's packages of git and rsync, which mise installs with its conda
# backend, and the `7zz` of the 7-Zip release, which mise installs with its aqua backend. A
# development machine runs the same programs through `mise exec`, so the image and every machine
# read the history with one version of git, copy with one version of rsync and pack with one
# version of 7-Zip. mise is the release binary of MISE_VERSION, checked against the SHA-256 that
# its release lists in SHASUMS256.txt, and removed once the tools are installed.
#
# Before it installs anything, the script fails when the python3 of the image is not the release of
# Python that mise.toml pins. The Dockerfiles name their base image by a tag and a digest, and
# Docker pulls the digest without reading the tag, so a tag that names a new release beside the
# digest of the earlier one would build the image from the earlier release.
#
#   sh install_tools.sh DIR    DIR holds the mise.toml that pins the tools
set -eu
MISE_VERSION=2026.10.18
pinned=$(python3 -c 'import sys, tomllib
print(tomllib.load(open(sys.argv[1], "rb"))["tools"]["python"])' "$1/mise.toml")
running=$(python3 -c 'import platform; print(platform.python_version())')
if [ "$running" != "$pinned" ]; then
    echo "install_tools.sh: the base image runs Python $running, and mise.toml pins $pinned. Name the base image python:$pinned-slim by the digest that docker buildx imagetools inspect python:$pinned-slim prints." >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64) arch=x64 sha256=e97c38973336aa2ff350fea50f47b412b266fe263b102b7200ff1e9b118c6942 ;;
    aarch64) arch=arm64 sha256=3a3c72376687ba21a588d0f5a2c88c76f483fb6cdece7b0fc43d5e3132b08a52 ;;
    *) echo "install_tools.sh: mise publishes no Linux binary for $(uname -m)" >&2; exit 1 ;;
esac
python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
    "https://github.com/jdx/mise/releases/download/v$MISE_VERSION/mise-v$MISE_VERSION-linux-$arch" \
    /usr/local/bin/mise
echo "$sha256  /usr/local/bin/mise" | sha256sum -c -
chmod +x /usr/local/bin/mise
export MISE_DATA_DIR=/opt/mise MISE_CACHE_DIR=/tmp/mise-cache MISE_STATE_DIR=/tmp/mise-state
cd "$1"
mise trust --quiet mise.toml
mise install conda:git conda:rsync aqua:ip7z/7zip
for tool in git rsync 7zz; do
    ln -sf "$(mise which "$tool")" "/usr/local/bin/$tool"
done
rm -rf /usr/local/bin/mise /tmp/mise-cache /tmp/mise-state
git --version
rsync --version | head -n 1
7zz i | grep -m 1 '^7-Zip'
