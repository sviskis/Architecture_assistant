"""Architecture Assistant GUI - the operator's control panel (v1.1).

This package is an **external host / presentation application**: it depends on
the assistant (``architecture_assistant_gui -> architecture_assistant``) and the
frozen core never depends on it. It is deliberately *outside* the assistant's
scanned source tree, so the architecture baseline (v1.1, 56 modules, three
deterministic rules) is untouched by this package.

What it may do
--------------
* call the assistant's public :mod:`architecture_assistant.composition` API;
* render plain ``dict`` / ``list`` / scalar payloads that the core worker
  produced from the Monitor, the reporting projection and the run results;
* ask the operator for an actor and a reason before any human write.

What it may never do
--------------------
* write SQLite, open a connection, or hold a repository or any core object in
  the Tk thread;
* change a step, bypass the authoritative transition table or force a state;
* duplicate workflow, review or compliance logic;
* decide anything: the core stays authoritative and every action must still fail
  closed there, so the GUI's enable/disable logic is *advisory* only.

Entry point: ``python -m architecture_assistant_gui``.
"""

from __future__ import annotations

__all__: list[str] = []
