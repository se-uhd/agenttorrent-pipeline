"""The collection, the publication and the command line of the AgentContext pipeline, as one
package.

The code of every stage is in the repository, and everything that a run reads and writes is under
one data root, `DATA_ROOT`, with a directory per stage. `STAGE_DIR` is the directory of the
collection stage, which contains `data/`, `output/`, `release/`, `run_logs/` and the three caches.
The library and the tools resolve their default paths under these directories. The defaults of the
step modules are relative to the working directory, which the runner sets to `STAGE_DIR`.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The data root that the pipeline reads and writes: this repository's `data/`, unless
# AGENTCONTEXT_DATA_ROOT names another directory. The editable install binds `import agentcontext`
# to one checkout wherever a script runs, and a run with its data elsewhere therefore sets the
# variable. The container sets it to its one mount, because the code comes from the image and the
# data from the volume. The subsample run over the mirrors sets it to the `data/` of its own
# checkout. A run in another checkout that does not set it writes into this repository's caches,
# because the scripts of that checkout import this package.
DATA_ROOT = Path(os.environ.get("AGENTCONTEXT_DATA_ROOT") or REPO_ROOT / "data").resolve()
SAMPLING_DIR = DATA_ROOT / "sampling"
CLASSIFICATION_DIR = DATA_ROOT / "classification"
STAGE_DIR = DATA_ROOT / "collection"
ANALYSIS_DIR = DATA_ROOT / "analysis"

# The program that packs, lists and extracts every 7-Zip archive of the pipeline: the `7zz` of the
# 7-Zip releases, which mise.toml pins. `mise exec` puts it on the PATH of a development machine,
# and deploy/install_tools.sh links it into /usr/local/bin of both images. The release does not
# provide `7z`. `deploy/entrypoint.py` repeats the name.
SEVEN_ZIP = "7zz"


def csv_archive(path: Path) -> Path:
    """Return the first volume of a split CSV archive, or the single-file archive of the CSV.

    The commit archive exceeds GitHub's per-file limit. Every numbered
    volume must remain beside the first volume for 7-Zip to extract it.
    """
    split = Path(str(path) + ".7z.001")
    return split if split.is_file() else Path(str(path) + ".7z")
