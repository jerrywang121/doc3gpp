"""SQLAlchemy-backed implementation of :class:`SpecRepository`."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.storage.db.models import SpecORM, SpecVersionORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.rich_filters import apply_text_filter


class SQLAlchemySpecRepository:
    """SQLAlchemy implementation that stores rows in ``specs`` / ``spec_versions``."""

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def upsert(self, spec: Spec) -> None:
        with self._session_factory() as session:
            existing = session.get(SpecORM, spec.spec_id)
            if existing is not None:
                existing.type = spec.type
                existing.title = spec.title
                existing.status = spec.status
                existing.radio_tech = spec.radio_tech
                existing.initial_release = spec.initial_release
                existing.tsg = spec.tsg
                existing.wis = spec.wis
                if spec.last_synced_at is not None:
                    existing.last_synced_at = spec.last_synced_at
            else:
                session.add(
                    SpecORM(
                        spec_id=spec.spec_id,
                        type=spec.type,
                        title=spec.title,
                        status=spec.status,
                        radio_tech=spec.radio_tech,
                        initial_release=spec.initial_release,
                        tsg=spec.tsg,
                        wis=spec.wis,
                        last_synced_at=spec.last_synced_at,
                    )
                )
            session.commit()

    def upsert_versions(self, versions: list[SpecVersion]) -> int:
        if not versions:
            return 0
        # The 3GPP DynaReport spec detail page lists the same
        # ``(spec_id, version)`` row more than once when a spec has
        # been re-uploaded (different ``upload_date`` / comment /
        # ``version_id``). The PK is composite on ``(spec_id, version)``
        # and the semantically-correct row is the most recent
        # re-upload, so collapse duplicates here before any
        # ``session.get`` / ``session.add`` work — the
        # ``autoflush=False`` session used in production would
        # otherwise miss the pending insert on the second iteration
        # and queue two ``INSERT`` statements for the same PK.
        deduped = _dedupe_versions(versions)
        with self._session_factory() as session:
            for v in deduped:
                existing = session.get(SpecVersionORM, (v.spec_id, v.version))
                if existing is not None:
                    existing.ftp_url = v.ftp_url
                    existing.release = v.release
                    existing.meeting_id = v.meeting_id
                    existing.meeting_name = v.meeting_name
                    existing.upload_date = v.upload_date
                    existing.version_id = v.version_id
                    if v.pdf_url is not None:
                        existing.pdf_url = v.pdf_url
                    if v.crs is not None:
                        existing.crs = v.crs
                    existing.comment = v.comment
                else:
                    session.add(
                        SpecVersionORM(
                            spec_id=v.spec_id,
                            version=v.version,
                            ftp_url=v.ftp_url,
                            release=v.release,
                            meeting_id=v.meeting_id,
                            meeting_name=v.meeting_name,
                            upload_date=v.upload_date,
                            version_id=v.version_id,
                            pdf_url=v.pdf_url,
                            crs=v.crs,
                            comment=v.comment,
                        )
                    )
            session.commit()
        return len(deduped)

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        type: str | None = None,
        spec_id: str | None = None,
        title: str | None = None,
        status: str | None = None,
        radio_tech: str | None = None,
        initial_release: str | None = None,
        wis: str | None = None,
    ) -> list[Spec]:
        with self._session_factory() as session:
            stmt = select(SpecORM)
            if tsg:
                stmt = stmt.where(SpecORM.tsg == tsg.upper())
            if type:
                stmt = stmt.where(SpecORM.type == type.upper())
            if spec_id:
                stmt = apply_text_filter(stmt, SpecORM.spec_id, spec_id)
            if title:
                stmt = apply_text_filter(stmt, SpecORM.title, title)
            if status:
                stmt = apply_text_filter(stmt, SpecORM.status, status)
            if radio_tech:
                stmt = apply_text_filter(stmt, SpecORM.radio_tech, radio_tech)
            if initial_release:
                stmt = apply_text_filter(stmt, SpecORM.initial_release, initial_release)
            if wis:
                stmt = apply_text_filter(stmt, SpecORM.wis, wis)
            stmt = stmt.order_by(SpecORM.spec_id).offset(offset).limit(limit)
            rows = session.scalars(stmt).all()
        return [_orm_to_spec(r) for r in rows]

    def get(self, spec_id: str) -> Spec | None:
        with self._session_factory() as session:
            row = session.get(SpecORM, spec_id)
        return _orm_to_spec(row) if row is not None else None

    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[SpecVersion]:
        with self._session_factory() as session:
            stmt = (
                select(SpecVersionORM)
                .where(SpecVersionORM.spec_id == spec_id)
                .order_by(SpecVersionORM.version.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
        return [_orm_to_version(r) for r in rows]


def _orm_to_spec(row: SpecORM) -> Spec:
    return Spec(
        spec_id=row.spec_id,
        type=row.type or "",
        title=row.title or "",
        status=row.status,
        radio_tech=row.radio_tech,
        initial_release=row.initial_release,
        tsg=row.tsg,
        wis=row.wis,
        last_synced_at=_as_utc(row.last_synced_at),
    )


def _orm_to_version(row: SpecVersionORM) -> SpecVersion:
    return SpecVersion(
        spec_id=row.spec_id,
        version=row.version,
        ftp_url=row.ftp_url,
        release=row.release,
        meeting_id=row.meeting_id,
        meeting_name=row.meeting_name,
        upload_date=row.upload_date,
        version_id=row.version_id,
        pdf_url=row.pdf_url,
        crs=row.crs,
        comment=row.comment,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, handling naive SQLite returns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedupe_versions(versions: list[SpecVersion]) -> list[SpecVersion]:
    """Collapse duplicate ``(spec_id, version)`` rows in ``versions``.

    The 3GPP DynaReport spec detail page lists the same version more
    than once when it has been re-uploaded (different ``upload_date``
    / ``comment`` / ``version_id``). The PK is composite on
    ``(spec_id, version)`` and the semantically-correct row is the
    most recent re-upload, so pick the one with the latest
    ``upload_date`` (ties broken by last-write-wins — preserves the
    parser's iteration order).
    """
    by_key: dict[tuple[str, str], SpecVersion] = {}
    for v in versions:
        key = (v.spec_id, v.version)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = v
            continue
        # Prefer the row with the newer upload_date. When both are
        # None (or equal), the later input wins — preserves the
        # parser's iteration order which matches the page's render
        # order.
        if _is_newer_upload(v, existing):
            by_key[key] = v
    return list(by_key.values())


def _is_newer_upload(candidate: SpecVersion, current: SpecVersion) -> bool:
    """Return ``True`` when ``candidate.upload_date`` supersedes
    ``current.upload_date`` for the purpose of choosing a winner.

    * Real date vs. real date — the strictly later one wins (equal
      dates still return ``True`` so the later input row wins, which
      preserves the parser's iteration order when the page renders
      two rows for the same version with identical upload dates).
    * Real date vs. ``None`` — the real date wins.
    * ``None`` vs. ``None`` — return ``True`` so last-write-wins
      produces a deterministic single-row outcome (avoids IntegrityError
      on the composite PK).
    """
    if candidate.upload_date is None and current.upload_date is None:
        return True
    if candidate.upload_date is None:
        return False
    if current.upload_date is None:
        return True
    return candidate.upload_date >= current.upload_date
