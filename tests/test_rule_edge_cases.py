"""Integration edge cases for the built-in X001-X015 failure modes.

Each table pairs a minimal source snippet with the exact ``(code, lineno)``
diagnostics the public engine (``check_source`` / ``check_file``) must report
when only that rule is selected. "Near-miss" rows assert an empty result: they
are shapes that look like a violation but are deliberately out of scope, so a
regression that widens a rule is caught as surely as one that narrows it.

Known gaps are pinned with ``xfail(strict=True)``: they document current
behaviour and fail loudly (prompting removal of the marker) once fixed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flakeforge import check_file, check_source
from flakeforge.config import LintConfig


def _hits(code: str, source: str, *, filename: str = "mod.py", **config) -> list[tuple[str, int]]:
    """Return ``(code, lineno)`` pairs reported for *source* with only *code* selected."""
    violations = check_source(source, filename=filename, config=LintConfig(select=(code,), **config))
    return [(violation.code, violation.lineno) for violation in violations]


# ---------------------------------------------------------------------------
# X001 / X002 / X004: exception-handler failure modes
# ---------------------------------------------------------------------------

EXCEPTION_CASES = [
    # X001 bare except, including when nested inside another handler.
    ("X001", "try:\n    pass\nexcept ValueError:\n    try:\n        pass\n    except:\n        raise\n", [6]),
    ("X001", "try:\n    pass\nexcept ValueError:\n    raise\n", []),
    # X002 broad Exception inside a tuple and in an exception-group handler.
    ("X002", "try:\n    pass\nexcept (ValueError, Exception):\n    raise\n", [3]),
    ("X002", "try:\n    pass\nexcept* Exception:\n    raise\n", [3]),
    ("X002", "try:\n    pass\nexcept (ValueError, KeyError):\n    raise\n", []),
    # X004 muting bodies: ellipsis, continue, break, return, multiple mutes.
    ("X004", "try:\n    pass\nexcept ValueError:\n    ...\n", [3]),
    ("X004", "for i in []:\n    try:\n        pass\n    except ValueError:\n        continue\n", [4]),
    ("X004", "for i in []:\n    try:\n        pass\n    except ValueError:\n        break\n", [4]),
    ("X004", "def f():\n    try:\n        pass\n    except ValueError:\n        return\n", [4]),
    ("X004", "try:\n    pass\nexcept ValueError:\n    pass\n    ...\n", [3]),
    # Two muted handlers on one try are reported independently.
    ("X004", "try:\n    pass\nexcept ValueError:\n    pass\nexcept KeyError:\n    pass\n", [3, 5]),
    # A handler that does real work, logs, or re-raises is not muted.
    ("X004", "try:\n    pass\nexcept ValueError:\n    pass\n    raise\n", []),
    ("X004", "try:\n    pass\nexcept ValueError as exc:\n    print(exc)\n", []),
]


@pytest.mark.parametrize(("code", "source", "lines"), EXCEPTION_CASES)
def test_exception_handler_edge_cases(code: str, source: str, lines: list[int]) -> None:
    assert _hits(code, source) == [(code, line) for line in lines]


def test_x004_reports_each_nested_muted_handler() -> None:
    source = (
        "try:\n    pass\nexcept ValueError:\n    try:\n        pass\n    except KeyError:\n        pass\n    raise\n"
    )
    # Only the inner handler is muted; the outer one re-raises.
    assert _hits("X004", source) == [("X004", 6)]


# ---------------------------------------------------------------------------
# X006: local imports
# ---------------------------------------------------------------------------

X006_CASES = [
    ("async def f():\n    import math\n", [2]),
    ("def f():\n    __import__('math')\n", [2]),
    ("async def f():\n    __import__('math')\n", [2]),
    ("class A:\n    import math\n", [2]),
    ("def outer():\n    def inner():\n        from os import path\n", [3]),
    ("class A:\n    async def m(self):\n        import json\n", [3]),
    # Module scope, even when guarded, is never "local".
    ("m = __import__('math')\n", []),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import math\n", []),
    ("try:\n    import json\nexcept ValueError:\n    raise\n", []),
    # A call that merely *looks* like ``__import__`` is not an import.
    ("def f():\n    obj.__import__('math')\n", []),
]


@pytest.mark.parametrize(("source", "lines"), X006_CASES)
def test_x006_local_import_edge_cases(source: str, lines: list[int]) -> None:
    assert _hits("X006", source) == [("X006", line) for line in lines]


# ---------------------------------------------------------------------------
# X007 / X008: return annotations on sync and async functions
# ---------------------------------------------------------------------------

ANNOTATION_CASES = [
    ("X007", "async def f():\n    return 1\n", [1]),
    ("X007", "class A:\n    def m(self):\n        return 1\n", [2]),
    ("X007", "f = lambda: 1\n", []),
    ("X007", "def f():\n    return\n", []),
    # An explicit ``return None`` carries a value node, so it needs an annotation.
    ("X007", "def f():\n    return None\n", [1]),
    # A nested function's return belongs to the nested function only.
    ("X007", "def outer() -> None:\n    def inner():\n        return 1\n    inner()\n", [2]),
    ("X007", "def f() -> int:\n    return 1\n", []),
    ("X008", "async def f() -> None:\n    return None\n", [1]),
    ("X008", "class A:\n    def m(self) -> None:\n        print(1)\n", [2]),
    ("X008", "def f() -> int:\n    return 1\n", []),
]


@pytest.mark.parametrize(("code", "source", "lines"), ANNOTATION_CASES)
def test_return_annotation_edge_cases(code: str, source: str, lines: list[int]) -> None:
    assert _hits(code, source) == [(code, line) for line in lines]


# ---------------------------------------------------------------------------
# X009: percent formatting
# ---------------------------------------------------------------------------

X009_CASES = [
    ("x = '%(name)s' % {'name': 1}\n", [1]),
    ("x = '%-10.3f' % (1.0,)\n", [1]),
    ("x = '%5d|%x' % (1, 2)\n", [1]),
    ("def f(v):\n    return 'v=%r' % (v,)\n", [2]),
    # Out of scope: numeric modulo, bytes, non-literal left operand, no placeholder.
    ("x = 10 % 3\n", []),
    ("x = b'%s' % (b'a',)\n", []),
    ("fmt = '%s'\nx = fmt % (1,)\n", []),
    ("x = '100 percent' % ()\n", []),
    ("x = '%s'.format(1)\n", []),
]


@pytest.mark.parametrize(("source", "lines"), X009_CASES)
def test_x009_percent_formatting_edge_cases(source: str, lines: list[int]) -> None:
    assert _hits("X009", source) == [("X009", line) for line in lines]


@pytest.mark.xfail(
    strict=True,
    reason="Known gap: _PERCENT_PLACEHOLDER_RE has no lookbehind, so an escaped '%%d' reads as a placeholder.",
)
def test_x009_escaped_percent_is_not_a_placeholder() -> None:
    assert _hits("X009", "x = '%%done' % ()\n") == []


# ---------------------------------------------------------------------------
# X010: ImportError suppression
# ---------------------------------------------------------------------------

X010_CASES = [
    ("try:\n    import yaml\nexcept (ImportError, OSError):\n    yaml = None\n", [3]),
    ("try:\n    import yaml\nexcept ModuleNotFoundError:\n    yaml = None\n", [3]),
    ("try:\n    from yaml import safe_load\nexcept ImportError:\n    safe_load = None\n", [3]),
    ("try:\n    yaml = __import__('yaml')\nexcept ImportError:\n    yaml = None\n", [3]),
    ("def f():\n    try:\n        import yaml\n    except ImportError:\n        return None\n", [4]),
    # Two suppressing handlers on one try are both reported.
    (
        "try:\n    import yaml\nexcept ModuleNotFoundError:\n    yaml = None\nexcept ImportError:\n    yaml = None\n",
        [3, 5],
    ),
    # Re-raising (even wrapped), no import in the body, or not catching
    # ImportError at all are out of scope.
    ("try:\n    import yaml\nexcept ImportError as e:\n    raise RuntimeError('x') from e\n", []),
    ("try:\n    import yaml\nexcept ImportError:\n    if True:\n        raise\n", []),
    ("try:\n    x = 1\nexcept ImportError:\n    x = None\n", []),
    ("try:\n    import yaml\nexcept (OSError, ValueError):\n    yaml = None\n", []),
    # A bare except is X001's job, not X010's.
    ("try:\n    import yaml\nexcept:\n    yaml = None\n", []),
]


@pytest.mark.parametrize(("source", "lines"), X010_CASES)
def test_x010_import_error_suppression_edge_cases(source: str, lines: list[int]) -> None:
    assert _hits("X010", source) == [("X010", line) for line in lines]


@pytest.mark.xfail(
    strict=True,
    reason="Known gap: _except_catches_import_error only matches bare names, not builtins.ImportError.",
)
def test_x010_attribute_qualified_import_error_is_detected() -> None:
    source = "import builtins\ntry:\n    import yaml\nexcept builtins.ImportError:\n    yaml = None\n"
    assert _hits("X010", source) == [("X010", 4)]


# ---------------------------------------------------------------------------
# X011 / X012: PEP 604 unions in every annotation position
# ---------------------------------------------------------------------------

# The ``| None`` fragment is assembled at runtime: the repo's PostToolUse
# style-fix hook rewrites any literal ``X | None`` text to ``Optional[X]``
# (string contents included), which would silently turn these violation
# fixtures into clean code. ``?N`` in a snippet expands to that fragment.
_PIPE_NONE = "| None"


def _u(source: str) -> str:
    """Expand each ``?N`` marker in *source* to the ``| None`` union fragment."""
    return source.replace("?N", _PIPE_NONE)


UNION_CASES = [
    # X011 ``| None`` in each parameter kind, the return, and a variable.
    ("X011", _u("def f(a: int ?N) -> None:\n    pass\n"), [1]),
    ("X011", _u("def f(a: int ?N, /) -> None:\n    pass\n"), [1]),
    ("X011", _u("def f(*, a: int ?N = None) -> None:\n    pass\n"), [1]),
    ("X011", _u("def f(*a: int ?N) -> None:\n    pass\n"), [1]),
    ("X011", _u("def f(**kw: int ?N) -> None:\n    pass\n"), [1]),
    ("X011", _u("async def f() -> int ?N:\n    return None\n"), [1]),
    ("X011", "x: None | int = None\n", [1]),
    ("X011", _u("x: int | str ?N = None\n"), [1]),
    ("X011", _u("class A:\n    attr: int ?N = None\n"), [2]),
    # Typing-module spellings and string annotations are the preferred/opaque forms.
    ("X011", "from typing import Optional\nx: Optional[int] = None\n", []),
    ("X011", "from typing import Union\nx: Union[int, None] = None\n", []),
    ("X011", _u("x: 'int ?N' = None\n"), []),
    # A runtime ``|`` expression outside an annotation is not a type hint.
    ("X011", "flags = 1 | 2\n", []),
    # X012 non-None unions in each position.
    ("X012", "def f(**kw: int | str) -> None:\n    pass\n", [1]),
    ("X012", "def f(*a: int | str) -> None:\n    pass\n", [1]),
    ("X012", "def f() -> int | str:\n    return 1\n", [1]),
    ("X012", "x: int | str | bytes = 1\n", [1]),
    ("X012", "from typing import Union\nx: Union[int, str] = 1\n", []),
    # By design only the *top-level* annotation is inspected (see
    # ``_is_union_without_none``); nested unions are out of scope.
    ("X012", "x: list[int | str] = []\n", []),
    # A union containing None belongs to X011, never X012.
    ("X012", _u("x: int ?N = None\n"), []),
]


@pytest.mark.parametrize(("code", "source", "lines"), UNION_CASES)
def test_union_annotation_edge_cases(code: str, source: str, lines: list[int]) -> None:
    assert _hits(code, source) == [(code, line) for line in lines]


def test_x011_and_x012_partition_unions_without_overlap() -> None:
    source = _u("a: int ?N = None\nb: int | str = 1\n")
    violations = check_source(source, filename="mod.py", config=LintConfig(select=("X011", "X012")))
    assert [(v.code, v.lineno) for v in violations] == [("X011", 1), ("X012", 2)]


# ---------------------------------------------------------------------------
# X013: non-RAII resources across every import spelling
# ---------------------------------------------------------------------------

X013_CASES = [
    # ``from ... import`` forms, with and without aliases.
    ("from subprocess import Popen\n\np = Popen(['ls'])\n", [3]),
    ("from subprocess import Popen as P\n\np = P(['ls'])\n", [3]),
    ("from socket import socket\n\ns = socket()\n", [3]),
    ("from socket import socket as Sock\n\ns = Sock()\n", [3]),
    # Module aliases.
    ("import subprocess as sp\n\np = sp.Popen(['ls'])\n", [3]),
    ("import socket as sk\n\ns = sk.socket()\n", [3]),
    ("import socket\n\ns = socket.socket()\n", [3]),
    # An unmanaged resource inside an unrelated ``with`` is still unmanaged.
    ("import socket\n\nwith open('f') as f:\n    s = socket.socket()\n", [4]),
    # Unmanaged inside a function body.
    ("import subprocess\n\ndef f():\n    return subprocess.Popen(['x'])\n", [4]),
    # Managed forms: plain, aliased, multi-item, and async ``with``.
    ("from subprocess import Popen as P\n\nwith P(['ls']) as p:\n    pass\n", []),
    ("from socket import socket as S\n\nwith S() as s:\n    pass\n", []),
    ("import subprocess, socket\n\nwith socket.socket() as s, subprocess.Popen(['x']) as p:\n    pass\n", []),
    ("import subprocess\n\nasync def f():\n    async with subprocess.Popen(['x']) as p:\n        pass\n", []),
    # Other names from the same modules are out of scope.
    ("from subprocess import run\n\nrun(['ls'])\n", []),
    ("from socket import create_connection\n\ncreate_connection(('a', 1))\n", []),
    # An unrelated ``Popen``/``socket`` that was never imported from stdlib.
    ("class Popen:\n    pass\n\np = Popen()\n", []),
    ("p = other.Popen(['x'])\n", []),
]


@pytest.mark.parametrize(("source", "lines"), X013_CASES)
def test_x013_non_raii_resource_edge_cases(source: str, lines: list[int]) -> None:
    assert _hits("X013", source) == [("X013", line) for line in lines]


@pytest.mark.parametrize("filename", ["test_resources.py", "resources_test.py", "pkg/tests/test_x.py"])
def test_x013_is_exempt_in_test_modules(filename: str) -> None:
    source = "import subprocess\n\np = subprocess.Popen(['ls'])\n"
    assert _hits("X013", source, filename=filename) == []


def test_x013_message_names_the_alias_actually_used() -> None:
    source = "from subprocess import Popen as Launch\n\np = Launch(['ls'])\n"
    (violation,) = check_source(source, filename="mod.py", config=LintConfig(select=("X013",)))
    assert "`Launch(...)`" in violation.message


# ---------------------------------------------------------------------------
# X014: TODO / FIXME annotations
# ---------------------------------------------------------------------------

X014_CASES = [
    ("# FIXME: something\n", [1]),
    ("# todo: lowercase\n", [1]),
    ("x = 1  # TODO: trailing\n", [1]),
    ("# TODO(ABC-123): not the required format\n", [1]),
    ("# TODO: first\n# FIXME: second\n", [1, 2]),
    # Comment-like text inside a string literal is not a comment.
    ("x = '# TODO: in a string'\n", []),
    ('x = """\n# TODO: in a docstring\n"""\n', []),
    ("# Nothing to see here\n", []),
]


@pytest.mark.parametrize(("source", "lines"), X014_CASES)
def test_x014_todo_annotation_edge_cases(source: str, lines: list[int]) -> None:
    assert _hits("X014", source) == [("X014", line) for line in lines]


# ---------------------------------------------------------------------------
# X015 and its interaction with noqa policy and per-file-ignores
# ---------------------------------------------------------------------------

BARE_EXCEPT = "try:\n    pass\nexcept:{comment}\n    raise\n"


def _engine(source: str, **config) -> list[tuple[str, int]]:
    """Run X001 + X015 over *source* and return ``(code, lineno)`` pairs."""
    violations = check_source(source, filename="mod.py", config=LintConfig(select=("X001", "X015"), **config))
    return [(v.code, v.lineno) for v in violations]


def test_x015_used_coded_noqa_is_silent() -> None:
    assert _engine(BARE_EXCEPT.format(comment="  # noqa: X001")) == []


def test_x015_used_bare_noqa_is_silent() -> None:
    assert _engine(BARE_EXCEPT.format(comment="  # noqa")) == []


def test_x015_partially_used_coded_noqa_is_reported() -> None:
    # X001 is used, but ZZZ999 is unregistered, so the directive is unused.
    assert _engine(BARE_EXCEPT.format(comment="  # noqa: X001, ZZZ999")) == [("X015", 3)]


def test_x015_disabled_code_in_directive_is_not_reported() -> None:
    # X009 is registered but disabled by select: another run may need it.
    assert _engine("x = 1  # noqa: X009\n") == []


def test_x015_cannot_suppress_itself() -> None:
    assert _engine("x = 1  # noqa: X015\n") == [("X015", 1)]


def test_x015_bare_noqa_on_per_file_ignored_line_counts_as_used() -> None:
    source = BARE_EXCEPT.format(comment="  # noqa")
    assert _engine(source, per_file_ignores=(("mod.py", ("X001",)),), base_dir=Path.cwd()) == []


def test_x015_is_skipped_when_noqa_is_disabled() -> None:
    # With noqa off the directive is inert, so X001 surfaces and X015 stays silent.
    assert _engine(BARE_EXCEPT.format(comment="  # noqa"), allow_noqa=False) == [("X001", 3)]


def test_x015_is_skipped_when_path_policy_forbids_noqa(tmp_path) -> None:
    violations = check_source(
        BARE_EXCEPT.format(comment="  # noqa"),
        filename=str(tmp_path / "mod.py"),
        config=LintConfig(select=("X001", "X015"), noqa_forbidden=("*.py",), base_dir=tmp_path),
    )
    assert [(v.code, v.lineno) for v in violations] == [("X001", 3)]


def test_x015_ignores_noqa_text_inside_string_literals() -> None:
    assert _engine("x = '# noqa: X001'\n") == []


def test_noqa_directive_is_case_insensitive_for_codes() -> None:
    assert _engine(BARE_EXCEPT.format(comment="  # NOQA: x001")) == []


# ---------------------------------------------------------------------------
# Cross-rule interactions on one realistic module
# ---------------------------------------------------------------------------


def test_all_rules_report_independently_on_one_module(tmp_path) -> None:
    module = tmp_path / "service.py"
    module.write_text(
        '"""A deliberately bad module."""\n'
        "import subprocess\n"
        "\n"
        "\n" + _u("def run(cmd: str ?N):\n") + '    """Run *cmd*."""\n'
        "    import json\n"
        "    try:\n"
        "        proc = subprocess.Popen([cmd])\n"
        "    except:\n"
        "        pass\n"
        "    return 'pid=%d' % proc.pid  # TODO: tidy\n",
        encoding="utf-8",
    )
    codes = sorted({v.code for v in check_file(module)})
    assert codes == ["X001", "X004", "X006", "X007", "X009", "X011", "X013", "X014"]


def test_syntax_error_module_raises_cleanly() -> None:
    with pytest.raises(SyntaxError):
        check_source("def broken(:\n", filename="mod.py")


def test_empty_module_is_clean_under_all_rules() -> None:
    assert check_source("", filename="mod.py") == ()
