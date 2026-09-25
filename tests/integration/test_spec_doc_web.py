from fastapi.testclient import TestClient
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_app
from doc3gpp.settings.loader import get_settings


def test_spec_doc_schema_route(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings())) as client:
        r = client.get("/spec-docs/schema?format=json")
    assert r.status_code == 200 and any(f["table"] == "spec_doc_chunks" for f in r.json())


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
            json={"spec_ids": []},
        )
        fetch_response = client.get("/spec-docs/fetch")

    assert parse_response.status_code == 400
    assert "parse/spec-docs" in parse_response.json()["detail"]
    assert fetch_response.status_code == 404


def test_spec_doc_search_form_uses_full_width_query_and_plural_metadata(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings())) as client:
        response = client.get("/spec-docs/search")

    assert response.status_code == 200
    assert 'label class="span-5">Query' in response.text
    assert 'name="sections"' in response.text
    assert 'name="tables"' in response.text
