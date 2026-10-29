# 01 - Conformance tripwire and test coverage

**Parent task:** [README.md](README.md)
**Status:** ✅ Complete

## Requirements

- **Conformance tripwire** (`tests/test_telemetry_conformance.py`): assert against
  **literal expected values that mirror the spec doc**, so local drift fails CI:
  - `FaultEvent` field names (Python attrs, including `fault_class`) and the
    `to_wire_dict` key set (including the renamed `"class"`).
  - the `outcome` vocabulary == `{"pass", "rejected", "suppressed", "advisory",
    "escalated"}`, with `escalated` documented as reserved/unused on FlakeForge.
  - constants `domain == "code"`, `tier == "deterministic"`.
  - `SCHEMA_VERSION == "1.0"`.
  - every registered built-in `X###` code resolves to a fault class other than
    `"unmapped"` (parity with `fault_classes.md`).
  The test's header comment names the spec doc path and states it is a tripwire, not
  cross-repo enforcement.
- **Unit** (`tests/test_telemetry.py`): event construction + `to_wire_dict`; each
  reference sink; the outcome mapping over clean/violation/baseline results; the
  `unmapped` fallback; the `details` no-source-text guard.
- **Custom sink** (`tests/test_custom_sinks.py`): a project-local sink loaded via a
  tmp module on `project_root`; a bad spec exits `2`; `--no-config` refuses it.
- **Mock** (`tests/mock/`): an emission error is swallowed and the exit code is
  unchanged.
- **Integration** (`tests/test_cli.py`/`test_api.py`): a `check` run with
  `--telemetry-sink file:<tmp>` + `--generated-by-model` writes well-formed JSONL
  (each line round-trips through `json.loads`, `model` is the flag value); a
  disabled run writes nothing and is byte-identical to pre-telemetry output; a
  `--write-baseline` run emits `advisory`.

## Files

- `tests/test_telemetry_conformance.py`, `tests/test_telemetry.py`,
  `tests/test_custom_sinks.py`, `tests/mock/...`, `tests/test_cli.py`,
  `tests/test_api.py`.
