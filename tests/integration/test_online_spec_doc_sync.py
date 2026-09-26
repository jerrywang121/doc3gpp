"""Online integration test for the spec-document corpus (fetch -> parse -> toc -> search).

End-to-end against live 3gpp.org FTP for a small spec, resolving the
numeric-newest version at RUNTIME (no pins): versions roll forward
upstream, so the test calls ``SpecDocService.parse("38.331")`` with no
``version=`` and lets :func:`resolve_spec_doc_version` pick the newest
stored ``spec_versions`` row. The reference zips from the design review
(38.331 ``38331-j30.zip`` single-docx, 38.523-1 ``38523-1-j50.zip``
multi-docx section split) pin the *shape* of the corpus, not exact
versions. Run explicitly with::

    python -m pytest -m online -rs

The test skips itself unless the ``@pytest.mark.online`` marker is
selected (default pytest skips online tests via
``pyproject.toml [tool.pytest.ini_options]``) so the SQLite suite stays
self-contained. It also skips when the optional ``python-docx`` extra
is missing (``pip install doc3gpp[extract]``) or when the live
endpoints are unreachable from this environment.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.online


def _docx_available() -> bool:
    """Return True iff ``python-docx`` imports cleanly (needed to parse)."""
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_online_38331_fetch_parse(sqlite_env):
    """End-to-end against live 3GPP FTP: sync -> parse (default numeric-newest version) -> toc -> search."""
    from doc3gpp.models.spec_doc import SpecDocSearchFilters
    from doc3gpp.services.factory import (
        build_spec_doc_service,
        build_spec_service,
        build_tsg_service,
    )
    from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
    from doc3gpp.storage.db.migrate import create_schema

    create_schema("all")
    build_tsg_service().seed_defaults()
    try:
        build_spec_service().sync_spec("38.331")
    except httpx.HTTPError as exc:
        pytest.skip(f"online spec endpoints not reachable in this environment: {exc}")
    svc = build_spec_doc_service()
    try:
        src = svc.parse("38.331")  # default: numeric-newest version (e.g. 38331-j30.zip line)
    except httpx.HTTPError as exc:
        pytest.skip(f"online spec FTP not reachable in this environment: {exc}")
    assert src.chunk_count and src.chunk_count > 0
    toc = svc.get_toc("38.331", src.version)
    assert toc.entries and toc.files
    hits = SpecDocSearchService().search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert hits


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_online_385231_fetch_parse(sqlite_env):
    """Multi-file ordering path: 38.523-1 ships section-split .docx files.

    Same shape as :func:`test_online_38331_fetch_parse` but against the
    multi-docx corpus shape (the ``38523-1-j50.zip`` line): the parsed
    TOC must cover at least one source file.
    """
    from doc3gpp.services.factory import (
        build_spec_doc_service,
        build_spec_service,
        build_tsg_service,
    )
    from doc3gpp.storage.db.migrate import create_schema

    create_schema("all")
    build_tsg_service().seed_defaults()
    try:
        build_spec_service().sync_spec("38.523-1")
    except httpx.HTTPError as exc:
        pytest.skip(f"online spec endpoints not reachable in this environment: {exc}")
    svc = build_spec_doc_service()
    try:
        src = svc.parse("38.523-1")  # default: numeric-newest version
    except httpx.HTTPError as exc:
        pytest.skip(f"online spec FTP not reachable in this environment: {exc}")
    assert src.chunk_count and src.chunk_count > 0
    toc = svc.get_toc("38.523-1", src.version)
    assert toc.entries and len(toc.files) >= 1
