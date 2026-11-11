#!/bin/bash
# Prepare development on macOS or Linux in the virtual environment of the checkout, .venv, which
# mise creates from the Python that mise.toml pins and activates under `mise exec`. Run from the
# repository root:
#     mise exec -- bash dev/setup.sh
# Install the distributions of locks/development.txt and the editable package into .venv, and
# extract the current tracked commit CSVs, the sampling frame and the labels. --data-only skips
# the installation, as in the development container, whose image contains the distributions of
# the dependency lock. The collected trees and the other inputs come from the latest handover.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --data-only ]; }; then
    echo "Usage: $0 [--data-only]" >&2
    exit 2
fi

if [ "${1:-}" != --data-only ]; then
    case "$(uname -s)" in
        Darwin | Linux) ;;
        *) echo "Supported hosts: macOS and Linux" >&2; exit 1 ;;
    esac
    # The locked distributions go into the virtual environment of this checkout only, never into
    # mise's shared installation of Python or into another environment.
    venv=$(cd "$ROOT/.venv" 2>/dev/null && pwd -P) || venv=""
    active=$(cd "${VIRTUAL_ENV:-/nonexistent}" 2>/dev/null && pwd -P) || active=""
    if [ -z "$venv" ] || [ "$venv" != "$active" ]; then
        echo "$ROOT/.venv is not the active virtual environment. Run mise exec -- bash dev/setup.sh, which creates and activates it from mise.toml." >&2
        exit 1
    fi
    # mise creates .venv once from the release of Python that mise.toml pins, and keeps it when the
    # pin changes, so a .venv of another release is refused until it is created again.
    "$venv/bin/python" - "$ROOT/mise.toml" "$venv" <<'EOF' || exit 1
import platform, sys, tomllib
pinned = tomllib.load(open(sys.argv[1], "rb"))["tools"]["python"]
if platform.python_version() != pinned:
    sys.exit(f"{sys.argv[2]} runs Python {platform.python_version()}, and mise.toml pins "
             f"{pinned}. Remove it with rm -rf {sys.argv[2]}, then run mise exec -- bash "
             f"dev/setup.sh, which creates it again from the pin.")
EOF
    for tool in git 7zz rsync pdftotext uv; do
        command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
    done
    # setuptools first, from locks/build.txt, because it builds venn, which PyPI has as a source
    # distribution only, and the editable package without build isolation. An isolated build
    # would download a setuptools that no dependency lock lists. --require-hashes refuses a
    # distribution that the dependency lock does not list, --exact removes every installed
    # distribution that it does not list, and --reinstall replaces a distribution that was
    # installed by hand at a locked version, such as from a local directory, with the locked file.
    # The installs read no configuration of uv: --no-config ignores the uv.toml files and the
    # [tool.uv] tables that uv finds, and UV_CONFIG_FILE, which --no-config does not ignore, is
    # unset. So are the variables that make uv build a distribution from its source distribution
    # instead of installing its wheel, or refuse to build venn. A build from the source
    # distribution passes the hash check and installs other files than CI installs. Under
    # UV_PYTHON or UV_SYSTEM_PYTHON, uv ignores the active virtual environment and installs into
    # the interpreter that the variable selects, such as mise's shared installation of Python.
    # Both are unset, and --python names the interpreter of .venv, which uv prefers to either.
    # UV_NO_EDITABLE is unset as well. Under it, uv installs the package of the checkout from a
    # wheel rather than editable and leaves build/ and agentcontext.egg-info in the checkout.
    # UV_CONSTRAINT, UV_OVERRIDE and UV_EXCLUDE are unset, because they name requirements files
    # that constrain, override or exclude distributions of the dependency lock.
    # CI runs the same three installs (.github/workflows/pipeline-tests.yml).
    unset UV_CONFIG_FILE UV_NO_BINARY UV_NO_BINARY_PACKAGE UV_NO_BUILD UV_NO_BUILD_PACKAGE \
        UV_PYTHON UV_SYSTEM_PYTHON UV_NO_EDITABLE UV_CONSTRAINT UV_OVERRIDE UV_EXCLUDE
    uv pip install -q --no-config --python "$venv/bin/python" --require-hashes \
        -r "$ROOT/locks/build.txt"
    uv pip install -q --no-config --python "$venv/bin/python" --exact --reinstall \
        --require-hashes --no-build-isolation -r "$ROOT/locks/development.txt"
    uv pip install -q --no-config --python "$venv/bin/python" --no-deps --no-build-isolation \
        -e "$ROOT"
fi

# The tracked outputs describe the current development dataset. Setup must not
# reconstruct the inputs of an earlier collection or regenerate current outputs from one.
# A merge, a pull or a switch of branches replaces a tracked archive and leaves the CSV that was
# extracted from the earlier archive, and the analysis stops on such a CSV
# (agentcontext/archives.py). Setup therefore extracts a CSV again when it differs from its
# archive, and keeps it when it matches.
#
# An extraction writes into a temporary file or directory beside its archive first. When setup
# stops before it moves or removes it, after an error or a termination signal, the trap removes
# it, because an untracked file in the checkout fails the verification.
leftover=""
trap 'if [ -n "$leftover" ]; then rm -rf "$leftover"; fi' EXIT
cd "$ROOT/data/collection"
for name in all_ai_commits other_ai_commits; do
    archive="output/$name.csv.7z.001"
    [ -f "$archive" ] || archive="output/$name.csv.7z"
    [ -f "$archive" ] || continue
    if [ -f "output/$name.csv" ]; then
        # Exit 0 when the CSV matches its archive, 2 when it differs and 1 when 7zz cannot list
        # the archive. The package of this checkout comes first, because an editable install
        # can bind `agentcontext` to another checkout.
        status=0
        PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "output/$name.csv" "$archive" <<'EOF' || status=$?
import sys
from pathlib import Path
from agentcontext import archives
try:
    matches = archives.copy_matches(Path(sys.argv[1]), Path(sys.argv[2]))
except archives.ArchiveUnreadable as error:
    sys.exit(str(error))
sys.exit(0 if matches else 2)
EOF
        case "$status" in
            0) continue ;;
            2) echo "output/$name.csv differs from $archive. Extracting it again." >&2 ;;
            *) exit 1 ;;
        esac
    fi
    commits_tmp=$(mktemp "$PWD/output/.$name.XXXXXX")
    leftover=$commits_tmp
    if 7zz e -so "$archive" > "$commits_tmp" && [ -s "$commits_tmp" ]; then
        mv "$commits_tmp" "output/$name.csv"
        leftover=""
    else
        echo "Could not extract output/$name.csv from $archive" >&2
        exit 1
    fi
done

# The analysis reads the sampling frame and the labels beside their archives, and the archive of
# the labels also holds the record of their model and the repositories that the classification
# left out. Setup extracts each file of these archives that is missing. The filter and the
# labeling write newer files beside the archives in a checkout, after hours of queries to GitHub
# or a paid run of a model, so setup keeps a file that differs from its archive, names it and
# extracts no file of that archive. The analysis stops on such a file until it is extracted again
# or its archive is packed again (docs/archives.md).
cd "$ROOT/data"
labels_match=0
for archive in sampling/ghs_filtered.json.7z classification/repos_annotated.7z; do
    [ -f "$archive" ] || continue
    dir=$(dirname "$archive")
    # The names of the files of the archive that are missing beside it, on standard output.
    # Exit 0 when every file beside it matches the archive, 2 when one differs and 1 when 7zz
    # cannot list the archive.
    status=0
    missing=$(PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 - "$archive" <<'EOF'
import sys
from pathlib import Path
from agentcontext import archives
archive = Path(sys.argv[1])
try:
    names = sorted({path.replace("\\", "/").rsplit("/", 1)[-1]
                    for path, _, _ in archives.listed_files(archive)})
    differing = [name for name in names if (archive.parent / name).is_file()
                 and not archives.copy_matches(archive.parent / name, archive)]
except archives.ArchiveUnreadable as error:
    sys.exit(str(error))
for name in differing:
    print(f"{archive.parent / name} differs from {archive}. Keeping it and extracting no file of "
          f"the archive. Extract the file again or pack the archive again (docs/archives.md).",
          file=sys.stderr)
print(" ".join(name for name in names if not (archive.parent / name).is_file()))
sys.exit(2 if differing else 0)
EOF
) || status=$?
    case "$status" in
        0) ;;
        2) continue ;;
        *) exit 1 ;;
    esac
    if [ -n "$missing" ]; then
        # Into a directory beside the archive first, so that no file is installed when the
        # archive lacks a file at its top level or when a file fails its CRC, which 7zz still
        # writes.
        scratch=$(mktemp -d "$PWD/$dir/.extract.XXXXXX")
        leftover=$scratch
        # The names are words without spaces, one argument each.
        # shellcheck disable=SC2086
        if ! 7zz x -y -bso0 -bsp0 -o"$scratch" "$archive" $missing; then
            echo "Could not extract $missing from $archive" >&2
            exit 1
        fi
        # The listing gives each file by its name alone. 7zz extracts no file for a name that
        # the archive stores in a directory, and the analysis reads the file beside the archive.
        for name in $missing; do
            if [ ! -f "$scratch/$name" ]; then
                echo "$archive does not contain $name at its top level" >&2
                exit 1
            fi
        done
        for name in $missing; do
            mv "$scratch/$name" "$dir/$name"
        done
        rm -rf "$scratch"
        leftover=""
    fi
    if [ "$archive" = classification/repos_annotated.7z ]; then
        labels_match=1
    fi
done
# The list of the sample, which engineered-project-selection derives from the labels and the
# cloning step reads. Setup writes it when it is missing or differs from the list that the labels
# give, and only from labels that match their archive.
if [ "$labels_match" = 1 ] && [ -f classification/repos_annotated.csv ]; then
    selection=(env "PYTHONPATH=$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m agentcontext
               engineered-project-selection
               --annotated-csv "$ROOT/data/classification/repos_annotated.csv"
               -o "$ROOT/data/classification/repos_annotated_included.csv")
    "${selection[@]}" --check >/dev/null 2>&1 || "${selection[@]}"
fi
