"""The steps of the package, each with a `main(argv)` entry point.

The package contains engineered_project_selection, repository_cloning, commit_reclassification,
collected_file_cleanup, detection, file_history, import_detection, output_cross_check,
collected_files_archive and release. The checks over the unmatched paths and over the deletions
of the collected file cleanup are in `checks.unmatched` and `checks.deletions`. The runner starts
each step as a child process, `python -m agentcontext.steps.<name>`, and the command line has a
subcommand for each step.
"""
