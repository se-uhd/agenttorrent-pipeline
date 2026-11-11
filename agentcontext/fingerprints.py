"""The files of a package module and of every package module that it imports, and a fingerprint
of their code.

The runner records a step by the hash of its module closure, and the cloning step records the
collector's code with each repository that it collects (`reuse`). Both read the imports from
the source rather than executing them, and a module therefore gets a fingerprint even where the
interpreter cannot import it. The fingerprint of the collector's code and of the code of the
imports also leaves out the texts of exceptions (`code_fingerprint` with `exception_texts=False`),
because no step publishes such a text or decides by it. The runner's fingerprint of a step and
the `classified_by` digest of the commit classification keep them.
"""

import ast
import hashlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def module_closure(name: str, root: Path = PACKAGE_ROOT, stop=frozenset()) -> list:
    """The files of a package module and of every package module that it imports, transitively,
    in the package at `root`, without the modules that `stop` names and what only they import.

    The result of a step depends on the shared modules of the package as well as on the step's
    own file: `paths.py` decides what the collection stage copies, and `heuristics.py` how every
    step classifies. The closure follows `import agentcontext...` and
    `from agentcontext... import ...` statements, except those under `if TYPE_CHECKING:`, which
    never run. `resume.py` names the collector's configuration there for its type hints.
    Following that import would put the collector's code into the record of every step that
    reads the list of finished repositories.
    """
    package = root.name
    seen, queue, files = set(stop), [name], []
    while queue:
        current = queue.pop()
        if current in seen or not current.startswith(package):
            continue
        seen.add(current)
        relative = current.split(".")[1:]
        path = root.joinpath(*relative)
        path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
        if not path.is_file():
            continue
        files.append(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in _run_nodes(tree):
            if isinstance(node, ast.Import):
                queue += [alias.name for alias in node.names if alias.name.startswith(package)]
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(package):
                queue.append(node.module)
                queue += [f"{node.module}.{alias.name}" for alias in node.names]
    return sorted(files)


def imports_closure(paths: list, root: Path = PACKAGE_ROOT) -> list:
    """The files of the package at `root` that the files `paths` import, each with its module
    closure (`module_closure`), for a script that runs outside the package, such as an
    analysis script, and imports its modules."""
    package = root.name
    names = set()
    for path in paths:
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in _run_nodes(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names
                             if alias.name.split(".")[0] == package)
            elif (isinstance(node, ast.ImportFrom) and node.module
                  and node.module.split(".")[0] == package):
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    # Importing a module runs the `__init__.py` of each package that contains it.
    names |= {".".join(name.split(".")[:end]) for name in names
              for end in range(1, name.count(".") + 1)}
    return sorted({file for name in names for file in module_closure(name, root)})


def _is_type_checking(node: ast.AST) -> bool:
    """Whether `node` is `if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:`."""
    test = node.test if isinstance(node, ast.If) else None
    return ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"))


def _run_nodes(tree: ast.AST):
    """The nodes of `tree`, as `ast.walk` gives them, without the body of an
    `if TYPE_CHECKING:` block. Its `else` branch runs and is kept."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        if _is_type_checking(node):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _is_docstring(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _is_log_statement(node: ast.stmt) -> bool:
    """A statement that only writes a log message, `logs.info(...)` and the like."""
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "logs")


def _has_space(text: str) -> bool:
    return any(character.isspace() for character in text)


def _is_text(node: ast.expr) -> bool:
    """Whether `node` builds the text of a message: a string or an f-string with a space in its
    literal text, the sum or `%` formatting of such a text with another expression, or a text's
    `join` or `format`. A string without a space, such as `"exit"`, is a value rather than a
    sentence."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and _has_space(node.value)
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(part, ast.Constant) and _has_space(part.value)
                   for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_text(node.left) or _is_text(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in ("join", "format") and _is_text(node.func.value)
    return False


class _WhatRuns(ast.NodeTransformer):
    """Removes the docstrings and the statements that only write a log message, and, when
    `exception_texts` is False, the texts of the exceptions that a `raise` statement constructs."""

    def __init__(self, exception_texts: bool = True):
        super().__init__()
        self.exception_texts = exception_texts

    def visit_Raise(self, node):
        """A `raise` of a call, with each argument that is the text of a message
        (`_is_text`) replaced by an empty string, unless `exception_texts` is True. The other
        arguments, such as the cause of `history.HistoryUnavailable`, stay, because a caller can
        store them."""
        self.generic_visit(node)
        if self.exception_texts or not isinstance(node.exc, ast.Call):
            return node
        call = node.exc
        call.args = [ast.Constant(value="") if _is_text(arg) else arg for arg in call.args]
        for keyword in call.keywords:
            if _is_text(keyword.value):
                keyword.value = ast.Constant(value="")
        return node

    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if not isinstance(statements, list) or not statements:
                continue
            setattr(node, field, [s for i, s in enumerate(statements)
                                  if not (field == "body" and i == 0 and _is_docstring(s))
                                  and not _is_log_statement(s)])
        return node


def code_fingerprint(files: list, exception_texts: bool = True, root: Path = PACKAGE_ROOT) -> str:
    """One SHA-256 over the code that `files` run: each file's syntax tree, named by its path
    under the package at `root`, without docstrings and without the statements that only write
    a log message. A comment, a docstring, a log message or the layout of the code leaves it
    unchanged. With `exception_texts=False`, the text of an exception that a `raise` statement
    constructs leaves it unchanged as well (`_WhatRuns.visit_Raise`)."""
    digest, package = hashlib.sha256(), Path(root).resolve()
    for path in map(Path, files):
        tree = _WhatRuns(exception_texts).visit(ast.parse(path.read_text(encoding="utf-8")))
        resolved = path.resolve()
        name = resolved.relative_to(package) if resolved.is_relative_to(package) else path.name
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(ast.dump(tree, annotate_fields=False).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
