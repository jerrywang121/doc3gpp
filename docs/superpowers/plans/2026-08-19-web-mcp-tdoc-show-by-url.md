# Web + MCP URL-anchored TDoc read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface `doc3gpp tdoc show --ftp-url <url>` over the web (`GET /tdocs/by-url?ftp_url=<url>`) and the MCP `get_tdoc(ftp_url=...)` tool, with byte-identical JSON output to the CLI's `--format json` path and a polymorphic HTML template that renders either record shape.

**Architecture:** Single source of truth is `TDocShowRecordByUrl.from_ftp_url(ftp_url, repos)` (`src/doc3gpp/models/tdoc_show.py:163`). The web route and MCP tool each normalise the URL via `parsers.normalizers.normalize_ftp_path`, build a `TDocShowRepos`, call the classmethod, and emit the same `render.to_jsonable(record)` payload the CLI emits. Auto-sync is never triggered in URL mode (CLI parity). A new `TDocUrlNotFoundError(LookupError)` exception slots into the existing `_MCP_RESOURCE_BY_EXC` / `_ERROR_SLUGS` / `_STATUS_BY_EXC` tables for 404 / `-32004` mapping. `tdoc_show.html` is polymorphed: sections that gate on `record.tdoc` are skipped in URL mode; sections that gate on `record.cover` / `record.ttcn` / `record.files` work as-is.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2, Pydantic v2, SQLAlchemy 2.0, pytest, sqlite (test fixture).

**Spec:** `docs/superpowers/specs/2026-08-19-web-mcp-tdoc-show-by-url-design.md`

**Working directory:** Branch `web-mcp-tdoc-show-by-url` (already created, design doc committed at `5eec002`).

---

## Global Constraints

- Python 3.10+ syntax (PEP 604 unions, no `from __future__ import annotations` removal needed).
- SQLAlchemy 2.0 `select()` / `session.scalars()` style; no legacy `query()`.
- All HTTP `?format=json` responses and MCP tool results use `render.to_jsonable(...)` for byte parity with the CLI.
- The CLI is **not** modified; the existing CLI `--ftp-url` path keeps its current behaviour.
- Auto-sync (`trigger_auto_sync`) is never called from URL mode.
- `LookupError` → HTTP 404, MCP `-32004` (`MCP_CODE_NOT_FOUND`) — keep the three tables in lock-step.
- Tests use the existing `client` / `sqlite_env` fixtures from `tests/unit/test_web_routes.py` (FastAPI `TestClient`, in-process sqlite).
- Ruff is the only configured linter (`ruff check .`).
- Per `docs/conventions.md` §"Documentation sync", update `docs/web-server.md` and `AGENTS.md` in the same change set.

---

## Task 1: Add `TDocUrlNotFoundError` exception

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (next to `TDocNotFoundError` near line 281)
- Modify: `src/doc3gpp/web/errors.py` (lines 93-104, 130-142, 145-158)
- Test: `tests/unit/test_tdoc_url_not_found.py` (new file)

**Interfaces:**
- Produces: `class TDocUrlNotFoundError(LookupError)` with attribute `ftp_url: str` and message hinting at `doc3gpp tdoc sync` / `doc3gpp tdoc parse --from-url`.
- Consumes: nothing new.

- [ ] **Step 1.1: Write the failing test**

Create `tests/unit/test_tdoc_url_not_found.py`:

```python
"""Unit tests for the new TDocUrlNotFoundError exception class."""
from __future__ import annotations

from doc3gpp.services.tdoc_cr_service import TDocUrlNotFoundError
from doc3gpp.web.errors import _ERROR_SLUGS, _MCP_RESOURCE_BY_EXC, _STATUS_BY_EXC


def test_tdoc_url_not_found_is_lookup_error() -> None:
    err = TDocUrlNotFoundError("TSG_RAN/WG5/foo.zip")
    assert isinstance(err, LookupError)
    assert err.ftp_url == "TSG_RAN/WG5/foo.zip"


def test_tdoc_url_not_found_message_mentions_sync_hint() -> None:
    err = TDocUrlNotFoundError("TSG_RAN/WG5/foo.zip")
    msg = str(err)
    assert "TSG_RAN/WG5/foo.zip" in msg
    assert "doc3gpp tdoc sync" in msg
    assert "doc3gpp tdoc parse --from-url" in msg


def test_tdoc_url_not_found_is_registered_in_error_slugs() -> None:
    assert _ERROR_SLUGS[TDocUrlNotFoundError] == "tdoc_url_not_found"


def test_tdoc_url_not_found_is_registered_in_mcp_table() -> None:
    resource, code = _MCP_RESOURCE_BY_EXC[TDocUrlNotFoundError]
    assert resource == "tdoc"
    # MCP_CODE_NOT_FOUND is -32004
    assert code == -32004


def test_tdoc_url_not_found_is_registered_in_status_table() -> None:
    assert _STATUS_BY_EXC[TDocUrlNotFoundError] == 404
```

- [ ] **Step 1.2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_tdoc_url_not_found.py -v`
Expected: FAIL with `ImportError: cannot import name 'TDocUrlNotFoundError'` and `KeyError: TDocUrlNotFoundError` on the table lookups.

- [ ] **Step 1.3: Add the exception class**

In `src/doc3gpp/services/tdoc_cr_service.py`, immediately after the existing `TDocNotYetOnFTPError` class (around line 281), add:

```python
class TDocUrlNotFoundError(LookupError):
    """No row matches the requested FTP URL across any of the six URL-keyed tables.

    Raised by ``TDocShowRecordByUrl.from_ftp_url`` consumers (the web
    ``GET /tdocs/by-url`` route and the MCP ``get_tdoc(ftp_url=...)``
    tool) when the normalised URL resolves to no rows in ``tdocs``,
    ``tdoc_cr_cover_page``, ``tdoc_cr_ttcn_details``, ``tdoc_extracts``,
    ``tdoc_cr_change_details``, or ``tdoc_files``. Distinct from
    :class:`TDocNotFoundError` (which is raised for a missing
    ``tdoc_id`` lookup).
    """

    def __init__(self, ftp_url: str) -> None:
        self.ftp_url = ftp_url
        super().__init__(
            f"No stored rows match ftp_url {ftp_url!r}. The URL was looked "
            "up against tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
            "tdoc_extracts, tdoc_cr_change_details, and tdoc_files; "
            "none matched. The upstream document may not have been "
            "ingested yet — run 'doc3gpp tdoc sync' on the parent "
            f"meeting, or 'doc3gpp tdoc parse --from-url {ftp_url}' to "
            "populate the URL-keyed tables."
        )
```

- [ ] **Step 1.4: Register the exception in three error tables**

In `src/doc3gpp/web/errors.py`:

1. Top of the file, alongside the existing `from doc3gpp.services.tdoc_cr_service import TDocNotFoundError`, add:

```python
from doc3gpp.services.tdoc_cr_service import (
    TDocNotFoundError,
    TDocUrlNotFoundError,
)
```

2. In `_MCP_RESOURCE_BY_EXC` (around line 93), add the entry right after `TDocNotFoundError`:

```python
    TDocUrlNotFoundError: ("tdoc", MCP_CODE_NOT_FOUND),
```

3. In `_ERROR_SLUGS` (around line 130), add:

```python
    TDocUrlNotFoundError: "tdoc_url_not_found",
```

4. In `_STATUS_BY_EXC` (around line 145), add:

```python
    TDocUrlNotFoundError: 404,
```

- [ ] **Step 1.5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_tdoc_url_not_found.py -v`
Expected: 5 passed.

- [ ] **Step 1.6: Lint**

Run: `ruff check src/doc3gpp/services/tdoc_cr_service.py src/doc3gpp/web/errors.py tests/unit/test_tdoc_url_not_found.py`
Expected: clean (no warnings).

- [ ] **Step 1.7: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py \
        src/doc3gpp/web/errors.py \
        tests/unit/test_tdoc_url_not_found.py
git commit -m "feat: add TDocUrlNotFoundError for URL-anchored reads"
```

---

## Task 2: Add `GET /tdocs/by-url` web route

**Files:**
- Modify: `src/doc3gpp/web/routes/tdocs.py` (after the existing `show_tdoc` route, around line 302)
- Test: `tests/unit/test_web_routes.py` (append tests at end)

**Interfaces:**
- Consumes: `TDocShowRepos` (existing), `TDocShowRecordByUrl.from_ftp_url` (existing), `normalize_ftp_path` (existing), `derive_cache_file` (existing), `TDocUrlNotFoundError` (from Task 1).
- Produces: GET `/tdocs/by-url` (HTML + JSON), 200/400/404.

- [ ] **Step 2.1: Write the failing route tests**

Append the following tests to `tests/unit/test_web_routes.py` (at the bottom, after the last existing function):

```python
# ---------------------------------------------------------------------------
# TDoc show --ftp-url parity surface: GET /tdocs/by-url
# ---------------------------------------------------------------------------


def test_show_tdoc_by_url_returns_404_when_no_rows(
    client: TestClient, sqlite_env: Any,
) -> None:
    """``GET /tdocs/by-url?ftp_url=<unseen>`` returns 404."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    response = client.get("/tdocs/by-url", params={"ftp_url": "TSG_RAN/missing.zip"})
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "tdoc_url_not_found"
    assert "TSG_RAN/missing.zip" in body["detail"]


def test_show_tdoc_by_url_returns_400_when_param_missing(
    client: TestClient, sqlite_env: Any,
) -> None:
    """``GET /tdocs/by-url`` without ftp_url returns 400 invalid_filter."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    response = client.get("/tdocs/by-url")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"
    assert "ftp_url" in body["detail"]


def test_show_tdoc_by_url_returns_400_on_empty_after_normalize(
    client: TestClient, sqlite_env: Any,
) -> None:
    """Empty / slash-only / ``ftp://``-only URLs all 400."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    for value in ("", "/", "ftp://", "  "):
        response = client.get("/tdocs/by-url", params={"ftp_url": value})
        assert response.status_code == 400, f"value={value!r}"
        assert response.json()["error"] == "invalid_filter"


def test_show_tdoc_by_url_full_url_matches_bare_path(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A full https URL and a bare relative path resolve the same row."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc

    create_schema()
    bare = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=bare))

    response = client.get(
        "/tdocs/by-url",
        params={"ftp_url": f"https://www.3gpp.org/ftp/{bare}"},
    )
    assert response.status_code == 200
    assert "R5s260001" in response.text


def test_show_tdoc_by_url_json_byte_matches_cli(
    client: TestClient, sqlite_env: Any,
) -> None:
    """``GET /tdocs/by-url?ftp_url=...&format=json`` is byte-identical to the CLI.

    Builds a ``TDocShowRecordByUrl`` via the same classmethod the CLI
    uses and asserts the JSON envelope matches bit-for-bit.
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.models.tdoc_show import TDocShowRepos
    from doc3gpp.web.render import to_jsonable

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    file_repo = SQLAlchemyTDocFileRepository()
    tdoc_repo.upsert(TDoc(tdoc_id="R5s260001", ftp_url=url, title="Foo"))
    cr_repo.upsert(TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001"))
    file_repo.upsert_many(
        [TDocFile(ftp_url="R5/26.001/R5s260001_rev1.zip", tdoc_id="R5s260001", type="revision")]
    )

    repos = TDocShowRepos(
        tdoc=tdoc_repo,
        cr=cr_repo,
        cr_ttcn=cr_repo,
        cr_change_details=cr_repo,
        file=file_repo,
    )
    from doc3gpp.models.tdoc_show import TDocShowRecordByUrl

    expected = TDocShowRecordByUrl.from_ftp_url(url, repos)
    expected_json = to_jsonable(expected)

    response = client.get(
        "/tdocs/by-url",
        params={"ftp_url": f"https://www.3gpp.org/ftp/{url}", "format": "json"},
    )
    assert response.status_code == 200
    assert response.json() == expected_json


def test_show_tdoc_by_url_no_parent_tdoc_renders_placeholder(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A URL with only a cover row (no parent TDoc) renders the placeholder text."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    create_schema()
    url = "R5/26.001/R5s260002.zip"
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(tdoc_id="orphan", ftp_url=url, cr_num="0002")
    )
    response = client.get("/tdocs/by-url", params={"ftp_url": url})
    assert response.status_code == 200
    assert "No parent tdocs row matches this URL" in response.text


def test_show_tdoc_by_url_parse_card_omitted_in_url_mode(
    client: TestClient, sqlite_env: Any,
) -> None:
    """URL mode never renders the Parse card (no parent TDoc to anchor on)."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    create_schema()
    url = "R5/26.001/R5s260003.zip"
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(tdoc_id="orphan", ftp_url=url, cr_num="0003")
    )
    response = client.get("/tdocs/by-url", params={"ftp_url": url})
    assert response.status_code == 200
    assert "id=\"parse-form\"" not in response.text


def test_show_tdoc_by_url_404_when_only_extracted_at_present(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A lone ``tdoc_extracts`` row (extracted_at only) still raises 404.

    The 404 rule is "every of tdoc / cover / ttcn / changes / files
    is empty" — a lone ``extracted_at`` is not a meaningful hit.
    """
    from datetime import datetime, timezone

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.models.tdoc_cr import TDocExtractMeta

    create_schema()
    url = "R5/26.001/R5s260004.zip"
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        TDocExtractMeta(
            tdoc_id="orphan",
            ftp_url=url,
            cache_file="cache.zip",
            doc_filename="doc.zip",
            parser_version="v1",
            extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    response = client.get("/tdocs/by-url", params={"ftp_url": url})
    assert response.status_code == 404
    assert response.json()["error"] == "tdoc_url_not_found"
```

- [ ] **Step 2.2: Run the new tests to verify they fail**

Run: `python -m pytest tests/unit/test_web_routes.py -v -k "by_url"`
Expected: every `test_show_tdoc_by_url_*` test fails (404 / route-not-found / 422).

- [ ] **Step 2.3: Implement the route**

In `src/doc3gpp/web/routes/tdocs.py`, after the existing `show_tdoc` route (after line 302), add:

```python
@router.get("/by-url", include_in_schema=False)
async def show_tdoc_by_url(
    request: Request,
    ftp_url: str | None = Query(default=None, alias="ftp-url"),
    format: str | None = Query(default=None, alias="format"),
    file_repo: Any = Depends(get_tdoc_file_repo),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Render ``tdoc_show.html`` polymorphed on URL mode, or return JSON.

    The URL-anchored read; mirrors ``doc3gpp tdoc show --ftp-url``.
    Auto-sync is never triggered because there is no parent TDoc /
    meeting to anchor a sync on (CLI parity).
    """
    if not ftp_url:
        raise InvalidFilterError("ftp_url query param is required")
    normalised = normalize_ftp_path(ftp_url)
    if not normalised:
        raise InvalidFilterError(
            f"ftp_url {ftp_url!r} normalised to an empty path"
        )

    repos = _build_show_repos(request, file_repo)
    record = TDocShowRecordByUrl.from_ftp_url(normalised, repos)

    if (
        record.tdoc is None
        and record.cover is None
        and record.ttcn is None
        and record.changes is None
        and not record.files
    ):
        raise TDocUrlNotFoundError(normalised)

    if format == "json":
        return JSONResponse(content=to_jsonable(record))

    has_cached_zip = False
    if record.ftp_url:
        cache_file = derive_cache_file(record.ftp_url)
        has_cached_zip = (Path(settings.cache.dir) / "zips" / cache_file).exists()

    return templates.TemplateResponse(
        request=request,
        name="tdoc_show.html",
        context={
            "active_nav": "tdocs",
            "record": record,
            "has_cached_zip": has_cached_zip,
        },
    )
```

Add the new imports at the top of the file (next to the existing
`TDocShowRecord` import around line 26):

```python
from doc3gpp.models.tdoc_show import (
    TDocShowRecord,
    TDocShowRecordByUrl,
    TDocShowRepos,
)
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.services.tdoc_cr_service import (
    TDocNotFoundError,
    TDocUrlNotFoundError,
    _read_cached_markdown_path,
)
```

(Drop the duplicate `TDocShowRecord, TDocShowRepos` if already
imported and re-import as a tuple; merge with the existing import.)

- [ ] **Step 2.4: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/test_web_routes.py -v -k "by_url"`
Expected: all `test_show_tdoc_by_url_*` tests pass.

- [ ] **Step 2.5: Lint**

Run: `ruff check src/doc3gpp/web/routes/tdocs.py tests/unit/test_web_routes.py`
Expected: clean.

- [ ] **Step 2.6: Commit**

```bash
git add src/doc3gpp/web/routes/tdocs.py tests/unit/test_web_routes.py
git commit -m "feat(web): GET /tdocs/by-url URL-anchored read"
```

---

## Task 3: Make `tdoc_show.html` polymorphic on URL mode

**Files:**
- Modify: `src/doc3gpp/web/templates/tdoc_show.html`
- Test: covered by Task 2's `test_show_tdoc_by_url_*` tests (no new tests in this task).

**Interfaces:**
- Consumes: `record` (either `TDocShowRecord` or `TDocShowRecordByUrl`), `show_mode` ("tdoc_id" default or "by_url"), `has_cached_zip`.

- [ ] **Step 3.1: Confirm the existing tests still pass against the unmodified template**

Run: `python -m pytest tests/unit/test_web_routes.py -v -k "show_tdoc and not by_url"`
Expected: all existing `test_show_tdoc_*` tests pass (no template change yet).

- [ ] **Step 3.2: Update the title and h1 to handle URL mode**

In `src/doc3gpp/web/templates/tdoc_show.html`, replace the existing first three blocks:

```html
{% block title %}doc3gpp · tdoc {{ record.tdoc.tdoc_id }}{% endblock %}
{% block content %}
  <h1>TDoc <code>{{ record.tdoc.tdoc_id }}</code></h1>
  <p class="meta">
    {% if record.tdoc.title %}<span>{{ record.tdoc.title }}</span>{% endif %}
  </p>
```

with:

```html
{% if record.tdoc %}
  {% set anchor = record.tdoc.tdoc_id %}
  {% set anchor_kind = "tdoc" %}
{% else %}
  {% set anchor = record.ftp_url %}
  {% set anchor_kind = "ftp_url" %}
{% endif %}
{% block title %}doc3gpp · {{ anchor_kind }} {{ anchor }}{% endblock %}
{% block content %}
  <h1>TDoc <code>{{ anchor }}</code></h1>
  <p class="meta">
    {% if record.tdoc and record.tdoc.title %}<span>{{ record.tdoc.title }}</span>{% endif %}
  </p>
```

- [ ] **Step 3.3: Wrap the `## TDoc` card in a `record.tdoc` guard**

Replace the entire `<section class="card">` block at lines 9-39 of
`tdoc_show.html` (the block whose `<h2>` reads `TDoc`):

```html
  <section class="card">
    <h2>TDoc</h2>
    <dl class="kv">
      <dt>Meeting ID</dt><dd>{{ record.tdoc.meeting_id or '-' }}</dd>
      ...
      <dt>Related WIs</dt><dd>{{ record.tdoc.related_wis or '-' }}</dd>
    </dl>
    {% if record.tdoc.ftp_url %}
      <p>
        <a class="btn" href="/tdocs/{{ record.tdoc.tdoc_id }}/content?format=html">View as HTML</a>
        <a class="btn" href="/tdocs/{{ record.tdoc.tdoc_id }}/content?format=markdown">Download markdown</a>
      </p>
    {% endif %}
  </section>
```

with:

```html
  {% if record.tdoc %}
    <section class="card">
      <h2>TDoc</h2>
      <dl class="kv">
        <dt>Meeting ID</dt><dd>{{ record.tdoc.meeting_id or '-' }}</dd>
        <dt>Type</dt><dd>{{ record.tdoc.type or '-' }}</dd>
        <dt>Status</dt><dd>{{ record.tdoc.status or '-' }}</dd>
        <dt>Spec</dt><dd>{{ record.tdoc.spec or '-' }}</dd>
        <dt>Version</dt><dd>{{ record.tdoc.version or '-' }}</dd>
        <dt>Release</dt><dd>{{ record.tdoc.release or '-' }}</dd>
        <dt>CR Num</dt><dd>{{ record.tdoc.cr_num or '-' }}</dd>
        <dt>CR Cat</dt><dd>{{ record.tdoc.cr_cat or '-' }}</dd>
        <dt>FTP URL</dt>
        <dd>
          {% if record.tdoc.ftp_url %}
            {% if has_cached_zip %}
              <a href="/tdocs/{{ record.tdoc.tdoc_id }}/download"><code>{{ record.tdoc.ftp_url }}</code></a>
            {% else %}
              <a href="https://www.3gpp.org/ftp/{{ record.tdoc.ftp_url }}"><code>{{ record.tdoc.ftp_url }}</code></a>
            {% endif %}
          {% else %}-{% endif %}
        </dd>
        <dt>Uploaded</dt><dd>{{ record.tdoc.uploaded_date.isoformat() if record.tdoc.uploaded_date else '-' }}</dd>
        <dt>Related WIs</dt><dd>{{ record.tdoc.related_wis or '-' }}</dd>
      </dl>
      {% if record.tdoc.ftp_url %}
        <p>
          <a class="btn" href="/tdocs/{{ record.tdoc.tdoc_id }}/content?format=html">View as HTML</a>
          <a class="btn" href="/tdocs/{{ record.tdoc.tdoc_id }}/content?format=markdown">Download markdown</a>
        </p>
      {% endif %}
    </section>
  {% else %}
    <section class="card placeholder">
      <h2>TDoc</h2>
      <p>No parent tdocs row matches this URL. The URL still surfaces in <code>tdoc_cr_cover_page</code> / <code>tdoc_cr_ttcn_details</code> / <code>tdoc_files</code> because the upstream document appeared in a sync but no parent TDoc row was stored.</p>
      {% if record.ftp_url %}
        <p>
          <a class="btn" href="https://www.3gpp.org/ftp/{{ record.ftp_url }}">Open on 3GPP FTP</a>
        </p>
      {% endif %}
    </section>
  {% endif %}
```

- [ ] **Step 3.4: Wrap the Parse card in a `record.tdoc` guard**

Replace the existing `<section class="card">` block whose `<h2>`
reads `Parse` (around lines 41-62):

```html
  {% if record.tdoc.ftp_url %}
    <section class="card">
      <h2>Parse</h2>
      ...
    </section>
  {% endif %}
```

with:

```html
  {% if record.tdoc and record.tdoc.ftp_url %}
    <section class="card">
      <h2>Parse</h2>
      <form
        id="parse-form"
        class="parse-form"
        method="post"
        action="/jobs/parse/tdocs"
        data-tdoc-id="{{ record.tdoc.tdoc_id }}"
      >
        <label class="inline-check">
          <input type="checkbox" name="force"> Force re-parse
        </label>
        <label class="inline-check">
          <input type="checkbox" name="full"> Full extraction
        </label>
        <button type="submit" class="btn primary">Parse this TDoc</button>
        <span class="parse-queued" style="display:none">Parse job queued</span>
      </form>
      <div id="parse-job-target"></div>
    </section>
  {% endif %}
```

- [ ] **Step 3.5: Update the auxiliary files empty placeholder hint for URL mode**

Replace the existing empty-placeholder block at lines 163-168:

```html
  {% else %}
    <section class="card placeholder">
      <h2>Auxiliary files</h2>
      <p>No auxiliary files attached.</p>
    </section>
  {% endif %}
```

with:

```html
  {% else %}
    <section class="card placeholder">
      <h2>Auxiliary files</h2>
      {% if record.tdoc %}
        <p>No auxiliary files attached.</p>
      {% else %}
        <p>No auxiliary files match this URL. Run <code>doc3gpp tdoc parse --from-url {{ record.ftp_url }}</code> to populate auxiliary rows.</p>
      {% endif %}
    </section>
  {% endif %}
```

- [ ] **Step 3.6: Run all show_tdoc tests (existing + new)**

Run: `python -m pytest tests/unit/test_web_routes.py -v -k "show_tdoc"`
Expected: every `test_show_tdoc_*` and `test_show_tdoc_by_url_*` test passes (no regressions on the tdoc_id path; new URL-mode tests pass).

- [ ] **Step 3.7: Lint**

Run: `ruff check .` (templates aren't linted; this catches any stray Python regression in the routes.)
Expected: clean.

- [ ] **Step 3.8: Commit**

```bash
git add src/doc3gpp/web/templates/tdoc_show.html
git commit -m "feat(web): polymorphic tdoc_show.html for URL-anchored record"
```

---

## Task 4: Extend MCP `get_tdoc` tool with `ftp_url` arg

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py` (the existing `get_tdoc` tool, around line 284)
- Test: `tests/unit/web/test_mcp_server.py` (new tests; create the file if it doesn't exist)

**Interfaces:**
- Consumes: `TDocShowRepos` (existing), `TDocShowRecordByUrl.from_ftp_url` (existing), `normalize_ftp_path` (existing), `TDocUrlNotFoundError` (from Task 1), `InvalidFilterError` (existing).
- Produces: extended `get_tdoc(tdoc_id=None, ftp_url=None)` tool with XOR validator; JSON string returned via `_to_json`.

- [ ] **Step 4.1: Write the failing MCP tool tests**

Check whether `tests/unit/web/test_mcp_server.py` exists. If not, create it:

```python
"""Unit tests for the MCP ``get_tdoc`` tool (tdoc_id / ftp_url XOR)."""
from __future__ import annotations

import json
from typing import Any

import pytest


@pytest.fixture
def mcp_state(sqlite_env: Any) -> Any:
    """Build a minimal MCP state for the ``get_tdoc`` tool."""
    from doc3gpp.web.state import WebState
    from doc3gpp.services.factory import (
        build_meeting_service,
        build_tdoc_repository,
        build_tdoc_file_repository,
    )

    state = WebState.__new__(WebState)
    state.settings = sqlite_env["settings"]
    state.services = type("S", (), {})()
    state.services.meeting = build_meeting_service()
    state.services.tdoc = build_tdoc_repository()
    state.services.tdoc_file_repo = build_tdoc_file_repository()
    state.services.tsg = None  # not exercised here
    state.services.wi = None
    state.services.spec = None
    state.services.search = None
    state.services.semantic_search = None
    state.services.job_repo = None
    state.jobs = None
    return state


def test_mcp_get_tdoc_xor_validator_rejects_both(mcp_state: Any) -> None:
    from doc3gpp.web.mcp_server import build_mcp_server

    server = build_mcp_server(mcp_state)
    tool = next(t for t in server._tool_manager._tools.values() if t.name == "get_tdoc")
    with pytest.raises(Exception) as exc_info:
        tool.fn(tdoc_id="R5s260001", ftp_url="TSG_RAN/foo.zip")
    assert "exactly one of tdoc_id or ftp_url" in str(exc_info.value).lower()


def test_mcp_get_tdoc_xor_validator_rejects_neither(mcp_state: Any) -> None:
    from doc3gpp.web.mcp_server import build_mcp_server

    server = build_mcp_server(mcp_state)
    tool = next(t for t in server._tool_manager._tools.values() if t.name == "get_tdoc")
    with pytest.raises(Exception) as exc_info:
        tool.fn()
    assert "exactly one of tdoc_id or ftp_url" in str(exc_info.value).lower()


def test_mcp_get_tdoc_404_on_no_rows(mcp_state: Any) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.mcp_server import build_mcp_server

    create_schema()
    server = build_mcp_server(mcp_state)
    tool = next(t for t in server._tool_manager._tools.values() if t.name == "get_tdoc")
    with pytest.raises(Exception) as exc_info:
        tool.fn(ftp_url="TSG_RAN/missing.zip")
    assert "no stored rows match ftp_url" in str(exc_info.value).lower()


def test_mcp_get_tdoc_url_normalisation(mcp_state: Any) -> None:
    """A full https URL and a bare relative path resolve the same record."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.web.mcp_server import build_mcp_server

    create_schema()
    bare = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=bare))

    server = build_mcp_server(mcp_state)
    tool = next(t for t in server._tool_manager._tools.values() if t.name == "get_tdoc")
    payload_full = tool.fn(ftp_url=f"https://www.3gpp.org/ftp/{bare}")
    payload_bare = tool.fn(ftp_url=bare)
    assert payload_full == payload_bare


def test_mcp_get_tdoc_by_url_returns_json_envelope(mcp_state: Any) -> None:
    """The URL-mode JSON envelope mirrors the CLI ``--format json`` shape."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.web.mcp_server import build_mcp_server

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=url))
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001")
    )
    SQLAlchemyTDocFileRepository().upsert_many(
        [TDocFile(ftp_url="R5/26.001/R5s260001_rev1.zip", tdoc_id="R5s260001", type="revision")]
    )

    server = build_mcp_server(mcp_state)
    tool = next(t for t in server._tool_manager._tools.values() if t.name == "get_tdoc")
    payload = tool.fn(ftp_url=url)
    parsed = json.loads(payload)
    assert parsed["ftp_url"] == url
    assert parsed["tdoc"]["tdoc_id"] == "R5s260001"
    assert parsed["cover"]["cr_num"] == "0001"
    assert len(parsed["files"]) == 1


def test_mcp_get_tdoc_existing_tdoc_id_path_unchanged(mcp_state: Any) -> None:
    """Regression: the ``tdoc_id`` path still works (no behavioural change)."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.web.mcp_server import build_mcp_server

    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url="x.zip"))
    server = build_mcp_server(mcp_state)
    tool = next(t for t in server._tool_manager._tools.values() if t.name == "get_tdoc")
    payload = json.loads(tool.fn(tdoc_id="R5s260001"))
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"
```

- [ ] **Step 4.2: Run the new tests to verify they fail**

Run: `python -m pytest tests/unit/web/test_mcp_server.py -v`
Expected: `test_mcp_get_tdoc_xor_validator_rejects_both` and `test_mcp_get_tdoc_xor_validator_rejects_neither` fail because the current `get_tdoc` tool doesn't accept `ftp_url`. The other tests also fail.

- [ ] **Step 4.3: Extend the `get_tdoc` tool**

In `src/doc3gpp/web/mcp_server.py`, replace the existing `get_tdoc`
tool definition (around line 284-301):

```python
    @server.tool(name="get_tdoc", description="Get a single tdoc by id, including its cover-page and extract details.")
    @_mcp_error_guard
    def get_tdoc(tdoc_id: Annotated[str, Field(description="Canonical tdoc id (e.g. 'R5-260013').")]) -> str:
        from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import SQLAlchemyTDocCrTtcnRepository
        from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import SQLAlchemyTDocCrChangeDetailsRepository
        from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
        from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
        from doc3gpp.web.routes.tdocs import TDocShowRepos, TDocShowRecord

        repos = TDocShowRepos(
            tdoc=SQLAlchemyTDocRepository(),
            cr=SQLAlchemyTDocCrRepository(),
            cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
            cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
            file=services.tdoc_file_repo,
        )
        record = TDocShowRecord.from_tdoc_id(tdoc_id, repos)
        return _to_json(render.to_jsonable(record))
```

with:

```python
    @server.tool(
        name="get_tdoc",
        description=(
            "Get a single tdoc by id (or by FTP URL), including its cover-page, "
            "TTCN sidecar, and extract metadata. Exactly one of `tdoc_id` or "
            "`ftp_url` must be supplied. In URL mode the response surfaces every "
            "row across tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
            "tdoc_files, and tdoc_extracts whose ftp_url matches; auto-sync is "
            "never triggered."
        ),
    )
    @_mcp_error_guard
    def get_tdoc(
        tdoc_id: Annotated[
            str | None,
            Field(description="Canonical tdoc id (e.g. 'R5-260013'). Mutually exclusive with ftp_url."),
        ] = None,
        ftp_url: Annotated[
            str | None,
            Field(
                description=(
                    "3GPP FTP URL (full URL or relative path) — surfaces every row "
                    "across the four URL-keyed tables whose ftp_url matches. "
                    "Mutually exclusive with tdoc_id."
                )
            ),
        ] = None,
    ) -> str:
        from doc3gpp.models.tdoc_show import (
            TDocShowRecord,
            TDocShowRecordByUrl,
            TDocShowRepos,
        )
        from doc3gpp.parsers.normalizers import normalize_ftp_path
        from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
            SQLAlchemyTDocCrChangeDetailsRepository,
        )
        from doc3gpp.storage.repositories.tdoc_cr_sql import (
            SQLAlchemyTDocCrRepository,
        )
        from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
            SQLAlchemyTDocCrTtcnRepository,
        )
        from doc3gpp.storage.repositories.tdoc_sql import (
            SQLAlchemyTDocRepository,
        )

        if (tdoc_id is None) == (ftp_url is None):
            raise InvalidFilterError(
                "Provide exactly one of tdoc_id or ftp_url"
            )

        repos = TDocShowRepos(
            tdoc=SQLAlchemyTDocRepository(),
            cr=SQLAlchemyTDocCrRepository(),
            cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
            cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
            file=services.tdoc_file_repo,
        )

        if ftp_url is not None:
            normalised = normalize_ftp_path(ftp_url)
            if not normalised:
                raise InvalidFilterError(
                    f"ftp_url {ftp_url!r} normalised to an empty path"
                )
            record = TDocShowRecordByUrl.from_ftp_url(normalised, repos)
            if (
                record.tdoc is None
                and record.cover is None
                and record.ttcn is None
                and record.changes is None
                and not record.files
            ):
                raise TDocUrlNotFoundError(normalised)
        else:
            record = TDocShowRecord.from_tdoc_id(tdoc_id, repos)

        return _to_json(render.to_jsonable(record))
```

Add the new import for `TDocUrlNotFoundError` at the top of
`mcp_server.py` next to the existing `TDocNotFoundError` import.

- [ ] **Step 4.4: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/web/test_mcp_server.py -v`
Expected: every test passes.

- [ ] **Step 4.5: Run the existing MCP end-to-end test to confirm no regressions**

Run: `python -m pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: existing tests pass (the registered-tool-names assertion should already include `get_tdoc`; the new `ftp_url` arg just adds a schema field).

- [ ] **Step 4.6: Lint**

Run: `ruff check src/doc3gpp/web/mcp_server.py tests/unit/web/test_mcp_server.py`
Expected: clean.

- [ ] **Step 4.7: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/unit/web/test_mcp_server.py
git commit -m "feat(mcp): get_tdoc accepts ftp_url (XOR with tdoc_id)"
```

---

## Task 5: Integration parity test — HTTP ↔ MCP ↔ CLI

**Files:**
- Modify: `tests/integration/test_web_end_to_end.py` (add a parity block)
- Modify: `tests/integration/test_mcp_end_to_end.py` (add a parity check)

**Interfaces:**
- Consumes: FastAPI `TestClient`, the live `MCPServer` mount, the seeded fixtures.

- [ ] **Step 5.1: Write the failing HTTP/MCP/CLI parity test**

Append to `tests/integration/test_web_end_to_end.py`:

```python
def test_get_tdoc_by_url_byte_parity_with_cli(client: TestClient) -> None:
    """``GET /tdocs/by-url?ftp_url=<url>&format=json`` matches the CLI's ``--format json`` byte-for-byte.

    The CLI's ``tdoc show --ftp-url`` path renders the same
    ``TDocShowRecordByUrl`` DTO via ``render.to_jsonable``; the HTTP
    route does the same. Both must emit identical bytes.
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.models.tdoc_show import TDocShowRepos, TDocShowRecordByUrl
    from doc3gpp.web.render import to_jsonable

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    file_repo = SQLAlchemyTDocFileRepository()
    tdoc_repo.upsert(TDoc(tdoc_id="R5s260001", ftp_url=url, title="Parity"))
    cr_repo.upsert(TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001"))
    file_repo.upsert_many(
        [TDocFile(ftp_url="R5/26.001/R5s260001_rev1.zip", tdoc_id="R5s260001", type="revision")]
    )

    expected = TDocShowRecordByUrl.from_ftp_url(
        url,
        TDocShowRepos(
            tdoc=tdoc_repo,
            cr=cr_repo,
            cr_ttcn=cr_repo,
            cr_change_details=cr_repo,
            file=file_repo,
        ),
    )
    expected_json = to_jsonable(expected)

    response = client.get(
        "/tdocs/by-url",
        params={"ftp_url": url, "format": "json"},
    )
    assert response.status_code == 200
    assert response.json() == expected_json
```

In `tests/integration/test_mcp_end_to_end.py`, find the registered-
tool-names assertion (search for `registered tool names` or the
existing `assert "get_tdoc" in ...` line) and add an MCP/HTTP parity
test in the same file:

```python
def test_mcp_get_tdoc_by_url_matches_http_route(client: TestClient) -> None:
    """MCP ``get_tdoc(ftp_url=...)`` output equals HTTP ``/tdocs/by-url?ftp_url=...&format=json``."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    import json

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=url))
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001")
    )

    http_payload = client.get(
        "/tdocs/by-url", params={"ftp_url": url, "format": "json"}
    ).json()

    # MCP envelope: hit the same tool via the registered tool manager.
    from doc3gpp.web.mcp_server import build_mcp_server
    server = build_mcp_server(client.app.state)
    tool = next(
        t for t in server._tool_manager._tools.values() if t.name == "get_tdoc"
    )
    mcp_payload = json.loads(tool.fn(ftp_url=url))

    assert mcp_payload == http_payload
```

- [ ] **Step 5.2: Run the new tests to verify they pass**

Run: `python -m pytest tests/integration/test_web_end_to_end.py::test_get_tdoc_by_url_byte_parity_with_cli tests/integration/test_mcp_end_to_end.py::test_mcp_get_tdoc_by_url_matches_http_route -v`
Expected: both pass.

- [ ] **Step 5.3: Run the existing integration suite for regressions**

Run: `python -m pytest tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py -v`
Expected: every test passes (no regressions on the existing routes / tools).

- [ ] **Step 5.4: Lint**

Run: `ruff check tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py`
Expected: clean.

- [ ] **Step 5.5: Commit**

```bash
git add tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py
git commit -m "test: HTTP/MCP/CLI byte-parity for tdoc show --ftp-url"
```

---

## Task 6: Update docs

**Files:**
- Modify: `docs/web-server.md` (HTTP routes table, MCP tools description, TDoc detail page prose)
- Modify: `AGENTS.md` (the "Workflows in one line" entry for `tdoc show --ftp-url`)

**Interfaces:**
- Consumes: existing prose paragraphs in `docs/web-server.md`; the existing entry for `tdoc show --ftp-url` in `AGENTS.md`.

- [ ] **Step 6.1: Update `docs/web-server.md` HTTP routes table**

Find the existing HTTP routes table (around line 171) and add a new
row immediately after the existing `GET /tdocs/{id}/download` row:

```markdown
| GET | `/tdocs/by-url` | URL-anchored TDoc show (`?ftp_url=<url>`; HTML or JSON). 404 when the URL matches no row in any of the six URL-keyed tables. |
```

- [ ] **Step 6.2: Update the MCP tools description for `get_tdoc`**

Find the "Read tools" paragraph (around line 354) and update the
`get_tdoc` description:

```markdown
**Read tools** — `list_meetings`, `get_meeting`, `list_tdocs`,
`get_tdoc`, `get_tdoc_content`, ...
```

Change it to (no change needed beyond the bullet list) but
**add a sentence** in the prose after the bullet list:

```markdown
`get_tdoc` accepts either `tdoc_id` (canonical id, e.g. `R5-260013`)
or `ftp_url` (a 3GPP FTP URL or relative path) — exactly one of the
two must be supplied. The URL mode surfaces every row across the
six URL-keyed tables whose `ftp_url` matches; auto-sync is never
triggered (no parent TDoc / meeting to anchor on). 404 when the URL
resolves to no rows.
```

- [ ] **Step 6.3: Update the "TDoc detail page" prose section**

Find the "TDoc detail page" prose paragraph (around line 232) and
add a sentence at the end of that section:

```markdown
The same template (`tdoc_show.html`) renders in URL-anchored mode
when invoked by `GET /tdocs/by-url?ftp_url=<url>`: the TDoc card is
replaced with a "no parent tdocs row" placeholder when no parent
TDoc matches, the Parse card is omitted (no parent TDoc to filter
on), and the FTP URL field links directly to the 3GPP FTP root.
Cover / TTCN / auxiliary-files cards render identically to the
`tdoc_id`-anchored view.
```

- [ ] **Step 6.4: Update `AGENTS.md`**

Find the "Workflows in one line" entry for `tdoc show --ftp-url`
in the `AGENTS.md` (around line 250, in the doc3gpp CLI workflows
section). Extend it to mention the new web + MCP surface:

The existing bullet ends with:
> The URL is the row identity...

Add after that bullet:
> The same URL-anchored composition is reachable via
> `GET /tdocs/by-url?ftp_url=<url>` (HTML or JSON) and the MCP
> `get_tdoc(ftp_url=...)` tool. Both surfaces call
> `TDocShowRecordByUrl.from_ftp_url(ftp_url, repos)` and emit the
> byte-identical JSON envelope the CLI emits; auto-sync is never
> triggered.

- [ ] **Step 6.5: Verify the docs render correctly (markdown lint)**

Run: `npx markdownlint-cli2 docs/web-server.md AGENTS.md 2>&1 | head -40`
(or skip this step if markdownlint-cli2 isn't installed locally —
the docs are markdown, the only enforcement is reviewer eyes.)

- [ ] **Step 6.6: Commit**

```bash
git add docs/web-server.md AGENTS.md
git commit -m "docs: web + MCP URL-anchored tdoc show surface"
```

---

## Task 7: Final verification

**Files:**
- (no code changes; verification only)

- [ ] **Step 7.1: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: every test passes (unit + integration, sqlite-only).

- [ ] **Step 7.2: Run the linter**

Run: `ruff check .`
Expected: clean (no warnings).

- [ ] **Step 7.3: Run the type checker (if mypy is configured)**

Run: `python -m mypy src/doc3gpp 2>&1 | head -40`
Expected: no new errors (the project may already have a few baseline
issues — verify by running on `main` first if unsure).

- [ ] **Step 7.4: Manual smoke check**

Boot the web server:

```bash
doc3gpp server start --no-open
```

Hit each surface:

```bash
# HTTP HTML
curl -i "http://127.0.0.1:8765/tdocs/by-url?ftp_url=TSG_RAN/WG5_Radio/.../file.zip"

# HTTP JSON
curl -i "http://127.0.0.1:8765/tdocs/by-url?ftp_url=TSG_RAN/WG5_Radio/.../file.zip&format=json"

# MCP (via the existing test harness or a manual JSON-RPC call)
```

Confirm:
- HTML renders the polymorphic template.
- JSON byte-matches the CLI's `--format json`.
- A bogus URL → 404 with `{"error": "tdoc_url_not_found"}`.
- An empty `ftp_url=` → 400 with `{"error": "invalid_filter"}`.

Stop the server:

```bash
doc3gpp server stop
```

- [ ] **Step 7.5: Final commit (no code changes; just verify the working tree)**

```bash
git status
```

Expected: clean working tree (every change committed in a discrete
commit during its task).

---

## Self-Review

**1. Spec coverage:**

| Spec section | Implemented by |
| --- | --- |
| §1 Architecture (composition classmethod reuse, normalisation) | Tasks 1, 2, 4 |
| §2 `GET /tdocs/by-url` (route + behaviour) | Task 2 |
| §2 MCP `get_tdoc` extended signature | Task 4 |
| §2 `TDocUrlNotFoundError` | Task 1 |
| §2 `tdoc_show.html` polymorphic | Task 3 |
| §2 Error / status mapping | Tasks 1, 2, 4 |
| §3 Touched files | Tasks 1–6 |
| §4 Unit tests (web routes) | Task 2 |
| §4 Unit tests (MCP) | Task 4 |
| §4 Integration tests (parity) | Task 5 |
| §4 Docs sync | Task 6 |
| §5 Anti-patterns | Honoured throughout (single template, single route, single MCP tool, no auto-sync, no service-layer changes) |

**2. Placeholder scan:** No "TBD", "TODO", "implement later", or vague instructions. Each step has either code, a runnable command, or a concrete edit description.

**3. Type consistency:**

- `TDocUrlNotFoundError` is consistently `(LookupError)` with attribute `ftp_url: str`. Referenced as `TDocUrlNotFoundError(...)` everywhere; never `tdoc_url_not_found`.
- `TDocShowRecordByUrl.from_ftp_url(ftp_url: str, repos: TDocShowRepos)` is the single composition entry point used by both web and MCP.
- `show_mode` template variable is set to `"by_url"` in the route but never read in Task 3 — drop it from the route context (it's redundant given the `record.tdoc is None` guard). Fixed inline below.

Inline fix: in Task 2.3's `templates.TemplateResponse` context, drop the `"show_mode"` key (it is not consumed by the template after Task 3's refactor):

```python
        context={
            "active_nav": "tdocs",
            "record": record,
            "has_cached_zip": has_cached_zip,
        },
```

**4. Scope:** Seven tasks, six commits + one verification step. Each task is self-contained and produces a passing test or doc change.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-19-web-mcp-tdoc-show-by-url.md`. Two execution options:

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
