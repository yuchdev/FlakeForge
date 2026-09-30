"""Rule registry and provider loading."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module, metadata
from pathlib import Path
from typing import Any, Optional, Protocol

ENTRY_POINT_GROUP = "flakeforge.rules"
RegisterRulesCallable = Callable[["RuleRegistry"], None]
RULE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]*\d{3}$")


class RuleLike(Protocol):
    """Structural type for a rule usable by the registry.

    :ivar code: The rule's unique code (e.g. ``"X001"``).
    :ivar description: Human-readable summary of what the rule enforces.
    """

    code: str
    description: str

    def check(self, context: Any):
        """Yield the rule's violations for the analysis *context*."""
        ...


#: Origin tag for built-in rules and any registration made outside a provider
#: loading phase (e.g. a hand-built :class:`RuleRegistry`). See
#: :attr:`RuleRegistration.origin`.
BUILTIN_ORIGIN = "builtin"


@dataclass(frozen=True)
class RuleRegistration:
    """A rule bound to its owning provider and enablement metadata.

    :ivar code: The rule's unique code.
    :ivar description: Human-readable rule summary.
    :ivar rule: The callable rule implementation.
    :ivar provider: Identifier of the provider that registered the rule.
    :ivar enabled: Whether the rule runs by default.
    :ivar reserved: Whether the code is reserved and not user-selectable.
    :ivar origin: Where the rule entered the resolved registry, as a stable
        taxonomy independent of the provider-chosen ``provider`` label:
        ``"builtin"``, ``"rule_module:<name>"`` for a project-local module, or
        ``"entry_point:<dist>"`` for an installed provider. Stamped by
        :func:`resolve_registry` per loading phase; a directly-built registry
        leaves it at :data:`BUILTIN_ORIGIN`. Trailing field with a default, so
        existing callers constructing a registration positionally are unaffected.
    """

    code: str
    description: str
    rule: RuleLike
    provider: str
    enabled: bool = True
    reserved: bool = False
    origin: str = BUILTIN_ORIGIN


class DuplicateRuleCodeError(ValueError):
    """Raised when multiple rules claim the same rule code."""


class InvalidRuleCodeError(ValueError):
    """Raised when a rule code does not follow the public naming contract."""


class RuleProviderLoadError(RuntimeError):
    """Raised when a rule provider cannot be imported or registered."""


class RuleRegistry:
    """In-memory catalogue mapping rule codes to their registrations."""

    def __init__(self):
        """Initialise an empty registry."""
        self._registrations: dict[str, RuleRegistration] = {}
        self._current_origin: str = BUILTIN_ORIGIN

    @contextmanager
    def registering_origin(self, origin: str) -> Iterator[None]:
        """Stamp every rule registered inside this block with *origin*.

        :func:`resolve_registry` opens one scope per provider loading phase so a
        provider's own ``register_rules`` -- which only passes ``provider`` --
        still yields a truthful :attr:`RuleRegistration.origin` without any
        provider-side change. Scopes restore the previous origin on exit, so the
        default (:data:`BUILTIN_ORIGIN`) applies to any registration made outside
        a scope.

        :param origin: The origin tag to apply while the block is active.
        :returns: A context manager yielding ``None``.
        """
        previous = self._current_origin
        self._current_origin = origin
        try:
            yield
        finally:
            self._current_origin = previous

    def register(
        self,
        rule: RuleLike,
        *,
        provider: str,
        enabled: bool = True,
        reserved: bool = False,
    ):
        """Register *rule* under its code.

        :param rule: The rule to register.
        :param provider: Identifier of the registering provider.
        :param enabled: Whether the rule runs by default.
        :param reserved: Whether the code is reserved and not user-selectable.
        :raises InvalidRuleCodeError: If the rule code is malformed.
        :raises DuplicateRuleCodeError: If the code is already registered.
        """
        code = str(rule.code)
        validate_rule_code(code)
        if code in self._registrations:
            existing = self._registrations[code]
            raise DuplicateRuleCodeError(f"Duplicate rule code {code}: {existing.provider} and {provider}")
        self._registrations[code] = RuleRegistration(
            code=code,
            description=rule.description,
            rule=rule,
            provider=provider,
            enabled=enabled,
            reserved=reserved,
            origin=self._current_origin,
        )

    def get(self, code: str) -> RuleRegistration:
        """Return the registration for *code* (case-insensitive)."""
        return self._registrations[code.upper()]

    def all(self) -> tuple[RuleRegistration, ...]:
        """Return every registration ordered by rule code."""
        return tuple(self._registrations[code] for code in sorted(self._registrations))

    def enabled_rules(self) -> tuple[RuleRegistration, ...]:
        """Return the registrations whose rules are enabled."""
        return tuple(registration for registration in self.all() if registration.enabled)

    def known_codes(self) -> tuple[str, ...]:
        """Return the codes of all registered rules, ordered."""
        return tuple(registration.code for registration in self.all())


def resolve_registry(
    *,
    rule_modules: Iterable[str] = (),
    include_entry_points: bool = True,
    project_root: Optional[Path] = None,
) -> RuleRegistry:
    """Build a registry from built-in rules plus any configured providers.

    Project-local ``rule_modules`` are imported with *project_root* placed first
    on ``sys.path``, but only for the duration of that import (plan contract C7).
    This lets a standalone run load a target repository's rule modules without
    the repository being installed. It also means running the linter against an
    untrusted checkout that lists ``rule_modules`` in its config **executes that
    repository's code**; callers that must not trust the target pass
    ``project_root=None`` (the CLI's ``--no-config`` mode does exactly this).

    :param rule_modules: Importable modules exposing ``register_rules``.
    :param include_entry_points: Whether to load installed entry-point providers.
    :param project_root: The resolved config ``base_dir`` to prepend to
        ``sys.path`` while project-local modules load; ``None`` (or an empty
        *rule_modules*) inserts nothing and imports from the existing path only.
        This must be the resolved config directory, never a raw path argument.
    :returns: A populated :class:`RuleRegistry`.
    :raises RuleProviderLoadError: If a provider fails to import or register.
    :raises DuplicateRuleCodeError: If two providers claim the same code.
    :raises InvalidRuleCodeError: If a provider registers a malformed code.
    """
    # Imported lazily to avoid an import cycle: ``catalog`` imports from both
    # ``rules`` (which imports ``api``) and this module at import time, so
    # hoisting this to module scope raises ImportError on a partially-initialised
    # module. Importing the linter's own internals here, before the ``sys.path``
    # window opens below, keeps a target module named like ``flakeforge`` from
    # shadowing them.
    from .catalog import builtin_registrations  # noqa: X006

    registry = RuleRegistry()
    for registration in builtin_registrations():
        registry.register(
            registration.rule,
            provider=registration.provider,
            enabled=registration.enabled,
            reserved=registration.reserved,
        )

    module_names = tuple(rule_modules)
    resolved_root = project_root.resolve() if project_root is not None else None
    # Insert nothing when there is no resolved base_dir or nothing to import.
    load_root = resolved_root if module_names else None
    with _project_root_on_syspath(load_root):
        for module_name in module_names:
            try:
                with registry.registering_origin(f"rule_module:{module_name}"):
                    _load_register_function(module_name)(registry)
            except (DuplicateRuleCodeError, InvalidRuleCodeError):
                raise
            except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
                raise RuleProviderLoadError(_rule_module_load_message(module_name, resolved_root, exc)) from exc

    if include_entry_points:
        for entry_point in _iter_entry_points():
            provider_label = _describe_entry_point(entry_point)
            try:
                register_rules = entry_point.load()
                with registry.registering_origin(f"entry_point:{_entry_point_dist(entry_point)}"):
                    register_rules(registry)
            except (DuplicateRuleCodeError, InvalidRuleCodeError):
                raise
            except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
                raise RuleProviderLoadError(f"Failed to load installed rule provider {provider_label}: {exc}") from exc

    return registry


@contextmanager
def _project_root_on_syspath(project_root: Optional[Path]) -> Iterator[None]:
    """Prepend *project_root* to ``sys.path`` for the duration of the block.

    The entry is inserted at index ``0`` so project-local modules resolve ahead
    of installed packages, then removed in ``finally`` by rebinding the same
    list object's contents (``sys.path[:] = saved``) so any C-level or third
    party reference to the original ``sys.path`` object stays valid. Restoration
    runs even when the wrapped import raises. When *project_root* is ``None``
    the context is a no-op.

    .. warning::

       This mutates process-global ``sys.path`` and is **not thread-safe**;
       concurrent registry builds must not share an interpreter thread. Provider
       loading that fans out is expected to use process isolation (a future
       ``--jobs`` ``ProcessPoolExecutor``), never a thread pool. The restore does
       not unwind ``sys.modules``: a target module imported here stays cached,
       so a repository shipping a top-level module named like a stdlib module or
       like ``flakeforge`` can still shadow a name that has not yet been imported
       elsewhere in the process.

    :param project_root: An absolute, resolved directory, or ``None`` for a
        no-op context.
    :returns: A context manager yielding ``None``.
    """
    if project_root is None:
        yield
        return
    saved = sys.path[:]
    sys.path.insert(0, str(project_root))
    try:
        yield
    finally:
        sys.path[:] = saved


def _rule_module_load_message(module_name: str, base_dir: Optional[Path], exc: Exception) -> str:
    """Return the :class:`RuleProviderLoadError` message for a failed import.

    Names the module and the ``base_dir`` it was searched from without echoing
    the target module's source text.

    :param module_name: The rule module that failed to load.
    :param base_dir: The resolved directory prepended to ``sys.path``, or
        ``None`` when the module was resolved from the existing path only.
    :param exc: The originating exception.
    :returns: A single-line diagnostic message.
    """
    location = f"base_dir {base_dir}" if base_dir is not None else "the existing sys.path"
    return f"Failed to load rule module {module_name} from {location}: {exc}"


def _load_register_function(module_name: str) -> RegisterRulesCallable:
    """Import *module_name* and return its ``register_rules`` callable.

    :raises AttributeError: If the module lacks a callable ``register_rules``.
    """
    module = import_module(module_name)
    register_rules = getattr(module, "register_rules", None)
    if register_rules is None or not callable(register_rules):
        raise AttributeError(f"{module_name} does not define callable register_rules(registry)")
    return register_rules


def _iter_entry_points() -> tuple[metadata.EntryPoint, ...]:
    """Return this project's rule entry points sorted deterministically."""
    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        selected = entry_points.select(group=ENTRY_POINT_GROUP)
    else:
        selected = entry_points.get(ENTRY_POINT_GROUP, [])
    return tuple(sorted(selected, key=lambda entry_point: (entry_point.name, entry_point.value)))


def validate_rule_code(code: str) -> str:
    """Return *code* unchanged if it matches the rule-code contract.

    :raises InvalidRuleCodeError: If *code* is not an uppercase alphanumeric
        prefix followed by three digits.
    """
    if not RULE_CODE_RE.fullmatch(code):
        raise InvalidRuleCodeError(
            f"Invalid rule code {code!r}: expected an uppercase alphanumeric prefix followed by three digits"
        )
    return code


def _describe_entry_point(entry_point) -> str:
    """Return a human-readable label for *entry_point*, including its dist."""
    dist_name = getattr(getattr(entry_point, "dist", None), "name", None)
    if dist_name:
        return f"{entry_point.name} ({dist_name}: {entry_point.value})"
    return f"{entry_point.name} ({entry_point.value})"


def _entry_point_dist(entry_point) -> str:
    """Return the distribution name owning *entry_point* for origin reporting.

    The installing distribution's name is the natural identity for an installed
    provider. It is unavailable on older ``importlib.metadata`` back-ends and on
    synthetic entry points, so the entry-point name is the fallback -- always a
    non-empty, stable string -- keeping the ``entry_point:<dist>`` origin tag
    populated either way.

    :param entry_point: The entry point being loaded.
    :returns: The distribution name, or the entry-point name as a fallback.
    """
    dist_name = getattr(getattr(entry_point, "dist", None), "name", None)
    return dist_name or entry_point.name
