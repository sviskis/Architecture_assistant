"""Plain presentation helpers and editable drafts; never workflow mutations."""
from __future__ import annotations

import json
from collections.abc import Mapping

SEATS = ("agent_a", "lead", "agent_b")
SEAT_LABELS = {"agent_a": "Architect A", "lead": "Lead architect", "agent_b": "Architect B"}
BRIEF_TEMPLATE = """Mērķis:

Kas jau ir projektā:

Ko nedrīkst mainīt:

Gatavības kritēriji:

"""


def readable(value):
    if isinstance(value, Mapping):
        return "\n".join(f"{str(k).replace('_', ' ').capitalize()}: {readable(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return "\n".join(f"• {readable(v)}" for v in value)
    return str(value or "—")


def proposal_document(proposal):
    if not proposal:
        return "No architecture yet. Start with your brief, then run the architect discussion."
    sections = [("ARCHITECTURE", proposal.get("summary")),
                ("Status", proposal.get("status")), ("Your requirement", proposal.get("requirement")),
                ("Design rationale", proposal.get("rationale")), ("Modules and responsibilities", proposal.get("modules")),
                ("Data flow", proposal.get("data_flows")), ("Dependencies", proposal.get("external_dependencies")),
                ("Rules", proposal.get("architecture_rules")), ("Proposed rules (not automatically enforced)", proposal.get("proposed_rules")),
                ("Risks and mitigation", proposal.get("risks")), ("Decisions", proposal.get("adr_candidates")),
                ("Implementation phases", proposal.get("implementation_phases")),
                ("Open questions", proposal.get("unresolved_questions"))]
    return "\n\n".join(f"{title}\n{'─' * 32}\n{readable(value)}" for title, value in sections)


def execution_plan(proposal, project):
    """Create a reviewable initial plan from an approved design, without I/O.

    Dependency order is preserved. Cycles are reported instead of silently
    producing an unsafe order. All acceptance criteria live in descriptions,
    because the established importer deliberately accepts a narrow schema.
    """
    if proposal.get("status") != "APPROVED":
        raise ValueError("Approve the architecture before preparing an execution plan.")
    if proposal.get("project") != project.get("name"):
        raise ValueError("The architecture belongs to another project.")
    modules = list(proposal.get("modules") or ())
    if not modules:
        raise ValueError("The approved architecture has no modules to implement.")
    by_name = {}
    for module in modules:
        name = str(module.get("name") or "").strip()
        if not name or name in by_name:
            raise ValueError("Architecture modules need unique, non-empty names.")
        by_name[name] = module
    ordered, visiting, visited = [], set(), set()

    def visit(name):
        if name in visiting:
            raise ValueError(f"Dependency cycle involving {name}; revise the architecture first.")
        if name in visited:
            return
        visiting.add(name)
        deps = by_name[name].get("dependencies") or ()
        if isinstance(deps, str):
            deps = [part.strip() for part in deps.split(",")]
        for dep in deps:
            if dep in by_name:
                visit(dep)
        visiting.remove(name)
        visited.add(name)
        ordered.append(by_name[name])

    for name in by_name:
        visit(name)
    steps = []
    provenance = f"Approved proposal {proposal.get('proposal_id')}, revision {proposal.get('revision_no')}."

    def add(phase, title, description, human=False):
        steps.append(dict(step_no=len(steps) + 1, phase=phase, title=title,
                          description=f"{provenance}\n{description}", risk="MEDIUM" if human else "LOW",
                          max_attempts=3, requires_human=human))

    add("CONTEXT", "Inspect the project and confirm implementation scope",
        f"Read the existing source, tests and project instructions. Record current behaviour.\n"
        f"Requirement: {proposal.get('requirement')}\n"
        f"Resolve these questions before implementation: {readable(proposal.get('unresolved_questions'))}\n"
        "Acceptance: source locations, test commands and a scoped implementation checklist are recorded; no code changed.", True)
    rules = readable(proposal.get("architecture_rules"))
    for module in ordered:
        add("CLINE", f"Implement {module['name']}",
            f"Responsibility: {module.get('responsibility', '')}\n"
            f"Dependencies: {readable(module.get('dependencies'))}\n"
            f"Boundaries: {module.get('boundary_notes', '')}\nRules: {rules}\n"
            "Acceptance: implement only this module's scope, add meaningful tests for its contract, "
            "run the relevant tests and report commands, outcomes and changed files. Report blockers instead of guessing.")
    add("TEST", "Integrate modules and check acceptance criteria",
        f"Data flows: {readable(proposal.get('data_flows'))}\n"
        f"Risks: {readable(proposal.get('risks'))}\n"
        f"Requirement: {proposal.get('requirement')}\n"
        "Acceptance: exercise the complete flow, run regression tests and report any unmet requirements.")
    add("FINAL", "Review the result and document handover",
        "Acceptance: compare the implementation with the approved design, document setup and usage, "
        "list test evidence and outstanding limitations. Request human acceptance.", True)
    if len(steps) > 500:
        raise ValueError("The draft exceeds the importer's 500-step limit.")
    return json.dumps({"schema_version": "1.0", "plan_version": project["plan_version"],
                       "project": {"name": project["name"], "mode": project["mode"]},
                       "steps": steps}, ensure_ascii=False, indent=2)


def cline_handoff(channel):
    if not channel.get("task_exists"):
        return "No dispatched task yet. Import a plan, then run the workflow until it waits for the worker."
    return (f"Work on the current Architecture Assistant task only.\n"
            f"Project source: {channel.get('source_root')}\n"
            f"Read the task JSON: {channel.get('task_path')}\n"
            f"Read its context_file and follow its instructions and report_schema.\n"
            f"If present, also read the current directive: {channel.get('directive_path')}\n"
            f"Implement only the task scope, run relevant tests and record actual results.\n"
            f"Write the report to: {channel.get('report_path')}\n"
            "Use the exact protocol/project/plan_version/step_no/attempt from the task. "
            "Publish JSON atomically using a temporary sibling file and rename. "
            "Do not edit the assistant database or archive reports. "
            "Report architecture questions and blockers; do not invent test outcomes. Stop after this task.")
