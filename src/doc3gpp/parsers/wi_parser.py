"""Parser for 3GPP Work Item (WI) DynaReport pages.

The WIs page is a Joomla-rendered HTML document whose data lives inside a
single ``<table class="dsp-tsgwgxwis ...">``. Each data row has three
``<td>`` cells:

* column 1: an ``<a href="...?workitemId=<id>">`` whose text is the WI title,
* column 2: the WI acronym,
* column 3: the release marker (for example ``Rel-19``).

This module is intentionally pure: it takes raw HTML and produces a list of
:class:`Wi` dataclasses, never touching the network or storage layers.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from doc3gpp.models.wi import Wi


def parse_3gpp_wis(html: str, tsg_short: str) -> list[Wi]:
    """Parse WI rows from a 3GPP DynaReport WIs page.

    Args:
        html: Raw HTML body of a ``dynareport?code=TSG-WG--<tsg>--wis.htm``
            response.
        tsg_short: Canonical TSG short name (e.g. ``R5``). Stored on every
            returned :class:`Wi` as its ``tsg_short`` foreign key.

    Returns:
        The list of WI rows found in the page. Empty when the expected
        ``dsp-tsgwgxwis`` table is missing (for example, on the site's
        "no WIs" page). Rows that lack a ``workitemId`` anchor or have
        fewer than three ``<td>`` cells are silently skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"class": "dsp-tsgwgxwis"})
    if table is None:
        return []

    canonical_tsg = tsg_short.upper()
    wis: list[Wi] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        anchor = cells[0].find("a")
        if anchor is None or not anchor.has_attr("href"):
            continue

        wi_id = _extract_wi_id(str(anchor["href"]))
        if wi_id is None:
            continue

        name = _normalize_text(anchor.get_text())
        acronym = _normalize_text(cells[1].get_text())
        release = _normalize_text(cells[2].get_text())

        wis.append(
            Wi(
                wi_id=wi_id,
                acronym=acronym,
                release=release,
                name=name,
                tsg_short=canonical_tsg,
            )
        )

    return wis


def _extract_wi_id(href: str) -> int | None:
    """Return the ``workitemId`` integer embedded in a WI detail URL.

    Returns ``None`` if no numeric identifier is present, allowing the caller
    to skip rows that do not conform to the expected link shape.
    """
    match = re.search(r"workitemId=(\d+)", href)
    if match is None:
        return None
    return int(match.group(1))


def _normalize_text(text: str) -> str:
    """Collapse internal whitespace runs (including newlines/tabs) into a single space."""
    return re.sub(r"\s+", " ", text).strip()
