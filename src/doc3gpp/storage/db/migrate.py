from __future__ import annotations

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocCrDetailOrm  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocExtractOrm  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocFileORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TsgORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import WiORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.session import get_engine


def create_schema() -> None:
    """Create database tables for configured backend."""

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
