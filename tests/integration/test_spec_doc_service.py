"""Integration tests for SpecDocService fetch/parse/toc (Task 9)."""
import io
import zipfile

import pytest
from docx import Document

from doc3gpp.models.spec import SpecVersion
from doc3gpp.models.spec_doc import (
    SpecDocNoDocxError,
    SpecDocUnknownVersionError,
)
from doc3gpp.services.spec_doc_service import SpecDocService
from doc3gpp.storage.db.migrate import create_schema


class FakeSpecRepo:
    def __init__(self, versions):
        self._v = versions

    def list_versions(self, spec_id, **kw):
        return self._v.get(spec_id, [])


def _make_zip_bytes(paragraphs=("hello handover world",), name="38331-j30.docx"):
    doc = Document()
    doc.add_heading("1 Scope", level=1)
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    docx = buf.getvalue()
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr(name, docx)
    return zb.getvalue()


def _versions():
    return {
        "38.331": [
            SpecVersion(
                "38.331",
                "18.5.0",
                "https://www.3gpp.org/ftp/x/38331.zip",
                "Rel-18",
            )
        ]
    }


def test_parse_many_buckets_and_identity_skip(sqlite_env):
    zip_bytes = _make_zip_bytes()
    versions = _versions()
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(versions),
        fetcher=lambda url: zip_bytes,
        cache_dir=sqlite_env.parent / "speccache",
    )
    create_schema("all")
    r1 = svc.parse_many(["38.331"])
    assert "38.331" in r1.successes
    r2 = svc.parse_many(["38.331"])
    assert "38.331" in r2.skipped  # immutable: second parse skips
    toc = svc.get_toc("38.331", "18.5.0")
    assert toc.entries and toc.entries[0].section_no == "1"


def test_post_chunk_cache_failure_does_not_mark_source_parsed(sqlite_env, monkeypatch):
    create_schema("all")
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=lambda url: _make_zip_bytes(),
        cache_dir=sqlite_env.parent / "speccache",
    )
    original_write = svc._write_markdown_cache
    calls = 0

    def fail_once(spec_id, version, ordered):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("markdown cache unavailable")
        return original_write(spec_id, version, ordered)

    monkeypatch.setattr(svc, "_write_markdown_cache", fail_once)

    first = svc.parse_many(["38.331"])
    assert "38.331" in first.failures
    source = svc.get_source("38.331", "18.5.0")
    assert source is not None and source.parsed_at is None

    second = svc.parse_many(["38.331"])
    assert "38.331" in second.successes
    assert calls == 2


def test_fetch_skip_uses_zip_cache_without_network(sqlite_env):
    create_schema("all")
    zip_bytes = _make_zip_bytes()
    cache_dir = sqlite_env.parent / "speccache"
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=lambda url: zip_bytes,
        cache_dir=cache_dir,
    )
    svc.parse_many(["38.331"])

    def _boom(url):
        raise AssertionError(f"network must not be touched: {url}")

    svc2 = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()), fetcher=_boom, cache_dir=cache_dir
    )
    src = svc2.fetch("38.331")
    assert src.version == "18.5.0" and src.parsed_at is not None


def test_oversized_zip_lands_in_skipped(sqlite_env):
    from doc3gpp.settings.schema import Settings, SpecDocSettings

    create_schema("all")
    settings = Settings(spec_doc=SpecDocSettings(max_zip_size_kb=1))
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=lambda url: _make_zip_bytes(),
        cache_dir=sqlite_env.parent / "speccache",
        settings=settings,
    )
    result = svc.parse_many(["38.331"])
    assert "38.331" in result.skipped
    assert "38.331" not in result.successes


def test_unknown_spec_lands_in_failures(sqlite_env):
    create_schema("all")
    svc = SpecDocService(
        spec_repo=FakeSpecRepo({}),
        fetcher=lambda url: b"",
        cache_dir=sqlite_env.parent / "speccache",
    )
    result = svc.parse_many(["99.999"])
    assert "99.999" in result.failures
    assert "spec sync" in result.failures["99.999"]


def test_unknown_version_lands_in_failures(sqlite_env):
    create_schema("all")
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=lambda url: _make_zip_bytes(),
        cache_dir=sqlite_env.parent / "speccache",
    )
    result = svc.parse_many(["38.331"], version="99.0")
    assert "38.331" in result.failures


def test_no_docx_lands_in_failures(sqlite_env):
    create_schema("all")
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("readme.txt", b"no docx here")
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=lambda url: zb.getvalue(),
        cache_dir=sqlite_env.parent / "speccache",
    )
    result = svc.parse_many(["38.331"])
    assert "38.331" in result.failures
    assert "no .docx" in result.failures["38.331"]
    with pytest.raises(SpecDocNoDocxError):
        svc.parse("38.331", force=True)


def test_force_reparses_and_redownloads(sqlite_env):
    create_schema("all")
    calls: list[str] = []

    def _counting_fetcher(url):
        calls.append(url)
        return _make_zip_bytes()

    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=_counting_fetcher,
        cache_dir=sqlite_env.parent / "speccache",
    )
    r1 = svc.parse_many(["38.331"])
    assert "38.331" in r1.successes
    assert len(calls) == 1
    r2 = svc.parse_many(["38.331"], force=True)
    assert "38.331" in r2.successes
    assert len(calls) == 2


def test_get_toc_miss_raises(sqlite_env):
    create_schema("all")
    svc = SpecDocService(
        spec_repo=FakeSpecRepo(_versions()),
        fetcher=lambda url: _make_zip_bytes(),
        cache_dir=sqlite_env.parent / "speccache",
    )
    with pytest.raises(SpecDocUnknownVersionError):
        svc.get_toc("38.331", "18.5.0")
