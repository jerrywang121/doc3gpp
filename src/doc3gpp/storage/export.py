from __future__ import annotations

import csv
from pathlib import Path

from doc3gpp.models.tdoc import TDocWithMeeting


def export_tdocs_csv(path: Path, records: list[TDocWithMeeting]) -> None:
    """Export TDoc records (with their meeting name) to CSV."""

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tdoc_id", "title", "meeting", "url"])
        for item in records:
            writer.writerow([
                item.tdoc.tdoc_id,
                item.tdoc.title,
                item.meeting_name or "",
                item.tdoc.url or "",
            ])
