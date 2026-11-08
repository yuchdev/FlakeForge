import pytest

from flakeforge.config import (
    CONFIG_KEYS,
    LEGACY_SECTION_WARNING,
    ConfigValidationError,
    LintConfig,
    describe_config_source,
    discovery_anchor,
    isolated_config,
    load_config,
    resolve_config_origins,
    validate_config,
)


def test_discovery_anchor_no_paths_returns_cwd(tmp_path) -> None:
    assert discovery_anchor([], cwd=tmp_path) == tmp_path.resolve()


def test_discovery_anchor_single_directory_returns_directory(tmp_path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    assert discovery_anchor([str(target)], cwd=tmp_path) == target.resolve()


def test_discovery_anchor_single_file_returns_parent(tmp_path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    sample = target / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")
    assert discovery_anchor([str(sample)], cwd=tmp_path) == target.resolve()


def test_discovery_anchor_relative_path_resolves_against_cwd(tmp_path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    assert discovery_anchor(["proj"], cwd=tmp_path) == target.resolve()


def test_discovery_anchor_several_paths_returns_common_ancestor(tmp_path) -> None:
    pkg_a = tmp_path / "proj" / "pkg_a"
    pkg_b = tmp_path / "proj" / "pkg_b"
    pkg_a.mkdir(parents=True)
    pkg_b.mkdir(parents=True)
    anchor = discovery_anchor([str(pkg_a), str(pkg_b)], cwd=tmp_path)
    assert anchor == (tmp_path / "proj").resolve()


def test_discovery_anchor_several_files_in_same_directory(tmp_path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    first = target / "a.py"
    second = target / "b.py"
    first.write_text("x = 1\n", encoding="utf-8")
    second.write_text("y = 2\n", encoding="utf-8")
    anchor = discovery_anchor([str(first), str(second)], cwd=tmp_path)
    assert anchor == target.resolve()


def test_isolated_config_ignores_on_disk_config_and_anchors_base_dir(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "flakeforge.toml").write_text('ignore = ["X001"]\nrule_modules = ["evil"]\n', encoding="utf-8")
    config = isolated_config([str(project)], cwd=tmp_path)
    # Defaults only: nothing from the hostile file leaks in (C3/C7).
    assert config == LintConfig()
    assert config.ignore == ()
    assert config.rule_modules == ()
    # Patterns resolve against the discovery anchor (C6).
    assert config.base_dir == project.resolve()


def test_isolated_config_no_paths_anchors_on_cwd(tmp_path) -> None:
    config = isolated_config([], cwd=tmp_path)
    assert config.base_dir == tmp_path.resolve()


def test_load_config_from_pyproject_section(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.flakeforge]\nselect = ["x002"]\nallow_noqa = false\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path)
    assert config == LintConfig(select=("X002",), allow_noqa=False)
    assert config.base_dir == tmp_path


def test_load_config_falls_back_to_legacy_flake8_lint_section(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.flake8_lint]\nignore = ["x003"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.ignore == ("X003",)
    assert config.legacy_mode is True
    assert config.warnings == (LEGACY_SECTION_WARNING,)


def test_load_config_prefers_standalone_toml_over_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.select == ("X002",)


def test_load_config_searches_upward_without_merging(tmp_path) -> None:
    project = tmp_path / "project"
    nested = project / "pkg" / "subpkg"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X002"]\n',
        encoding="utf-8",
    )
    config = load_config(cwd=nested)
    assert config.select == ("X002",)
    assert config.base_dir == project


def test_explicit_config_path_is_loaded_relative_to_cwd(tmp_path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    config_path = configs / "flakeforge.toml"
    config_path.write_text('include = ["src"]\n', encoding="utf-8")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    config = load_config("..//configs/flakeforge.toml", cwd=cwd)
    assert config.include == ("src",)
    assert config.base_dir == configs


def test_config_rejects_non_boolean_allow_noqa() -> None:
    with pytest.raises(ConfigValidationError):
        LintConfig.from_mapping({"allow_noqa": "false"})


def test_config_rejects_non_sequence_include_values() -> None:
    with pytest.raises(ConfigValidationError):
        LintConfig.from_mapping({"include": 1})


def test_config_rejects_string_include_values() -> None:
    with pytest.raises(ConfigValidationError):
        LintConfig.from_mapping({"include": "src"})


def test_config_rejects_non_string_sequence_items() -> None:
    with pytest.raises(ConfigValidationError):
        LintConfig.from_mapping({"ignore": [1]})


@pytest.mark.parametrize("key", ["include", "exclude", "noqa_allowed", "noqa_forbidden"])
def test_config_rejects_absolute_posix_path_pattern(key) -> None:
    with pytest.raises(ConfigValidationError, match=rf"{key} pattern '/etc' must be relative"):
        LintConfig.from_mapping({key: ["/etc"]})


@pytest.mark.parametrize(
    "pattern",
    [r"C:\Users\app", "C:/Users/app", "C:foo", r"\\server\share\pkg", r"\rooted"],
)
def test_config_rejects_anchored_windows_pattern(pattern) -> None:
    with pytest.raises(ConfigValidationError, match=r"must be relative"):
        LintConfig.from_mapping({"include": [pattern]})


def test_config_accepts_relative_parent_pattern() -> None:
    config = LintConfig.from_mapping({"include": ["../src", "pkg/**/*.py", "~/x", "../../*.py"]})
    assert config.include == ("../src", "pkg/**/*.py", "~/x", "../../*.py")


def test_absolute_pattern_error_names_file_and_section(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text('exclude = ["/var/log"]\n', encoding="utf-8")
    with pytest.raises(
        ConfigValidationError,
        match=r"flakeforge\.toml: exclude pattern '/var/log' must be relative",
    ):
        load_config(cwd=tmp_path)


def test_absolute_pattern_rejected_in_legacy_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flake8_lint]\ninclude = ["/opt/pkg"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match=r"include pattern '/opt/pkg' must be relative"):
        load_config(cwd=tmp_path)


def test_validate_config_rejects_unknown_canonical_rule_selectors() -> None:
    with pytest.raises(ConfigValidationError, match="X999"):
        validate_config(LintConfig(select=("X001", "X999")), ("X001", "X002", "X003"))


def test_validate_config_keeps_known_reserved_x003() -> None:
    config = validate_config(LintConfig(ignore=("X003",)), ("X001", "X002", "X003"))
    assert config.ignore == ("X003",)


def test_validate_config_ignores_unknown_legacy_rules_with_warning() -> None:
    config = validate_config(
        LintConfig(select=("X001", "X999"), legacy_mode=True, warnings=(LEGACY_SECTION_WARNING,)),
        ("X001", "X002", "X003"),
    )
    assert config.select == ("X001",)
    assert any("X999" in warning for warning in config.warnings)


def test_per_file_ignores_parses_table_upper_cases_codes_and_keeps_order() -> None:
    config = LintConfig.from_mapping({"per_file_ignores": {"tests/**": ["x002"], "scripts/*.py": ["X0", "x001"]}})
    assert config.per_file_ignores == (
        ("tests/**", ("X002",)),
        ("scripts/*.py", ("X0", "X001")),
    )


def test_per_file_ignores_defaults_to_empty_table() -> None:
    assert LintConfig.from_mapping({}).per_file_ignores == ()
    assert LintConfig().per_file_ignores == ()
    assert "per_file_ignores" in CONFIG_KEYS


def test_per_file_ignores_rejects_non_table() -> None:
    with pytest.raises(ConfigValidationError, match=r"per_file_ignores must be a table"):
        LintConfig.from_mapping({"per_file_ignores": ["tests/**"]})


def test_per_file_ignores_rejects_non_string_codes() -> None:
    with pytest.raises(ConfigValidationError, match=r"per_file_ignores"):
        LintConfig.from_mapping({"per_file_ignores": {"tests/**": [1]}})


@pytest.mark.parametrize("glob", ["/etc/**", r"C:\Users\app", "C:/app", r"\\server\share"])
def test_per_file_ignores_rejects_absolute_glob(glob) -> None:
    with pytest.raises(ConfigValidationError, match=r"per_file_ignores pattern .* must be relative"):
        LintConfig.from_mapping({"per_file_ignores": {glob: ["X001"]}})


def test_per_file_ignores_absolute_glob_rejected_in_legacy_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flake8_lint]\nper_file_ignores = { "/opt/**" = ["X001"] }\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match=r"per_file_ignores pattern '/opt/\*\*' must be relative"):
        load_config(cwd=tmp_path)


def test_validate_config_rejects_unknown_per_file_ignore_code() -> None:
    config = LintConfig(per_file_ignores=(("tests/**", ("X999",)),))
    with pytest.raises(ConfigValidationError, match="X999"):
        validate_config(config, ("X001", "X002", "X003"))


def test_validate_config_keeps_known_per_file_ignore_code() -> None:
    config = LintConfig(per_file_ignores=(("tests/**", ("x002",)),))
    validated = validate_config(config, ("X001", "X002", "X003"))
    assert validated.per_file_ignores == (("tests/**", ("X002",)),)


def test_validate_config_drops_unknown_legacy_per_file_ignore_with_warning() -> None:
    config = LintConfig(
        per_file_ignores=(("tests/**", ("X002", "X999")),),
        legacy_mode=True,
        warnings=(LEGACY_SECTION_WARNING,),
    )
    validated = validate_config(config, ("X001", "X002", "X003"))
    assert validated.per_file_ignores == (("tests/**", ("X002",)),)
    assert any("X999" in warning for warning in validated.warnings)


def test_merge_replaces_per_file_ignores_when_provided() -> None:
    base = LintConfig(per_file_ignores=(("tests/**", ("X001",)),))
    merged = base.merge(per_file_ignores=(("scripts/*.py", ("X0",)),))
    assert merged.per_file_ignores == (("scripts/*.py", ("X0",)),)
    # A None leaves the field untouched.
    assert base.merge().per_file_ignores == (("tests/**", ("X001",)),)


def test_merge_normalizes_code_prefixes_without_touching_path_fields() -> None:
    merged = LintConfig().merge(
        select=("x0",),
        ignore=("x002",),
        noqa_allowed=("src/example.py",),
    )
    assert merged.select == ("X0",)
    assert merged.ignore == ("X002",)
    assert merged.noqa_allowed == ("src/example.py",)


def test_config_defaults_output_format_and_rule_plugins() -> None:
    config = LintConfig.from_mapping({})
    assert config.output_format == "text"
    assert config.rule_plugins is True


def test_config_parses_output_format_and_rule_plugins_from_file(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'output_format = "json"\nrule_plugins = false\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path)
    assert config.output_format == "json"
    assert config.rule_plugins is False


def test_config_rejects_non_string_output_format() -> None:
    with pytest.raises(ConfigValidationError, match="output_format"):
        LintConfig.from_mapping({"output_format": 1})


def test_config_rejects_non_boolean_rule_plugins() -> None:
    with pytest.raises(ConfigValidationError, match="rule_plugins"):
        LintConfig.from_mapping({"rule_plugins": "false"})


def test_merge_overrides_output_format_and_rule_plugins_in_both_directions() -> None:
    file_config = LintConfig(output_format="json", rule_plugins=False)
    # None leaves the file value intact.
    assert file_config.merge(output_format=None, rule_plugins=None) == file_config
    # Explicit values override the file value in either direction.
    overridden = file_config.merge(output_format="text", rule_plugins=True)
    assert overridden.output_format == "text"
    assert overridden.rule_plugins is True


def test_config_statistics_defaults_false() -> None:
    assert LintConfig.from_mapping({}).statistics is False


def test_config_parses_statistics_from_file(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text("statistics = true\n", encoding="utf-8")
    assert load_config(cwd=tmp_path).statistics is True


def test_config_rejects_non_boolean_statistics() -> None:
    with pytest.raises(ConfigValidationError, match="statistics"):
        LintConfig.from_mapping({"statistics": "yes"})


def test_merge_overrides_statistics_in_both_directions() -> None:
    file_config = LintConfig(statistics=True)
    # None leaves the file value intact; explicit values override either way.
    assert file_config.merge(statistics=None) == file_config
    assert file_config.merge(statistics=False).statistics is False
    assert LintConfig(statistics=False).merge(statistics=True).statistics is True


def test_flakeforge_toml_unknown_key_raises_with_did_you_mean(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text('exlude = ["build"]\n', encoding="utf-8")
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(cwd=tmp_path)
    message = str(excinfo.value)
    assert message == "flakeforge.toml: unknown key 'exlude' (did you mean 'exclude'?)"


def test_pyproject_unknown_key_names_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.flakeforge]\nslect = []\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(cwd=tmp_path)
    message = str(excinfo.value)
    assert message.startswith("pyproject.toml [tool.flakeforge]: unknown key 'slect'")
    assert "did you mean 'select'" in message


def test_unknown_key_without_close_match_omits_hint() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        LintConfig.from_mapping({"totallyunrelated": 1}, source="flakeforge.toml")
    assert str(excinfo.value) == "flakeforge.toml: unknown key 'totallyunrelated'"


def test_legacy_section_unknown_key_becomes_warning(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flake8_lint]\nignore = ["X003"]\nexlude = ["build"]\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path)
    assert config.ignore == ("X003",)
    assert config.legacy_mode is True
    assert any("unknown key 'exlude'" in warning for warning in config.warnings)
    assert LEGACY_SECTION_WARNING in config.warnings


def test_type_error_carries_source_prefix(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text('allow_noqa = "false"\n', encoding="utf-8")
    with pytest.raises(ConfigValidationError, match=r"flakeforge\.toml: allow_noqa must be a boolean"):
        load_config(cwd=tmp_path)


def test_path_pattern_type_error_carries_source_prefix(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text('include = "src"\n', encoding="utf-8")
    with pytest.raises(ConfigValidationError, match=r"^flakeforge\.toml: include"):
        load_config(cwd=tmp_path)


def test_flakeforge_toml_wrapped_table_matches_flat(tmp_path) -> None:
    flat_dir = tmp_path / "flat"
    wrapped_dir = tmp_path / "wrapped"
    flat_dir.mkdir()
    wrapped_dir.mkdir()
    (flat_dir / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    (wrapped_dir / "flakeforge.toml").write_text(
        '[tool.flakeforge]\nselect = ["X002"]\n',
        encoding="utf-8",
    )
    flat = load_config(cwd=flat_dir)
    wrapped = load_config(cwd=wrapped_dir)
    assert flat.select == ("X002",)
    assert wrapped.select == ("X002",)
    assert flat == wrapped


def test_flakeforge_toml_both_flat_and_wrapper_is_error(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'select = ["X001"]\n[tool.flakeforge]\nignore = ["X002"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match="not both"):
        load_config(cwd=tmp_path)


def test_valid_config_with_all_known_keys_loads_unchanged(tmp_path) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'include = ["src"]\n'
        "exclude = []\n"
        'select = ["X001"]\n'
        "ignore = []\n"
        "allow_noqa = true\n"
        "noqa_allowed = []\n"
        "noqa_forbidden = []\n"
        "rule_modules = []\n"
        "rule_plugins = false\n"
        'output_format = "json"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=tmp_path)
    assert config.include == ("src",)
    assert config.select == ("X001",)
    assert config.rule_plugins is False
    assert config.output_format == "json"
    assert config.warnings == ()


def test_same_directory_flakeforge_toml_shadows_pyproject_with_warning(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.select == ("X002",)
    assert config.warnings == ("pyproject.toml [tool.flakeforge] is shadowed by flakeforge.toml; remove one",)


def test_same_directory_shadow_warning_names_legacy_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flake8_lint]\nignore = ["X003"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.select == ("X002",)
    assert config.warnings == ("pyproject.toml [tool.flake8_lint] is shadowed by flakeforge.toml; remove one",)


def test_same_directory_shadow_prefers_canonical_section_name(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001"]\n[tool.flake8_lint]\nignore = ["X003"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.warnings == ("pyproject.toml [tool.flakeforge] is shadowed by flakeforge.toml; remove one",)


def test_shadow_check_ignores_pyproject_without_flakeforge_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.black]\nline-length = 100\n",
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.select == ("X002",)
    assert config.warnings == ()


def test_shadow_check_does_not_error_on_unknown_key_in_shadowed_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nslect = ["X001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.select == ("X002",)
    assert config.warnings == ("pyproject.toml [tool.flakeforge] is shadowed by flakeforge.toml; remove one",)


def test_shadow_check_tolerates_unparseable_sibling_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is = = not valid toml\n", encoding="utf-8")
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config(cwd=tmp_path)
    assert config.select == ("X002",)
    assert config.warnings == ()


def test_nearest_pyproject_section_beats_parent_flakeforge_toml(tmp_path) -> None:
    parent = tmp_path / "project"
    nested = parent / "pkg"
    nested.mkdir(parents=True)
    (parent / "flakeforge.toml").write_text('select = ["X001"]\n', encoding="utf-8")
    (nested / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X002"]\n',
        encoding="utf-8",
    )
    config = load_config(cwd=nested)
    assert config.select == ("X002",)
    assert config.base_dir == nested
    assert config.warnings == ()


def test_pyproject_without_flakeforge_section_does_not_stop_upward_search(tmp_path) -> None:
    parent = tmp_path / "project"
    nested = parent / "pkg"
    nested.mkdir(parents=True)
    (parent / "flakeforge.toml").write_text('select = ["X001"]\n', encoding="utf-8")
    (nested / "pyproject.toml").write_text(
        "[tool.black]\nline-length = 100\n",
        encoding="utf-8",
    )
    config = load_config(cwd=nested)
    assert config.select == ("X001",)
    assert config.base_dir == parent
    assert config.warnings == ()


def test_explicit_config_accepts_pyproject_and_flakeforge_toml(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    from_pyproject = load_config("pyproject.toml", cwd=tmp_path)
    from_flakeforge = load_config("flakeforge.toml", cwd=tmp_path)
    assert from_pyproject.select == ("X001",)
    assert from_flakeforge.select == ("X002",)


def test_explicit_config_flakeforge_toml_does_not_warn_about_sibling_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    config = load_config("flakeforge.toml", cwd=tmp_path)
    assert config.select == ("X002",)
    assert config.warnings == ()


def test_explicit_config_pyproject_without_section_is_error(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.black]\nline-length = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError):
        load_config("pyproject.toml", cwd=tmp_path)


def test_resolve_config_origins_marks_file_default_and_cli() -> None:
    file_config = LintConfig(ignore=("X001",))
    origins = resolve_config_origins(file_config, cli_keys={"select"})
    assert set(origins) == set(CONFIG_KEYS)
    assert origins["ignore"] == "file"  # differs from default
    assert origins["select"] == "cli"  # supplied on the CLI
    assert origins["include"] == "default"  # untouched


def test_resolve_config_origins_cli_wins_over_file() -> None:
    # A key both the file and the CLI set is attributed to the winning source.
    file_config = LintConfig(select=("X001",))
    origins = resolve_config_origins(file_config, cli_keys={"select"})
    assert origins["select"] == "cli"


def test_describe_config_source_defaults_and_sections(tmp_path) -> None:
    assert describe_config_source(LintConfig()) == (None, None)

    ff = tmp_path / "flakeforge.toml"
    standalone = LintConfig(config_path=ff)
    assert describe_config_source(standalone) == (ff, None)

    pyproject = tmp_path / "pyproject.toml"
    canonical = LintConfig(config_path=pyproject)
    assert describe_config_source(canonical) == (pyproject, "[tool.flakeforge]")

    legacy = LintConfig(config_path=pyproject, legacy_mode=True)
    assert describe_config_source(legacy) == (pyproject, "[tool.flake8_lint]")


def test_baseline_key_loads_relative_and_defaults_empty(tmp_path) -> None:
    assert LintConfig.from_mapping({}).baseline == ""
    loaded = LintConfig.from_mapping({"baseline": "ci/baseline.json"})
    assert loaded.baseline == "ci/baseline.json"


def test_apply_baseline_defaults_false_and_loads_boolean() -> None:
    assert LintConfig.from_mapping({}).apply_baseline is False
    assert LintConfig.from_mapping({"apply_baseline": True}).apply_baseline is True


def test_baseline_absolute_path_is_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="must be relative"):
        LintConfig.from_mapping({"baseline": "/var/lib/baseline.json"})


def test_baseline_non_string_is_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="baseline must be a string"):
        LintConfig.from_mapping({"baseline": 3})


def test_merge_overrides_baseline() -> None:
    assert LintConfig(baseline="a.json").merge(baseline="b.json").baseline == "b.json"
    # None leaves the existing value untouched (precedence C2).
    assert LintConfig(baseline="a.json").merge(baseline=None).baseline == "a.json"


def test_per_file_ignores_rejects_empty_code() -> None:
    with pytest.raises(ConfigValidationError, match="must not be empty"):
        LintConfig.from_mapping({"per_file_ignores": {"tests/**": [""]}})
