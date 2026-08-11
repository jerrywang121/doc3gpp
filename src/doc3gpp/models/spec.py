"""Domain models for 3GPP specifications (TSs / TRs) and their versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True)
class Spec:
    """A 3GPP specification (TS or TR) header, scraped from the DynaReport list + detail pages.

    Attributes:
        spec_id: Full dotted spec identity (e.g. ``36.579-5``). Primary key.
        type: ``TS`` or ``TR``.
        title: Full spec title from the list page.
        status: From the detail page ``#statusVal``.
        radio_tech: Comma-joined ticked radio technologies (e.g. ``2G,3G,LTE,5G,6G``).
        initial_release: Normalised release marker (e.g. ``Rel-20``, ``R99``).
        tsg: Owning TSG short name FK to ``tsgs.short_name``.
        wis: Comma-joined related-WI acronyms (point-in-time snapshot).
        rapporteurs: Comma-joined company names from the detail page rapporteurs grid (e.g. ``Ericsson LM``).
        last_synced_at: UTC of the last **successful** detail-page sync
            (parsed + wis extracted + ETSI/CR follow-ups fetched + both
            header and ``spec_versions`` rows written). ``None`` when the
            row has never been synced, or when the most recent sync
            attempt for this spec crashed mid-flight — in which case
            the next sync retries the detail page so the missing data
            can be back-filled.
    """

    spec_id: str
    type: str
    title: str
    status: str | None = None
    radio_tech: str | None = None
    initial_release: str | None = None
    tsg: str | None = None
    wis: str | None = None
    rapporteurs: str | None = None
    last_synced_at: datetime | None = None


@dataclass(slots=True)
class SpecVersion:
    """A single versioned artefact of a spec.

    One row per ``(spec_id, version)`` pair. ``wki_id`` is a transient
    parser field used only to drive the ETSI PDF follow-up fetch; it is
    not persisted.

    Attributes:
        spec_id: FK to ``specs.spec_id``.
        version: e.g. ``18.3.0``.
        ftp_url: Absolute 3GPP FTP URL of the version zip.
        release: Canonical release marker (``draft`` / ``pre-release`` / ``Rel-N``).
        meeting_id: Numeric 3GPP meeting id.
        meeting_name: e.g. ``RAN#108``.
        upload_date: From the row's ``Upload date`` cell.
        version_id: ``?versionId=`` param used to build the CR list URL.
        pdf_url: ETSI "download as PDF" link (nullable).
        crs: Comma-joined ``tdoc_id``s from the CR list page (nullable).
        wki_id: Transient ETSI work-item id (not persisted).
    """

    spec_id: str
    version: str
    ftp_url: str
    release: str | None = None
    meeting_id: int | None = None
    meeting_name: str | None = None
    upload_date: date | None = None
    version_id: int | None = None
    pdf_url: str | None = None
    crs: str | None = None
    wki_id: int | None = None
