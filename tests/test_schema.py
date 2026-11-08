import json
import re

from flakeforge.cli import main


def test_generate_schema_contains_rules(capsys):
    assert main(["generate-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)

    assert "$schema" in schema
    assert schema["title"] == "FlakeForge Configuration"

    select_items = schema["properties"]["select"]["items"]["anyOf"][0]["oneOf"]
    codes = [item["anyOf"][0]["const"] for item in select_items]

    assert "X001" in codes
    assert "X015" in codes

    x001_desc = next(
        item["markdownDescription"] for item in select_items if item["anyOf"][0]["const"] == "X001"
    )
    assert x001_desc == "Do not use bare except."


def test_generate_schema_tool_wrapper(capsys):
    assert main(["generate-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)

    assert "tool" in schema["properties"]
    assert "flakeforge" in schema["properties"]["tool"]["properties"]
    assert "select" in schema["properties"]["tool"]["properties"]["flakeforge"]["properties"]


def test_generated_schema_accepts_rule_prefixes_and_case_variants(capsys):
    assert main(["generate-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    properties = schema["properties"]

    select_pattern = properties["select"]["items"]["anyOf"][1]["pattern"]
    ignore_pattern = properties["ignore"]["items"]["anyOf"][1]["pattern"]
    per_file_pattern = properties["per_file_ignores"]["additionalProperties"]["items"]["anyOf"][1][
        "pattern"
    ]

    assert re.fullmatch(select_pattern, "X")
    assert re.fullmatch(ignore_pattern, "X00")
    assert re.fullmatch(per_file_pattern, "x001")

    full_code_schema = properties["select"]["items"]["anyOf"][0]["oneOf"]
    x001_schema = next(item for item in full_code_schema if item["anyOf"][0]["const"] == "X001")
    assert re.fullmatch(x001_schema["anyOf"][1]["pattern"], "x001")
