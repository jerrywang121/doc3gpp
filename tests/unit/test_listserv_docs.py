"""Documentation and cross-surface contracts for LISTSERV archives."""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.listserv import (
    ListservAttachment,
    ListservDetail,
    ListservDetailMetadata,
    ListservMessage,
    ListservRecord,
    ListservSearchRecord,
    ListservSearchResult,
)
from doc3gpp.models.schema_info import schema_payload
from doc3gpp.settings.config_writer import load_default_template, read_toml
from doc3gpp.storage.db.models import (
    ListservArchivePageORM,
    ListservDetailORM,
    ListservMessageORM,
)
from doc3gpp.web.mcp_server import build_mcp_server
from doc3gpp.web.routes.listserv import search_listserv, show_listserv_message

DOC_ROOT = Path("docs")
DOCS = (
    "cli.md",
    "config.md",
    "processes.md",
    "architecture.md",
    "code-map.md",
    "web-server.md",
    "mcp.md",
    "3gpp-knowledge.md",
    "known-constraints.md",
)
LIST_ID = "RAN-WG1"
MESSAGE_URL = "https://list.etsi.org/scripts/wa.exe?A2=RAN-WG1;2026-10-15;EXACT"


def test_cli_guide_documents_sync() -> None:
    assert "doc3gpp listserv sync" in Path("docs/cli.md").read_text(
        encoding="utf-8"
    )


def test_mcp_enqueue_result_documents_exact_response_fields() -> None:
    text = Path("docs/mcp.md").read_text(encoding="utf-8")
    normalized_text = " ".join(text.split())
    listserv_section = text.split("### LISTSERV archives", 1)[1].split(
        "## Jobs", 1
    )[0]
    normalized = " ".join(listserv_section.split())
    result_contract = re.search(
        r"Enqueue results contain exactly (.*?);", normalized
    )

    assert result_contract is not None
    assert (
        result_contract.group(1)
        == "`job_id`, `status`, `message`, and `links`"
    )
    assert (
        "`links` contains the job-detail `self` link and the SSE `events` link"
        in normalized
    )
    assert (
        "LISTSERV enqueue tools use the distinct `job_id`/`status`/`url` contract"
        not in normalized_text
    )
    assert (
        "MCP enqueue tools create durable work rather than blocking on downloads/parsing."
        in normalized_text
    )
    assert (
        "Their returned JSON uses the same `job_id`, `status`, `message`, and `links` "
        "shape, including the LISTSERV enqueue tools."
        in normalized_text
    )


def test_listserv_guides_cover_the_public_contract() -> None:
    requirements = {
        "docs/config.md": (
            "max_content_chars = 10000",
            "auto_index_on_sync",
            "auto_embed_on_parse",
        ),
        "docs/processes.md": (
            "actual archive-page URL",
            "preceding calendar year",
            "displayed calendar date",
            "unknown-zone",
            "200 characters",
            "build_listserv_sync_service",
            "index_failed",
            "cancelled",
            "stdio",
        ),
        "docs/architecture.md": ("listserv_messages", "payload serializers"),
        "docs/code-map.md": (
            "build_listserv_sync_service",
            "build_listserv_semantic_service",
            "listserv_index.py",
            "listserv_jobs.py",
        ),
        "docs/web-server.md": (
            "/api/listserv/sync",
            "/api/listserv/index/status",
            "content_truncated",
            "preview_truncated",
            "completed_at",
            "index_failures",
        ),
        "docs/mcp.md": (
            "listserv_sync",
            "listserv_parse",
            "listserv_list",
            "listserv_search",
            "listserv_show",
            "listserv_index_status",
            "listserv_index_rebuild",
            'session.call_tool(\n    "listserv_search"',
        ),
        "docs/3gpp-knowledge.md": ("list.etsi.org", "allowlist"),
        "docs/known-constraints.md": (
            "attachment bytes",
            "best-effort",
            "content_truncated",
        ),
        "AGENTS.md": ("LISTSERV", "services/listserv_sync.py"),
    }
    for path, phrases in requirements.items():
        text = Path(path).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in text, f"{path} is missing {phrase!r}"


def test_generated_config_template_has_listserv_content_limit(tmp_path: Path) -> None:
    template_path = tmp_path / "doc3gpp.toml"
    template_path.write_text(load_default_template(), encoding="utf-8")

    settings = read_toml(template_path)

    assert settings["listserv"]["max_content_chars"] == 10000


def test_cli_help_names_all_six_listserv_commands() -> None:
    result = CliRunner().invoke(app, ["listserv", "--help"])

    assert result.exit_code == 0
    assert all(
        command in result.output
        for command in ("sync", "parse", "list", "search", "show", "index")
    )


def test_listserv_search_help_matches_documented_format_scope() -> None:
    result = CliRunner().invoke(app, ["listserv", "search", "--help"])

    assert result.exit_code == 0
    help_text = result.output.lower()
    assert "--format" in help_text
    assert "table or json" in help_text
    assert "--output" not in help_text
    assert "--compact" not in help_text

    cli_guide = Path("docs/cli.md").read_text(encoding="utf-8")
    listserv_section = re.search(
        r"^## LISTSERV commands\s*$\n(?P<section>.*?)(?=^## |\Z)",
        cli_guide,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert listserv_section is not None
    section = listserv_section.group("section")
    assert "`--format table|json`" in section
    assert "--output" not in section
    assert "--compact" not in section
    for line in section.splitlines():
        if line.startswith("doc3gpp listserv ") and "--format" in line:
            assert re.search(r"--format\s+(?:table|json)\b", line), line


def test_markdown_link_resolver_supports_local_anchors(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    guide = tmp_path / "Guide with spaces.md"
    source.write_text(
        "# Same File\n"
        "[same-file](#same-file)\n"
        "[relative](Guide%20with%20spaces.md#linked-heading)\n"
        "[encoded](Guide%20with%20spaces.md#caf%C3%A9)\n"
        "[duplicate](Guide%20with%20spaces.md#repeat-1)\n"
        "[explicit id](Guide%20with%20spaces.md#explicit-id)\n"
        "[explicit name](Guide%20with%20spaces.md#legacy-name)\n"
        "[external](https://example.invalid/guide#not-a-real-anchor)\n",
        encoding="utf-8",
    )
    guide.write_text(
        "# Linked Heading\n\n"
        "# Café\n\n"
        "# Repeat\n"
        "# Repeat\n\n"
        '<div id="explicit-id"></div>\n'
        '<a name="legacy-name"></a>\n'
        "```markdown\n"
        "# Fenced Heading\n"
        "```\n",
        encoding="utf-8",
    )

    _assert_markdown_links_resolve(source)


@pytest.mark.parametrize(
    ("target", "guide_text"),
    (
        ("missing%20guide.md#topic", None),
        ("guide.md#missing-topic", "# Existing Topic\n"),
    ),
)
def test_markdown_link_resolver_reports_missing_paths_and_fragments(
    tmp_path: Path, target: str, guide_text: str | None
) -> None:
    source = tmp_path / "source.md"
    source.write_text(f"[broken]({target})\n", encoding="utf-8")
    if guide_text is not None:
        (tmp_path / "guide.md").write_text(guide_text, encoding="utf-8")

    with pytest.raises(AssertionError) as exc_info:
        _assert_markdown_links_resolve(source)

    assert str(source) in str(exc_info.value)
    assert target in str(exc_info.value)


def test_markdown_link_resolver_does_not_anchor_fenced_headings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    guide = tmp_path / "guide.md"
    target = "guide.md#not-an-anchor"
    source.write_text(f"[fenced heading]({target})\n", encoding="utf-8")
    guide.write_text(
        "```markdown\n# Not an anchor\n```\n", encoding="utf-8"
    )

    with pytest.raises(AssertionError) as exc_info:
        _assert_markdown_links_resolve(source)

    assert str(source) in str(exc_info.value)
    assert target in str(exc_info.value)


_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_ATX_HEADING_PATTERN = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_SETEXT_HEADING_PATTERN = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")


def _markdown_lines_without_fences(markdown: str) -> list[str]:
    visible_lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines():
        fence = _FENCE_PATTERN.match(line)
        if fence_character is not None:
            if (
                fence is not None
                and fence.group(1)[0] == fence_character
                and len(fence.group(1)) >= fence_length
                and not fence.group(2).strip()
            ):
                fence_character = None
                fence_length = 0
            visible_lines.append("")
            continue
        if fence is not None:
            if fence.group(1)[0] == "`" and "`" in fence.group(2):
                visible_lines.append(line)
                continue
            fence_character = fence.group(1)[0]
            fence_length = len(fence.group(1))
            visible_lines.append("")
            continue
        visible_lines.append(line)
    return visible_lines


def _github_heading_slug(heading: str) -> str:
    text = html.unescape(heading)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`+", "", text).lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text.strip())


class _HTMLAnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()

    def handle_starttag(
        self, _tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.anchors.update(
            value
            for name, value in attrs
            if name.lower() in {"id", "name"} and value
        )


def _markdown_anchors(markdown: str) -> set[str]:
    lines = _markdown_lines_without_fences(markdown)
    anchors: set[str] = set()
    heading_counts: dict[str, int] = {}

    def add_heading(text: str) -> None:
        slug = _github_heading_slug(text)
        if not slug:
            return
        duplicate_number = heading_counts.get(slug, 0)
        anchors.add(slug if duplicate_number == 0 else f"{slug}-{duplicate_number}")
        heading_counts[slug] = duplicate_number + 1

    for index, line in enumerate(lines):
        atx_heading = _ATX_HEADING_PATTERN.match(line)
        if atx_heading is not None:
            heading = re.sub(r"[ \t]+#+[ \t]*$", "", atx_heading.group(2)).strip()
            add_heading(heading)
            continue
        if index == 0 or not _SETEXT_HEADING_PATTERN.match(line):
            continue
        preceding = lines[index - 1].strip()
        if preceding and _ATX_HEADING_PATTERN.match(lines[index - 1]) is None:
            add_heading(preceding)

    html_anchors = _HTMLAnchorParser()
    html_anchors.feed("\n".join(lines))
    html_anchors.close()
    anchors.update(html_anchors.anchors)
    return anchors


def _markdown_link_destination(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")]
    return target.split(maxsplit=1)[0]


def _assert_markdown_links_resolve(source: Path) -> None:
    source_text = source.read_text(encoding="utf-8")
    visible_text = "\n".join(_markdown_lines_without_fences(source_text))
    for raw_target in _MARKDOWN_LINK_PATTERN.findall(visible_text):
        target = _markdown_link_destination(raw_target)
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not (parsed.path or parsed.fragment):
            continue
        linked_path = (
            source.resolve()
            if not parsed.path
            else (source.parent / unquote(parsed.path)).resolve()
        )
        assert linked_path.exists(), (
            f"{source}: broken link {target!r}: target file "
            f"{linked_path} does not exist"
        )
        if parsed.fragment:
            fragment = unquote(parsed.fragment)
            anchors = _markdown_anchors(linked_path.read_text(encoding="utf-8"))
            assert fragment in anchors, (
                f"{source}: broken link {target!r}: missing fragment "
                f"{fragment!r} in {linked_path}"
            )


def test_schema_reporting_matches_listserv_orm_columns() -> None:
    from doc3gpp.storage.db.models import ListservListORM

    orm_by_table = {
        model.__tablename__: model
        for model in (
            ListservListORM,
            ListservMessageORM,
            ListservDetailORM,
            ListservArchivePageORM,
        )
    }
    reported: dict[str, dict[str, bool]] = {}
    for row in schema_payload("listserv"):
        reported.setdefault(str(row["table"]), {})[str(row["field"])] = bool(
            row["nullable"]
        )

    assert set(reported) == set(orm_by_table)
    for table, model in orm_by_table.items():
        columns = model.__table__.columns
        assert set(reported[table]) == set(columns.keys())
        assert reported[table] == {
            column.name: bool(column.nullable) for column in columns
        }


def test_documented_relative_links_resolve() -> None:
    for relative_doc in DOCS:
        source = DOC_ROOT / relative_doc
        _assert_markdown_links_resolve(source)


def _record_and_search_result() -> tuple[ListservRecord, ListservSearchResult]:
    message = ListservMessage(
        url=MESSAGE_URL,
        list_id=LIST_ID,
        subject="Stored R1-2610793",
        sender="[log in to unmask]",
        date=datetime.fromisoformat("2026-10-15T10:30:00+02:00"),
        date_precision="datetime",
        week="2026-10-W3",
        subject_tdocs=("R1-2610793",),
    )
    detail = ListservDetail(
        url=MESSAGE_URL,
        attachments=(
            ListservAttachment("contribution.docx", "https://list.etsi.org/a/R1-2610793"),
        ),
        tdocs=("R1-2610793",),
        content="x" * 250,
        content_truncated=True,
        parser_version="1.0.0",
        parsed_at=datetime.fromisoformat("2026-10-16T00:00:00+00:00"),
    )
    record = ListservRecord(
        message=message, detail=detail, tdocs=("R1-2610793",), source_revision=1
    )
    search_record = ListservSearchRecord(
        message=message,
        detail=ListservDetailMetadata(
            attachments=detail.attachments,
            content_truncated=detail.content_truncated,
            parser_version=detail.parser_version,
            parsed_at=detail.parsed_at,
        ),
        tdocs=record.tdocs,
        source_revision=record.source_revision,
    )
    return record, ListservSearchResult(
        record=search_record,
        score=None,
        snippet=None,
        matched_source=None,
        content_preview="x" * 200,
        preview_truncated=True,
    )


def test_http_mcp_and_cli_json_keep_shared_listserv_fields(monkeypatch) -> None:
    from doc3gpp.models.listserv_payloads import (
        listserv_record_payload,
        listserv_search_payload,
    )
    from doc3gpp.services import factory
    from doc3gpp.storage import db

    record, search_result = _record_and_search_result()
    record_service = SimpleNamespace(
        list_ids=lambda: [LIST_ID], show=lambda url: record if url == MESSAGE_URL else None
    )
    search_service = SimpleNamespace(
        search=lambda **_kwargs: [search_result], close=lambda: None
    )

    monkeypatch.setattr(factory, "build_listserv_service", lambda: record_service)
    monkeypatch.setattr(
        factory, "build_listserv_search_service", lambda: search_service
    )
    monkeypatch.setattr(db.migrate, "create_schema", lambda *_args, **_kwargs: None)

    http_show = show_listserv_message(url=MESSAGE_URL, service=record_service)
    http_search = search_listserv(
        date=None,
        list_id=None,
        sender=None,
        subject=None,
        tdocs=None,
        text=None,
        semantic=None,
        limit=20,
        offset=0,
        service=search_service,
    )

    runner = CliRunner()
    cli_show_result = runner.invoke(
        app, ["listserv", "show", MESSAGE_URL, "--format", "json"]
    )
    cli_search_result = runner.invoke(app, ["listserv", "search", "--format", "json"])
    assert cli_show_result.exit_code == 0, cli_show_result.output
    assert cli_search_result.exit_code == 0, cli_search_result.output
    cli_show = json.loads(cli_show_result.output)
    cli_search = json.loads(cli_search_result.output)

    mcp_state = SimpleNamespace(
        settings=object(),
        services=SimpleNamespace(
            tdoc_content=object(),
            listserv=record_service,
            listserv_search=search_service,
            listserv_index=SimpleNamespace(
                status=lambda: SimpleNamespace(to_dict=dict)
            ),
            job_repo=object(),
        ),
    )
    mcp_server = build_mcp_server(mcp_state)

    async def call_mcp(name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = await mcp_server.call_tool(name, arguments)
        assert result.is_error is False
        return json.loads(result.content[0].text)

    mcp_show = asyncio.run(call_mcp("listserv_show", {"url": MESSAGE_URL}))
    mcp_search = asyncio.run(call_mcp("listserv_search", {}))[0]
    expected_show = listserv_record_payload(record)
    expected_search = listserv_search_payload(search_result)

    assert http_show == expected_show == cli_show == mcp_show
    assert http_search == [expected_search]
    assert cli_search == [expected_search]
    assert mcp_search == expected_search
    for payload in (http_show, cli_show, mcp_show):
        assert payload["url"] == MESSAGE_URL
        assert payload["tdocs"] == ["R1-2610793"]
        assert payload["content_truncated"] is True
    for payload in (http_search[0], cli_search[0], mcp_search):
        assert payload["url"] == MESSAGE_URL
        assert payload["tdocs"] == ["R1-2610793"]
        assert payload["content_truncated"] is True
        assert payload["preview_truncated"] is True
