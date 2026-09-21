"""Boundary tests for the GUI package (v1.1).

The GUI is an **external host**: it depends on the assistant
(``architecture_assistant_gui -> architecture_assistant``) and the frozen core
must never depend on it. It also stays outside the assistant's scanned source
tree, so the architecture baseline is untouched. These tests pin all of that
statically, plus the two rules that must never break: the GUI performs no
database writes and cannot reach ``VERIFIED``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ArchitectureValidator,
    scan_directory,
)

#: The GUI package under test.
GUI_PACKAGE = "architecture_assistant_gui"
GUI_ROOT = Path(__file__).resolve().parents[1] / "src" / GUI_PACKAGE

#: The assistant's package: the only tree the architecture gate scans.
CORE_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
)

#: The only assistant modules the GUI may import (its whole coupling surface).
ALLOWED_CORE_IMPORTS = frozenset(
    {
        "architecture_assistant.composition",
        "architecture_assistant.domain.enums",
    }
)

#: Source fragments that would mean a database handle or a repository write.
#: Deliberately narrow: ``.append(`` alone would also match a UI log deque, and
#: the rule is about *state* writes, not about local lists.
FORBIDDEN_FRAGMENTS = (
    "sqlite3",
    "audit.append",
    "audit.upsert",
    "audit.delete",
    "steps.upsert",
    "steps.delete",
    "tasks.upsert",
    "tasks.delete",
    "projects.upsert",
    "projects.delete",
    "execute(",
    "commit(",
    "INSERT ",
    "UPDATE ",
    "DELETE ",
)


def _gui_sources() -> dict[Path, str]:
    """Every GUI source file, keyed by path."""
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(GUI_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _imported_roots(text: str) -> set[str]:
    """Every absolute module name imported by one source file."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add(f"{GUI_PACKAGE}.{node.module or ''}")
            elif node.module:
                roots.add(node.module)
    return roots


class TestDependencyDirection:
    """GUI -> core is allowed; core -> GUI is forbidden."""

    def test_the_gui_package_exists_and_is_outside_the_core(self) -> None:
        assert GUI_ROOT.is_dir()
        assert GUI_ROOT.parent.name == "src"
        assert GUI_ROOT.name != CORE_ROOT.name

    def test_every_gui_module_imports_only_stdlib_core_and_itself(self) -> None:
        allowed_core = ALLOWED_CORE_IMPORTS
        for path, text in _gui_sources().items():
            for module in _imported_roots(text):
                root = module.split(".")[0]
                if root in sys.stdlib_module_names or root == GUI_PACKAGE:
                    continue
                assert root == "architecture_assistant", (path, module)
                assert module in allowed_core, (path, module)

    def test_the_gui_never_imports_a_repository_or_the_database(self) -> None:
        forbidden = (
            "architecture_assistant.infrastructure",
            "architecture_assistant.ports",
            "architecture_assistant.architecture",
        )
        for path, text in _gui_sources().items():
            for module in _imported_roots(text):
                assert not module.startswith(forbidden), (path, module)

    def test_the_frozen_core_never_imports_the_gui(self) -> None:
        source = scan_directory(CORE_ROOT)
        for module in source.modules():
            for target in source.imports_of(module):
                assert not target.startswith(GUI_PACKAGE), (module, target)

    def test_the_core_source_never_mentions_the_gui_package(self) -> None:
        for path in sorted(CORE_ROOT.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert GUI_PACKAGE not in text, path

    def test_the_gui_is_not_part_of_the_scanned_architecture(self) -> None:
        modules = scan_directory(CORE_ROOT).modules()

        assert modules
        assert not [
            name
            for name in modules
            if GUI_PACKAGE in name or name.endswith(".gui")
        ]

    def test_the_architecture_self_check_is_still_compliant(self) -> None:
        source = scan_directory(CORE_ROOT)
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
        # The frozen core: 65 modules (Step 25 added the plan loader, Step 26 the
        # advisory architecture review, Step 27 the log-event contract and the
        # managed-project proposal pair, Step 28 the advisory supervision quartet:
        # the policy, the use-case with its gate, the headless runtime and the
        # offline scripted supervisor), three rules, baseline 1.1.
        assert len(source.modules()) == 65
        assert len(ArchitectureValidator().baseline.rules) == 3


class TestNoWritePath:
    """The panel reads and calls the core API - it never writes state itself."""

    def test_no_gui_module_mentions_sqlite_or_a_write_call(self) -> None:
        for path, text in _gui_sources().items():
            for fragment in FORBIDDEN_FRAGMENTS:
                assert fragment not in text, (path, fragment)

    def test_the_audit_panel_only_ever_reads(self) -> None:
        text = (GUI_ROOT / "core.py").read_text(encoding="utf-8")

        assert "storage.audit.list()" in text
        for writer in ("audit.append", "audit.update", "audit.delete"):
            assert writer not in text, writer

    def test_the_gui_cannot_reach_verified(self) -> None:
        from architecture_assistant_gui.controller import INTENTS

        intents = (GUI_ROOT / "controller.py").read_text(encoding="utf-8")
        actions = (GUI_ROOT / "core.py").read_text(encoding="utf-8")

        assert "VERIFY" not in intents
        assert "VERIFY" not in actions
        assert not [intent for intent in INTENTS if "verify" in intent.key]

    def test_both_human_write_paths_stay_in_the_core(self) -> None:
        """The GUI calls the core's human paths; it owns neither of them."""
        from architecture_assistant_gui.controller import INTENTS

        keys = {intent.key for intent in INTENTS}

        assert keys == {
            "load_plan",
            "import_plan",
            "run_review",
            "synthesize_proposal",
            "approve_proposal",
            "request_proposal_revision",
            "reject_proposal",
            "run_until_idle",
            "pause_project",
            "resume_project",
            "approve",
            "reject",
            "unblock",
            "resolve",
            "abort",
            "export_markdown",
            "export_excel",
            "open_reports_folder",
            "refresh",
            "view_snapshot",
            "reconnect",
            "analyze_report",
            "approve_and_send",
            "reject_directive",
            "waive_supervision",
            "escalate_supervision",
        }
        # Every mutating action is one core call, spelled out in the controller.
        actions = (GUI_ROOT / "controller.py").read_text(encoding="utf-8")
        for call in (
            "worker.run_until_idle()",
            "worker.pause_project(",
            "worker.resume_project(",
            "worker.approve(",
            "worker.reject(",
            "worker.unblock(",
            "worker.resolve(",
            "worker.abort(",
            "worker.export_markdown()",
            "worker.export_excel()",
            "worker.reconnect()",
            "worker.preview_plan(",
            "worker.import_plan(",
            "worker.run_architecture_review(",
            "worker.analyze_report()",
            "worker.approve_and_send(",
            "worker.reject_directive(",
            "worker.waive_supervision(",
            "worker.escalate_supervision(",
        ):
            assert call in actions, call


class TestEntryPoint:
    """The documented one-command entry point exists and parses its options."""

    def test_the_module_entry_point_parses_help_without_a_display(self) -> None:
        pytest.importorskip("tkinter")
        from architecture_assistant_gui.app import parse_args

        with pytest.raises(SystemExit) as exit_info:
            parse_args(["--help"])

        assert exit_info.value.code == 0

    def test_an_unknown_mode_is_rejected(self) -> None:
        from architecture_assistant_gui.app import parse_args

        with pytest.raises(SystemExit) as exit_info:
            parse_args(["--mode", "SIDEWAYS"])

        assert exit_info.value.code == 2

    def test_the_cli_defaults_come_from_the_core_config(self) -> None:
        from architecture_assistant.composition import CompositionConfig
        from architecture_assistant_gui.app import build_config, parse_args

        defaults = CompositionConfig()
        resolved = build_config(parse_args([]))

        assert resolved.database_path == defaults.database_path
        assert resolved.mode is defaults.mode
        assert resolved.report_dir == Path(defaults.report_dir)

    def test_the_cli_mode_only_seeds_a_new_project(self, tmp_path) -> None:
        """Decision 5: startup mode never overwrites a persisted mode."""
        from architecture_assistant_gui.app import build_config, parse_args
        from architecture_assistant_gui.core import CoreWorker

        config = build_config(
            parse_args(
                [
                    "--mode",
                    "AUTO",
                    "--database",
                    str(tmp_path / "assistant.db"),
                    "--exchange-dir",
                    str(tmp_path / "cline"),
                    "--report-dir",
                    str(tmp_path / "reports"),
                    "--source-root",
                    str(CORE_ROOT),
                ]
            )
        )
        worker = CoreWorker(config)
        worker.open()
        try:
            assert worker.payload()["project"]["mode"] == "AUTO"
        finally:
            worker.close()

        # Re-opening with a different mode leaves the persisted mode alone.
        other = CoreWorker(
            build_config(
                parse_args(
                    [
                        "--mode",
                        "MANUAL",
                        "--database",
                        str(tmp_path / "assistant.db"),
                        "--exchange-dir",
                        str(tmp_path / "cline"),
                        "--report-dir",
                        str(tmp_path / "reports"),
                        "--source-root",
                        str(CORE_ROOT),
                    ]
                )
            )
        )
        other.open()
        try:
            assert other.payload()["project"]["mode"] == "AUTO"
        finally:
            other.close()


    def test_supervision_is_off_unless_the_operator_asks_for_it(self) -> None:
        """The opt-in is explicit: no flag, no supervision, no polling."""
        from architecture_assistant_gui.app import build_config, parse_args

        assert build_config(parse_args([])).supervision is False
        assert build_config(parse_args(["--supervision"])).supervision is True

    def test_the_supervision_flag_reaches_the_core(self, tmp_path) -> None:
        """The one switch the panel offers must actually enable the gate."""
        from architecture_assistant_gui.app import build_config, parse_args
        from architecture_assistant_gui.core import CoreWorker

        config = build_config(
            parse_args(
                [
                    "--supervision",
                    "--database",
                    str(tmp_path / "assistant.db"),
                    "--exchange-dir",
                    str(tmp_path / "cline"),
                    "--report-dir",
                    str(tmp_path / "reports"),
                    "--source-root",
                    str(CORE_ROOT),
                ]
            )
        )
        worker = CoreWorker(config)
        worker.open()
        try:
            assert worker.supervisor_status()["enabled"] is True
        finally:
            worker.close()

        off = CoreWorker(
            build_config(
                parse_args(
                    [
                        "--database",
                        str(tmp_path / "other.db"),
                        "--exchange-dir",
                        str(tmp_path / "cline"),
                        "--report-dir",
                        str(tmp_path / "reports"),
                        "--source-root",
                        str(CORE_ROOT),
                    ]
                )
            )
        )
        off.open()
        try:
            assert off.supervisor_status()["enabled"] is False
        finally:
            off.close()


class TestWidgetModule:
    """The Tk modules import cleanly (no window is ever created here)."""

    def test_the_view_module_imports(self) -> None:
        pytest.importorskip("tkinter")
        from architecture_assistant_gui import views

        assert views.MainWindow is not None
        assert [heading for _key, heading, _width in views.AUDIT_COLUMNS]

    def test_the_application_module_imports_without_a_display(self) -> None:
        from architecture_assistant_gui import app

        assert callable(app.main)
        assert app.POLL_INTERVAL_MS >= 1
