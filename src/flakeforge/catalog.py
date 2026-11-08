"""Registration catalogue wiring the built-in ``X###`` rules into the registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .api import RuleContext, RuleViolation
from .registry import RuleRegistration
from .rules import (
    check_bare_except,
    check_broad_exception,
    check_circular_imports,
    check_docstrings,
    check_import_error_suppression,
    check_local_imports,
    check_missing_return_annotation,
    check_muted_exception,
    check_non_raii_resources,
    check_none_return_annotation,
    check_percent_formatting,
    check_todo_annotations,
    check_union_none_annotations,
    check_union_type_annotations,
    check_unused_noqa,
)


@dataclass(frozen=True)
class CallbackRule:
    """Adapt a plain callback function into a :class:`~flakeforge.api.Rule`.

    :ivar code: The rule's unique code.
    :ivar description: Human-readable summary of the rule.
    :ivar callback: Function producing violations for a context.
    """

    code: str
    description: str
    callback: Callable[[RuleContext], Iterable[RuleViolation]]

    def check(self, context: RuleContext) -> Iterable[RuleViolation]:
        """Delegate to the wrapped callback for *context*."""
        return self.callback(context)


def builtin_registrations() -> tuple[RuleRegistration, ...]:
    """Return the registrations for every built-in ``X###`` rule."""
    return (
        RuleRegistration(
            code="X001",
            description="Do not use bare except.",
            rule=CallbackRule("X001", "Do not use bare except.", check_bare_except),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X002",
            description="Do not use except Exception.",
            rule=CallbackRule("X002", "Do not use except Exception.", check_broad_exception),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X003",
            description="Do not create circular imports.",
            rule=CallbackRule(
                "X003",
                "Do not create circular imports.",
                check_circular_imports,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X004",
            description="Do not silently swallow exceptions.",
            rule=CallbackRule(
                "X004",
                "Do not silently swallow exceptions.",
                check_muted_exception,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X005",
            description="Require compliant docstrings.",
            rule=CallbackRule("X005", "Require compliant docstrings.", check_docstrings),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X006",
            description="Do not use local imports.",
            rule=CallbackRule("X006", "Do not use local imports.", check_local_imports),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X007",
            description="Require return annotations for value-returning functions.",
            rule=CallbackRule(
                "X007",
                "Require return annotations for value-returning functions.",
                check_missing_return_annotation,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X008",
            description="Do not annotate returns with None.",
            rule=CallbackRule(
                "X008",
                "Do not annotate returns with None.",
                check_none_return_annotation,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X009",
            description="Do not use percent formatting.",
            rule=CallbackRule("X009", "Do not use percent formatting.", check_percent_formatting),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X010",
            description="Do not suppress ImportError.",
            rule=CallbackRule(
                "X010",
                "Do not suppress ImportError.",
                check_import_error_suppression,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X011",
            description="Do not use Type | None.",
            rule=CallbackRule("X011", "Do not use Type | None.", check_union_none_annotations),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X012",
            description="Do not use Type1 | Type2.",
            rule=CallbackRule("X012", "Do not use Type1 | Type2.", check_union_type_annotations),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X013",
            description="Require RAII context management for subprocess.Popen and socket.socket.",
            rule=CallbackRule(
                "X013",
                "Require RAII context management for subprocess.Popen and socket.socket.",
                check_non_raii_resources,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X014",
            description="Enforce tracked TODO/FIXME metadata in comments.",
            rule=CallbackRule(
                "X014",
                "Enforce tracked TODO/FIXME metadata in comments.",
                check_todo_annotations,
            ),
            provider="flakeforge.builtin",
        ),
        RuleRegistration(
            code="X015",
            description="Report unused `# noqa` directives.",
            rule=CallbackRule(
                "X015",
                "Report unused `# noqa` directives.",
                check_unused_noqa,
            ),
            provider="flakeforge.builtin",
        ),
    )
