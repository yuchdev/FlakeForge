"""Baseline fingerprinting, loading, and writing for incremental adoption.

A *baseline* records the fingerprints of violations a project chooses to accept
today so that ``flakeforge`` can suppress them on later runs and fail only on
*new* violations (plan contract C1). This module is deliberately self-contained
and uses the standard library only (plan contract C8): it never imports the
engine, so the api layer orchestrates it without an import cycle.

**Fingerprint (ratified decision D2).** Each violation's fingerprint is::

    sha256(code + US + relative_path + US + normalized_line_text)

where ``US`` is the ASCII unit separator ``\\x1f`` -- an explicit field
separator so that, e.g., code ``X1`` + path ``a`` cannot collide with code ``X``
+ path ``1a``. The three fields are:

* ``code`` -- the rule code (e.g. ``X001``), verbatim.
* ``relative_path`` -- the violation's display filename: POSIX, relative to the
  config ``base_dir`` (plan contract C6), exactly as rendered in output.
* ``normalized_line_text`` -- the source line the violation points at, split on
  universal newlines - so no ``\\r`` or ``\\n`` terminator is ever included - and
  with surrounding ASCII whitespace stripped (:func:`str.strip`, removing
  leading/trailing spaces *and* tabs). Interior text -- including interior tabs
  -- is preserved verbatim. Stripping the ends means re-indenting a line does
  not resurface a baselined violation, while editing the line's content does.

Because a fingerprint hashes the line *text* rather than a line *number*, it
survives inserting or deleting unrelated lines above the violation. Identical
lines (same code, path and text) collide by design, so each fingerprint also
carries an **occurrence index**: the 0-based position of the violation among all
violations sharing that fingerprint, assigned in the deterministic C9 order
(filename, line, column, code, message). Removing one of several identical lines
therefore leaves the remaining occurrences matched and reports the removed one as
fixed.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Union

BASELINE_VERSION = 1
"""Schema version stamped into every baseline file; a mismatch is a load error."""

_FIELD_SEPARATOR = "\x1f"
"""ASCII unit separator placed between fingerprint fields so they cannot merge."""

_FINGERPRINT_LENGTH = 64
"""Number of hex characters in a SHA-256 digest, validated on load."""

_HEX_DIGITS = frozenset("0123456789abcdef")
"""Characters allowed in a stored fingerprint (lower-case hex, as ``hexdigest`` emits)."""

MAX_BASELINE_BYTES = 64 * 1024 * 1024
"""Largest baseline file ``load_baseline`` will read, guarding against runaway input."""


class BaselineError(ValueError):
    """Raised when a baseline file is missing, unreadable, or malformed.

    Subclasses :class:`ValueError` so the CLI's existing handler maps it to exit
    code ``2`` (plan contract C1) with a ``flakeforge: ...`` message.
    """


@dataclass(frozen=True, order=True)
class BaselineEntry:
    """One baselined violation identity.

    :ivar fingerprint: The 64-character SHA-256 hex digest (decision D2).
    :ivar occurrence: 0-based index among violations sharing this fingerprint,
        assigned in C9 order.
    """

    fingerprint: str
    occurrence: int


def normalize_line(line: str) -> str:
    """Return *line* normalized for fingerprinting.

    Surrounding ASCII whitespace (spaces and tabs) is stripped; interior text is
    left verbatim. Callers pass a single physical line already split on universal
    newlines, so no line terminator is present to normalize.

    :param line: A single source line.
    :returns: The normalized line text.
    """
    return line.strip()


def compute_fingerprint(code: str, relative_path: str, line_text: str) -> str:
    """Return the SHA-256 fingerprint for one violation (decision D2).

    :param code: The rule code.
    :param relative_path: The base-relative POSIX display filename (C6).
    :param line_text: The raw source line the violation points at; normalized
        here via :func:`normalize_line`.
    :returns: The 64-character hex digest.
    """
    payload = _FIELD_SEPARATOR.join((code, relative_path, normalize_line(line_text)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_entries(fingerprints: Sequence[str]) -> list[BaselineEntry]:
    """Pair each fingerprint with its occurrence index, preserving input order.

    *fingerprints* must already be in the deterministic C9 order (the caller
    sorts the violations first), so the Nth repeat of a fingerprint reliably maps
    to occurrence ``N``.

    :param fingerprints: Fingerprints in C9 order.
    :returns: :class:`BaselineEntry` values aligned one-to-one with *fingerprints*.
    """
    seen: dict[str, int] = {}
    entries: list[BaselineEntry] = []
    for fingerprint in fingerprints:
        occurrence = seen.get(fingerprint, 0)
        seen[fingerprint] = occurrence + 1
        entries.append(BaselineEntry(fingerprint=fingerprint, occurrence=occurrence))
    return entries


def dumps_baseline(entries: Iterable[BaselineEntry]) -> str:
    """Render *entries* as the deterministic, byte-stable baseline document.

    The same set of entries always produces byte-identical output: entries are
    de-duplicated and sorted, object keys are sorted, and the document ends with a
    single trailing newline. Built with :mod:`json` only (plan contract C8).

    :param entries: The entries to serialize.
    :returns: The JSON text, terminated by a newline.
    """
    ordered = sorted(set(entries))
    document = {
        "version": BASELINE_VERSION,
        "entries": [{"fingerprint": entry.fingerprint, "occurrence": entry.occurrence} for entry in ordered],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write_baseline(path: Union[str, Path], entries: Iterable[BaselineEntry]) -> Path:
    """Write *entries* to *path* as a byte-stable baseline file.

    :param path: Destination file path.
    :param entries: The entries to persist.
    :returns: The path written to.
    """
    destination = Path(path)
    destination.write_bytes(dumps_baseline(entries).encode("utf-8"))
    return destination


def load_baseline(path: Union[str, Path]) -> frozenset[BaselineEntry]:
    """Load and validate a baseline file into a set of entries.

    :param path: The baseline file to read.
    :returns: The frozenset of :class:`BaselineEntry` recorded in the file.
    :raises BaselineError: If the file is missing, is not valid JSON, carries an
        unexpected ``version``, or does not match the expected shape. The CLI maps
        this to exit code ``2`` (plan contract C1).
    """
    baseline_path = Path(path)
    try:
        info = baseline_path.stat()
    except OSError as exc:
        raise BaselineError(f"cannot read baseline file: {baseline_path}") from exc
    # Refuse FIFOs, devices and directories (a read could block forever) and
    # oversized files before reading anything.
    if not stat.S_ISREG(info.st_mode):
        raise BaselineError(f"baseline path is not a regular file: {baseline_path}")
    if info.st_size > MAX_BASELINE_BYTES:
        raise BaselineError(f"baseline file exceeds {MAX_BASELINE_BYTES} bytes: {baseline_path}")
    try:
        raw = baseline_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BaselineError(f"cannot read baseline file: {baseline_path}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"baseline file is not valid JSON: {baseline_path}") from exc
    return _entries_from_document(document, baseline_path)


def _entries_from_document(document: object, path: Path) -> frozenset[BaselineEntry]:
    """Validate a parsed baseline *document* and return its entries."""
    if not isinstance(document, dict):
        raise BaselineError(f"baseline file must be a JSON object: {path}")
    if document.get("version") != BASELINE_VERSION:
        raise BaselineError(f"unsupported baseline version {document.get('version')!r}; expected {BASELINE_VERSION}")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise BaselineError(f"baseline 'entries' must be a list: {path}")
    entries: set[BaselineEntry] = set()
    for item in raw_entries:
        entries.add(_entry_from_item(item, path))
    return frozenset(entries)


def _entry_from_item(item: object, path: Path) -> BaselineEntry:
    """Validate a single baseline entry mapping and return it."""
    if not isinstance(item, dict):
        raise BaselineError(f"each baseline entry must be an object: {path}")
    fingerprint = item.get("fingerprint")
    occurrence = item.get("occurrence")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != _FINGERPRINT_LENGTH
        or not _HEX_DIGITS.issuperset(fingerprint)
    ):
        raise BaselineError(f"baseline entry has an invalid fingerprint: {path}")

    # ``bool`` is an ``int`` subclass; reject it so a JSON ``true`` is not
    # silently treated as occurrence ``1``.
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0:
        raise BaselineError(f"baseline entry has an invalid occurrence: {path}")
    return BaselineEntry(fingerprint=fingerprint, occurrence=occurrence)
