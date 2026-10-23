"""Layering guard: the TDoc search facade and semantic search service never import storage."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICES_DIR = Path(__file__).resolve().parents[2] / "src" / "doc3gpp" / "services"
GUARDED_SERVICES = ("tdoc_search_facade.py", "semantic_search_service.py")


def _absolute_module(node: ast.ImportFrom, package: str) -> str:
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    return ".".join([*parts, node.module] if node.module else parts)


def _storage_imports(source: str, package: str = "doc3gpp.services") -> list[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(node, package)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return sorted(
        name
        for name in found
        if name == "doc3gpp.storage" or name.startswith("doc3gpp.storage.")
    )


def test_scanner_flags_storage_imports_in_every_form() -> None:
    source = (
        "import doc3gpp.storage.db.session\n"
        "from doc3gpp.storage.repositories.vector_sql import x\n"
        "from ..storage.repositories import y\n"
        "from doc3gpp import storage\n"
        "def f():\n"
        "    from doc3gpp.storage.db.migrate import z\n"
    )

    found = _storage_imports(source)

    assert {
        "doc3gpp.storage",
        "doc3gpp.storage.db.session",
        "doc3gpp.storage.db.migrate",
        "doc3gpp.storage.repositories",
        "doc3gpp.storage.repositories.vector_sql",
    } <= set(found)


def test_scanner_ignores_non_storage_imports() -> None:
    source = "from doc3gpp.models.search import SearchFilters\nimport json\n"

    assert _storage_imports(source) == []


@pytest.mark.parametrize("filename", GUARDED_SERVICES)
def test_guarded_service_imports_no_storage_code(filename: str) -> None:
    source = (SERVICES_DIR / filename).read_text(encoding="utf-8")

    assert _storage_imports(source) == []
