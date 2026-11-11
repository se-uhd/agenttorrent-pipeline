"""The collection stage's suites, one per package module, each named for its module:
`tests.test_<module>` for a top-level module, and `tests.test_<subpackage>_<module>` for a module
under `steps/`, `checks/`, `tools/` or `git/`. `test_ambient_env` tests the fixture in
`_ambient_env.py`. The suites `test_tech_stack`, `test_check_modes`, `test_data_layout`,
`test_doc_commit_refs`, `test_undefined_names`, `test_spec_case_counts`, `test_stated_facts` and
`test_glossary` check properties of the repository as a whole rather than of one module. Run the
suites from the repository root with `python3 -m unittest discover -s tests -t .`, or run a
module by name. No suite here edits `sys.path`, because each imports the code that it tests from
the installed `agentcontext` package.
"""

import os

# No suite reads the credentials file of the machine that it runs on. A developer's
# credentials.csv at the repository root would give its GitHub access tokens to the steps under
# test, which would then query GitHub. A suite that tests the file writes its own file and passes
# its path.
os.environ["AGENTCONTEXT_CREDENTIALS"] = os.devnull
