"""JSON Schema generation for flakeforge configuration."""

from __future__ import annotations

import json
import re
from typing import Any

from .registry import RuleRegistry


def _case_insensitive_literal(value: str) -> str:
    """Return a regex literal matching *value* without regard to letter case."""
    return "".join(
        f"[{char.lower()}{char.upper()}]" if char.isalpha() else re.escape(char)
        for char in value
    )


def generate_schema(registry: RuleRegistry) -> str:
    """Return a JSON Schema for flakeforge configuration as a string.

    The schema includes descriptions for every rule currently in *registry*,
    providing IDE hovers for rule IDs in ``select``, ``ignore`` and
    ``per_file_ignores``.
    """
    registrations = registry.all()
    rule_definitions = [
        {
            "anyOf": [
                {"const": reg.code},
                {"pattern": "^" + _case_insensitive_literal(reg.code) + "$"},
            ],
            "markdownDescription": reg.description,
        }
        for reg in registrations
    ]
    prefixes = {
        _case_insensitive_literal(reg.code[:length])
        for reg in registrations
        for length in range(1, len(reg.code) + 1)
    }
    prefix_pattern = "^(?:" + "|".join(sorted(prefixes)) + ")$"

    rule_list = {
        "type": "array",
        "items": {
            "type": "string",
            "anyOf": [
                {"oneOf": rule_definitions},
                {"pattern": prefix_pattern},
            ],
        },
    }

    config_properties: dict[str, Any] = {
        "include": {
            "type": "array",
            "description": "Glob patterns limiting which files are linted; empty means every file.",
            "items": {"type": "string"},
        },
        "exclude": {
            "type": "array",
            "description": "Glob patterns excluding files from linting.",
            "items": {"type": "string"},
        },
        "select": {
            "description": "Rule-code prefixes to enable; empty means every registered rule.",
            **rule_list,
        },
        "ignore": {
            "description": "Rule-code prefixes to disable.",
            **rule_list,
        },
        "allow_noqa": {
            "type": "boolean",
            "description": "Honour # noqa suppression comments.",
            "default": True,
        },
        "noqa_allowed": {
            "type": "array",
            "description": "Path patterns where # noqa is permitted (file-only).",
            "items": {"type": "string"},
        },
        "noqa_forbidden": {
            "type": "array",
            "description": "Path patterns where # noqa is rejected (file-only).",
            "items": {"type": "string"},
        },
        "per_file_ignores": {
            "type": "object",
            "description": "Per-file rule suppressions: a glob mapped to rule-code prefixes to skip.",
            "additionalProperties": rule_list,
        },
        "rule_modules": {
            "type": "array",
            "description": "Importable modules contributing extra rules.",
            "items": {"type": "string"},
        },
        "rule_plugins": {
            "type": "boolean",
            "description": "Load installed flakeforge.rules entry-point providers.",
            "default": True,
        },
        "output_format": {
            "type": "string",
            "description": "Formatter used to render results.",
            "enum": ["text", "json", "github", "sarif"],
            "default": "text",
        },
        "statistics": {
            "type": "boolean",
            "description": "Append a per-code count summary to the output.",
            "default": False,
        },
        "baseline": {
            "type": "string",
            "description": "Path to a baseline file whose recorded fingerprints are suppressed.",
        },
        "telemetry_sink": {
            "type": "string",
            "description": "Fault-telemetry sink spec (stdout, logging, file:<path>, etc.).",
        },
        "generated_by_model": {
            "type": "string",
            "description": "Generator model id recorded on events.",
        },
    }

    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "FlakeForge Configuration",
        "type": "object",
        "properties": {
            **config_properties,
            "tool": {
                "type": "object",
                "properties": {
                    "flakeforge": {
                        "type": "object",
                        "properties": config_properties,
                        "additionalProperties": False,
                    }
                },
                "additionalProperties": True,
            },
            "$schema": {"type": "string"},
        },
        "additionalProperties": True,
    }

    return json.dumps(schema, indent=2)
