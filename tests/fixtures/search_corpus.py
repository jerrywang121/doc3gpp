"""Shared fixture corpus for the FTS5 search tests.

10 rows spanning the variety of shapes the ``tdoc_search`` index needs
to handle:

* Plenary (RP-..., SP-...) and per-group (R5-..., S2-...) TDoc ids
* 2 with TTCN sidecars (``tdoc_cr_ttcn_details`` rows)
* 1 with a change-details row (``tdoc_cr_change_details``)
* 1 metadata-only row (no cover / ttcn / change — LS, DRAFT parity)
* 1 with mixed-case TDoc id prefix (``R5s-...`` style)
* 1 with a spec number reference (``38.300``)
* 1 with hyphenated jargon (``NB-IoT scheduling``)
* 2 with revised TDocs (sibling ``tdoc_id`` strings under the same
  meeting, different ``ftp_url``)

All blobs are tiny gzip-compressed JSON so the
``_build_index_text`` SQL JOIN runs end-to-end without a fixture
zip on disk.
"""

from __future__ import annotations

import gzip
import json
from typing import Any


def _gz(payload: Any) -> bytes:
    return gzip.compress(json.dumps(payload).encode("utf-8"), compresslevel=9)


# Tuple shape: (tdoc_id, title, ftp_url_suffix, release, spec, tsg, meeting_id)
ROWS: list[tuple[str, str, str, str, str | None, str, int]] = [
    ("RP-2200456", "NB-IoT scheduling for Rel-17", "u1", "Rel-17", "38.300", "RAN", 100),
    ("R5-1234567", "NR MAC procedures", "u2", "Rel-17", "38.321", "RAN1", 101),
    ("R5-1234567-rev1", "NR MAC procedures (revised)", "u3", "Rel-17", "38.321", "RAN1", 101),
    ("RP-2300001", "5G NR sidelink", "u4", "Rel-18", "38.300", "RAN", 100),
    ("SP-2100123", "SA plenary agenda", "u5", "Rel-17", None, "SA", 200),
    ("S2-1987654", "System architecture for NB-IoT", "u6", "Rel-17", "23.501", "SA2", 201),
    ("R5-260013", "TTCN for NB-IoT MAC", "u7", "Rel-17", "38.321", "RAN5", 102),
    ("R5-260014", "TTCN for NR sidelink", "u8", "Rel-18", "38.321", "RAN5", 102),
    ("R5-260015", "Change-marked CR", "u9", "Rel-17", "38.300", "RAN5", 102),
    ("R5s-260016", "LS to RAN — coordination", "u10", "Rel-17", None, "RAN5", 102),
]


# (tsg_short_name, tsg_name, description) — full NOT NULL payload required
# by the ``tsgs`` table (tsg_name + short_name + description are all
# NOT NULL; url is nullable).
TSGS: list[tuple[str, str, str]] = [
    ("RAN", "TSG RAN", "Radio Access Network"),
    ("SA", "TSG SA", "System Aspects"),
    ("RAN1", "TSG RAN WG1", "RAN WG1"),
    ("SA2", "TSG SA WG2", "SA WG2"),
    ("RAN5", "TSG RAN WG5", "RAN WG5"),
]


# (meeting_id, name, title, location, tsg_short, start_date, end_date)
# ``title`` and ``location`` are NOT NULL on ``meetings``.
MEETINGS: list[tuple[int, str, str, str, str, str, str]] = [
    (100, "RAN#100", "RAN#100 plenary", "Online", "RAN", "2026-01-01", "2026-01-05"),
    (101, "RAN1#101", "RAN1#101 meeting", "Online", "RAN1", "2026-01-01", "2026-01-05"),
    (102, "RAN5#102", "RAN5#102 meeting", "Online", "RAN5", "2026-01-01", "2026-01-05"),
    (200, "SA#200", "SA#200 plenary", "Online", "SA", "2026-01-01", "2026-01-05"),
    (201, "SA2#201", "SA2#201 meeting", "Online", "SA2", "2026-01-01", "2026-01-05"),
]


def build_corpus(engine: Any) -> list[str]:
    """Populate ``engine`` with the search-corpus rows.

    Returns the list of ``tdoc_id`` strings (in insertion order) so
    callers can drive downstream steps (e.g. eager
    :class:`SQLAlchemySearchIndexRepository.upsert`) without having
    to re-query the table.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        # TSGs first (FKs from ``meetings.tsg``).
        for short_name, tsg_name, description in TSGS:
            conn.execute(
                text(
                    "INSERT INTO tsgs (tsg_name, short_name, description) "
                    "VALUES (:tsg_name, :short_name, :description)"
                ),
                {
                    "tsg_name": tsg_name,
                    "short_name": short_name,
                    "description": description,
                },
            )

        # Meetings — ``title`` and ``location`` are NOT NULL.
        for mid, name, title, location, tsg, start_date, end_date in MEETINGS:
            conn.execute(
                text(
                    """
                    INSERT INTO meetings (
                        meeting_id, name, title, location, tsg,
                        start_date, end_date, ftp_url, tdoc_list_last_sync
                    ) VALUES (
                        :mid, :name, :title, :location, :tsg,
                        :start_date, :end_date, :ftp_url,
                        :tdoc_list_last_sync
                    )
                    """
                ),
                {
                    "mid": mid,
                    "name": name,
                    "title": title,
                    "location": location,
                    "tsg": tsg,
                    "start_date": start_date,
                    "end_date": end_date,
                    "ftp_url": f"https://x/{name}",
                    "tdoc_list_last_sync": "2026-01-05T00:00:00",
                },
            )

        # TDocs.
        for idx, (tid, title, suffix, release, spec, tsg, mid) in enumerate(
            ROWS, start=1
        ):
            del tsg  # TSG is stamped on the meeting; not stored on tdocs.
            ftp = f"https://x/{tid}-{suffix}.zip"
            conn.execute(
                text(
                    """
                    INSERT INTO tdocs (
                        tdoc_id, meeting_id, title, ftp_url, type, source,
                        uploaded_date, release, spec
                    ) VALUES (
                        :tid, :mid, :title, :ftp, 'CR', 'TSG',
                        '2026-01-02', :release, :spec
                    )
                    """
                ),
                {
                    "tid": tid,
                    "mid": mid,
                    "title": title,
                    "ftp": ftp,
                    "release": release,
                    "spec": spec,
                },
            )

            if idx in (7, 8):
                # TTCN sidecars for the two TTCN rows.
                conn.execute(
                    text(
                        """
                        INSERT INTO tdoc_cr_ttcn_details (
                            tdoc_id, ftp_url, testcase, ttcn_release,
                            required_changes, changed_functions
                        ) VALUES (
                            :tid, :ftp, :testcase, :ttcn_release,
                            :required_changes, :changed_functions
                        )
                        """
                    ),
                    {
                        "tid": tid,
                        "ftp": ftp,
                        "testcase": f"TC_{tid.replace('-', '_')}",
                        "ttcn_release": release,
                        "required_changes": _gz(
                            [{"file": "NB_IoT_mac.ttcn", "lines": "10-20"}]
                        ),
                        "changed_functions": "NB_IoT_mac.run",
                    },
                )

            if idx == 9:
                # Change details for row 9.
                conn.execute(
                    text(
                        """
                        INSERT INTO tdoc_cr_change_details (
                            tdoc_id, ftp_url, clauses, changes
                        ) VALUES (
                            :tid, :ftp, :clauses, :changes
                        )
                        """
                    ),
                    {
                        "tid": tid,
                        "ftp": ftp,
                        "clauses": "\n".join(["5.4.2.1", "5.4.2.2"]),
                        "changes": _gz(
                            [{"type": "ins", "text": "new NB-IoT MAC procedure"}]
                        ),
                    },
                )

    return [row[0] for row in ROWS]
