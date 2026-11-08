# 0002 - Unified Fault-Telemetry Event Emission

> **Status:** Proposed
>
> **Date:** 2026-10-26
>
> **Supersedes:** _(none)_
>
> **Superseded by:** _(none)_

## Context

FlakeForge (an AST lint engine, `dependencies = []`) and its sibling DocSentinel
(a documentation QA tool, a separate repository at `../DocSentinel`) are both
deterministic/heuristic quality gates over machine-generated content - FlakeForge
over code, DocSentinel over docs. Today each one answers a single pass/fail
question per run and discards everything it learned. Fault-mode rates a team
actually wants to track over time - fabricated-reference rate, cross-model
disagreement, human-in-the-loop (HITL) escalation rate, and for code the rate of
swallowed errors, broad excepts, resource-leak risks, and stale suppressions -
stay anecdotal because nothing durable is recorded when a rule evaluates
something. The demo framing is: "once these gates emit events, a model upgrade
becomes measurable."

The requirement is a single, structured telemetry event emitted whenever a rule
evaluates content, shaped so the two tools produce the **same** event vocabulary
and a downstream collector can aggregate a mixed stream from both. The event shape
is agreed:

```json
{
  "ts": "2026-08-30T00:00:00Z",
  "domain": "code",
  "class": "<fault class>",
  "tier": "deterministic",
  "rule": "X013",
  "outcome": "rejected",
  "model": "<generator-model-id or null>",
  "schema_version": "1.0",
  "details": {}
}
```

The canonical, cross-tool description of this contract - field names, types,
vocabularies and the `schema_version` policy - lives in DocSentinel's spec doc:
see [`../../../DocSentinel/docs/specs/fault-event-contract.md`](../../../DocSentinel/docs/specs/fault-event-contract.md)
(a document outside this repository; if you are reading this ADR standalone, that
relative path points from `FlakeForge/docs/adr/` to the DocSentinel checkout
beside it). DocSentinel's own decision is recorded in its
`docs/adr/0002-fault-telemetry-event-emission.md`.

Constraints fixed before this ADR (not re-litigated here):

- **No shared code, not even a path dependency.** The cross-tool contract is a
  **written specification**, not a package or a vendored module. FlakeForge
  implements its own `FaultEvent` and `FaultEventSink` independently and conforms
  to the spec by matching field names, types and vocabularies - nothing more. **No
  runtime import crosses the two repos**, in either direction. There is no shared
  package today and introducing one is explicitly out of scope.
- **`model` comes from a flag**, supplied by whatever CI step invokes
  `flakeforge check` (`--generated-by-model` / a `generated_by_model` config
  option). No frontmatter sniffing or content-based provenance detection.
- **stdlib-only.** FlakeForge has zero runtime dependencies and keeps them: the
  telemetry module and every reference sink use only `dataclasses`, `json`,
  `datetime`, `logging`, `sqlite3`, `pathlib`, `importlib`.
- **Opt-in.** Emission is a behaviour change, so it is gated behind an empty
  `flakeforge[telemetry]` extra and is off until a sink is configured - not
  because of install weight (there is none) but because emitting events at all is
  a new behaviour worth asking for explicitly.

FlakeForge's existing contracts shape where this lands. Every full-run consumer
funnels through `api.lint_paths()`, which assembles one `LintResult` and whose
`fingerprints` / `violations` / `registered_rules` already carry everything an
event needs; `config.py` validates every typed option through `_CONFIG_SCHEMA`
before building a frozen `LintConfig`; `format_json` already stamps an **integer**
`JSON_SCHEMA_VERSION` that versions the **report**, a different thing from a
telemetry event; and `registry.resolve_registry` already loads project-local code
by dotted module name with the config's `base_dir` on `sys.path` (contract C7).

**FlakeForge is the always-deterministic baseline.** Every built-in `X###` rule
is a pure AST/token check with no model call and no heuristic, so FlakeForge's
`tier` field is **always** `"deterministic"`. That is not a placeholder; it is a
designed, notable fact. In a mixed stream, FlakeForge events are the deterministic
control against which DocSentinel's mixed `fast`/`standard` (deterministic) and
`deep` (heuristic) tiers can be compared. A collector that sees `tier=heuristic`
from `domain=code` is looking at a bug or a future non-AST FlakeForge rule, and
should treat it as such.

## Decision

Introduce fault-telemetry in one new module, emitted from exactly one call site in
`api.lint_paths()`.

### 1. Independent, tool-agnostic module (`src/flakeforge/telemetry.py`)

A self-contained module holding FlakeForge's own `FaultEvent`, the
`FaultEventSink` Protocol, the reference sinks, the sink factory/loader, and the
`events_for_result` adapter. It imports only the stdlib plus FlakeForge's own
`LintResult`/`LintConfig`/registry types - and **nothing from DocSentinel**. It is
not a copy of DocSentinel's module and does not need to be; it only has to produce
the same wire shape.

- **`FaultEvent` - a frozen dataclass** (`@dataclass(frozen=True)`), not a
  `TypedDict`. Justification: the whole codebase models contracts as frozen
  dataclasses (`RuleViolation`, `LintResult`, `RuleContext`, `LintConfig`,
  `RuleRegistration`); a frozen dataclass gives a construction-time contract and
  immutability a `TypedDict` cannot, and it serialises cleanly to the wire dict.
  Proposed shape (described, not implemented; house style is `Optional[T]`, never
  `T | None`, per the project's own X011):

  ```python
  @dataclass(frozen=True)
  class FaultEvent:
      ts: str                          # RFC-3339 UTC, 'Z' suffix (datetime.now(timezone.utc))
      domain: str                      # always "code" for FlakeForge
      fault_class: str                 # serialised under the wire key "class"
      tier: str                        # always "deterministic" for FlakeForge
      rule: str                        # "X013"
      outcome: str                     # see vocabulary below
      model: Optional[str]             # generator model id, or None
      schema_version: str = "1.0"
      details: Mapping[str, Union[str, int, float, bool, None]] = field(default_factory=dict)
  ```

  The Python attribute is `fault_class` because `class` is a keyword; a
  module-level `to_wire_dict(event) -> dict` helper renames it to `"class"` on the
  way out so the wire schema matches the agreed shape exactly. `details` is a
  shallow, JSON-primitive mapping only - the open escape hatch for rule-specific
  fields, bounded to primitives so no sink is ever handed an unserialisable or
  secret-bearing object graph.

- **`schema_version`** is a **string** on the event (`"1.0"`), deliberately
  independent of `api.JSON_SCHEMA_VERSION` (the integer that versions the report).
  Its purpose is to version the **cross-tool event contract** shared with
  DocSentinel so a collector reading a mixed stream can route and migrate by
  version. Additive changes (new `details` keys, new `outcome` values) bump the
  minor; renaming or removing a top-level field bumps the major and requires a
  coordinated change in both repos and the spec doc.

- **Sink abstraction - a `Protocol` (PEP 544 structural typing, no inheritance):**

  ```python
  class FaultEventSink(Protocol):
      def emit(self, event: FaultEvent) -> None: ...
      def close(self) -> None: ...
  ```

  A sink satisfies the Protocol structurally - a user class need not import or
  subclass anything. `close()` is part of the contract so file/db sinks can flush
  and release handles; `NullSink`/`LoggingSink` implement it as a no-op.

### 2. Reference sinks (all stdlib-only)

Four reference implementations ship in `telemetry.py`:

- **`NullSink`** - the default no-op; `emit` and `close` do nothing. Returned when
  telemetry is unconfigured, so the rest of the engine never special-cases a
  disabled run.

- **`LoggingSink(logger=..., level=logging.INFO)`** - routes each event through the
  stdlib `logging` module at a configurable level. Honours the project's "no
  `print` for diagnostics; use a real logger" rule and lets a CI step reuse its
  existing logging config/handlers.

  ```python
  class LoggingSink:
      def __init__(self, logger: Optional[logging.Logger] = None, level: int = logging.INFO) -> None:
          self._logger = logger or logging.getLogger("flakeforge.telemetry")
          self._level = level

      def emit(self, event: FaultEvent) -> None:
          self._logger.log(self._level, "%s", json.dumps(to_wire_dict(event)))

      def close(self) -> None:
          pass
  ```

- **`JsonlSink(stream, *, owns_stream=False)`** - writes `to_wire_dict(event)` as
  one compact JSON line per `emit`, flushing per line; `close` closes the stream
  only when it owns it (a file it opened, never `sys.stdout`). This is the
  default-when-enabled sink: `"stdout"` writes to `sys.stdout`, `"file:<path>"`
  opens `<path>` in append mode.

  ```python
  class JsonlSink:
      def __init__(self, stream: TextIO, *, owns_stream: bool = False) -> None:
          self._stream = stream
          self._owns_stream = owns_stream

      def emit(self, event: FaultEvent) -> None:
          self._stream.write(json.dumps(to_wire_dict(event), separators=(",", ":")) + "\n")
          self._stream.flush()

      def close(self) -> None:
          if self._owns_stream:
              self._stream.close()
  ```

- **`SqliteSink(path)`** - the concrete example of a database sink, using stdlib
  `sqlite3`. It creates a single `fault_events` table on first use and inserts one
  row per event (top-level fields as columns, `details` as a JSON text column).
  **Postgres and other databases are "bring your own sink":** a `psycopg`/SQLAlchemy
  sink is a user class satisfying the same `FaultEventSink` Protocol, loaded via
  the custom-sink loader below. FlakeForge adds **no** database-client dependency;
  `sqlite3` is the stdlib proof that the Protocol is enough.

  ```python
  class SqliteSink:
      def __init__(self, path: Union[str, Path]) -> None:
          self._conn = sqlite3.connect(str(path))
          self._conn.execute(
              "CREATE TABLE IF NOT EXISTS fault_events ("
              "ts TEXT, domain TEXT, class TEXT, tier TEXT, rule TEXT, "
              "outcome TEXT, model TEXT, schema_version TEXT, details TEXT)"
          )

      def emit(self, event: FaultEvent) -> None:
          row = to_wire_dict(event)
          self._conn.execute(
              "INSERT INTO fault_events VALUES (:ts,:domain,:class,:tier,:rule,"
              ":outcome,:model,:schema_version,:details)",
              {**row, "details": json.dumps(row["details"])},
          )
          self._conn.commit()

      def close(self) -> None:
          self._conn.close()
  ```

- **Write your own sink** - any object with a conforming `emit`/`close` is a sink.
  No base class, no registration decorator:

  ```python
  # my_project/telemetry_sinks.py
  class CountingSink:
      """Minimal custom sink: tally outcomes in memory, satisfy the Protocol."""

      def __init__(self) -> None:
          self.counts: dict[str, int] = {}

      def emit(self, event) -> None:
          self.counts[event.outcome] = self.counts.get(event.outcome, 0) + 1

      def close(self) -> None:
          pass

  def build_sink():            # factory the loader calls with no args
      return CountingSink()
  ```

### 3. Sink selection and the custom-sink loader

One factory, `build_sink(spec, *, project_root) -> FaultEventSink`, maps the
`telemetry_sink` string to a sink:

| `telemetry_sink` value | Sink |
|------------------------|------|
| `""` (default) | `NullSink` - telemetry off |
| `"stdout"` | `JsonlSink` over `sys.stdout` |
| `"file:<path>"` | `JsonlSink` over `<path>` opened append (owns the stream) |
| `"sqlite:<path>"` | `SqliteSink(<path>)` |
| `"logging"` | `LoggingSink` at the default level |
| any other string | treated as a **dotted import target** and loaded like a `rule_module` |

For the last row, the loader **reuses the existing `rule_modules` extension-loading
mechanism** rather than inventing a new one. The same trust boundary (contract C7)
and the same `_project_root_on_syspath(project_root)` window that
`registry.resolve_registry` opens for rule modules apply: the target repo's
`base_dir` is placed first on `sys.path` only while the sink module imports, and
only when a config file vouched for that directory (`LintConfig.rule_module_root`;
`--no-config` loads no project-local sink). The spec is a dotted path to either a
zero-arg `build_sink()` factory **or** a zero-arg-constructable class exposing
`emit`/`close` - the same "import a dotted name, call a well-known attribute"
shape as `_load_register_function`. Loading a custom sink therefore **executes
code the target repository controls**, exactly like `rule_modules`, and is
documented as such.

A sink that fails to load is a configuration error (exit `2`, contract C1) raised
before any file is checked - distinct from a sink that fails *during* emission,
which is swallowed (see below).

### 4. The adapter - `events_for_result`

```python
def events_for_result(
    result: LintResult,
    registry: RuleRegistry,
    config: LintConfig,
    *,
    model: Optional[str],
) -> Iterator[FaultEvent]: ...
```

It reads the **already-assembled** `LintResult` - it does not re-run rules and does
not duplicate violation iteration. Mapping rules:

- `domain` is the constant `"code"`.
- `tier` is the constant `"deterministic"` (see Context).
- `fault_class` from the rule-code -> fault-class table in
  [`../../src/flakeforge/fault_classes.md`](../../src/flakeforge/fault_classes.md).
  A rule with no table entry maps to the fallback class `"unmapped"`.
- `rule` is the `X###` code.
- `model` is the resolved `generated_by_model` value, or `None` when empty
  (serialised as JSON `null`).
- `outcome` per the table below.

#### Outcome vocabulary (shared with DocSentinel)

| `outcome` | FlakeForge meaning |
|-----------|--------------------|
| `pass` | a rule ran and produced no surviving violation - one `pass` event per evaluated, enabled rule that was clean |
| `rejected` | a violation survived to the reported set (the run exits `1`) - one per reported `RuleViolation` |
| `suppressed` | a violation was computed but dropped by the baseline filter (`fingerprints` minus the reported set) - one per baseline-suppressed finding |
| `advisory` | a violation was recorded but not enforced: emitted for every captured violation during a `--write-baseline` run (which always exits `0`), where findings are accepted, not rejected |
| `escalated` | **reserved and unused on the FlakeForge side.** FlakeForge has no HITL/escalation flow; the value exists only so the vocabulary is shared and DocSentinel/collectors can use it. Do not invent a FlakeForge escalation path to populate it |

`# noqa` and `per_file_ignores` suppression is **not** surfaced as `suppressed` in
v1, because that information is computed inside `check_tree` and is not carried on
`LintResult`. Mapping those to `suppressed` is a documented follow-up (it needs
`check_tree` to expose its per-line "covered" set); until then only baseline drops
count as `suppressed`, which is the accurate, non-fabricated subset.

### 5. Emission call site - one place in `api.lint_paths()`

Immediately **after** the `LintResult` is assembled and **before** it is returned,
`lint_paths` builds a sink from `config.telemetry_sink` and feeds it
`events_for_result(...)`. One call site, reusing the finished
`violations`/`fingerprints`/`registered_rules`. Emission never changes the return
value, never changes the exit code, and **never raises into the caller**: a sink
error during emission is caught and swallowed (telemetry is observational, not a
gate), and the whole block is skipped when `telemetry_sink` is empty. The Flake8
adapter (`plugin.py`) and the pytest helper (`testing.py`) bypass `lint_paths` and
are **out of scope for v1** - they emit nothing, documented explicitly so their
silence is not mistaken for a clean telemetry stream.

### 6. Config and CLI surface

Two new typed `LintConfig` options, added to `_CONFIG_SCHEMA` and validated exactly
like the existing keys (contract C5), with matching `init`-template entries so the
schema/template lockstep test stays green:

| Option | Coercer | Default | Notes |
|--------|---------|---------|-------|
| `telemetry_sink` | `_as_str` | `""` | the sink spec from the table in section 3. **Not** path-constrained like `include`/`baseline`: a `file:`/`sqlite:` path is an **output artifact** a CI step owns, not an input path resolved against `base_dir`, so it is **not** run through `_as_config_path` and absolute paths are allowed |
| `generated_by_model` | `_as_str` | `""` | free string; `""` serialises to `model: null` |

Two new flags on the `check` subparser only (not the shared parent parser, since
`rules`/`config show` do not emit events), overlaid through the existing
`LintConfig.merge(...)` path and recorded in `_cli_override_keys`:

- `--telemetry-sink SPEC` -> `telemetry_sink`
- `--generated-by-model ID` -> `generated_by_model`

### 7. Optional extra

Add `telemetry = []` to `[project.optional-dependencies]` - an empty behaviour-gate
alias (the module has no third-party deps). Installing `flakeforge[telemetry]`
signals intent; nothing emits until `telemetry_sink` is set. `dependencies` stays
`[]` (contract C8). `flakeforge config show` already lists every schema key, so
`telemetry_sink`/`generated_by_model` and their origins surface there for free.

## Alternatives Considered

| Alternative | Pros | Cons | Reason rejected |
|-------------|------|------|-----------------|
| A shared/vendored `faultevents` package imported by both repos (DocSentinel's internal seam) | One definition, zero drift by construction | Creates a runtime coupling and a publish/version burden across two repos; violates the agreed "no shared code, not even a path dependency" | Rejected - the cross-tool contract is a **written spec**; each tool implements independently |
| `TypedDict` instead of a frozen dataclass | Zero construction cost; already a dict for JSON | No immutability, no construction-time contract, diverges from every other model in the codebase | Rejected - frozen dataclass matches house style |
| Emit from inside each rule's `check()` | Rule has the richest local context for `details` | Duplicates the violation iteration the engine already does, forces all current and future `X###` rules to know about telemetry, re-couples the module into every rule | Rejected - violates the single-call-site constraint |
| A bespoke sink loader separate from `rule_modules` | Could tailor the sink-loading UX | A second code-loading path with its own `sys.path`/trust semantics to audit and keep in sync with C7 | Rejected - reuse `rule_modules`' loader and its audited trust boundary |
| A separate `telemetry_enabled` bool plus a path (DocSentinel's shape) | Explicit on/off independent of the path | Two options to keep consistent; FlakeForge's single `telemetry_sink` spec already encodes "off" as `""` | Rejected - one `telemetry_sink` string is the smaller surface for FlakeForge |
| Independent `FaultEvent` + `FaultEventSink` in `telemetry.py`, one `lint_paths` call site, `rule_modules`-style sink loader (chosen) | No cross-repo import; stdlib-only; one call site; pluggable sinks; reuses an audited loader | A hand-maintained mapping table and a conformance test are the only guards against drift from DocSentinel | **Accepted** |

## Consequences

### Positive

- Code fault-mode rates become measurable over time from a uniform JSON Lines (or
  SQLite, or custom) stream that shares DocSentinel's vocabulary; the mapping table
  turns raw `X###` violations into named fault classes a collector can aggregate
  across both tools.
- FlakeForge stays zero-runtime-dependency: the module and all four reference
  sinks are stdlib-only, and the opt-in extra adds no packages.
- One call site in `lint_paths` means every full-run consumer emits identically,
  with no second iteration of violations and no per-rule changes.
- The `FaultEventSink` Protocol makes a future HTTP/OTLP or Postgres sink a new
  user class, not a change to callers; "bring your own sink" needs no FlakeForge
  release.
- Default-off and swallow-on-error: enabling telemetry cannot change a run's
  verdict, exit code, or `LintResult`.
- FlakeForge is a clean `tier="deterministic"` baseline in a mixed stream.

### Negative

- **A new cross-component contract** now binds FlakeForge and DocSentinel through a
  written spec with no compiler to enforce it. Changing the event shape means a
  coordinated `schema_version` bump in both repos plus any collector. **Affected
  components:** `src/flakeforge/telemetry.py` (event + sinks + adapter),
  `api.lint_paths()` (call site), `config.py`/`cli.py` (surface),
  `src/flakeforge/fault_classes.md` (mapping), and the DocSentinel mirror. **Migration
  path:** this ADR and DocSentinel's ADR 0002 are the anchor; the conformance test
  (section below) is the tripwire; a major bump re-runs that coordination.
- The fault-class mapping is hand-maintained. A new `X###` rule that ships without a
  table entry falls back to `"unmapped"` and must be flagged in review, or its
  events mis-aggregate silently. The conformance test asserts every registered
  built-in code has a mapping entry.
- `details` is a deliberate open escape hatch. To honour the no-secrets/no-PII rule
  it is **bounded to JSON primitives and must never carry source text, file
  contents, or a matched snippet** - only the rule code, counts, and the
  already-`base_dir`-relative display paths that the normal report would print
  anyway (`RuleViolation.message` is already static and names no user source; keep
  `details` to the same standard). The adapter is responsible for this; it is a
  review checklist item.
- No real cross-repo enforcement exists: the conformance test guards *FlakeForge's*
  own literals against the spec, not DocSentinel's. Genuine lockstep is a human
  process until (if ever) a shared package is extracted.

## Validation / Rollout

- Rollout is behind `telemetry_sink` (default `""`), so landing the code is inert
  until a CI step opts in - zero behaviour change for existing users and existing
  tests.
- **Drift control (tripwire):** a conformance test in the FlakeForge suite asserts
  FlakeForge's own `FaultEvent` field names, the `outcome` vocabulary
  (`pass`/`rejected`/`suppressed`/`advisory`/`escalated`), the constant
  `domain="code"`/`tier="deterministic"`, and `schema_version="1.0"` against
  **literal expected values that mirror the spec doc**. It is a tripwire against
  silent local drift, not real cross-repo enforcement (the spec lives in another
  repo); when it fails, a human reconciles FlakeForge, DocSentinel, and the spec.
- Acceptance: with `telemetry_sink` set, a `check` run writes one event per reported
  violation (`rejected`), one per baseline-suppressed finding (`suppressed`), and one
  `pass` per evaluated clean rule; each JSONL line round-trips through `json.loads`;
  `class`/`tier`/`outcome` match the table; `model` is the flag value or `null`; a
  `--write-baseline` run emits `advisory`; a disabled run writes nothing; a sink
  error during emission does not change the exit code; a bad `telemetry_sink` spec
  exits `2` before any file is checked.
- Implementation: `python-expert` (the module, the sinks, the loader, the adapter,
  the `lint_paths` wiring, and the config/CLI surface - see
  [Milestone 0002](../roadmap/0002-fault-telemetry/plan.md)). Tests:
  `testing-expert` (unit: event construction + `to_wire_dict` key rename, each sink,
  the outcome mapping, the conformance tripwire; mock: an emission error swallowed;
  integration: a `check` run with `--telemetry-sink` + `--generated-by-model`, and
  the custom-sink loader through a project-local module).
- The DocSentinel mirror ADR adopts the identical vocabulary; only its adapter
  (`domain="docs"`, `DS###` mapping, a real `heuristic` tier for the deep profile)
  differs.

## Links

- **Roadmap milestone:** [docs/roadmap/0002-fault-telemetry/plan.md](../roadmap/0002-fault-telemetry/plan.md)
- **Fault-class mapping:** [src/flakeforge/fault_classes.md](../../src/flakeforge/fault_classes.md)
- **Cross-tool contract (canonical, other repo):** [`../../../DocSentinel/docs/specs/fault-event-contract.md`](../../../DocSentinel/docs/specs/fault-event-contract.md)
- **Supporting specs:** [docs/architecture.md](../architecture.md), [docs/custom-rules.md](../custom-rules.md)
