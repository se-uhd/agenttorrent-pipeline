"""The computations of the stated statistics that `docs/statistics.json` names by a `function`
or a `collection` source, and the readers of the files that they read.

A computation is a function of a module of this package, registered with `checkout` or
`collection` under the name `MODULE.FUNCTION`, such as `commits.count`. It takes a `Files` and
the arguments of its source, and it returns a number. A `checkout` computation reads the files of
the checkout. A `collection` computation reads the data root of the working copy of a handover,
which the checkout does not contain. `python -m agentcontext statistics --collection` computes it
there during the import of the handover and writes its value to the collection statistics file of
the data root, `collection/collection_statistics.json`, which the import copies into the checkout
(`docs/tools/statistics.md`). A value of the collection statistics file is current only while the
code that computed it is unchanged: the module of its computation and every module of the package
that the module imports, directly or through another module, this module among them
(`code_digest`).

`Files` reads a CSV, a JSON file and a JSON Lines file whole or compressed: a `.xz` file, a `.7z`
archive that holds one file, and an archive in numbered volumes through its first volume,
`.7z.001`. `memo` keeps a value for as long as the `Files` exists, and the statistics command keeps
one `Files`, so that the computations parse a large file once.
"""

import ast
import csv
import gc
import hashlib
import importlib
import io
import json
import lzma
import operator
import queue
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from agentcontext import SEVEN_ZIP

COLLECTION_STATISTICS = Path("collection") / "collection_statistics.json"
# The package whose modules the code of a computation is read from.
PACKAGE = Path(__file__).resolve().parents[1]

_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow}


class Unavailable(Exception):
    """A value that cannot be computed: its file is missing or does not contain it."""


class Files:
    """The files under `root` that a computation reads."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict = {}

    def __repr__(self) -> str:
        return f"Files({str(self.root)!r})"

    def exists(self, relative: str) -> bool:
        """Whether the file is there."""
        return (self.root / relative).is_file()

    def path(self, relative: str) -> Path:
        """The file on disk."""
        path = self.root / relative
        if not path.is_file():
            raise Unavailable(f"Could not read {path} (no such file)")
        return path

    def size(self, relative: str) -> int:
        """The size of the file in bytes."""
        return self.path(relative).stat().st_size

    def bytes(self, relative: str) -> bytes:
        """The content of the file, decompressed when it is a `.xz` file or a `.7z` archive."""
        if relative.endswith((".7z", ".7z.001")):
            return _extract(self.path(relative))
        data = self.path(relative).read_bytes()
        if relative.endswith(".xz"):
            try:
                return lzma.decompress(data)
            except lzma.LZMAError as error:
                raise Unavailable(f"Could not decompress {relative} ({error})") from error
        return data

    def text(self, relative: str) -> str:
        """The text of the file."""
        return self.bytes(relative).decode("utf-8")

    def json(self, relative: str):
        """The JSON value of the file."""
        key = ("json", relative)
        if key not in self._cache:
            try:
                self._cache[key] = json.loads(self.bytes(relative))
            except ValueError as error:
                raise Unavailable(f"Could not read {relative} ({error})") from error
        return self._cache[key]

    def lines(self, relative: str) -> Iterator[bytes]:
        """An iterator over the lines of a file, each with its line feed, decompressed when it is
        a `.xz` file or a `.7z` archive. Lines end at a line feed only, since a JSON string can
        hold a character that `str.splitlines` takes for a line end, such as U+2028. A `.xz`
        file is decompressed in a thread that starts at once, because decompressing releases
        the interpreter lock: the lines are parsed while the rest is decompressed, and the
        files that a computation opens together are decompressed side by side."""
        if not relative.endswith(".xz"):
            return iter(io.BytesIO(self.bytes(relative)))
        data = self.path(relative).read_bytes()
        blocks: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=_decompress, args=(data, relative, blocks), daemon=True).start()
        return _split_lines(blocks)

    def jsonl(self, relative: str) -> Iterator:
        """The JSON value of every line of a JSON Lines file."""
        for line in self.lines(relative):
            if line.strip():
                yield json.loads(line)

    def rows(self, relative: str):
        """Every data row of a CSV as a dict, read as a stream."""
        csv.field_size_limit(sys.maxsize)
        if relative.endswith((".7z", ".7z.001")):
            with _archive_stream(self.path(relative)) as stream:
                yield from csv.DictReader(stream)
        else:
            yield from csv.DictReader(io.StringIO(self.text(relative), newline=""))

    def header(self, relative: str) -> list:
        """The columns of a CSV."""
        csv.field_size_limit(sys.maxsize)
        if relative.endswith((".7z", ".7z.001")):
            with _archive_stream(self.path(relative), whole=False) as stream:
                return next(csv.reader(stream), [])
        return next(csv.reader(io.StringIO(self.text(relative), newline="")), [])

    def lists(self, relative: str) -> Iterator[list]:
        """Every row of a CSV as a list of its fields, the header first, read as a stream. A
        list costs less memory than the dict of `rows` for a large CSV."""
        csv.field_size_limit(sys.maxsize)
        if relative.endswith((".7z", ".7z.001")):
            with _archive_stream(self.path(relative)) as stream:
                yield from csv.reader(stream)
        else:
            yield from csv.reader(io.StringIO(self.text(relative), newline=""))

    def glob(self, pattern: str) -> list[str]:
        """The files that match a glob pattern, as paths relative to the root, sorted."""
        return sorted(path.relative_to(self.root).as_posix()
                      for path in self.root.glob(pattern) if path.is_file())

    def constant(self, relative: str, name: str, index=None, length=False):
        """A top-level constant of a Python file, its item `index` or its length, evaluated
        without importing the file."""
        return self.memo(("constant", relative, name, index, length),
                         lambda: constant(self.text(relative), name, relative, index, length))

    def json_lines(self, relative: str) -> list:
        """The JSON value of every line of a JSON Lines file as a list, parsed once for these
        files. The list and its values are shared and must not change."""
        return self.memo(("json lines", relative), lambda: list(self.jsonl(relative)))

    def memo(self, key, compute):
        """The value of `compute()` under `key`, computed once for these files."""
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]


# The compressed bytes that `_decompress` passes to the decompressor at a time, large enough that
# the thread rarely waits for the interpreter lock.
_XZ_STEP = 1 << 20


def _decompress(data: bytes, relative: str, blocks: queue.SimpleQueue) -> None:
    """Put the decompressed blocks of the `.xz` content `data` on `blocks`, then None, or the
    error that ended it. The result is that of `lzma.decompress`. The streams of a file follow
    one another, and a later stream that does not decode is left out with what follows it. Data
    that ends inside a stream cannot be read."""
    try:
        position, first = 0, True
        while first or position < len(data):
            decompressor = lzma.LZMADecompressor()
            # The blocks of a later stream wait until the stream ends, because a later stream
            # that does not decode is left out whole.
            later = []
            try:
                while not decompressor.eof and position < len(data):
                    chunk = data[position:position + _XZ_STEP]
                    block = decompressor.decompress(chunk)
                    if first:
                        blocks.put(block)
                    else:
                        later.append(block)
                    position += len(chunk)
            except lzma.LZMAError:
                if first:
                    raise
                break
            if not decompressor.eof:
                raise lzma.LZMAError("the data ends before the end of its stream")
            for block in later:
                blocks.put(block)
            position -= len(decompressor.unused_data)
            first = False
        blocks.put(None)
    except lzma.LZMAError as error:
        blocks.put(Unavailable(f"Could not decompress {relative} ({error})"))


def _split_lines(blocks: queue.SimpleQueue) -> Iterator[bytes]:
    """The lines of the decompressed blocks that `_decompress` puts on a queue, each with its
    line feed."""
    rest = b""
    while True:
        block = blocks.get()
        if block is None:
            break
        if isinstance(block, Unavailable):
            raise block
        pieces = (rest + block).split(b"\n")
        rest = pieces.pop()
        for piece in pieces:
            yield piece + b"\n"
    if rest:
        yield rest


def _extract(path: Path) -> bytes:
    """The one file of a `.7z` archive."""
    try:
        proc = subprocess.run([SEVEN_ZIP, "e", "-so", str(path)], capture_output=True)
    except OSError as error:
        raise Unavailable(f"Could not run 7zz to read {path} ({error})") from error
    if proc.returncode != 0:
        raise Unavailable(f"7zz exited {proc.returncode} reading {path}")
    return proc.stdout


# The buffer between `7zz` and its reader. `7zz` writes a decompressed block whole before it
# decompresses the next, and a pipe holds 64 KiB, so through a pipe `7zz` waits while the reader
# parses. A socket can hold a block, and both then work at once. A system can cap the size.
_ARCHIVE_BUFFER = 4 << 20


class _archive_stream:
    """A text stream of the one file of a `.7z` archive, which `7zz` extracts to its standard
    output, so that a large CSV is read without a copy on disk. A reader that stops early, such
    as a reader of the header, ends `7zz`, which is no failure. With `whole`, the exit code of
    `7zz` counts."""

    def __init__(self, path: Path, whole: bool = True):
        self.path = path
        self.whole = whole

    def __enter__(self):
        self.reader, writer = socket.socketpair()
        for end, option in ((writer, socket.SO_SNDBUF), (self.reader, socket.SO_RCVBUF)):
            try:
                end.setsockopt(socket.SOL_SOCKET, option, _ARCHIVE_BUFFER)
            except OSError:
                pass
        try:
            self.process = subprocess.Popen([SEVEN_ZIP, "e", "-so", str(self.path)],
                                            stdout=writer.fileno(), stderr=subprocess.DEVNULL)
        except OSError as error:
            self.reader.close()
            raise Unavailable(f"Could not run 7zz to read {self.path} ({error})") from error
        finally:
            writer.close()
        self.stream = self.reader.makefile("rb", buffering=1 << 20)
        return io.TextIOWrapper(self.stream, encoding="utf-8", newline="")

    def __exit__(self, kind, value, traceback):
        if not self.whole:
            self.process.kill()
        self.stream.close()
        self.reader.close()
        returncode = self.process.wait()
        if kind is None and self.whole and returncode != 0:
            raise Unavailable(f"7zz exited {returncode} reading {self.path}")
        return False


def constant(source: str, name: str, label: str, index=None, length=False):
    """A top-level constant of Python source, from literals and arithmetic over its other
    constants, its item `index` or its length. A `frozenset`, `set` or `tuple` call with one
    literal argument counts as that literal."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise Unavailable(f"Could not read {label} ({error})") from error
    assigned = {}
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) and node.value else [])
        for target in targets:
            if isinstance(target, ast.Name):
                assigned[target.id] = node.value

    def literal(node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("frozenset", "set", "tuple") and len(node.args) == 1
                and not node.keywords):
            return node.args[0]
        return node

    def evaluate(node, seen):
        node = literal(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -evaluate(node.operand, seen)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](evaluate(node.left, seen), evaluate(node.right, seen))
        if isinstance(node, ast.Name) and node.id in assigned and node.id not in seen:
            return evaluate(assigned[node.id], seen | {node.id})
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return tuple(evaluate(item, seen) for item in node.elts)
        if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
            return tuple(evaluate(key, seen) for key in node.keys)
        raise Unavailable(f"The constant {name} of {label} is not a number computed from "
                          f"literals and constants")

    if name not in assigned:
        raise Unavailable(f"{label} has no top-level constant {name}")
    node = literal(assigned[name])
    if length and isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        # A sequence's length needs no item evaluated, and a tuple of calls has a length too.
        return len(node.keys if isinstance(node, ast.Dict) else node.elts)
    value = evaluate(node, {name})
    if length:
        if not isinstance(value, tuple):
            raise Unavailable(f"The constant {name} of {label} is not a sequence and has no "
                              f"length")
        return len(value)
    if index is None and isinstance(value, tuple):
        raise Unavailable(f"The constant {name} of {label} is a sequence, but the statistic "
                          f"gives no index")
    if index is not None:
        if not isinstance(value, tuple) or not -len(value) <= index < len(value):
            raise Unavailable(f"The constant {name} of {label} has no item {index}")
        value = value[index]
    if not isinstance(value, (int, float)):
        raise Unavailable(f"The constant {name} of {label} is not a number computed from "
                          f"literals and constants")
    return value


# The registered computations: {"MODULE.FUNCTION": ("checkout" or "collection", function)}.
_REGISTERED: dict = {}


def _register(kind: str, function):
    module = function.__module__.rpartition(".")[2]
    _REGISTERED[f"{module}.{function.__name__}"] = (kind, function)
    return function


def checkout(function):
    """Register a computation over the files of the checkout."""
    return _register("checkout", function)


def collection(function):
    """Register a computation over the data root of the working copy of a handover."""
    return _register("collection", function)


def lookup(name: str) -> tuple:
    """The kind and the function of a registered computation, importing its module."""
    module = name.partition(".")[0]
    if not module.isidentifier() or not Path(__file__).with_name(f"{module}.py").is_file():
        raise Unavailable(f"No module of agentcontext.statistics holds the computation {name}")
    importlib.import_module(f"{__name__}.{module}")
    if name not in _REGISTERED:
        raise Unavailable(f"agentcontext.statistics.{module} registers no computation {name}")
    return _REGISTERED[name]


def call(name: str, args: dict, files: Files):
    """The value of a computation over `files`, a whole number or a fraction."""
    _kind, function = lookup(name)
    # The cyclic garbage collector would scan the parsed files that `Files.memo` keeps each time
    # a computation allocates more objects, and parsing a large file allocates millions. Parsed
    # rows and records hold no reference cycles, so the collector pauses while a computation
    # runs and resumes afterwards.
    enabled = gc.isenabled()
    gc.disable()
    try:
        value = function(files, **args)
    except Unavailable:
        raise
    except (OSError, KeyError, ValueError, TypeError, IndexError, ZeroDivisionError) as error:
        raise Unavailable(f"The computation {name}({call_arguments(args)}) over {files} "
                          f"failed with {type(error).__name__} ({error})") from error
    finally:
        if enabled:
            gc.enable()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Unavailable(f"The computation {name} returned {value!r}, which is not a number")
    return value


def call_arguments(args: dict) -> str:
    """The arguments of a computation as the keyword arguments of a call: `distinct="row"`."""
    return ", ".join(f"{name}={json.dumps(value, ensure_ascii=False)}"
                     for name, value in sorted(args.items()))


def call_key(name: str, args: dict) -> str:
    """The key of a computation with its arguments, as the collection statistics file records it."""
    return name + json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _module_file(module: str, package: Path) -> Path | None:
    """The source file of a module of the package by its dotted name, or None for a name that
    is no module of the package, such as `agentcontext.REPO_ROOT`."""
    parts = module.split(".")
    if parts[0] != package.name:
        return None
    base = package.joinpath(*parts[1:])
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if len(parts) > 1 and base.with_name(f"{base.name}.py").is_file():
        return base.with_name(f"{base.name}.py")
    return None


def _imported(module: str, path: Path, package: Path) -> set:
    """The modules of the package that a source file imports anywhere in it, each with the
    packages that contain it, because importing a module runs the `__init__.py` of each."""
    found = set()
    for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import counts from the package of the module, which for an
                # `__init__.py` is the module itself.
                parts = module.split(".")[:None if path.name == "__init__.py" else -1]
                parts = parts[:len(parts) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            # `from agentcontext import commits` imports a module, and
            # `from agentcontext import REPO_ROOT` a name of a module.
            names = [base] + [f"{base}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            parts = name.split(".")
            for end in range(1, len(parts) + 1):
                prefix = ".".join(parts[:end])
                if _module_file(prefix, package) is not None:
                    found.add(prefix)
    return found


def code_files(name: str, package: Path = PACKAGE) -> list[Path]:
    """The source files of the code of a computation: its module and every module of the
    package that the module imports, directly or through another module, sorted."""
    module = f"{package.name}.statistics.{name.partition('.')[0]}"
    if _module_file(module, package) is None:
        raise Unavailable(f"No module of {package.name}.statistics holds the computation {name}")
    seen: dict = {}
    pending = [module]
    while pending:
        current = pending.pop()
        path = _module_file(current, package)
        if current in seen or path is None:
            continue
        seen[current] = path
        pending.extend(_imported(current, path, package))
    return sorted(seen.values())


def code_digest(name: str, package: Path = PACKAGE) -> str:
    """The SHA-256 of the code of a computation, over the path and the content of each of its
    `code_files`. A value of the collection statistics file is current only while that code is
    unchanged, since a change of a module that the computation imports can change its value."""
    digest = hashlib.sha256()
    for path in code_files(name, package):
        data = path.read_bytes()
        relative = path.relative_to(package.parent).as_posix()
        digest.update(f"{relative}\0{len(data)}\0".encode("utf-8") + data)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    """The SHA-256 of a file, read in blocks, so that a handover of gigabytes fits in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()
