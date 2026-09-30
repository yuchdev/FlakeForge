"""Shared helpers and constants used by the built-in rules in :mod:`flakeforge.rules`."""

from __future__ import annotations

import ast
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .api import RuleContext, RuleViolation

# Parsed import edges of neighbouring modules, keyed by their effective import
# identity and invalidated by modification time so repeated runs in one process
# never go stale.
_MODULE_IMPORT_CACHE: dict[tuple[Path, Path, str], tuple[int, tuple[ModuleImport, ...]]] = {}

_TODO_ANNOTATION_RE = re.compile(
    r"^#\s*(?:TODO|FIXME):\s*"
    r"\[(?P<date>\d{4}-\d{2}-\d{2})\]"
    r"\[(?P<owner>[A-Za-z0-9][A-Za-z0-9_-]*)\]\s+"
    r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\s+"
    r"\[issue:\s*#(?P<issue>\d+),\s*(?P<url>https?://[^\]\s]+)\]\s+-\s+\S.*$"
)
_TEST_DOCSTRING_HEADER_RE = re.compile(r"^\s*\[(Unit|Integration|Local|E2E)]\s+\S.*$")

_TEST_DOCSTRING_SECTIONS = ("Scenario:", "Boundaries:", "On failure, first check:")

_PERCENT_PLACEHOLDER_RE = re.compile(r"%(?:\(\w+\))?[-#0 +]*\d*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrs]")


def _violation(context: RuleContext, node: ast.AST, code: str, message: str) -> RuleViolation:
    """Build a :class:`RuleViolation` at *node*'s position for *code*."""
    return RuleViolation(
        filename=context.filename,
        lineno=getattr(node, "lineno", 1),
        col_offset=getattr(node, "col_offset", 0),
        code=code,
        message=message,
    )


def _contains_exception(exc_node: ast.expr) -> bool:
    """Return whether *exc_node* names ``Exception`` directly or in a tuple."""
    if isinstance(exc_node, ast.Name) and exc_node.id == "Exception":
        return True
    if isinstance(exc_node, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id == "Exception" for elt in exc_node.elts)
    return False


@dataclass(frozen=True)
class ModuleImport:
    """One runtime import edge extracted from a module's AST.

    :ivar module: Absolute dotted name of the imported project module.
    :ivar lineno: 1-based line of the import statement creating the edge.
    :ivar col_offset: 0-based column of the import statement.
    """

    module: str
    lineno: int
    col_offset: int


@dataclass(frozen=True)
class ModuleLocation:
    """Where a module lives on disk and how it is addressed as an import.

    :ivar root: Directory the module is imported from, i.e. the directory that
        would have to be on ``sys.path`` for :attr:`name` to be importable.
    :ivar name: Absolute dotted module name, such as ``pkg.sub.mod``.
    :ivar is_package: Whether the module is a package's ``__init__`` module.
    """

    root: Path
    name: str
    is_package: bool

    @property
    def package(self) -> str:
        """Return the dotted package that relative imports resolve against."""
        if self.is_package:
            return self.name
        parent, _, _ = self.name.rpartition(".")
        return parent


class ModuleImportGraph:
    """Directed graph of the runtime import dependencies between modules.

    Nodes are absolute dotted module names and edges are the module-level
    imports found in each module's AST. The graph is a plain container: callers
    populate it with :meth:`add` and interrogate it with :meth:`imports_of` and
    :meth:`find_path`.
    """

    def __init__(self):
        """Initialise an empty graph."""
        self._edges: dict[str, tuple[ModuleImport, ...]] = {}

    def add(self, module: str, imports: Sequence[ModuleImport]):
        """Record *imports* as the outgoing edges of *module*."""
        self._edges[module] = tuple(imports)

    def modules(self) -> tuple[str, ...]:
        """Return every module with recorded outgoing edges, ordered by name."""
        return tuple(sorted(self._edges))

    def imports_of(self, module: str) -> tuple[ModuleImport, ...]:
        """Return the recorded outgoing edges of *module*."""
        return self._edges.get(module, ())

    def find_path(self, source: str, target: str) -> tuple[str, ...]:
        """Return the shortest import path leading from *source* to *target*.

        The path includes both endpoints, so a module importing itself yields a
        single-element path. An empty tuple means *target* is unreachable.
        Neighbours are visited in name order so the result is deterministic.
        """
        if source == target:
            return (source,)
        queue: deque[tuple[str, ...]] = deque([(source,)])
        visited = {source}
        while queue:
            path = queue.popleft()
            for neighbour in sorted({edge.module for edge in self.imports_of(path[-1])}):
                if neighbour == target:
                    return (*path, neighbour)
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                queue.append((*path, neighbour))
        return ()


class _RuntimeImportCollector(ast.NodeVisitor):
    """Collect the import statements that run when a module is first imported."""

    def __init__(self):
        """Initialise with no collected nodes and zero deferred depth."""
        self.nodes: list[Union[ast.Import, ast.ImportFrom]] = []
        self._deferred_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Treat a sync function body as deferred."""
        self._visit_deferred(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Treat an async function body as deferred."""
        self._visit_deferred(node)

    def visit_Lambda(self, node: ast.Lambda):
        """Treat a lambda body as deferred."""
        self._visit_deferred(node)

    def visit_If(self, node: ast.If):
        """Defer an ``if TYPE_CHECKING:`` body while keeping its ``else`` runtime."""
        if not _is_type_checking_test(node.test):
            self.generic_visit(node)
            return
        self._deferred_depth += 1
        for statement in node.body:
            self.visit(statement)
        self._deferred_depth -= 1
        for statement in node.orelse:
            self.visit(statement)

    def visit_Import(self, node: ast.Import):
        """Record a runtime ``import x`` statement."""
        self._record(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Record a runtime ``from x import y`` statement."""
        self._record(node)

    def _visit_deferred(self, node: ast.AST):
        """Visit *node*'s children with any imports marked as deferred."""
        self._deferred_depth += 1
        self.generic_visit(node)
        self._deferred_depth -= 1

    def _record(self, node: Union[ast.Import, ast.ImportFrom]):
        """Keep *node* only when it executes at module import time."""
        if self._deferred_depth == 0:
            self.nodes.append(node)


def _is_type_checking_test(test: ast.expr) -> bool:
    """Return whether *test* is the conventional ``TYPE_CHECKING`` guard."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def module_location(filename: str) -> Optional[ModuleLocation]:
    """Return the import location of *filename*, or ``None`` when unresolvable.

    The import root is found by walking up from the file for as long as the
    containing directories are packages, mirroring how the module would be
    imported from that root. Files that do not exist on disk - a synthetic name
    handed to :func:`~flakeforge.check_source`, for instance - have no location.
    """
    path = Path(filename)
    if path.suffix != ".py" or not path.is_file():
        return None
    resolved = path.resolve()
    is_package = resolved.stem == "__init__"
    parts: list[str] = [] if is_package else [resolved.stem]
    directory = resolved.parent
    while (directory / "__init__.py").is_file():
        parts.append(directory.name)
        directory = directory.parent
    if not parts:
        return None
    return ModuleLocation(root=directory, name=".".join(reversed(parts)), is_package=is_package)


def _resolve_module_path(root: Path, module: str) -> Optional[Path]:
    """Return the file implementing *module* under *root*, or ``None``."""
    if not module:
        return None
    parts = module.split(".")
    candidates = (
        root.joinpath(*parts[:-1], f"{parts[-1]}.py"),
        root.joinpath(*parts, "__init__.py"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _loaded_package_prefixes(location: ModuleLocation) -> frozenset[str]:
    """Return the package prefixes already loaded while *location* executes."""
    package = location.name if location.is_package else location.package
    if not package:
        return frozenset()
    parts = package.split(".")
    return frozenset(".".join(parts[:index]) for index in range(1, len(parts) + 1))


def _runtime_import_targets(
    root: Path,
    module: str,
    *,
    loaded_packages: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Return the project modules Python executes while importing *module*."""
    if _resolve_module_path(root, module) is None:
        return ()
    targets: list[str] = []
    parts = module.split(".")
    for index in range(1, len(parts) + 1):
        candidate = ".".join(parts[:index])
        if candidate in loaded_packages and index != len(parts):
            continue
        if _resolve_module_path(root, candidate) is not None:
            targets.append(candidate)
    return tuple(targets)


def _absolute_import_base(node: ast.ImportFrom, location: ModuleLocation) -> Optional[str]:
    """Return the absolute dotted package an ``ImportFrom`` resolves against."""
    if node.level == 0:
        return node.module or None
    package_parts = [part for part in location.package.split(".") if part]
    ascent = node.level - 1
    if ascent > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - ascent]
    if node.module:
        base_parts = [*base_parts, *node.module.split(".")]
    return ".".join(base_parts) or None


def _import_targets(
    node: Union[ast.Import, ast.ImportFrom],
    location: ModuleLocation,
) -> tuple[str, ...]:
    """Return the project modules a single import statement pulls in.

    Targets that do not resolve to a file under the import root are dropped, so
    standard-library and third-party imports never enter the graph. For
    ``from X import Y`` the submodule ``X.Y`` wins when it exists on disk,
    because that is the module Python actually executes; otherwise ``Y`` is a
    plain name and the dependency is on ``X`` itself.
    """
    loaded_packages = _loaded_package_prefixes(location)
    if isinstance(node, ast.Import):
        targets: list[str] = []
        for alias in node.names:
            targets.extend(_runtime_import_targets(location.root, alias.name, loaded_packages=loaded_packages))
        return tuple(dict.fromkeys(targets))
    base = _absolute_import_base(node, location)
    if base is None:
        return ()
    targets: list[str] = []
    for alias in node.names:
        submodule = f"{base}.{alias.name}"
        if _resolve_module_path(location.root, submodule) is not None:
            targets.extend(_runtime_import_targets(location.root, submodule, loaded_packages=loaded_packages))
        elif _resolve_module_path(location.root, base) is not None:
            targets.extend(_runtime_import_targets(location.root, base, loaded_packages=loaded_packages))
    return tuple(dict.fromkeys(targets))


def extract_module_imports(tree: ast.AST, location: ModuleLocation) -> tuple[ModuleImport, ...]:
    """Return the runtime, module-level import edges of a located module.

    Imports Python does not execute while first loading the module - those
    nested in a function body or guarded by ``if TYPE_CHECKING:`` - are skipped,
    because deferring an import is the standard way to *break* an import cycle
    rather than a symptom of one.
    """
    collector = _RuntimeImportCollector()
    collector.visit(tree)
    imports: list[ModuleImport] = []
    for node in collector.nodes:
        for target in _import_targets(node, location):
            imports.append(ModuleImport(module=target, lineno=node.lineno, col_offset=node.col_offset))
    return tuple(imports)


def _cached_module_imports(path: Path, root: Path, module: str) -> tuple[ModuleImport, ...]:
    """Return the import edges of the module at *path*, reusing a parse cache.

    The cache is keyed by path and invalidated by modification time, so a long
    lived process (an editor running the Flake8 adapter, for example) still sees
    edits, while a single run parses each neighbouring module only once.
    """
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        # An unreadable neighbour simply contributes no edges to the graph.
        unreadable: tuple[ModuleImport, ...] = ()
        return unreadable
    cache_key = (path, root, module)
    cached = _MODULE_IMPORT_CACHE.get(cache_key)
    if cached is not None and cached[0] == modified_ns:
        return cached[1]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        # A neighbour that cannot be read or parsed is treated as a graph leaf.
        unparsable: tuple[ModuleImport, ...] = ()
        _MODULE_IMPORT_CACHE[cache_key] = (modified_ns, unparsable)
        return unparsable
    location = ModuleLocation(root=root, name=module, is_package=path.stem == "__init__")
    imports = extract_module_imports(tree, location)
    _MODULE_IMPORT_CACHE[cache_key] = (modified_ns, imports)
    return imports


def build_import_graph(tree: ast.AST, location: ModuleLocation) -> ModuleImportGraph:
    """Build the import graph reachable from the module described by *location*.

    The module under analysis contributes the edges found in *tree*, which may
    legitimately differ from what is on disk, and every project module it
    reaches is read from the import root. Traversal stops at modules that do not
    resolve to a file under that root, so the graph never spans outside the
    project.
    """
    graph = ModuleImportGraph()
    graph.add(location.name, extract_module_imports(tree, location))
    pending: deque[str] = deque(edge.module for edge in graph.imports_of(location.name))
    seen = {location.name, *pending}
    while pending:
        module = pending.popleft()
        path = _resolve_module_path(location.root, module)
        if path is None:
            continue
        imports = _cached_module_imports(path, location.root, module)
        graph.add(module, imports)
        for edge in imports:
            if edge.module not in seen:
                seen.add(edge.module)
                pending.append(edge.module)
    return graph


def _is_muting_stmt(stmt: ast.stmt) -> bool:
    """Return whether *stmt* silently mutes an exception handler."""
    if isinstance(stmt, (ast.Pass, ast.Continue, ast.Break, ast.Return)):
        return True
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis


def _has_proper_docstring(node: ast.AST, lines: Optional[Sequence[str]]) -> bool:
    """Return whether *node*'s first statement is a triple-quoted docstring.

    When *lines* is provided, the opening quote style is verified against the
    source so that implicitly-concatenated or non-triple-quoted strings fail.
    """
    body = getattr(node, "body", None)
    if not body:
        return False
    first_stmt = body[0]
    if not isinstance(first_stmt, ast.Expr):
        return False
    value = getattr(first_stmt, "value", None)
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return False
    if lines is None:
        return True
    doc_line = lines[first_stmt.lineno - 1].lstrip()
    return doc_line.startswith(('"""', "'''"))


def _docstring_has_structured_header(doc: str) -> bool:
    """Return whether *doc* opens with a ``[Unit|Integration|...]`` header."""
    for line in doc.splitlines():
        if line.strip():
            return bool(_TEST_DOCSTRING_HEADER_RE.match(line))
    return False


def _docstring_has_section(doc: str, section_name: str) -> bool:
    """Return whether any line in *doc* starts with *section_name*."""
    return any(line.strip().startswith(section_name) for line in doc.splitlines())


def _has_test_imports(tree: ast.AST) -> bool:
    """Return whether *tree* imports ``pytest`` or ``unittest``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in {"pytest", "unittest"}:
                    return True
        elif (
            isinstance(node, ast.ImportFrom) and node.module and node.module.split(".", 1)[0] in {"pytest", "unittest"}
        ):
            return True
    return False


def _is_test_module(context: RuleContext) -> bool:
    """Return whether *context* refers to a test module by name or imports."""
    path = Path(context.filename)
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if any(parent.name == "tests" for parent in path.parents):
        return True
    return _has_test_imports(context.tree)


def _iter_test_functions(tree: ast.AST) -> Iterable[ast.AST]:
    """Return the top-level ``test*`` functions inside ``Test*`` scopes."""

    class Visitor(ast.NodeVisitor):
        """Collect outermost test functions while tracking class/def nesting."""

        def __init__(self):
            """Initialise the empty class stack, depth counter, and results."""
            self.class_stack: list[ast.ClassDef] = []
            self.function_depth = 0
            self.results: list[ast.AST] = []

        def visit_ClassDef(self, node: ast.ClassDef):
            """Track the enclosing class while visiting its body."""
            self.class_stack.append(node)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef):
            """Dispatch a sync function definition to the shared handler."""
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
            """Dispatch an async function definition to the shared handler."""
            self._visit_function(node)

        def _visit_function(self, node: ast.AST):
            """Record *node* when it is an outermost ``test*`` function."""
            name = getattr(node, "name", "")
            outermost = self.function_depth == 0
            if (
                outermost
                and name.startswith("test")
                and (not self.class_stack or self.class_stack[-1].name.startswith("Test"))
            ):
                self.results.append(node)
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

    visitor = Visitor()
    visitor.visit(tree)
    return visitor.results


class _LocalImportVisitor(ast.NodeVisitor):
    """Track scope depth and record imports found below module scope."""

    def __init__(self, context: RuleContext):
        """Initialise the scope-depth counter and violation list for *context*."""
        self.context = context
        self.depth = 0
        self.violations: list[RuleViolation] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Descend into a sync function body, tracking nesting depth."""
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Descend into an async function body, tracking nesting depth."""
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef):
        """Descend into a class body, tracking nesting depth."""
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_Import(self, node: ast.Import):
        """Record ``import`` statements found below module scope."""
        if self.depth > 0:
            self.violations.append(
                _violation(
                    self.context,
                    node,
                    "X006",
                    "Do not use local imports inside function or class bodies; move imports to module scope.",
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Record ``from ... import`` statements found below module scope."""
        if self.depth > 0:
            self.violations.append(
                _violation(
                    self.context,
                    node,
                    "X006",
                    "Do not use local imports inside function or class bodies; move imports to module scope.",
                )
            )

    def visit_Call(self, node: ast.Call):
        """Record ``__import__()`` calls found below module scope."""
        if self.depth > 0 and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            self.violations.append(
                _violation(
                    self.context,
                    node,
                    "X006",
                    "Do not use local imports inside function or class "
                    "bodies; move imports to module scope (including "
                    "__import__() calls).",
                )
            )
        self.generic_visit(node)


def _function_returns_value(node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> bool:
    """Return whether *node* has a ``return`` with a value in its own body."""
    queue: list[ast.AST] = list(node.body)
    while queue:
        current = queue.pop()
        if isinstance(current, ast.Return) and current.value is not None:
            return True
        if isinstance(
            current,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        queue.extend(ast.iter_child_nodes(current))
    return False


def _expr_is_none(expr: Optional[ast.expr]) -> bool:
    """Return whether *expr* is the ``None`` literal."""
    if expr is None:
        return False
    if isinstance(expr, ast.Name) and expr.id == "None":
        return True
    return isinstance(expr, ast.Constant) and expr.value is None


def _has_percent_placeholders(value: str) -> bool:
    """Return whether *value* contains a printf-style placeholder."""
    return bool(_PERCENT_PLACEHOLDER_RE.search(value))


def _except_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """Return whether *handler* catches ``ImportError``/``ModuleNotFoundError``."""
    if handler.type is None:
        return False
    names = {"ImportError", "ModuleNotFoundError"}
    if isinstance(handler.type, ast.Name):
        return handler.type.id in names
    if isinstance(handler.type, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id in names for elt in handler.type.elts)
    return False


def _except_handler_has_raise(handler: ast.ExceptHandler) -> bool:
    """Return whether *handler*'s body contains a ``raise`` statement."""
    return any(isinstance(node, ast.Raise) for stmt in handler.body for node in ast.walk(stmt))


def _has_import_in_body(body: Sequence[ast.stmt]) -> bool:
    """Return whether *body* performs any import (statement or ``__import__``)."""
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
                return True
    return False


def _is_stub_body(node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> bool:
    """Return whether *node*'s body is a single ``...`` stub statement."""
    if len(node.body) != 1:
        return False
    stmt = node.body[0]
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis


def _is_union_with_none(annotation: Optional[ast.expr]) -> bool:
    """Return whether *annotation* is a top-level ``X | None`` union."""
    if not _is_top_level_union(annotation):
        return False
    members = list(_iter_union_members(annotation))
    return any(_expr_is_none(member) for member in members)


def _is_union_without_none(annotation: Optional[ast.expr]) -> bool:
    """Return whether *annotation* is a top-level union with no ``None`` member."""
    if not _is_top_level_union(annotation):
        return False
    members = list(_iter_union_members(annotation))
    return not any(_expr_is_none(member) for member in members)


def _iter_annotations(tree: ast.AST) -> Iterable[ast.AST]:
    """Yield every parameter, return, and variable annotation in *tree*."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
                if arg.annotation is not None:
                    yield arg.annotation
            if node.args.vararg and node.args.vararg.annotation is not None:
                yield node.args.vararg.annotation
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                yield node.args.kwarg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _is_top_level_union(annotation: Optional[ast.expr]) -> bool:
    """Return whether *annotation* is a ``|`` union at its top level."""
    return isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr)


def _iter_union_members(annotation: ast.expr) -> Iterable[ast.expr]:
    """Yield the flattened member expressions of a ``|`` union annotation."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        yield from _iter_union_members(annotation.left)
        yield from _iter_union_members(annotation.right)
        return
    yield annotation


def _is_named_test_module(context: RuleContext) -> bool:
    """Return whether *context* looks like a test module by filename."""
    path = Path(context.filename)
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _non_raii_resource_call_name(
    call: ast.Call,
    *,
    subprocess_aliases: set[str],
    socket_module_aliases: set[str],
    popen_aliases: set[str],
    socket_aliases: set[str],
) -> Optional[str]:
    """Return the matched non-RAII resource call name, or ``None`` when unrelated."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.attr == "Popen" and func.value.id in subprocess_aliases:
            return f"{func.value.id}.Popen"
        if func.attr == "socket" and func.value.id in socket_module_aliases:
            return f"{func.value.id}.socket"
    if isinstance(func, ast.Name):
        if func.id in popen_aliases:
            return func.id
        if func.id in socket_aliases:
            return func.id
    return None
