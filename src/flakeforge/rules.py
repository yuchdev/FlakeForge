"""Built-in AST rules for flakeforge, ordered by code (X001-X015).

Maintainer / agent guidance:

- This module contains only the rule ``_check_*`` functions themselves. Keep them
  in ascending code order (X001, X002, ..., X015); a new built-in goes at its
  code's position, never appended out of order.
- Every helper function, helper class, and constant the rules use belongs in
  :mod:`flakeforge.rule_helpers` (``src/flakeforge/rule_helpers.py``), not here.
- Rule registration (``CallbackRule`` and ``builtin_registrations()``) lives in
  :mod:`flakeforge.catalog` (``src/flakeforge/catalog.py``).
"""

from __future__ import annotations

import ast
import tokenize
from collections.abc import Iterable
from io import StringIO

from .api import RuleContext, RuleViolation
from .rule_helpers import (
    _TEST_DOCSTRING_SECTIONS,
    _TODO_ANNOTATION_RE,
    ModuleImport,
    ModuleImportGraph,
    ModuleLocation,
    _contains_exception,
    _docstring_has_section,
    _docstring_has_structured_header,
    _except_catches_import_error,
    _except_handler_has_raise,
    _expr_is_none,
    _function_returns_value,
    _has_import_in_body,
    _has_percent_placeholders,
    _has_proper_docstring,
    _is_muting_stmt,
    _is_named_test_module,
    _is_stub_body,
    _is_test_module,
    _is_union_with_none,
    _is_union_without_none,
    _iter_annotations,
    _iter_test_functions,
    _LocalImportVisitor,
    _non_raii_resource_call_name,
    _violation,
    build_import_graph,
    extract_module_imports,
    module_location,
)

# Import-graph helpers re-exported so the public names documented in the
# changelog stay importable from ``flakeforge.rules``.
__all__ = [
    "ModuleImport",
    "ModuleImportGraph",
    "ModuleLocation",
    "build_import_graph",
    "extract_module_imports",
    "module_location",
]


def _check_bare_except(context: RuleContext) -> Iterable[RuleViolation]:
    """X001: flag bare ``except:`` handlers."""
    for node in ast.walk(context.tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            yield _violation(
                context,
                node,
                "X001",
                "Do not use bare `except:`; catch specific exceptions.",
            )


def _check_broad_exception(context: RuleContext) -> Iterable[RuleViolation]:
    """X002: flag ``except Exception:`` handlers."""
    for node in ast.walk(context.tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None and _contains_exception(node.type):
            yield _violation(
                context,
                node,
                "X002",
                "Do not use `except Exception:`; catch a more specific exception.",
            )


def _check_circular_imports(context: RuleContext) -> Iterable[RuleViolation]:
    """X003: flag module-level imports that take part in an import cycle."""
    location = module_location(context.filename)
    if location is None:
        yield from ()
        return
    graph = build_import_graph(context.tree, location)
    for edge in graph.imports_of(location.name):
        return_path = graph.find_path(edge.module, location.name)
        if not return_path:
            continue
        chain = " -> ".join((location.name, *return_path))
        yield RuleViolation(
            filename=context.filename,
            lineno=edge.lineno,
            col_offset=edge.col_offset,
            code="X003",
            message=(
                f"Circular import detected: {chain}; move the shared definitions into a "
                "separate module, or defer this import so it does not run at import time."
            ),
        )


def _check_muted_exception(context: RuleContext) -> Iterable[RuleViolation]:
    """X004: flag handlers whose body only mutes the caught exception."""
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not node.body or all(_is_muting_stmt(stmt) for stmt in node.body):
            yield _violation(
                context,
                node,
                "X004",
                "Do not silently swallow exceptions; handle them or re-raise.",
            )


def _check_docstrings(context: RuleContext) -> Iterable[RuleViolation]:
    """X005: require compliant docstrings on classes, functions, and methods.

    Test modules additionally require the structured header and Scenario /
    Boundaries / On failure sections on their test functions.
    """
    lines = context.source.splitlines() if context.source is not None else None
    generic_message = (
        "Missing or improperly formatted docstring: add a coherent docstring block as the first statement in the body."
    )

    if _is_test_module(context):
        test_nodes = set(_iter_test_functions(context.tree))
        for node in ast.walk(context.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node in test_nodes:
                if not _has_proper_docstring(node, lines):
                    yield _violation(
                        context,
                        node,
                        "X005",
                        "Missing or improperly formatted test docstring: add a "
                        "structured docstring as the first statement and include "
                        "Scenario, Boundaries, and On failure, first check "
                        "sections.",
                    )
                    continue
                raw_doc = ast.get_docstring(node, clean=False) or ""
                sections_ok = all(
                    _docstring_has_section(raw_doc, section)
                    for section in _TEST_DOCSTRING_SECTIONS
                )
                if not _docstring_has_structured_header(raw_doc) or not sections_ok:
                    yield _violation(
                        context,
                        node,
                        "X005",
                        "Missing or incomplete structured test docstring: include the required header and sections.",
                    )
            elif not _has_proper_docstring(node, lines):
                yield _violation(context, node, "X005", generic_message)
        return

    for node in ast.walk(context.tree):
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ) and not _has_proper_docstring(node, lines):
            yield _violation(context, node, "X005", generic_message)


def _check_local_imports(context: RuleContext) -> Iterable[RuleViolation]:
    """X006: flag imports nested inside function or class bodies."""

    visitor = _LocalImportVisitor(context)
    visitor.visit(context.tree)
    return tuple(visitor.violations)


def _check_missing_return_annotation(context: RuleContext) -> Iterable[RuleViolation]:
    """X007: flag value-returning functions that lack a return annotation."""
    for node in ast.walk(context.tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _function_returns_value(node)
            and node.returns is None
        ):
            yield _violation(
                context,
                node,
                "X007",
                "Function or method returns a value but has no return type "
                "annotation; add an explicit '-> return_type' annotation.",
            )


def _check_none_return_annotation(context: RuleContext) -> Iterable[RuleViolation]:
    """X008: flag explicit ``-> None`` return annotations on non-stub functions."""
    for node in ast.walk(context.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_stub_body(node):
            continue
        if _expr_is_none(node.returns):
            yield _violation(
                context,
                node,
                "X008",
                "Do not use an explicit '-> None' return annotation; omit the "
                "return type instead for functions that return nothing.",
            )


def _check_percent_formatting(context: RuleContext) -> Iterable[RuleViolation]:
    """X009: flag old-style ``%`` string formatting on string literals."""
    for node in ast.walk(context.tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and _has_percent_placeholders(node.left.value)
        ):
            yield _violation(
                context,
                node,
                "X009",
                "Do not use old-style '%' string formatting (e.g. '%s', '%d'); use f-strings instead.",
            )


def _check_import_error_suppression(context: RuleContext) -> Iterable[RuleViolation]:
    """X010: flag try/except blocks that swallow import failures."""
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if (
                _except_catches_import_error(handler)
                and not _except_handler_has_raise(handler)
                and _has_import_in_body(node.body)
            ):
                yield _violation(
                    context,
                    handler,
                    "X010",
                    "Do not suppress ImportError/ModuleNotFoundError in "
                    "try/except; let import failures propagate instead of "
                    "converting them into optional dependencies.",
                )


def _check_union_none_annotations(context: RuleContext) -> Iterable[RuleViolation]:
    """X011: flag ``Type | None`` annotations that should use ``Optional``."""
    for annotation in _iter_annotations(context.tree):
        if _is_union_with_none(annotation):
            yield _violation(
                context,
                annotation,
                "X011",
                "Do not use `Type | None` in type hints; use `Optional[Type]` from typing instead.",
            )


def _check_union_type_annotations(context: RuleContext) -> Iterable[RuleViolation]:
    """X012: flag ``Type1 | Type2`` annotations that should use ``Union``."""
    for annotation in _iter_annotations(context.tree):
        if _is_union_without_none(annotation):
            yield _violation(
                context,
                annotation,
                "X012",
                "Do not use `Type1 | Type2` in type hints; use `Union[Type1, Type2]` from typing instead.",
            )


def _check_non_raii_resources(context: RuleContext) -> Iterable[RuleViolation]:
    """X013: require ``subprocess.Popen`` and ``socket.socket`` to be context-managed."""
    if _is_named_test_module(context):
        return

    subprocess_aliases = {"subprocess"}
    socket_module_aliases = {"socket"}
    popen_aliases: set[str] = set()
    socket_aliases: set[str] = set()

    for node in ast.walk(context.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_aliases.add(alias.asname or alias.name)
                elif alias.name == "socket":
                    socket_module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name == "Popen":
                        popen_aliases.add(alias.asname or alias.name)
            elif node.module == "socket":
                for alias in node.names:
                    if alias.name == "socket":
                        socket_aliases.add(alias.asname or alias.name)

    managed_context_call_ids = {
        id(call)
        for node in ast.walk(context.tree)
        if isinstance(node, (ast.With, ast.AsyncWith))
        for item in node.items
        for call in ast.walk(item.context_expr)
        if isinstance(call, ast.Call)
    }

    for call in (node for node in ast.walk(context.tree) if isinstance(node, ast.Call)):
        if id(call) in managed_context_call_ids:
            continue

        call_name = _non_raii_resource_call_name(
            call,
            subprocess_aliases=subprocess_aliases,
            socket_module_aliases=socket_module_aliases,
            popen_aliases=popen_aliases,
            socket_aliases=socket_aliases,
        )
        if call_name is None:
            continue

        yield _violation(
            context,
            call,
            "X013",
            f"`{call_name}(...)` is not used as a context manager; wrap it in a `with` statement "
            "so the process or socket is always closed, even on exceptions.",
        )


def _check_todo_annotations(context: RuleContext) -> Iterable[RuleViolation]:
    """X014: enforce tracked TODO/FIXME metadata in source comments."""
    if context.source is None:
        yield from ()
        return

    for token in tokenize.generate_tokens(StringIO(context.source).readline):
        if token.type != tokenize.COMMENT:
            continue
        comment_upper = token.string.upper()
        if "TODO" not in comment_upper and "FIXME" not in comment_upper:
            continue
        if _TODO_ANNOTATION_RE.fullmatch(token.string.strip()):
            continue
        yield RuleViolation(
            filename=context.filename,
            lineno=token.start[0],
            col_offset=token.start[1],
            code="X014",
            message=(
                "Malformed TODO/FIXME annotation: use `# TODO: [YYYY-MM-DD][developer-name] debt-slug "
                "[issue: #123, https://example.invalid/issues/123] - short action/context`."
            ),
        )


def _check_unused_noqa(context: RuleContext) -> Iterable[RuleViolation]:
    """X015: stub check for the engine-emitted unused-``# noqa`` rule.

    Unlike every other built-in, X015 is *not* detected by walking the AST:
    whether a ``# noqa`` suppressed anything depends on the outcome of every
    other rule plus the engine-owned ``# noqa`` / ``per_file_ignores`` logic. The
    engine therefore emits X015 itself in :func:`flakeforge.api.check_tree`
    (repo convention: suppression and noqa handling are engine-owned, never
    rule-owned). This registration exists so X015 takes part in registry
    listing, ``select`` / ``ignore`` filtering, and duplicate-code detection like
    any other built-in; its ``check`` yields nothing.

    :param context: The module analysis context (unused).
    :returns: An always-empty iterable of violations.
    """
    del context
    return ()
