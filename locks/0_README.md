# Dependency locks

Every Python environment of the project installs its distributions from a dependency lock in this
directory and from no other source. A dependency lock lists every distribution that its
environment installs, the dependencies of dependencies included, with its exact version and the
SHA-256 of every file of that version on PyPI. The requirements files beside the scripts pin only
the direct dependencies, one version per distribution in every file, which
`tests/test_tech_stack.py` checks.

`dev/lock.sh` writes the dependency locks from the requirements files with the uv that
`mise.toml` pins, for major.minor of the release of Python that `mise.toml` pins. uv resolves them
with `--universal`, so that one dependency lock serves macOS on arm64, Linux on x86-64 (CI and the
production image) and Linux on arm64 (the development image on Apple silicon). An entry with an
environment marker is installed only where its marker holds. A marker that excludes only
WebAssembly or PyPy, such as `click==8.5.0 ; sys_platform != 'emscripten'` or
`cffi==2.0.0 ; platform_python_implementation != 'PyPy'`, holds on every platform of the
project, and the installers install its entry everywhere. A marker that names Windows or
WebAssembly, such as `pywin32==312 ; sys_platform == 'win32'`, holds on no platform of the
project, and the installers leave its entry out. The second line of each file is the
`uv pip compile` command that wrote it, with the requirements files that it reads.

`development.txt` locks every requirements file. The other dependency locks are resolved with
`development.txt` as constraints, so that a distribution has the same version and the same hashes
in every dependency lock:

| Dependency lock | Requirements files | Installed by |
|---|---|---|
| `development.txt` | every requirements file | `dev/setup.sh` into `.venv` on macOS and Linux, CI into its `.venv`, and the development image |
| `pipeline.txt` | `build-requirements.txt` and the requirements files of the five stages | the production image |
| `build.txt` | `build-requirements.txt`, the setuptools that pyproject.toml names | every environment first |

CI installs `development.txt` with the same commands as `dev/setup.sh`, so the suites run with the
same distributions in CI and on a development machine. Only the sampling runner suite runs before
the install, with `python3 -S`, which imports no site packages, and the verification runs it the
same way.

Every install requires hashes (`--require-hashes`), so the installer refuses a distribution that
the dependency lock does not list. `build.txt` comes first, because its setuptools builds venn,
which PyPI has as a source distribution only, and the editable package of the repository. These
builds run without build isolation (`--no-build-isolation`), because an isolated build would
download a setuptools that no dependency lock lists.

## Changing a pin

Change the pin in every requirements file that names the distribution, then write the dependency
locks again and install the development lock:

```shell
mise exec -- bash dev/lock.sh
mise exec -- bash dev/setup.sh
```

Then build the development image again with `mise exec -- bash deploy/build_development_image.sh`,
with `arm64` on Apple silicon, because the image installs `build.txt` and `development.txt` when it
is built, and a container of an earlier image holds the distributions of the earlier lock
([development](../docs/development.md#linux-x86-64-development-container)).

uv keeps every version of the existing dependency locks that the requirements files still allow,
so a changed pin changes that distribution and the distributions that depend on it. To move a
distribution that no requirements file pins, run `mise exec -- bash dev/lock.sh
--upgrade-package NAME`, and `--upgrade` to resolve every distribution again.
`tests/test_tech_stack.py` fails when a dependency lock lists a pin of its requirements files at
another version or not at all, when it credits a distribution to a requirements file that no
longer pins it, when a requirements file is in no dependency lock, and when no environment
installs a dependency lock.
