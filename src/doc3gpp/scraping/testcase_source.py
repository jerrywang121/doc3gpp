"""Transport for the RAN5 TTCN status History folder (network only)."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, unquote, urljoin

from bs4 import BeautifulSoup

from doc3gpp.models.testcase import TestcaseSourceNotFoundError
from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)

HISTORY_URL = "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/Reporting/TTCN_status/History/"

_FILENAME_RE = re.compile(
    r"TTCN CR Agreement Status (\d{4})-wk(\d{2})(?:[_-]?(?:r|rev)(\d+))?\.zip$",
    re.IGNORECASE,
)


def parse_status_filename(name: str) -> tuple[int, int, int] | None:
    m = _FILENAME_RE.search(name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else 0)


def list_history_files(client: ScraperClient | None = None) -> list[str]:
    if client is not None:
        html = client.get_text(HISTORY_URL)
    else:
        with ScraperClient() as new_client:
            html = new_client.get_text(HISTORY_URL)
    out: list[str] = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        raw = unquote(str(a["href"]).split("?")[0].split("#")[0].split("/")[-1])
        if not raw.lower().endswith(".zip"):
            continue
        if parse_status_filename(raw) is None:
            logger.debug("Ignoring non-status zip %r", raw)
            continue
        out.append(raw)
    return out


def select_latest(filenames: list[str]) -> str:
    keyed = [(parse_status_filename(n), n) for n in filenames]
    keyed = [(k, n) for k, n in keyed if k is not None]
    if not keyed:
        raise TestcaseSourceNotFoundError("no TTCN CR Agreement Status zip found in History/")
    keyed.sort(key=lambda kv: kv[0])
    return keyed[-1][1]


def fetch_testcase_zip(filename: str, client: ScraperClient | None = None) -> bytes:
    url = urljoin(HISTORY_URL, quote(filename))
    logger.debug("Fetching testcase status zip at %s", url)
    if client is not None:
        return client.get_bytes(url)
    with ScraperClient() as c:
        return c.get_bytes(url)
