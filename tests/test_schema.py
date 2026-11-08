import json

from flakeforge.cli import main


def test_generate_schema_contains_rules(capsys):
    assert main(["generate-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    
    assert "$schema" in schema
    assert schema["title"] == "FlakeForge Configuration"
    
    # Check if built-in rules are present in the definitions
    select_items = schema["properties"]["select"]["items"]["oneOf"]
    codes = [item["const"] for item in select_items]
    
    assert "X001" in codes
    assert "X015" in codes
    
    # Check for a specific rule description
    x001_desc = next(item["markdownDescription"] for item in select_items if item["const"] == "X001")
    assert x001_desc == "Do not use bare except."

def test_generate_schema_tool_wrapper(capsys):
    assert main(["generate-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    
    assert "tool" in schema["properties"]
    assert "flakeforge" in schema["properties"]["tool"]["properties"]
    assert "select" in schema["properties"]["tool"]["properties"]["flakeforge"]["properties"]
