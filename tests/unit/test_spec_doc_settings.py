from doc3gpp.settings.schema import Settings, SpecDocSettings


def test_spec_doc_defaults():
    s = SpecDocSettings()
    assert s.max_zip_size_kb == 0
    assert s.max_chunk_chars == 1500
    assert s.chunk_overlap is None
    assert s.auto_index_on_parse is True
    assert s.auto_embed_on_parse is True
    assert tuple(s.bm25_weights) == (5.0, 5.0, 5.0, 1.0, 1.0, 1.0)


def test_spec_doc_bm25_wrong_length_rejected():
    import pytest

    with pytest.raises(ValueError):
        SpecDocSettings(bm25_weights=[1.0, 2.0])


def test_settings_has_specdata_url():
    s = Settings()
    assert s.specdata_database_url is None
    assert s.spec_doc.max_chunk_chars == 1500


def test_resolve_specdata_sibling(tmp_path, monkeypatch):
    from doc3gpp.storage.db.session import resolve_specdata_database_url
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setenv(
        "DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/doc3gpp.db"
    )
    get_settings.cache_clear()
    try:
        url = resolve_specdata_database_url()
        assert url.endswith("doc3gpp_specdata.db")
    finally:
        get_settings.cache_clear()


def test_create_schema_specdata_scope(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from sqlalchemy import inspect

    from doc3gpp.storage.db.session import get_specdata_engine

    create_schema("specdata")
    tables = set(inspect(get_specdata_engine()).get_table_names())
    assert "spec_doc_sources" in tables
    assert "spec_doc_chunks" in tables
    assert "spec_doc_tocs" in tables
