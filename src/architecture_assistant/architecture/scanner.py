"""Deterministic source scanning: build a :class:`SourceModel` from Python code.

The scanner uses the standard-library :mod:`ast` module only. Relative imports
are resolved to absolute module names so that layer rules can be evaluated
against real module paths.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Mapping, Optional

from .model import SourceModel
from .rules import ROOT_PACKAGE

__all__ = [
    "module_name_for",
    "extract_imports",
    "build_source_model",
    "discover_python_files",
    "scan_directory",
]


def module_name_for(
    path: Path,
    root: Path,
    root_package: str = ROOT_PACKAGE,
) -> tuple[str, bool]:
    """Return ``(module_name, is_package)`` for a file below ``root``.

    ``.../architecture_assistant/domain/models.py``
        -> ``("architecture_assistant.domain.models", False)``
    ``.../architecture_assistant/domain/__init__.py``
        -> ``("architecture_assistant.domain", True)``
    """
    relative = Path(path).relative_to(Path(root)).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join([root_package, *parts]), is_package


def _package_of(module: str, is_package: bool) -> str:
    """The package a module lives in (a package is its own container)."""
    if is_package:
        return module
    return module.rsplit(".", 1)[0]


def _resolve_import_from(node: ast.ImportFrom, package: str) -> Optional[str]:
    """Resolve an ``ImportFrom`` node to an absolute module name."""
    if node.level == 0:
        return node.module
    parts = package.split(".")
    # level 1 -> the current package; level N -> N-1 levels up
    keep = len(parts) - (node.level - 1)
    if keep < 1:
        return None  # relative import beyond the root package
    base = parts[:keep]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def extract_imports(text: str, module: str, is_package: bool) -> frozenset[str]:
    """Return the absolute module names imported by one source file."""
    tree = ast.parse(text, filename=module)
    package = _package_of(module, is_package)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import_from(node, package)
            if resolved:
                found.add(resolved)
    return frozenset(found)


def discover_python_files(root: Path) -> tuple[Path, ...]:
    """All ``*.py`` files below ``root``, in deterministic order."""
    root = Path(root)
    return tuple(sorted(root.rglob("*.py"), key=str))


def build_source_model(
    files: Mapping[Path, str],
    root: Path,
    root_package: str = ROOT_PACKAGE,
) -> SourceModel:
    """Build a :class:`SourceModel` from ``{path: source_text}``.

    Pure with respect to the filesystem - callers supply the source text, which
    keeps the model builder fully testable.
    """
    root = Path(root)
    imports: dict[str, frozenset[str]] = {}
    for path in sorted(files, key=str):
        module, is_package = module_name_for(Path(path), root, root_package)
        imports[module] = extract_imports(files[path], module, is_package)
    return SourceModel(imports=imports, root_package=root_package)


def scan_directory(root: Path, root_package: str = ROOT_PACKAGE) -> SourceModel:
    """Read every ``*.py`` file below ``root`` and build the source model."""
    root = Path(root)
    files = {
        path: path.read_text(encoding="utf-8")
        for path in discover_python_files(root)
    }
    return build_source_model(files, root, root_package)
