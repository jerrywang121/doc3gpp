"""Parser for 3GPP spec DynaReport pages (list + detail).

Pure module: takes raw HTML and produces domain objects, never touching
the network or storage.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.parsers.spec_release import normalise_release, release_from_version

_LIST_TABLE_CLASSES = ["dsptab", "adynspec", "dsp-tsgwg"]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_spec_list(html: str, tsg: str) -> list[Spec]:
    """Parse spec rows from the per-TSG DynaReport list page.

    The relevant table has class ``dsptab adynspec dsp-tsgwg``. Each data
    row has three cells: ``Spec`` (type + ``<a>`` to detail), ``Title``,
    ``Rapporteur``. Rows missing the spec anchor or the type token are
    silently skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_=_LIST_TABLE_CLASSES)
    if table is None:
        return []
    canonical = tsg.upper()
    specs: list[Spec] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        spec_cell = cells[0]
        anchor = spec_cell.find("a")
        if anchor is None or not anchor.get("href"):
            continue
        cell_text = _normalize(spec_cell.get_text())
        type_token = _extract_type_token(cell_text, anchor.get_text())
        if type_token is None:
            continue
        specs.append(
            Spec(
                spec_id=_normalize(anchor.get_text()),
                type=type_token,
                title=_normalize(cells[1].get_text()),
                tsg=canonical,
            )
        )
    return specs


def _extract_type_token(cell_text: str, anchor_text: str) -> str | None:
    """Return ``TS`` / ``TR`` from the spec cell, or ``None``."""
    without_anchor = cell_text.replace(anchor_text, "")
    m = re.search(r"\b(TS|TR)\b", without_anchor, flags=re.IGNORECASE)
    if m is None:
        return None
    return m.group(1).upper()


def parse_spec_detail(
    html: str, spec_id: str, tsg: str
) -> tuple[Spec, list[SpecVersion]]:
    """Parse the detail page into a header + version rows.

    Returns ``(header, versions)``. ``header`` carries the parsed
    ``status``, ``initial_release``, ``radio_tech`` and ``wis`` fields
    plus the ``spec_id``/``type``/``title``/``tsg`` given or left for
    the caller to fill. The ``type``/``title`` come from the list page;
    callers that only have the detail page may pass placeholders.
    """
    soup = BeautifulSoup(html, "lxml")

    status = _text_of_id(soup, "statusVal")
    initial_release_raw = _text_of_id(soup, "initialPlannedReleaseVal")
    initial_release = normalise_release(initial_release_raw) if initial_release_raw else None

    radio_tech_vals = soup.find(id="radioTechnologyVals")
    radio_tech: str | None = None
    if radio_tech_vals is not None:
        checked = []
        for span in radio_tech_vals.find_all("span"):
            checkbox = span.find("input", type="checkbox")
            label = span.find("label")
            if checkbox is None or label is None:
                continue
            if checkbox.get("checked") is not None:
                checked.append(_normalize(label.get_text()))
        if not checked:
            checked = [
                _normalize(lbl.get_text())
                for lbl in radio_tech_vals.find_all("label")
            ]
        if checked:
            radio_tech = ",".join(checked)

    wis = _extract_related_wis(soup)

    header = Spec(
        spec_id=spec_id,
        type=_spec_type_from_id(spec_id),
        title="",
        status=status,
        radio_tech=radio_tech,
        initial_release=initial_release,
        tsg=tsg.upper(),
        wis=wis,
    )

    versions: list[SpecVersion] = []
    for row in soup.find_all("tr"):
        ftp_anchor = row.find("a", id=lambda v: v and "lnkFtpDownload" in v)
        if ftp_anchor is None:
            continue
        version = _normalize(ftp_anchor.get_text())
        if not version:
            continue
        versions.append(_parse_version_row(spec_id, version, row))
    return header, versions


def _text_of_id(soup: BeautifulSoup, element_id: str) -> str | None:
    el = soup.find(id=element_id)
    if el is None:
        return None
    text = _normalize(el.get_text())
    return text or None


def _spec_type_from_id(spec_id: str) -> str:
    base = spec_id.split("-")[0]
    try:
        int(base)
    except ValueError:
        return ""
    return "TS" if int(base) < 40000 else "TR"


def _extract_related_wis(soup: BeautifulSoup) -> str | None:
    grid = (
        soup.find(id="SpecificationRelatedWorkItems_relatedWiGrid")
        or soup.find(id="relatedWIs")
        or soup.find(id="relatedWorkItems")
    )
    if grid is None:
        return None

    telerik_rows = grid.find_all(
        "tr", class_=lambda c: c and ("rgRow" in c or "rgAltRow" in c)
    )
    if telerik_rows:
        acronyms: list[str] = []
        for row in telerik_rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            text = _normalize(cells[1].get_text())
            if text and text not in acronyms:
                acronyms.append(text)
        return ",".join(acronyms) if acronyms else None

    acronyms = []
    for span in grid.find_all("span"):
        text = _normalize(span.get_text())
        if text and text not in acronyms:
            acronyms.append(text)
    return ",".join(acronyms) if acronyms else None


def _parse_version_row(spec_id: str, version: str, row) -> SpecVersion:
    ftp_anchor = row.find("a", id=lambda v: v and "lnkFtpDownload" in v)
    ftp_url = ftp_anchor.get("href", "") if ftp_anchor else ""

    meeting_anchor = row.find("a", id=lambda v: v and "lnkMeetings" in v)
    meeting_id: int | None = None
    meeting_name: str | None = None
    if meeting_anchor is not None:
        meeting_name = _normalize(meeting_anchor.get_text()) or None
        href = meeting_anchor.get("href", "")
        m = re.search(r"m_id=(\d+)", href)
        if m:
            meeting_id = int(m.group(1))

    crs_anchor = row.find("a", id=lambda v: v and "imgRelatedCRs" in v)
    version_id: int | None = None
    if crs_anchor is not None:
        href = crs_anchor.get("href", "")
        m = re.search(r"versionId=(\d+)", href)
        if m:
            version_id = int(m.group(1))

    wki_anchor = row.find("a", id=lambda v: v and "imgRelatedWI" in v)
    wki_id: int | None = None
    if wki_anchor is not None:
        m = re.search(r"WKI_ID=(\d+)", wki_anchor.get("href", ""))
        if m:
            wki_id = int(m.group(1))

    upload_date: date | None = None
    for cell in row.find_all("td"):
        text = _normalize(cell.get_text())
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            upload_date = date.fromisoformat(text)
            break

    comment: str | None = None
    remark = row.find(class_="lblRemarkText")
    if remark is not None:
        comment = _normalize(remark.get_text())[:256] or None

    release = release_from_version(version) if version else None

    return SpecVersion(
        spec_id=spec_id,
        version=version,
        ftp_url=ftp_url,
        release=release,
        meeting_id=meeting_id,
        meeting_name=meeting_name,
        upload_date=upload_date,
        version_id=version_id,
        comment=comment,
        wki_id=wki_id,
    )


def _spec_id_no_dot(spec_id: str) -> str:
    """Return the dotless URL slug form (e.g. ``36.579-5`` → ``36579-5``)."""
    return spec_id.replace(".", "")


def extract_etsi_pdf_url(html: str) -> str | None:
    """Return the first ``.pdf`` download link in an ETSI work-item page."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if href.lower().endswith(".pdf"):
            return href
    return None


def extract_cr_tdocs(html: str) -> list[str]:
    """Return every ``tdoc_id`` in the rendered CR list table.

    Matches anchors with ``id="wgTdocDetailsLink"``. The page is not
    paginated here (default page size 200).
    """
    soup = BeautifulSoup(html, "lxml")
    ids: list[str] = []
    for a in soup.find_all("a", id="wgTdocDetailsLink"):
        text = _normalize(a.get_text())
        if text:
            ids.append(text)
    return ids


__all__ = [
    "parse_spec_list",
    "parse_spec_detail",
    "extract_etsi_pdf_url",
    "extract_cr_tdocs",
    "normalise_release",
    "release_from_version",
]
