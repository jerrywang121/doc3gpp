"""Direct-mode ``--from-path`` dispatch: type confirm via auto-sync.

Covers Blocker-7 rulings from the adjudicated Task 15 brief:

1. A single-file ``--from-path`` extracts the tdoc_id from the
   filename, fires ``trigger_auto_sync(tdoc_ids=[...])`` (gated on
   ``Settings.sync.auto_sync``), then confirms ``tdocs.type`` /
   ``tdocs.source`` from the stored row so the parser registry can
   dispatch on LS rows.
2. When the row is still missing after auto-sync (or auto-sync is
   disabled), the caller assumes CR — ``(None, None)``.
3. A filename with no TDoc-id pattern returns ``(None, None)`` and
   never triggers auto-sync.

All tests are monkeypatched — no network, no subprocess.
"""

from __future__ import annotations

from doc3gpp.cli import _resolve_direct_tdoc_type, _tdoc_parse_direct
from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _seed_tdocs_row(tdoc_id: str, *, type: str | None, source: str | None) -> None:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type=type, source=source)
    )


def test_direct_type_confirmed_from_tdocs_row(sqlite_env, monkeypatch) -> None:
    """An LS row seeded in ``tdocs`` drives the direct-parse dispatch."""
    create_schema()
    _seed_tdocs_row("R5-240001", type="LS", source="3GPP TSG RAN WG2")

    calls: list[tuple] = []

    def _record(**kwargs):
        calls.append(kwargs)
        return 0, 0

    monkeypatch.setattr("doc3gpp.cli_auto_sync.trigger_auto_sync", _record)

    resolved_type, resolved_source = _resolve_direct_tdoc_type("R5-240001.zip")
    assert resolved_type == "LS"
    assert resolved_source == "3GPP TSG RAN WG2"
    assert len(calls) == 1
    assert calls[0]["tdoc_ids"] == ["R5-240001"]


def test_direct_type_missing_row_assumes_cr(sqlite_env, monkeypatch) -> None:
    """No ``tdocs`` row after auto-sync → caller assumes CR."""
    create_schema()
    calls: list[tuple] = []

    def _record(**kwargs):
        calls.append(kwargs)
        return 0, 0

    monkeypatch.setattr("doc3gpp.cli_auto_sync.trigger_auto_sync", _record)

    resolved_type, resolved_source = _resolve_direct_tdoc_type("R5-240001.zip")
    assert (resolved_type, resolved_source) == (None, None)
    assert len(calls) == 1


def test_direct_type_no_id_in_filename(sqlite_env, monkeypatch) -> None:
    """A filename without a TDoc-id pattern never triggers auto-sync."""
    create_schema()
    calls: list[tuple] = []

    def _record(**kwargs):
        calls.append(kwargs)
        return 0, 0

    monkeypatch.setattr("doc3gpp.cli_auto_sync.trigger_auto_sync", _record)

    resolved_type, resolved_source = _resolve_direct_tdoc_type("notes.docx")
    assert (resolved_type, resolved_source) == (None, None)
    assert calls == []


def test_direct_parse_direct_threads_type(tmp_path, monkeypatch) -> None:
    """``_tdoc_parse_direct`` forwards the confirmed type/source to the service."""
    captured: dict = {}

    class _StubService:
        def extract_from_bytes(
            self,
            payload: bytes,
            filename: str | None = None,
            *,
            tdoc_id: str | None = None,
            ftp_url: str | None = None,
            tdoc_type: str | None = None,
            source: str | None = None,
            force: bool = False,
            full: bool = True,
            max_tdoc_size_bytes: int | None = None,
        ):
            captured["payload"] = payload
            captured["filename"] = filename
            captured["tdoc_type"] = tdoc_type
            captured["source"] = source
            from doc3gpp.models.tdoc_cr import DirectParseResult

            return DirectParseResult(
                source_kind="local",
                markdown=payload.decode("utf-8"),
                details=None,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id="R5-240001",
                tdoc_id_in_tdocs=False,
            )

    monkeypatch.setattr(
        "doc3gpp.cli._resolve_direct_tdoc_type",
        lambda from_path: ("LS", "3GPP TSG RAN WG2"),
    )
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda **kwargs: _StubService(),
    )

    src = tmp_path / "R5-240001.docx"
    src.write_bytes(b"stub payload")
    _tdoc_parse_direct(
        from_path=str(src),
        from_url=None,
        fmt="raw",
        output=None,
        full=False,
        max_tdoc_size_bytes=0,
        compact=False,
    )
    assert captured["tdoc_type"] == "LS"
    assert captured["source"] == "3GPP TSG RAN WG2"
