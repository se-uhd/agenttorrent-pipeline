"""
The environment variables that change what the steps that run git do, and the functions that
a test module uses to remove them for its tests.

The suites here clone from a `file://` remote and reach it by rewriting
`https://github.com/...` through `GIT_CONFIG_COUNT` and the related variables. A step that
finds `GITHUB_TOKEN` in the environment builds `https://<token>@github.com/...` instead
(`git.clone.build_clone_url`), which the rewrite does not match. Git then tries the real host
and retries up to its limit, and the step collects nothing from the repository. The suites
would then pass or fail depending on whether the machine that runs them exports a GitHub access
token, which is not a property of the code under test.

A test module calls the functions from its `setUpModule` and `tearDownModule`, so that the
variables are absent during that module's tests and restored afterwards for the rest of the
process.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
"""

import os

# Repository cloning and the file history step, the modules that clone, read `GITHUB_TOKEN` and
# turn it into a clone URL. `agentcontext.credentials` reads all four variables for the
# measurements, the snapshot validation and the scripts of the other stages, and it reads
# `GITHUB_TOKENS` as a comma-separated list. tests/test_ambient_env.py checks the list against
# every credential that the package reads from the environment.
CREDENTIAL_VARS = ("GITHUB_TOKEN", "GITHUB_TOKENS", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def take(store: dict) -> dict:
    """Remove the credential variables from the environment, keeping them in `store`."""
    for name in CREDENTIAL_VARS:
        if name in os.environ:
            store[name] = os.environ.pop(name)
    return store


def restore(store: dict) -> dict:
    """Put back whatever `take` removed, and empty `store`."""
    os.environ.update(store)
    store.clear()
    return store
