"""Unit tests for the architecture baseline, deterministic rules and validator.

Coverage: the layer helper, each rule (positive and negative), baseline
invariants, the ``ast``-based scanner (absolute and relative imports), the
validator (collection, deterministic ordering, serialization) and a self-check
that the assistant's own ``src`` tree satisfies architecture v1.0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

import pytest

from architecture_assistant.architecture import (
    ALLOWED_IMPORTS,
    APPLICATION_LAYER,
    ARCHITECTURE_BASELINES,
    ARCHITECTURE_CURRENT,
    ARCHITECTURE_LAYER,
    ARCHITECTURE_V1,
    ARCHITECTURE_V1_1,
    COMPOSITION_LAYER,
    DOMAIN_LAYER,
    FORBIDDEN_LAYER_IMPORT,
    INFRASTRUCTURE_LAYER,
    KNOWN_LAYERS,
    PORTS_LAYER,
    ROOT_PACKAGE,
    SQLITE_OUTSIDE_INFRASTRUCTURE,
    UNKNOWN_LAYER,
    V1_ALLOWED_IMPORTS,
    V1_KNOWN_LAYERS,
    ArchitectureBaseline,
    ArchitectureRule,
    ArchitectureValidator,
    SourceModel,
    build_source_model,
    discover_python_files,
    extract_imports,
    layer_of,
    module_name_for,
    scan_directory,
)
from architecture_assistant.domain.enums import Severity


def source_model(
    imports: Mapping[str, Iterable[str]],
    root_package: str = ROOT_PACKAGE,
) -> SourceModel:
    """Build a SourceModel straight from ``{module: [imports]}``."""
    return SourceModel(
        imports={
            module: frozenset(targets) for module, targets in imports.items()
        },
        root_package=root_package,
    )


def rule_check(rule_id: str, source: SourceModel):
    """Run one baseline rule against a source model."""
    return ARCHITECTURE_V1.rule(rule_id).check(source)


class TestLayerOf:
    def test_submodule_reports_its_layer(self) -> None:
        assert layer_of(f"{ROOT_PACKAGE}.domain.models") == DOMAIN_LAYER

    def test_layer_package_reports_its_layer(self) -> None:
        assert layer_of(f"{ROOT_PACKAGE}.ports") == PORTS_LAYER

    def test_root_package_is_the_empty_layer(self) -> None:
        assert layer_of(ROOT_PACKAGE) == ""

    def test_stdlib_module_has_no_layer(self) -> None:
        assert layer_of("sqlite3") is None

    def test_third_party_module_has_no_layer(self) -> None:
        assert layer_of("pytest") is None

    def test_custom_root_package(self) -> None:
        assert layer_of("pkg.domain.models", "pkg") == DOMAIN_LAYER


class TestUnknownLayerRule:
    def test_known_layers_pass(self) -> None:
        source = source_model(
            {f"{ROOT_PACKAGE}.{layer}.mod": () for layer in V1_KNOWN_LAYERS}
        )
        assert rule_check(UNKNOWN_LAYER, source) == ()

    def test_root_package_module_is_ignored(self) -> None:
        assert rule_check(UNKNOWN_LAYER, source_model({ROOT_PACKAGE: ()})) == ()

    def test_new_top_level_package_is_a_violation(self) -> None:
        source = source_model({f"{ROOT_PACKAGE}.helpers.util": ()})
        violations = rule_check(UNKNOWN_LAYER, source)
        assert len(violations) == 1
        violation = violations[0]
        assert violation.rule_id == UNKNOWN_LAYER
        assert violation.module == f"{ROOT_PACKAGE}.helpers.util"
        assert violation.target == "helpers"
        assert violation.severity is Severity.HIGH
        assert "unknown layer" in violation.message

    def test_violations_are_deterministically_ordered(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.zeta.mod": (),
                f"{ROOT_PACKAGE}.alpha.mod": (),
            }
        )
        violations = rule_check(UNKNOWN_LAYER, source)
        assert [violation.module for violation in violations] == [
            f"{ROOT_PACKAGE}.alpha.mod",
            f"{ROOT_PACKAGE}.zeta.mod",
        ]


class TestForbiddenLayerImportRule:
    def test_allowed_dependency_direction_passes(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.ports.repositories": (
                    f"{ROOT_PACKAGE}.domain.models",
                ),
                f"{ROOT_PACKAGE}.application.context": (
                    f"{ROOT_PACKAGE}.domain.models",
                    f"{ROOT_PACKAGE}.ports.repositories",
                ),
                f"{ROOT_PACKAGE}.architecture.rules": (
                    f"{ROOT_PACKAGE}.domain.enums",
                ),
                f"{ROOT_PACKAGE}.infrastructure.sqlite": (
                    f"{ROOT_PACKAGE}.domain.models",
                    f"{ROOT_PACKAGE}.ports.repositories",
                ),
            }
        )
        assert rule_check(FORBIDDEN_LAYER_IMPORT, source) == ()

    def test_domain_importing_ports_is_a_violation(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.domain.models": (
                    f"{ROOT_PACKAGE}.ports.repositories",
                )
            }
        )
        violations = rule_check(FORBIDDEN_LAYER_IMPORT, source)
        assert len(violations) == 1
        assert violations[0].target == f"{ROOT_PACKAGE}.ports.repositories"
        assert violations[0].severity is Severity.HIGH
        assert violations[0].rule_id == FORBIDDEN_LAYER_IMPORT

    def test_application_importing_infrastructure_is_a_violation(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.application.context": (
                    f"{ROOT_PACKAGE}.infrastructure.sqlite",
                )
            }
        )
        violations = rule_check(FORBIDDEN_LAYER_IMPORT, source)
        assert len(violations) == 1
        assert "infrastructure" in violations[0].message

    def test_architecture_layer_importing_application_is_a_violation(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.architecture.validator": (
                    f"{ROOT_PACKAGE}.application.context",
                )
            }
        )
        assert len(rule_check(FORBIDDEN_LAYER_IMPORT, source)) == 1

    def test_intra_layer_imports_are_allowed(self) -> None:
        source = source_model(
            {f"{ROOT_PACKAGE}.domain.models": (f"{ROOT_PACKAGE}.domain.enums",)}
        )
        assert rule_check(FORBIDDEN_LAYER_IMPORT, source) == ()

    def test_stdlib_and_root_package_imports_are_allowed(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.domain.models": (
                    "dataclasses",
                    "typing",
                    ROOT_PACKAGE,
                )
            }
        )
        assert rule_check(FORBIDDEN_LAYER_IMPORT, source) == ()

    def test_targets_are_reported_in_sorted_order(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.domain.models": (
                    f"{ROOT_PACKAGE}.ports.repositories",
                    f"{ROOT_PACKAGE}.application.context",
                )
            }
        )
        violations = rule_check(FORBIDDEN_LAYER_IMPORT, source)
        targets = [violation.target for violation in violations]
        assert targets == sorted(targets)


class TestSqliteOutsideInfrastructureRule:
    def test_infrastructure_may_import_sqlite(self) -> None:
        source = source_model(
            {f"{ROOT_PACKAGE}.infrastructure.sqlite": ("sqlite3",)}
        )
        assert rule_check(SQLITE_OUTSIDE_INFRASTRUCTURE, source) == ()

    @pytest.mark.parametrize(
        "layer",
        [DOMAIN_LAYER, PORTS_LAYER, APPLICATION_LAYER, ARCHITECTURE_LAYER],
        ids=[DOMAIN_LAYER, PORTS_LAYER, APPLICATION_LAYER, ARCHITECTURE_LAYER],
    )
    def test_other_layers_may_not_import_sqlite(self, layer: str) -> None:
        source = source_model({f"{ROOT_PACKAGE}.{layer}.mod": ("sqlite3",)})
        violations = rule_check(SQLITE_OUTSIDE_INFRASTRUCTURE, source)
        assert len(violations) == 1
        assert violations[0].severity is Severity.CRITICAL
        assert violations[0].target == "sqlite3"
        assert violations[0].rule_id == SQLITE_OUTSIDE_INFRASTRUCTURE

    def test_module_without_sqlite_is_fine(self) -> None:
        source = source_model({f"{ROOT_PACKAGE}.domain.mod": ("json",)})
        assert rule_check(SQLITE_OUTSIDE_INFRASTRUCTURE, source) == ()



class TestBaseline:
    def test_version_and_name(self) -> None:
        assert ARCHITECTURE_V1.version == "1.0"
        assert ARCHITECTURE_V1.name == "Layered Architecture Baseline"

    def test_declares_the_three_rules_in_order(self) -> None:
        assert ARCHITECTURE_V1.rule_ids() == (
            UNKNOWN_LAYER,
            FORBIDDEN_LAYER_IMPORT,
            SQLITE_OUTSIDE_INFRASTRUCTURE,
        )

    def test_rule_lookup_by_id(self) -> None:
        rule = ARCHITECTURE_V1.rule(FORBIDDEN_LAYER_IMPORT)
        assert rule.id == FORBIDDEN_LAYER_IMPORT
        assert rule.description
        assert callable(rule.check)

    def test_unknown_rule_lookup_raises(self) -> None:
        with pytest.raises(KeyError):
            ARCHITECTURE_V1.rule("does-not-exist")

    def test_allowed_imports_cover_exactly_the_known_layers(self) -> None:
        assert set(ALLOWED_IMPORTS) == set(KNOWN_LAYERS)
        assert set(ARCHITECTURE_V1.allowed_imports) == set(V1_KNOWN_LAYERS)
        assert set(V1_ALLOWED_IMPORTS) == set(V1_KNOWN_LAYERS)
        assert set(ARCHITECTURE_V1_1.allowed_imports) == set(KNOWN_LAYERS)

    def test_allowed_import_matrix(self) -> None:
        assert ARCHITECTURE_V1.allowed_imports[DOMAIN_LAYER] == frozenset()
        assert ARCHITECTURE_V1.allowed_imports[PORTS_LAYER] == frozenset(
            {DOMAIN_LAYER}
        )
        assert ARCHITECTURE_V1.allowed_imports[APPLICATION_LAYER] == frozenset(
            {DOMAIN_LAYER, PORTS_LAYER}
        )
        assert ARCHITECTURE_V1.allowed_imports[ARCHITECTURE_LAYER] == frozenset(
            {DOMAIN_LAYER}
        )
        assert ARCHITECTURE_V1.allowed_imports[INFRASTRUCTURE_LAYER] == frozenset(
            {DOMAIN_LAYER, PORTS_LAYER}
        )

    def test_duplicate_rule_ids_are_rejected(self) -> None:
        rule = ARCHITECTURE_V1.rules[0]
        with pytest.raises(ValueError, match="unique"):
            ArchitectureBaseline(
                version="1.0",
                name="broken",
                description="d",
                known_layers=(DOMAIN_LAYER,),
                allowed_imports={DOMAIN_LAYER: frozenset()},
                rules=(rule, rule),
            )

    def test_baseline_without_rules_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one rule"):
            ArchitectureBaseline(
                version="1.0",
                name="broken",
                description="d",
                known_layers=(DOMAIN_LAYER,),
                allowed_imports={DOMAIN_LAYER: frozenset()},
                rules=(),
            )

    def test_matrix_referencing_an_unknown_layer_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown layer"):
            ArchitectureBaseline(
                version="1.0",
                name="broken",
                description="d",
                known_layers=(DOMAIN_LAYER,),
                allowed_imports={DOMAIN_LAYER: frozenset({"nope"})},
                rules=ARCHITECTURE_V1.rules,
            )


class TestArchitectureRuleValidation:
    def test_empty_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="rule id"):
            ArchitectureRule(
                id="",
                description="d",
                severity=Severity.HIGH,
                check=lambda source: (),
            )

    def test_empty_description_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="rule description"):
            ArchitectureRule(
                id="x",
                description="   ",
                severity=Severity.HIGH,
                check=lambda source: (),
            )

    def test_non_callable_check_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="callable"):
            ArchitectureRule(
                id="x",
                description="d",
                severity=Severity.HIGH,
                check=None,  # type: ignore[arg-type]
            )



class TestModuleNameFor:
    def test_module_file(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        path = root / "domain" / "models.py"
        assert module_name_for(path, root) == (
            f"{ROOT_PACKAGE}.domain.models",
            False,
        )

    def test_package_init(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        path = root / "domain" / "__init__.py"
        assert module_name_for(path, root) == (f"{ROOT_PACKAGE}.domain", True)

    def test_root_package_init(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        assert module_name_for(root / "__init__.py", root) == (ROOT_PACKAGE, True)

    def test_custom_root_package(self, tmp_path: Path) -> None:
        root = tmp_path / "pkg"
        assert module_name_for(root / "mod.py", root, "pkg") == ("pkg.mod", False)


class TestExtractImports:
    def test_absolute_imports(self) -> None:
        text = "import json\nimport sqlite3\nfrom dataclasses import dataclass\n"
        assert extract_imports(
            text, f"{ROOT_PACKAGE}.infrastructure.sqlite", False
        ) == frozenset({"json", "sqlite3", "dataclasses"})

    def test_relative_sibling_import(self) -> None:
        text = "from .enums import Phase\n"
        assert extract_imports(text, f"{ROOT_PACKAGE}.domain.models", False) == (
            frozenset({f"{ROOT_PACKAGE}.domain.enums"})
        )

    def test_relative_parent_import(self) -> None:
        text = (
            "from ..domain.models import Step\n"
            "from ..ports.repositories import TaskKey\n"
        )
        assert extract_imports(
            text, f"{ROOT_PACKAGE}.application.context", False
        ) == frozenset(
            {
                f"{ROOT_PACKAGE}.domain.models",
                f"{ROOT_PACKAGE}.ports.repositories",
            }
        )

    def test_relative_import_inside_package_init(self) -> None:
        text = "from .enums import Phase\n"
        assert extract_imports(text, f"{ROOT_PACKAGE}.domain", True) == frozenset(
            {f"{ROOT_PACKAGE}.domain.enums"}
        )

    def test_plain_relative_import_without_module_name(self) -> None:
        text = "from . import context\n"
        assert extract_imports(text, f"{ROOT_PACKAGE}.application", True) == (
            frozenset({f"{ROOT_PACKAGE}.application"})
        )

    def test_import_inside_a_function_is_detected(self) -> None:
        text = "def f():\n    import sqlite3\n    return sqlite3\n"
        assert extract_imports(text, f"{ROOT_PACKAGE}.domain.mod", False) == (
            frozenset({"sqlite3"})
        )

    def test_future_import_is_recorded_as_stdlib(self) -> None:
        text = "from __future__ import annotations\n"
        assert extract_imports(text, f"{ROOT_PACKAGE}.domain.mod", False) == (
            frozenset({"__future__"})
        )

    def test_result_is_a_frozenset_and_repeatable(self) -> None:
        text = "import b\nimport a\n"
        first = extract_imports(text, f"{ROOT_PACKAGE}.domain.mod", False)
        assert isinstance(first, frozenset)
        assert first == extract_imports(text, f"{ROOT_PACKAGE}.domain.mod", False)


class TestBuildSourceModel:
    def test_builds_modules_from_source_text(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        files = {
            root / "domain" / "__init__.py": "from . import models\n",
            root / "domain" / "models.py": "from .enums import Phase\n",
        }
        source = build_source_model(files, root)
        assert source.module_count == 2
        assert source.imports_of(f"{ROOT_PACKAGE}.domain") == frozenset(
            {f"{ROOT_PACKAGE}.domain"}
        )
        assert source.imports_of(f"{ROOT_PACKAGE}.domain.models") == frozenset(
            {f"{ROOT_PACKAGE}.domain.enums"}
        )

    def test_module_order_is_deterministic(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        files = {root / "b.py": "", root / "a.py": ""}
        assert build_source_model(files, root).modules() == (
            f"{ROOT_PACKAGE}.a",
            f"{ROOT_PACKAGE}.b",
        )

    def test_unknown_module_has_no_imports(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        source = build_source_model({root / "a.py": ""}, root)
        assert source.imports_of("not.scanned") == frozenset()


class TestScanDirectory:
    def test_scans_a_real_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "architecture_assistant"
        (root / "domain").mkdir(parents=True)
        (root / "__init__.py").write_text("", encoding="utf-8")
        (root / "domain" / "__init__.py").write_text("", encoding="utf-8")
        (root / "domain" / "models.py").write_text(
            "import sqlite3\n", encoding="utf-8"
        )
        source = scan_directory(root)
        assert source.module_count == 3
        assert "sqlite3" in source.imports_of(f"{ROOT_PACKAGE}.domain.models")

    def test_discover_python_files_is_sorted(self, tmp_path: Path) -> None:
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "b.py").write_text("", encoding="utf-8")
        (root / "a.py").write_text("", encoding="utf-8")
        assert [path.name for path in discover_python_files(root)] == [
            "a.py",
            "b.py",
        ]



class TestValidator:
    def test_compliant_source_produces_no_violations(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.domain.models": ("dataclasses",),
                f"{ROOT_PACKAGE}.ports.repositories": (
                    f"{ROOT_PACKAGE}.domain.models",
                ),
                f"{ROOT_PACKAGE}.application.context": (
                    f"{ROOT_PACKAGE}.domain.models",
                    f"{ROOT_PACKAGE}.ports.repositories",
                ),
            }
        )
        result = ArchitectureValidator().validate(source)
        assert result.is_compliant is True
        assert result.violation_count == 0
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
        assert result.checked_rules == ARCHITECTURE_CURRENT.rule_ids()

    def test_collects_violations_from_every_rule(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.domain.rogue": (
                    "sqlite3",
                    f"{ROOT_PACKAGE}.ports.repositories",
                ),
                f"{ROOT_PACKAGE}.helpers.util": (),
            }
        )
        result = ArchitectureValidator().validate(source)
        assert {violation.rule_id for violation in result.violations} == {
            UNKNOWN_LAYER,
            FORBIDDEN_LAYER_IMPORT,
            SQLITE_OUTSIDE_INFRASTRUCTURE,
        }
        assert result.is_compliant is False

    def test_violations_are_sorted_deterministically(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.domain.zeta": (
                    "sqlite3",
                    f"{ROOT_PACKAGE}.ports.repositories",
                ),
                f"{ROOT_PACKAGE}.domain.alpha": (
                    f"{ROOT_PACKAGE}.application.context",
                ),
            }
        )
        result = ArchitectureValidator().validate(source)
        keys = [violation.ordering_key for violation in result.violations]
        assert keys == sorted(keys)

    def test_validate_is_repeatable(self) -> None:
        source = source_model(
            {f"{ROOT_PACKAGE}.domain.z": (f"{ROOT_PACKAGE}.ports.repositories",)}
        )
        validator = ArchitectureValidator()
        assert validator.validate(source) == validator.validate(source)

    def test_to_dict_is_json_serializable(self) -> None:
        source = source_model({f"{ROOT_PACKAGE}.domain.z": ("sqlite3",)})
        data = ArchitectureValidator().validate(source).to_dict()
        assert json.loads(json.dumps(data)) == data
        assert data["is_compliant"] is False
        assert data["violation_count"] == len(data["violations"])
        assert data["violations"][0]["severity"] == Severity.CRITICAL.value

    def test_violations_for_filters_by_rule(self) -> None:
        source = source_model(
            {f"{ROOT_PACKAGE}.domain.z": ("sqlite3",)}
        )
        result = ArchitectureValidator().validate(source)
        assert len(result.violations_for(SQLITE_OUTSIDE_INFRASTRUCTURE)) == 1
        assert result.violations_for(UNKNOWN_LAYER) == ()

    def test_baseline_property_exposes_the_baseline(self) -> None:
        assert ArchitectureValidator().baseline is ARCHITECTURE_CURRENT
        assert ArchitectureValidator().baseline.version == "1.1"

    def test_uses_the_injected_baseline(self) -> None:
        custom = ArchitectureBaseline(
            version="9.9",
            name="minimal",
            description="minimal baseline",
            known_layers=(DOMAIN_LAYER,),
            allowed_imports={DOMAIN_LAYER: frozenset()},
            rules=(ARCHITECTURE_V1.rule(UNKNOWN_LAYER),),
        )
        validator = ArchitectureValidator(custom)
        assert validator.baseline.version == "9.9"

        result = validator.validate(source_model({f"{ROOT_PACKAGE}.helpers.util": ()}))
        assert result.baseline_version == "9.9"
        assert result.checked_rules == (UNKNOWN_LAYER,)
        assert result.violation_count == 1


def _src_root() -> Path:
    """The assistant source tree (the validator's own target)."""
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestSelfCheck:
    """The assistant must satisfy the architecture baseline it declares."""

    def test_assistant_source_tree_is_compliant(self) -> None:
        source = scan_directory(_src_root())
        assert source.module_count > 0
        result = ArchitectureValidator().validate(source)
        assert result.violations == ()
        assert result.is_compliant is True

    def test_sqlite_is_confined_to_infrastructure(self) -> None:
        source = scan_directory(_src_root())
        importers = {
            module
            for module in source.modules()
            if "sqlite3" in source.imports_of(module)
        }
        assert importers
        assert all(
            layer_of(module) == INFRASTRUCTURE_LAYER for module in importers
        )

    def test_every_scanned_module_lives_in_a_known_layer(self) -> None:
        source = scan_directory(_src_root())
        layers = {layer_of(module) for module in source.modules()}
        layers.discard("")
        assert layers <= set(KNOWN_LAYERS)

    def test_composition_layer_is_legal_under_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        layers = {layer_of(module) for module in source.modules()}
        assert COMPOSITION_LAYER in layers
        assert ArchitectureValidator().validate(source).violations == ()

    def test_architecture_v1_would_reject_the_composition_layer(self) -> None:
        """Proof that the change is real: v1.0 predates the composition root."""
        source = scan_directory(_src_root())
        violations = ArchitectureValidator(ARCHITECTURE_V1).validate(source)
        assert violations.is_compliant is False
        assert any(
            violation.rule_id == UNKNOWN_LAYER
            and violation.target == COMPOSITION_LAYER
            for violation in violations.violations
        )

    def test_baseline_rule_ids_are_the_persistable_rule_set(self) -> None:
        assert ARCHITECTURE_V1.rule_ids() == (
            UNKNOWN_LAYER,
            FORBIDDEN_LAYER_IMPORT,
            SQLITE_OUTSIDE_INFRASTRUCTURE,
        )
        assert all(rule_id.strip() for rule_id in ARCHITECTURE_V1.rule_ids())



class TestVersionedBaselines:
    """Architecture v1.1 adds the composition layer without changing v1.0."""

    def test_version_history(self) -> None:
        assert tuple(
            baseline.version for baseline in ARCHITECTURE_BASELINES
        ) == ("1.0", "1.1")
        assert ARCHITECTURE_CURRENT is ARCHITECTURE_V1_1
        assert ARCHITECTURE_V1.version == "1.0"
        assert ARCHITECTURE_V1_1.version == "1.1"

    def test_v1_freezes_the_five_layers(self) -> None:
        assert V1_KNOWN_LAYERS == (
            DOMAIN_LAYER,
            PORTS_LAYER,
            APPLICATION_LAYER,
            ARCHITECTURE_LAYER,
            INFRASTRUCTURE_LAYER,
        )
        assert COMPOSITION_LAYER not in V1_KNOWN_LAYERS
        assert ARCHITECTURE_V1.known_layers == V1_KNOWN_LAYERS

    def test_v1_1_adds_exactly_one_layer(self) -> None:
        assert KNOWN_LAYERS == V1_KNOWN_LAYERS + (COMPOSITION_LAYER,)
        assert ARCHITECTURE_V1_1.known_layers == KNOWN_LAYERS

    def test_no_existing_layer_gained_a_permission(self) -> None:
        for layer, allowed in V1_ALLOWED_IMPORTS.items():
            assert ALLOWED_IMPORTS[layer] == allowed

    def test_composition_may_import_every_other_layer(self) -> None:
        assert ALLOWED_IMPORTS[COMPOSITION_LAYER] == frozenset(V1_KNOWN_LAYERS)

    def test_composition_layer_is_legal_only_under_v1_1(self) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.{COMPOSITION_LAYER}.root": (
                    f"{ROOT_PACKAGE}.architecture.rules",
                    f"{ROOT_PACKAGE}.infrastructure.storage",
                    f"{ROOT_PACKAGE}.application.orchestrator",
                )
            }
        )
        current = ArchitectureValidator(ARCHITECTURE_V1_1).validate(source)
        assert current.is_compliant is True
        historic = ArchitectureValidator(ARCHITECTURE_V1).validate(source)
        assert historic.is_compliant is False
        # Under v1.0 the layer does not exist at all, so it is both unknown and
        # forbidden from importing anything.
        assert {violation.rule_id for violation in historic.violations} == {
            UNKNOWN_LAYER,
            FORBIDDEN_LAYER_IMPORT,
        }
        assert any(
            violation.rule_id == UNKNOWN_LAYER
            and violation.target == COMPOSITION_LAYER
            for violation in historic.violations
        )

    def test_no_layer_may_import_composition(self) -> None:
        for layer in V1_KNOWN_LAYERS:
            source = source_model(
                {
                    f"{ROOT_PACKAGE}.{layer}.thing": (
                        f"{ROOT_PACKAGE}.{COMPOSITION_LAYER}.root",
                    )
                }
            )
            result = ArchitectureValidator().validate(source)
            assert [
                violation.rule_id for violation in result.violations
            ] == [FORBIDDEN_LAYER_IMPORT], layer

    def test_application_still_may_not_import_architecture_or_infrastructure(
        self,
    ) -> None:
        source = source_model(
            {
                f"{ROOT_PACKAGE}.application.rogue": (
                    f"{ROOT_PACKAGE}.architecture.rules",
                    f"{ROOT_PACKAGE}.infrastructure.storage",
                )
            }
        )
        result = ArchitectureValidator().validate(source)
        assert result.is_compliant is False
        assert {violation.rule_id for violation in result.violations} == {
            FORBIDDEN_LAYER_IMPORT
        }
        assert {violation.target for violation in result.violations} == {
            f"{ROOT_PACKAGE}.architecture.rules",
            f"{ROOT_PACKAGE}.infrastructure.storage",
        }

    def test_composition_is_the_only_layer_allowed_to_import_architecture(
        self,
    ) -> None:
        for layer in V1_KNOWN_LAYERS:
            source = source_model(
                {
                    f"{ROOT_PACKAGE}.{layer}.thing": (
                        f"{ROOT_PACKAGE}.architecture.rules",
                    )
                }
            )
            result = ArchitectureValidator().validate(source)
            expected = layer == ARCHITECTURE_LAYER
            assert result.is_compliant is expected, layer

    def test_rule_parameterisation_is_per_version(self) -> None:
        """The same rule id enforces each version's own frozen matrix."""
        v1_unknown = ARCHITECTURE_V1.rule(UNKNOWN_LAYER).check(
            source_model({f"{ROOT_PACKAGE}.{COMPOSITION_LAYER}.root": ()})
        )
        current_unknown = ARCHITECTURE_CURRENT.rule(UNKNOWN_LAYER).check(
            source_model({f"{ROOT_PACKAGE}.{COMPOSITION_LAYER}.root": ()})
        )
        assert len(v1_unknown) == 1
        assert current_unknown == ()
        assert v1_unknown is not current_unknown
