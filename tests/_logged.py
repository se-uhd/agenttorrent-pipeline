"""What a program logged, split as its tests read it: the INFO lines, which contain its counts
and tables, and the WARNING and ERROR lines."""

import re

_PROBLEM = re.compile(r"^(?:WARNING|ERROR|CRITICAL) [A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*: ")


def split(text: str) -> tuple:
    """(the INFO lines, the WARNING and ERROR lines) of a program's log, each as one string."""
    info, problems = [], []
    for line in text.splitlines(keepends=True):
        (problems if _PROBLEM.match(line) else info).append(line)
    return "".join(info), "".join(problems)
