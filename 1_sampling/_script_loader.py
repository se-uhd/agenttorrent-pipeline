"""Load a numbered sampling script as a module, for its tests.

`1_download_ghs.py` and the other numbered scripts cannot be imported by name, because a name
that starts with a digit is not an identifier. The suites load them by path through this
module. The module also provides a stub for a third-party package that is not installed. CI runs
the suite of the sampling runner before it installs the stage's requirements, when the
interpreter has neither `requests` nor `yaml`. With the stubs, a script can still be imported,
and the functions that never call those packages can be tested. A test that needs the real
package is skipped when it finds the stub, and the script suites run after the install, with
the real packages.

`synced_writes` records how a script replaces a file, for the suites that test that a file is
synced to disk before its rename.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import stat
import sys
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _stub(name: str, **attrs) -> types.ModuleType:
    """The real module when it is installed, else a stub with `attrs`."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        module = types.ModuleType(name)
        module.__is_stub__ = True
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module


def is_stub(name: str) -> bool:
    return getattr(sys.modules.get(name), "__is_stub__", False)


def load_script(filename: str) -> types.ModuleType:
    """The numbered script as a module, with the packages that it imports present or stubbed."""
    _stub("requests", get=None, post=None,
          RequestException=type("RequestException", (Exception,), {}))
    _stub("yaml", safe_load=None)
    path = HERE / filename
    spec = importlib.util.spec_from_file_location("_sampling_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def synced_writes(write, directory: Path) -> list:
    """Run `write` with the real `os.fsync` and `os.replace` recording each call, and return the
    calls in order: `("file", inode)` for a synced file, `("directory", True)` for a sync of
    `directory` and `("directory", False)` for any other directory, and
    `("rename", inode, name)` for the rename of the file with that inode to `name`."""
    entry = os.stat(directory)
    calls = []
    sync, rename = os.fsync, os.replace

    def recorded_sync(handle):
        status = os.fstat(handle)
        if stat.S_ISDIR(status.st_mode):
            calls.append(("directory", (status.st_dev, status.st_ino)
                          == (entry.st_dev, entry.st_ino)))
        else:
            calls.append(("file", status.st_ino))
        sync(handle)

    def recorded_rename(source, destination):
        calls.append(("rename", os.stat(source).st_ino, Path(destination).name))
        rename(source, destination)

    with mock.patch("os.fsync", recorded_sync), mock.patch("os.replace", recorded_rename):
        write()
    return calls
