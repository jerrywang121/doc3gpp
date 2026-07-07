"""SQLAlchemy-backed implementation of :class:`TDocFileRepository`."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.storage.db.models import TDocFileORM
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyTDocFileRepository:
    """SQLAlchemy implementation that stores auxiliary TDoc files.

    Identity is the unique ``url`` column: a file lives at exactly one
    upstream location on the 3GPP FTP, so re-syncing the same meeting
    is idempotent — existing rows are refreshed in place with the
    latest ``file`` label.
    """

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        """Initialize the repository.

        Args:
            session_factory: Optional pre-built ``sessionmaker``. When
                omitted the function falls back to
                :func:`doc3gpp.storage.db.session.get_session_factory`. The
                parameter is primarily used by unit tests that want to bind a
                repository to an in-memory SQLite engine.
        """
        self._session_factory = session_factory or get_session_factory()

    def upsert_many(self, files: list[TDocFile]) -> int:
        """Insert or update multiple TDocFile records.

        Each input is matched by its ``url``; matches update the
        ``tdoc_id``, ``type``, ``file`` and ``uploaded_date`` columns in
        place, while non-matches become new rows.

        Returns:
            The number of input rows that were written (insert or update).
        """
        if not files:
            return 0

        with self._session_factory() as session:
            urls = [item.url for item in files]
            existing_rows = session.scalars(
                select(TDocFileORM).where(TDocFileORM.url.in_(urls))
            ).all()
            existing_by_url = {row.url: row for row in existing_rows}

            for item in files:
                target = existing_by_url.get(item.url)
                if target is None:
                    target = TDocFileORM(
                        tdoc_id=item.tdoc_id,
                        type=item.type,
                        file=item.file,
                        url=item.url,
                        uploaded_date=item.uploaded_date,
                    )
                    session.add(target)
                else:
                    target.tdoc_id = item.tdoc_id
                    target.type = item.type
                    target.file = item.file
                    target.uploaded_date = item.uploaded_date

            session.commit()
        return len(files)

    def list(
        self,
        limit: int = 20,
        tdoc_id: str | None = None,
        file_type: str | None = None,
        file_type_in: Iterable[str] | None = None,
    ) -> list[TDocFile]:
        """Return stored TDocFile records ordered by descending primary key.

        The auto-increment ``id`` is monotonic with insertion order, so
        descending ``id`` is a stable approximation of "most recently
        written". Optional filters:
        - ``tdoc_id``: exact match against the owning TDoc identifier.
        - ``file_type``: exact match against the ``type`` column.
        - ``file_type_in``: iterable of allowed ``type`` values.
        """
        with self._session_factory() as session:
            stmt = select(TDocFileORM)

            if tdoc_id is not None:
                stmt = stmt.where(TDocFileORM.tdoc_id == tdoc_id)

            if file_type is not None:
                stmt = stmt.where(TDocFileORM.type == file_type)

            if file_type_in is not None:
                values = list(file_type_in)
                if not values:
                    return []
                stmt = stmt.where(TDocFileORM.type.in_(values))

            stmt = stmt.order_by(TDocFileORM.id.desc()).limit(limit)
            rows = session.scalars(stmt).all()

        return [_orm_to_domain(row) for row in rows]

    def delete_for_tdoc_ids(self, tdoc_ids: Iterable[str]) -> int:
        """Delete every TDocFile whose ``tdoc_id`` is in ``tdoc_ids``.

        Returns the number of rows deleted. Empty input is a no-op so the
        caller can pass an empty list without a guard.
        """
        ids = list(tdoc_ids)
        if not ids:
            return 0
        with self._session_factory() as session:
            stmt = delete(TDocFileORM).where(TDocFileORM.tdoc_id.in_(ids))
            result = session.execute(stmt)
            session.commit()
        return int(result.rowcount or 0)


def _orm_to_domain(row: TDocFileORM) -> TDocFile:
    """Map an ORM row to a TDocFile dataclass."""
    return TDocFile(
        id=row.id,
        tdoc_id=row.tdoc_id,
        type=row.type,
        file=row.file,
        url=row.url,
        uploaded_date=row.uploaded_date,
    )
