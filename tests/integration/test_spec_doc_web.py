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
