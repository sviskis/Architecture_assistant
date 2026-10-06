import json

import pytest

from architecture_assistant_gui.workspace import execution_plan


def draft(modules):
    return execution_plan(
        {"status": "APPROVED", "project": "demo", "modules": modules},
        {"name": "demo", "mode": "MANUAL", "plan_version": "1.0"},
    )


def test_text_modules_preserve_responsibilities_without_mutating_proposal():
    modules = [{"text": "Validator: Check EPUB: preserve content."}]
    plan = json.loads(draft(modules))
    assert plan["steps"][1]["title"] == "Implement Validator"
    assert "Check EPUB: preserve content." in plan["steps"][1]["description"]
    assert modules == [{"text": "Validator: Check EPUB: preserve content."}]


@pytest.mark.parametrize("modules", [
    [{"text": "No explicit name separator"}],
    [{"text": ": missing name"}],
    [{"text": "Validator: first"}, {"name": "Validator"}],
])
def test_invalid_or_duplicate_names_still_rejected(modules):
    with pytest.raises(ValueError, match="unique, non-empty"):
        draft(modules)


def test_structured_modules_keep_dependency_order():
    plan = json.loads(draft([
        {"name": "UI", "dependencies": ["Core"]},
        {"name": "Core", "responsibility": "Process files"},
    ]))
    assert [step["title"] for step in plan["steps"][1:3]] == [
        "Implement Core", "Implement UI",
    ]
