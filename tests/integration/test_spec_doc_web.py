from fastapi.testclient import TestClient

from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_app


def test_spec_doc_schema_route(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings())) as client:
        r = client.get("/spec-docs/schema?format=json")
    assert r.status_code == 200
    assert any(f["table"] == "spec_doc_chunks" for f in r.json())
    assert any(f["table"] == "spec_doc_zip_inventory" for f in r.json())


def test_spec_doc_toc_miss(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings())) as client:
        r = client.get("/specs/38.331/docs/toc?version=18.5.0&format=json")
    assert r.status_code in (400, 404)


def test_spec_doc_parse_job_exists_without_fetch_route(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings()), raise_server_exceptions=False) as client:
        parse_response = client.post(
            "/jobs/parse/spec-docs",
            json={"spec_id": ""},
        )
        fetch_response = client.get("/spec-docs/fetch")

    assert parse_response.status_code == 400
    assert "parse/spec-docs" in parse_response.json()["detail"]
    assert fetch_response.status_code == 404


def test_spec_doc_parse_job_accepts_scalar_spec_and_rejects_misaligned_selection(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings()), raise_server_exceptions=False) as client:
        accepted = client.post(
            "/jobs/parse/spec-docs",
            json={"spec_id": "38.331"},
        )
        rejected = client.post(
            "/jobs/parse/spec-docs",
            json={
                "spec_id": "38.331",
                "release": "Rel-18",
                "version": "17.5.0",
            },
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 400
    assert "does not match" in rejected.json()["detail"]


def test_spec_doc_search_form_uses_full_width_query_and_plural_metadata(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings())) as client:
        response = client.get("/spec-docs/search")

    assert response.status_code == 200
    assert 'label class="span-5" style="grid-column: span 5">Query' in response.text
    assert 'name="sections"' in response.text
    assert 'name="tables"' in response.text


def test_spec_doc_version_html_displays_inventory_independent_of_parse_state(
    sqlite_env, monkeypatch, tmp_path
) -> None:
    from doc3gpp.models.spec import Spec, SpecVersion
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.services.spec_doc_service import SpecDocService
    from doc3gpp.settings.schema import (
        CacheSettings,
        ServerSettings,
        Settings,
        SpecDocSettings,
    )
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )
    from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository

    create_schema("all")
    spec_id = "38.331"
    spec_repo = SQLAlchemySpecRepository()
    spec_repo.upsert(Spec(spec_id=spec_id, type="TS", title="NR RRC"))
    spec_repo.upsert_versions(
        [
            SpecVersion(
                spec_id=spec_id,
                version=version,
                ftp_url=f"https://example.test/{version}.zip",
                release=release,
            )
            for version, release in (
                ("19.0.0", "Rel-19"),
                ("18.5.0", "Rel-18"),
                ("17.0.0", "Rel-17"),
            )
        ]
    )
    inventory_repo = SQLAlchemySpecDocZipInventoryRepository()
    inventory_repo.upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="19.0.0",
            release="Rel-19",
            zip_files=(
                "a.docx",
                "a.docx",
                "Café.docx",
                "<script>alert(1)</script>",
            ),
        )
    )
    inventory_repo.upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="18.5.0",
            release="Rel-18",
            zip_files=(),
        )
    )

    settings = Settings(
        server=ServerSettings(enabled=True, port=8765),
        cache=CacheSettings(dir=tmp_path / "cache"),
        spec_doc=SpecDocSettings(cache_dir=tmp_path / "spec-doc-cache"),
    )
    cache_probe = SpecDocService(
        spec_repo=spec_repo,
        doc_repo=object(),
        inventory_repo=inventory_repo,
        settings=settings,
    )
    zip_cache = cache_probe._zip_path(spec_id, "19.0.0")
    zip_cache.parent.mkdir(parents=True, exist_ok=True)
    zip_cache.write_bytes(b"stale cached zip")
    zip_cache.unlink()
    assert not zip_cache.exists()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("spec detail reads must not download or parse ZIP files")

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.parse_spec_list", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.parse_spec_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_doc_service.fetch_spec_doc_zip", unexpected_call
    )
    monkeypatch.setattr(SpecDocService, "_read_zip_cache", unexpected_call)
    monkeypatch.setattr(SpecDocService, "prepare_parse", unexpected_call)

    with TestClient(build_app(settings)) as client:
        known = client.get(f"/specs/{spec_id}/docs?version=19.0.0")
        empty = client.get(f"/specs/{spec_id}/docs?version=18.5.0")
        unknown = client.get(f"/specs/{spec_id}/docs?version=17.0.0")
        htmx = client.get(
            f"/specs/{spec_id}/docs?version=19.0.0",
            headers={"HX-Request": "true"},
        )

    assert known.status_code == 200, known.text
    assert "This version has not been downloaded or parsed." in known.text
    assert "a.docx" in known.text
    assert known.text.count("<code>a.docx</code>") == 2
    assert "Café.docx" in known.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in known.text
    assert "<script>alert(1)</script>" not in known.text
    assert known.text.count('id="spec-doc-zip-inventory"') == 1
    assert known.text.index('id="spec-doc-zip-inventory"') < known.text.index(
        'id="results"'
    )

    assert empty.status_code == 200, empty.text
    assert "No files in this ZIP archive." in empty.text
    assert unknown.status_code == 200, unknown.text
    assert "ZIP inventory is unavailable for this version." in unknown.text
    assert (
        "doc3gpp spec doc parse --spec 38.331 --version 17.0.0 --force"
        in unknown.text
    )
    assert "doc3gpp db init --scope specdata" in unknown.text

    assert htmx.status_code == 200, htmx.text
    assert 'id="results"' in htmx.text
    assert "spec-doc-zip-inventory" not in htmx.text
    assert "a.docx" not in htmx.text
