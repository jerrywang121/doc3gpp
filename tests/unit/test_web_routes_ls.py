"""Tests for the LS Cover card on the web TDoc detail page (Task 12).

The tdoc detail route composes a :class:`TDocShowRecord` (which
already carries the LS sidecar via ``repos.ls``) and the template
renders an "LS Cover" card for ``type == 'LS'`` rows — mutually
exclusive with the CR Cover page card. The route consumes the LS
repository through the :func:`get_ls_repository` dependency so tests
can swap it via ``dependency_overrides``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_job_repo, get_settings, get_tdoc_file_repo
from doc3gpp.web.routes.tdocs import get_ls_repository


def _make_ls_details(**overrides: object) -> TDocLSDetails:
    base = dict(
        ftp_url="tsg/ls/R5-240001.doc",
        tdoc_id="R5-240001",
        variant="3gpp",
        title="LS on foo",
        response_to_doc="R5-234567",
        response_to_group="RAN WG3",
        release="Release 17",
        work_item_name="5G_eHealth",
        work_item_code="WI-123456",
        source="3GPP TSG RAN WG2",
        to_groups="RAN WG3\nRAN WG4",
        cc_groups="SA WG2",
        attachments=(
            {"doc_number": "TR 38.901 v0.1.0 [draft]", "description": "draft TR"},
            {"doc_number": "TS 38.300 v17.1.0", "description": ""},
        ),
    )
    base.update(overrides)
    return TDocLSDetails(**base)


class _EmptyJobRepo:
    """No-op :class:`JobRepository` so the nav badge renders without a lifespan."""

    def list(self, *, limit: int = 50, status: Any = None) -> list[Any]:
        return []


class _StubLSRepo:
    """Stub :class:`LSParserRepository` returning a fixed sidecar."""

    def get_by_url(self, ftp_url: str) -> TDocLSDetails | None:
        return TDocLSDetails(
            ftp_url=ftp_url,
            tdoc_id="R5-240001",
            title="Stubbed LS title",
        )


def _build_app() -> FastAPI:
    settings = Settings()
    app = build_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_tdoc_file_repo] = lambda: MagicMock()
    app.dependency_overrides[get_job_repo] = lambda: _EmptyJobRepo()
    return app


def _seed_ls_row(*, with_sidecar: bool) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc", type="LS"),
    )
    if with_sidecar:
        SQLAlchemyLSParserRepository().upsert(_make_ls_details())


def test_ls_cover_card_renders_for_ls_row(sqlite_env: Any) -> None:
    """An LS row with a sidecar renders the LS Cover card with every field."""
    _seed_ls_row(with_sidecar=True)
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/tdocs/R5-240001")
    assert response.status_code == 200
    html = response.text
    assert "<h2>LS Cover</h2>" in html
    assert "<dt>Title</dt><dd>LS on foo</dd>" in html
    assert "<code>R5-234567</code>" in html
    assert "from RAN WG3" in html
    assert "<dt>Release</dt><dd>Release 17</dd>" in html
    assert "5G_eHealth" in html
    assert "<code>WI-123456</code>" in html
    assert "<dt>Source</dt><dd>3GPP TSG RAN WG2</dd>" in html
    assert '<span class="tag">RAN WG3</span>' in html
    assert '<span class="tag">RAN WG4</span>' in html
    assert '<span class="tag">SA WG2</span>' in html
    assert "<code>TR 38.901 v0.1.0 [draft]</code>" in html
    assert "draft TR" in html
    assert "<code>TS 38.300 v17.1.0</code>" in html
    assert "<h2>Cover page</h2>" not in html


def test_ls_cover_card_placeholder_without_sidecar(sqlite_env: Any) -> None:
    """An LS row without a sidecar renders the placeholder, not the CR card."""
    _seed_ls_row(with_sidecar=False)
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/tdocs/R5-240001")
    assert response.status_code == 200
    html = response.text
    assert "<h2>LS Cover</h2>" in html
    assert "No LS sidecar parsed yet" in html
    assert "doc3gpp tdoc parse --tdoc R5-240001" in html
    assert "<h2>Cover page</h2>" not in html


def test_cr_row_renders_cover_page_not_ls_card(sqlite_env: Any) -> None:
    """A CR row keeps the CR Cover card and never renders the LS card."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/26.001/R5-260001.zip", type="CR"),
    )
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    html = response.text
    assert "<h2>Cover page</h2>" in html
    assert "LS Cover" not in html


def test_ls_show_json_includes_ls_block(sqlite_env: Any) -> None:
    """``GET /tdocs/{id}?format=json`` carries the ``ls`` block."""
    _seed_ls_row(with_sidecar=True)
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/tdocs/R5-240001?format=json")
    assert response.status_code == 200
    body = response.json()
    assert body["ls"]["title"] == "LS on foo"
    assert body["ls"]["to_groups"] == "RAN WG3\nRAN WG4"
    assert body["ls"]["attachments"][0]["doc_number"] == "TR 38.901 v0.1.0 [draft]"


def test_ls_show_json_omits_ls_when_absent(sqlite_env: Any) -> None:
    """No sidecar row -> the ``ls`` key is omitted (not null)."""
    _seed_ls_row(with_sidecar=False)
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/tdocs/R5-240001?format=json")
    assert response.status_code == 200
    assert "ls" not in response.json()


def test_ls_repository_dep_is_wired(sqlite_env: Any) -> None:
    """Overriding ``get_ls_repository`` changes the rendered card.

    Proves the route consumes the injected dependency rather than a
    hard-coded repository instance.
    """
    _seed_ls_row(with_sidecar=False)
    app = _build_app()
    app.dependency_overrides[get_ls_repository] = lambda: _StubLSRepo()
    with TestClient(app) as client:
        response = client.get("/tdocs/R5-240001")
    assert response.status_code == 200
    assert "Stubbed LS title" in response.text
