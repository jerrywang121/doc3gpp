"""Pure parsers for ETSI LISTSERV message detail pages, message bodies, attachments, and TDoc IDs."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from doc3gpp.models.listserv import ListservAttachment, normalize_tdoc_ids
from doc3gpp.parsers.cr.header import _TDOC_HEADER_PATTERN


class ListservDetailParseError(ValueError):
    """Raised when a LISTSERV detail page is empty, malformed, or requires login."""


_BODY_ONLY_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/html",
        "multipart/related",
        "multipart/alternative",
        "multipart/mixed",
        "application/xhtml+xml",
    }
)
_MULTIPART_MIME_TYPES = frozenset(
    {
        "multipart/related",
        "multipart/alternative",
        "multipart/mixed",
    }
)
_GENERIC_BODY_LABELS = frozenset(
    {
        "",
        "text/plain",
        "text/html",
        "application/xhtml+xml",
        "plain text",
        "html",
        "text",
        *_BODY_ONLY_MIME_TYPES,
    }
)

_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

_QUOTE_CLASS_KEYWORDS = (
    "gmail_quote",
    "gmail_attr",
    "yahoo_quoted",
    "moz-cite-prefix",
    "outlookmessageheader",
)
_QUOTE_ID_KEYWORDS = (
    "divrplyfwdmsg",
    "appendonsend",
    "zwchr",
)

_DELIMITER_LINE_RE = re.compile(
    r"^\s*[-_]{2,}\s*(?:original\s+message|forwarded\s+message|reply\s+message|message\s+d'origine)\s*[-_]{2,}\s*$",
    re.IGNORECASE,
)
_BEGIN_FORWARDED_RE = re.compile(
    r"^\s*begin\s+forwarded\s+message\s*:\s*$",
    re.IGNORECASE,
)
_ON_WROTE_SINGLE_LINE_RE = re.compile(
    r"^\s*on\s+(?:mon|tue|wed|thu|fri|sat|sun|[a-z]{3,9}\s+\d{1,2}|\d{1,2}\s+[a-z]{3,9}|\d{4}-\d{2}-\d{2})\b.{5,200}?\bwrote\s*:\s*$",
    re.IGNORECASE,
)
_HEADER_KEY_RE = re.compile(
    r"^\s*(?:from|sent|date|to|cc|subject)\s*:\s*\S+",
    re.IGNORECASE,
)
_FORMATTED_HEADER_KEY_RE = re.compile(
    r"^\s*(?:\*\*(from|sent|date|to|cc|subject)\s*:\*\*"
    r"|(from|sent|date|to|cc|subject)\s*:)\s*\S+",
    re.IGNORECASE,
)
_RAW_HTML_TAG_RE = re.compile(
    r"<!--[\s\S]*?-->|</?[A-Za-z!][A-Za-z0-9:_-]*(?:\s[^<>]*)?>",
)
_UNSAFE_MD_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(\s*(?:javascript|vbscript|data)\s*:[^)]*\)",
    re.IGNORECASE,
)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_login_or_empty(html: str, soup: BeautifulSoup) -> bool:
    if not html or not html.strip():
        return True
    if soup.find("input", attrs={"type": re.compile(r"^password$", re.IGNORECASE)}) is not None:
        return True
    title_text = _collapse_ws(soup.title.get_text()) if soup.title else ""
    return "login required" in title_text.lower()


def _is_usable_href(href: str) -> bool:
    stripped = href.strip()
    if not stripped or stripped.startswith("#"):
        return False
    lowered = stripped.lower()
    return not lowered.startswith(("javascript:", "mailto:", "vbscript:", "data:"))


def _query_params_upper(url: str) -> dict[str, list[str]]:
    query = urlsplit(url).query
    if not query:
        return {}
    result: dict[str, list[str]] = {}
    for key, values in parse_qs(query, keep_blank_values=True).items():
        result.setdefault(key.upper(), []).extend(values)
    return result


def _extract_part_mime(params: dict[str, list[str]], label: str) -> str:
    for raw_t in params.get("T", []):
        unquoted = unquote(raw_t)
        media = unquoted.partition(";")[0].strip().lower()
        if media:
            return media
    cleaned_label = _collapse_ws(label).lower()
    if cleaned_label in _BODY_ONLY_MIME_TYPES:
        return cleaned_label
    return ""


def _extract_attachment_filename(params: dict[str, list[str]]) -> str | None:
    for key in ("N", "ATTACHMENT", "FILENAME", "F"):
        for val in params.get(key, []):
            cleaned = _collapse_ws(unquote(val))
            if cleaned:
                return cleaned
    return None


def _is_parts_header_cell(text: str) -> bool:
    lowered = _collapse_ws(text).lower().rstrip(":")
    return lowered in (
        "parts/attachments",
        "parts / attachments",
        "attachments",
        "parts",
        "attachments/parts",
    )


def _collect_part_anchors(soup: BeautifulSoup) -> list[Tag]:
    anchors: list[Tag] = []
    seen_ids: set[int] = set()

    for cell in soup.find_all(["th", "td"]):
        if _is_parts_header_cell(cell.get_text(" ", strip=True)):
            tr = cell.find_parent("tr")
            container = tr if isinstance(tr, Tag) else cell.parent
            if isinstance(container, Tag):
                for a in container.find_all("a", href=True):
                    if id(a) not in seen_ids:
                        seen_ids.add(id(a))
                        anchors.append(a)

    for a in soup.find_all("a", href=True):
        if id(a) in seen_ids:
            continue
        href = str(a.get("href", ""))
        if not _is_usable_href(href):
            continue
        params = _query_params_upper(href)
        if "A3" in params:
            seen_ids.add(id(a))
            anchors.append(a)

    return anchors


def _extract_inline_fallback_html(soup: BeautifulSoup) -> str | None:
    for selector in (
        {"id": re.compile(r"message[-_]?body|msg[-_]?body|body[-_]?content", re.IGNORECASE)},
        {"class": re.compile(r"message[-_]?body|msg[-_]?body|body[-_]?content", re.IGNORECASE)},
    ):
        found = soup.find(["div", "td", "section", "article", "pre"], attrs=selector)
        if isinstance(found, Tag) and _collapse_ws(found.get_text(" ", strip=True)):
            return str(found)

    for pre in soup.find_all("pre"):
        if _collapse_ws(pre.get_text(" ", strip=True)):
            return str(pre)

    # Check if there is non-chrome body prose outside header/navigation tables
    body_clone = BeautifulSoup(str(soup.body or soup), "lxml")
    for tag in body_clone.find_all(["script", "style", "head", "title", "iframe"]):
        tag.decompose()
    for table in body_clone.find_all("table"):
        table_classes = " ".join(table.get("class", [])) if isinstance(table.get("class"), list) else str(table.get("class", ""))
        if re.search(r"header|nav|toolbar", table_classes, re.IGNORECASE):
            table.decompose()
            continue
        headers = [
            _collapse_ws(cell.get_text(" ", strip=True)).lower().rstrip(":")
            for cell in table.find_all(["th", "td"])
        ]
        if any(h in ("subject", "from", "date", "parts/attachments") for h in headers):
            table.decompose()

    remaining_text = _collapse_ws(body_clone.get_text(" ", strip=True))
    if remaining_text:
        return str(body_clone)
    return None


def parse_detail_page(
    html: str, base_url: str
) -> tuple[str | None, tuple[ListservAttachment, ...], str | None]:
    """Parse an ETSI LISTSERV message detail page.

    Returns ``(body_part_url, attachments, inline_fallback_body)`` where:
    - ``body_part_url`` prefers the ``text/html`` message representation when both
      HTML and plain-text parts exist, falling back to ``text/plain`` or an iframe URL.
    - ``attachments`` contains only genuine named attachments (never ``text/plain``,
      ``text/html``, or ``multipart/related`` body representations).
    - ``inline_fallback_body`` provides sanitized Markdown when no body-part URL is
      present on the detail page.
    """
    soup = BeautifulSoup(html, "lxml")
    if _is_login_or_empty(html, soup):
        raise ListservDetailParseError("Detail page HTML is empty or requires login.")

    html_body_urls: list[str] = []
    plain_body_urls: list[str] = []
    iframe_urls: list[str] = []
    attachments: list[ListservAttachment] = []
    seen_attachment_urls: set[str] = set()

    for iframe in soup.find_all("iframe", src=True):
        raw_src = str(iframe.get("src", "")).strip()
        if not _is_usable_href(raw_src):
            continue
        resolved_src = urljoin(base_url, raw_src)
        params = _query_params_upper(resolved_src)
        mime = _extract_part_mime(params, "")
        if mime in ("text/html", "application/xhtml+xml"):
            html_body_urls.append(resolved_src)
        elif mime == "text/plain":
            plain_body_urls.append(resolved_src)
        else:
            iframe_urls.append(resolved_src)

    for anchor in _collect_part_anchors(soup):
        raw_href = str(anchor.get("href", "")).strip()
        if not _is_usable_href(raw_href):
            continue
        resolved_url = urljoin(base_url, raw_href)
        label = _collapse_ws(anchor.get_text(" ", strip=True))
        label_lower = label.lower()
        params = _query_params_upper(resolved_url)
        mime = _extract_part_mime(params, label)
        param_filename = _extract_attachment_filename(params)

        # Never treat multipart containers as downloadable attachments.
        if (
            mime in _MULTIPART_MIME_TYPES
            or label_lower in _MULTIPART_MIME_TYPES
            or mime.startswith("multipart/")
        ):
            continue

        is_generic_body_label = label_lower in _GENERIC_BODY_LABELS
        is_body_representation = param_filename is None and is_generic_body_label and (
            mime in ("text/html", "application/xhtml+xml", "text/plain")
            or label_lower in ("text/html", "application/xhtml+xml", "text/plain", "plain text", "html", "text")
        )
        if is_body_representation:
            if mime in ("text/html", "application/xhtml+xml") or label_lower in (
                "text/html",
                "application/xhtml+xml",
                "html",
            ):
                html_body_urls.append(resolved_url)
            elif mime == "text/plain" or label_lower in ("text/plain", "plain text", "text"):
                plain_body_urls.append(resolved_url)
            continue

        attachment_name = (
            label if (label and not is_generic_body_label) else (param_filename or "")
        )
        if not attachment_name or attachment_name.lower() in _GENERIC_BODY_LABELS:
            continue

        if resolved_url not in seen_attachment_urls:
            seen_attachment_urls.add(resolved_url)
            attachments.append(ListservAttachment(name=attachment_name, url=resolved_url))

    body_part_url: str | None = None
    if html_body_urls:
        body_part_url = html_body_urls[0]
    elif plain_body_urls:
        body_part_url = plain_body_urls[0]
    elif iframe_urls:
        body_part_url = iframe_urls[0]

    inline_fallback: str | None = None
    if body_part_url is None:
        raw_inline_html = _extract_inline_fallback_html(soup)
        if raw_inline_html is not None:
            parsed_inline = parse_message_body(raw_inline_html, is_html=True)
            inline_fallback = parsed_inline

    return body_part_url, tuple(attachments), inline_fallback


def _is_outlook_divider_or_header(tag: Tag) -> bool:
    raw_class = tag.get("class")
    class_str = (
        " ".join(raw_class).lower()
        if isinstance(raw_class, list)
        else str(raw_class or "").lower()
    )
    if "outlookmessageheader" in class_str:
        return True
    id_str = str(tag.get("id") or "").lower()
    if any(keyword in id_str for keyword in _QUOTE_ID_KEYWORDS):
        return True
    style_str = str(tag.get("style") or "").lower().replace(" ", "")
    return "border-top:solid#e1e1e1" in style_str or "border-top:solid#b5c4df" in style_str


def _is_quote_container(tag: Tag) -> bool:
    if tag.name == "blockquote":
        return True
    if tag.get("type", "") and str(tag.get("type", "")).lower() == "cite":
        return True
    raw_class = tag.get("class")
    class_str = " ".join(raw_class).lower() if isinstance(raw_class, list) else str(raw_class or "").lower()
    if any(keyword in class_str for keyword in _QUOTE_CLASS_KEYWORDS):
        return True
    if _is_outlook_divider_or_header(tag):
        return True
    style_str = str(tag.get("style") or "").lower().replace(" ", "")
    return "border-left:" in style_str and tag.name in ("div", "p")


def _looks_like_outlook_header_block(tag: Tag) -> bool:
    for child_block in tag.find_all(["div", "p", "table"], recursive=False):
        if _is_outlook_divider_or_header(child_block) or _looks_like_outlook_header_block(
            child_block
        ):
            return False
    text = tag.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or len(lines) > 8:
        return False
    header_matches = sum(1 for line in lines[:6] if _HEADER_KEY_RE.match(line))
    if header_matches < max(3, len(lines) - 1):
        return False
    has_from = any(line.lower().startswith("from:") for line in lines[:2])
    has_sent_or_date = any(line.lower().startswith(("sent:", "date:")) for line in lines[:5])
    has_to_or_subject = any(line.lower().startswith(("to:", "subject:")) for line in lines[:6])
    return has_from and has_sent_or_date and has_to_or_subject


def _decompose_boundary_and_trailing_siblings(tag: Tag) -> None:
    target = tag
    while isinstance(target.parent, Tag) and target.parent.name not in (
        "body",
        "html",
        "[document]",
    ):
        parent = target.parent
        has_prior_content = any(
            (isinstance(prev, Tag) and bool(_collapse_ws(prev.get_text(" ", strip=True))))
            or (isinstance(prev, NavigableString) and bool(str(prev).strip()))
            for prev in target.find_previous_siblings()
        )
        if has_prior_content:
            break
        target = parent

    for sibling in list(target.find_next_siblings()):
        if isinstance(sibling, Tag):
            sibling.decompose()
        elif isinstance(sibling, NavigableString):
            sibling.extract()
    target.decompose()


def _strip_html_quotes_and_unsafe(soup: BeautifulSoup) -> None:
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for bad in soup.find_all(
        [
            "script",
            "style",
            "head",
            "title",
            "meta",
            "link",
            "iframe",
            "object",
            "embed",
            "applet",
            "form",
            "input",
            "button",
            "svg",
            "math",
        ]
    ):
        bad.decompose()

    # Remove structural quote tags and any trailing forward/reply blocks after HR/Outlook separators
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        if _is_outlook_divider_or_header(tag) or (
            tag.name in ("div", "p", "table") and _looks_like_outlook_header_block(tag)
        ):
            _decompose_boundary_and_trailing_siblings(tag)
            continue
        if _is_quote_container(tag):
            tag.decompose()


def _sanitize_href(raw_href: str) -> str | None:
    cleaned = re.sub(r"[\x00-\x20]+", "", raw_href).strip()
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme:
        if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
            return None
        return cleaned
    if cleaned.startswith(("/", "#")):
        return cleaned
    return None


def _render_inline(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        text = str(node)
        # Escape raw HTML angle brackets so embedded tags in text nodes cannot execute
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        return re.sub(r"[ \t\r\f\v]+", " ", text)

    name = node.name.lower()
    if name == "br":
        return "\n"
    if name == "img":
        return ""
    if name in ("code", "tt"):
        inner = node.get_text().replace("`", "'")
        inner = inner.replace("<", "&lt;").replace(">", "&gt;")
        return f"`{inner}`" if inner else ""

    children_md = "".join(_render_inline(child) for child in node.children if isinstance(child, (Tag, NavigableString)))
    stripped = children_md.strip()
    if not stripped:
        return ""

    if name in ("strong", "b"):
        return f"**{stripped}**"
    if name in ("em", "i"):
        return f"*{stripped}*"
    if name == "a":
        raw_href = str(node.get("href") or "")
        safe_href = _sanitize_href(raw_href)
        if safe_href:
            return f"[{stripped}]({safe_href})"
        return stripped
    return children_md


def _render_table(table: Tag) -> str:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") != table:
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        row_cells = [
            _collapse_ws("".join(_render_inline(c) for c in cell.children if isinstance(c, (Tag, NavigableString)))).replace("|", "\\|")
            for cell in cells
        ]
        if any(row_cells):
            rows.append(row_cells)
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    padded = [r + [""] * (col_count - len(r)) for r in rows]
    header = "| " + " | ".join(padded[0]) + " |"
    divider = "| " + " | ".join(["---"] * col_count) + " |"
    body_lines = ["| " + " | ".join(r) + " |" for r in padded[1:]]
    return "\n".join([header, divider, *body_lines])


def _render_blocks(container: Tag) -> list[str]:
    blocks: list[str] = []
    inline_buffer: list[str] = []

    def flush_inline() -> None:
        if not inline_buffer:
            return
        combined = "".join(inline_buffer)
        lines = [_collapse_ws(line) for line in combined.splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        if cleaned:
            blocks.append(cleaned)
        inline_buffer.clear()

    for child in container.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            inline_buffer.append(_render_inline(child))
            continue
        if not isinstance(child, Tag):
            continue

        name = child.name.lower()
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            flush_inline()
            level = int(name[1])
            heading_text = _collapse_ws(_render_inline(child))
            if heading_text:
                blocks.append(f"{'#' * level} {heading_text}")
        elif name == "p":
            flush_inline()
            para = _render_inline(child)
            lines = [_collapse_ws(line) for line in para.splitlines()]
            cleaned = "\n".join(line for line in lines if line)
            if cleaned:
                blocks.append(cleaned)
        elif name in ("ul", "ol"):
            flush_inline()
            is_ordered = name == "ol"
            items: list[str] = []
            for idx, li in enumerate(child.find_all("li", recursive=False), start=1):
                item_text = _collapse_ws(_render_inline(li))
                if item_text:
                    prefix = f"{idx}. " if is_ordered else "- "
                    items.append(f"{prefix}{item_text}")
            if items:
                blocks.append("\n".join(items))
        elif name == "pre":
            flush_inline()
            code_text = child.get_text().strip("\r\n")
            code_text = code_text.replace("<", "&lt;").replace(">", "&gt;")
            if code_text:
                blocks.append(f"```\n{code_text}\n```")
        elif name == "table":
            flush_inline()
            table_md = _render_table(child)
            if table_md:
                blocks.append(table_md)
        elif name == "hr":
            flush_inline()
            blocks.append("---")
        elif name in ("div", "section", "article", "main", "body", "html", "center", "font"):
            flush_inline()
            nested = _render_blocks(child)
            blocks.extend(nested)
        else:
            inline_buffer.append(_render_inline(child))

    flush_inline()
    return blocks


def _is_plain_header_block_at(lines: list[str], idx: int) -> bool:
    line = lines[idx].strip()
    if line.lower().startswith("from:"):
        window = [ln.strip() for ln in lines[idx : idx + 7] if ln.strip()]
        matches = sum(1 for ln in window[:6] if _HEADER_KEY_RE.match(ln))
        has_date = any(ln.lower().startswith(("sent:", "date:")) for ln in window[:5])
        has_to_or_subj = any(ln.lower().startswith(("to:", "subject:")) for ln in window[:6])
        if matches >= 3 and has_date and has_to_or_subj:
            return True

    # HTML-rendered headers often have bold labels and paragraph breaks.
    # Stop at intervening prose to avoid joining unrelated header-like lines.
    headers: list[str] = []
    for header_line in lines[idx : idx + 12]:
        if not header_line.strip():
            continue
        match = _FORMATTED_HEADER_KEY_RE.match(header_line)
        if match is None:
            break
        key = (match.group(1) or match.group(2)).lower()
        if not headers and key != "from":
            return False
        headers.append(key)
        if len(headers) == 6:
            break

    return (
        bool(headers)
        and headers[0] == "from"
        and bool({"sent", "date"} & set(headers))
        and bool({"to", "subject"} & set(headers))
    )


def _is_quote_prefix(stripped_line: str) -> bool:
    return stripped_line.startswith((">", "&gt;"))


def _is_on_wrote_boundary_at(lines: list[str], idx: int) -> bool:
    line = lines[idx].strip()
    if _ON_WROTE_SINGLE_LINE_RE.match(line):
        # Confirm either a following '>' quote line or end-of-message reply block
        for follow in lines[idx + 1 : idx + 5]:
            stripped_follow = follow.strip()
            if not stripped_follow:
                continue
            return _is_quote_prefix(stripped_follow)
        return True

    # Two-line "On ... \n ... wrote:" followed by '>' quoted line
    if line.lower().startswith("on ") and idx + 1 < len(lines):
        combined = f"{line} {lines[idx + 1].strip()}"
        if _ON_WROTE_SINGLE_LINE_RE.match(combined):
            for follow in lines[idx + 2 : idx + 6]:
                stripped_follow = follow.strip()
                if not stripped_follow:
                    continue
                return _is_quote_prefix(stripped_follow)
    return False


def _strip_plain_quotes(text: str) -> str:
    raw_lines = text.splitlines()
    kept_lines: list[str] = []
    idx = 0
    total = len(raw_lines)

    while idx < total:
        line = raw_lines[idx]
        stripped = line.strip()

        if _DELIMITER_LINE_RE.match(stripped) or _BEGIN_FORWARDED_RE.match(stripped):
            break
        if _is_plain_header_block_at(raw_lines, idx):
            break
        if _is_on_wrote_boundary_at(raw_lines, idx):
            break

        if _is_quote_prefix(stripped):
            idx += 1
            continue

        kept_lines.append(line)
        idx += 1

    return "\n".join(kept_lines)


def _sanitize_plain_text_for_markdown(text: str) -> str:
    # Neutralize unsafe javascript:/data:/vbscript: markdown links
    cleaned = _UNSAFE_MD_LINK_RE.sub(r"\1", text)
    # Escape raw HTML tags like <script>, <iframe>, <img ...>, etc., while preserving comparison operators like "power > 0 dBm"
    cleaned = _RAW_HTML_TAG_RE.sub(
        lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        cleaned,
    )
    return cleaned


def parse_message_body(body: str, *, is_html: bool) -> str:
    """Convert a LISTSERV message body (HTML or plain text) into safe Markdown with trailing quotes removed."""
    if not body or not body.strip():
        return ""

    if is_html:
        soup = BeautifulSoup(body, "lxml")
        _strip_html_quotes_and_unsafe(soup)
        root = soup.body if soup.body is not None else soup
        blocks = _render_blocks(root)
        joined = "\n\n".join(blocks)
        cleaned = _strip_plain_quotes(joined)
        cleaned = _sanitize_plain_text_for_markdown(cleaned)
    else:
        cleaned = _strip_plain_quotes(body)
        cleaned = _sanitize_plain_text_for_markdown(cleaned)

    # Normalize excessive blank lines while preserving paragraph breaks
    cleaned = re.sub(r"\r\n?", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_tdocs(*texts: str) -> tuple[str, ...]:
    """Extract case-insensitively deduplicated uppercase 3GPP TDoc IDs preserving first-seen order."""
    matches: list[str] = []
    for text in texts:
        if not text:
            continue
        for candidate in (text, unquote(text)):
            for m in _TDOC_HEADER_PATTERN.finditer(candidate):
                matches.append(m.group(0))
    return normalize_tdoc_ids(matches)


__all__ = [
    "ListservDetailParseError",
    "extract_tdocs",
    "parse_detail_page",
    "parse_message_body",
]
