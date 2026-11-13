# ETSI LISTSERV Archives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user explicitly requested SDD and TDD.

**Goal:** Ingest, parse, search, display, and maintain indexes for public ETSI LISTSERV messages across the SDK, CLI, HTTP, web UI, and both MCP transports.

**Architecture:** Add isolated LISTSERV domain models, public-host-only transport, pure parsers, source repositories, coverage-aware services, and dedicated FTS/vector indexes in the main database. Compose them through existing factories, share search and display payloads, and run remote or rebuild operations through the existing web job worker.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, SQLite FTS5, existing optional vector/embedding stack, Pydantic 2, httpx, BeautifulSoup, Typer, FastAPI/Jinja, and existing MCP SDK.

**Spec:** `docs/superpowers/specs/2026-11-11-listserv-archives-design.md` (read the entire spec before execution).

## Global Constraints

- Use branch `feature/listserv-archives`; preserve all pre-existing work and **do not auto-commit**, including task/spec/plan artifacts.
- Public ETSI archives only; no authentication, private archives, attachment downloads, implicit sync/parse on reads, or fourth database.
- `sync` and `parse` require exactly one list ID; `parse` reads only already-synced messages.
- Store the full message `datetime` and source timezone offset when available; expose ISO-8601 without converting the displayed time. Compare its displayed calendar date, not the UTC-converted date; repeatable `--date` expressions intersect and support `=`, `!=`, `<`, `<=`, `>`, `>=`.
- Filter-only results sort newest datetime first; ranked modes sort by relevance then newest datetime, with URL as the final deterministic tie-break. Aware timestamps use UTC instants; naive timestamps use a UTC-like wall-clock sort key. A date-only listing uses midnight with `date_precision="date"`; unknown timezone/day-only ordering is approximate.
- Default sync interval is the preceding calendar year through today inclusive. A February 29 anniversary clamps to February 28 in a non-leap year.
- `[listserv] max_content_chars` defaults to 10,000 Unicode characters; only a subsequent parse/forced reparse applies a changed value.
- Extract case-insensitive unique TDoc IDs from subject, attachment names/URLs, and the full *current* body before truncation; never from discarded quoted text.
- Search indexes include metadata, attachment names, IDs, and stored body, but exclude URL strings from both text and vector input; return URLs in metadata.
- Search supports FTS, semantic, hybrid, filter-only; 200-character preview, separate nullable storage/preview truncation flags, deterministic `limit`/`offset`.
- Source changes and coverage commit atomically per page/message; failed derived-index updates cannot lose source or masquerade as fresh.
- Keep ORM/schema reporting in sync; respect existing layer boundaries, shared settings, index conventions, `_to_thread`, cooperative cancellation, and stdio draining.
- Normal tests are offline with sanitized fixtures; no email contents in logs. Update maintained docs and preserve unrelated files.

## Review Focus

The following implied failure modes must be pinned by the named task's tests, in addition to the spec's explicit cases:

1. A page for April contains a message dated May: do not skip it based solely on the archive label (Task 5).
2. A redirect or embedded-body link escapes `https://list.etsi.org`, including a lookalike hostname or credentials: reject before sending a request (Task 3).
3. A crash or index failure after a source commit leaves an older derived row: report stale and let rebuild repair it; do not claim freshness (Tasks 1 and 9).
4. A genuinely empty parsed message and an unparsed message have different null/empty values across search and show (Tasks 1, 6, 7, 11).
5. A changed subject introduces or removes a TDoc after a detail was parsed: combine the current subject's IDs with the current detail's IDs, not stale subject IDs (Tasks 1, 5, 7).

## File map and shared contracts

New files use `listserv` names in the existing layers. Do not copy the existing TDoc or spec-document corpus tables. In `src/doc3gpp/models/listserv.py` define frozen, slotted domain dataclasses:

- `DateInterval(start: date | None, end: date | None)` and `DateSet(intervals: tuple[DateInterval, ...])`, inclusive and normalized into sorted, disjoint intervals; `None` represents an unbounded endpoint and `()` the empty set.
- `ArchivePage(list_id: str, url: str, period: str)`; period is `YYYY-MM-Wn`, or `YYYY-MM` for month-only archives. Page identity is the actual canonical URL, not period.
- `ListservMessage(url: str, list_id: str, subject: str, sender: str, date: datetime, date_precision: Literal["datetime", "date"], week: str, subject_tdocs: tuple[str, ...])`; `date` is timezone-aware when upstream provides an offset and naive otherwise, preserving the displayed local time. List IDs normalize to uppercase; `week` is `YYYY-MM-Wn`. Serialize `date` as ISO-8601 and return `date_precision` on every surface.
- `ListservAttachment(name: str, url: str)`; `ListservDetail(url: str, attachments: tuple[ListservAttachment, ...], tdocs: tuple[str, ...], content: str, content_truncated: bool, parser_version: str, parsed_at: datetime)`; detail TDocs contain body and attachment IDs, and the read view unions those with the *current* subject IDs case-insensitively.
- `ListservFilters(list_id: str | None = None, sender: str | None = None, subject: str | None = None, tdocs: str | None = None, dates: DateSet | None = None, limit: int = 20, offset: int = 0)`; text fields use existing rich LIKE grammar, limit 0–150, offset nonnegative.
- `ListservBatchResult(processed: int, skipped: int, failures: dict[str, str])`; failure keys are page/message URLs, with safe descriptions and no body text. Expose partial failure rather than treating it as success.
- `ListservRecord(message: ListservMessage, detail: ListservDetail | None, tdocs: tuple[str, ...], source_revision: int)`; `ListservDetailMetadata(attachments: tuple[ListservAttachment, ...], content_truncated: bool, parser_version: str, parsed_at: datetime)`; `ListservSearchRecord(message: ListservMessage, detail: ListservDetailMetadata | None, tdocs: tuple[str, ...], source_revision: int)`; `ListservSearchResult(record: ListservSearchRecord, score: float | None, snippet: str | None, matched_source: str | None, content_preview: str | None, preview_truncated: bool | None)`. `source_revision` is an internal, non-serialized source version for FTS/vector freshness: increment once when the listing or parsed detail actually changes; unchanged upserts do not increment. Search DTOs must not contain full stored content. Serializers flatten these to stable JSON fields, including nullable detail fields and `content_truncated`.

Exact task interfaces below form the contracts between workers. A service may have private helpers, but cross-task callers must use these names/types. Use the existing `IndexRequest`, `IndexStatus`, and `IndexRebuildResult` models from `doc3gpp.models.index` rather than inventing a parallel wire format.

Each representative test below is a required test name and assertion, not a replacement for the other cases in the same step. Define its named fixtures locally using the scenario described in that step and import the task's public interfaces.

---

### Task 1: Dates, source models, and transactional storage

**Files:** Create `src/doc3gpp/models/listserv.py`, `src/doc3gpp/repository/listserv.py`, `src/doc3gpp/storage/repositories/listserv_sql.py`, `tests/unit/test_listserv_dates.py`, `tests/integration/test_listserv_repository_sqlite.py`; modify `src/doc3gpp/storage/db/models.py`, `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/models/schema_info.py`, `src/doc3gpp/settings/schema.py`, `src/doc3gpp/data/doc3gpp.toml.example`.

**Interfaces:** Produce `parse_date_set(expressions: Sequence[str]) -> DateSet`, `default_sync_dates(today: date) -> DateSet`, `date_matches(dates: DateSet, value: date) -> bool`, `coverage_contains(coverage: DateSet, requested: DateSet) -> bool`, and `union_coverage(left: DateSet, right: DateSet) -> DateSet`. Define `ListservRepository` with `commit_page(page: ArchivePage, messages: Sequence[ListservMessage], coverage: DateSet, next_urls: Sequence[str]) -> None`, `get_coverage(page: ArchivePage) -> DateSet`, `get_next_urls(page: ArchivePage) -> tuple[str, ...]`, `save_detail(detail: ListservDetail) -> None`, `get(url: str) -> ListservRecord | None`, `list_ids() -> list[str]`, `select(filters: ListservFilters) -> list[ListservRecord]`, and `iter_for_parse(list_id: str, dates: DateSet | None, force: bool) -> Iterator[ListservRecord]` (all matching records, independent of search pagination). Each `ListservRecord` returned by the repository carries its internal `source_revision`; listing or detail mutations increment it once only when data changes. Define `SQLAlchemyListservRepository` with those methods and durable per-source revisions consumed by Task 9.

- [ ] **Write failing unit and SQLite tests.** Assert `parse_date_set([">= 2026-01-01", "!= 2026-01-05", "<= 2026-01-10"])` includes January 4 and 6 but not January 5; coverage for `[1,4]` union `[6,10]` contains that predicate but not `[1,10]`. Reject malformed/impossible dates before touching DB; test all operators, unbounded and empty intersections, leap day, and the rolling-year endpoints. In SQLite, assert a committed empty page has coverage, a thrown exception during `commit_page` rolls back *both* rows and coverage, gzip round-trips Unicode, missing detail is `None` while parsed empty content/attachments are `""`/`()`, current subject IDs replace old subject IDs without discarding body IDs, FK integrity, stable filtered ordering, and source revisions become stale on source changes: new rows start at 1, changed listing/detail data bumps once, and identical upserts do not bump. Round-trip a timezone-aware datetime with a nonzero offset, one naive datetime and a date-only value without losing offset, time, or precision, and filter using the original displayed day. Assert new schema is main-only, idempotent and reflected by schema reporting. Test `ListservSettings.max_content_chars == 10000`, validation rejects values below 1, and example TOML loads.

  ```python
  def test_date_intersection_retains_hole():
      dates = parse_date_set([">= 2026-01-01", "!= 2026-01-05", "<= 2026-01-10"])
      assert date_matches(dates, date(2026, 1, 4))
      assert not date_matches(dates, date(2026, 1, 5))
      assert date_matches(dates, date(2026, 1, 6))
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/test_listserv_dates.py tests/integration/test_listserv_repository_sqlite.py`; expect failures because the new contracts do not exist.
- [ ] **Implement:** Add the shared dataclasses and date-set operations, protocol and SQL implementation. Persist the message's local wall-clock value in a SQL datetime column, original UTC offset in nullable seconds, day-versus-datetime precision, and a UTC-normalized (or naive-wall-time fallback) datetime sort key; reconstruct the original aware/naive Python datetime on reads. This avoids SQLite's loss of timezone offsets and lets calendar-date predicates use local time. Store normalized, inclusive interval unions and discovered pagination URLs by `(list_id, page_url)` atomically; store compressed detail content and nullable parse state separately from listing rows. Keep revision invalidation transactional with source updates. Wire additive main-schema bootstrap and settings/template/schema registry; never import ORM into services or domain models.
- [ ] **Run the same tests:** expect PASS.

### Task 2: Offline LISTSERV archive parsers

**Files:** Create `src/doc3gpp/parsers/listserv_archive.py`, `tests/fixtures/listserv/` with sanitized `index.html`, `split_week.html`, `month_only.html`, `empty.html`, `pagination.html`, `malformed.html`, and `tests/unit/test_listserv_archive.py`.

**Interfaces:** Consume Task 1 models. Produce `parse_archive_index(html: str, base_url: str, list_id: str) -> list[ArchivePage]` and `parse_archive_page(html: str, page: ArchivePage) -> tuple[list[ListservMessage], list[str]]`; the latter returns messages plus next-page URLs, and raises a recognizable parse error for non-listing/malformed/login pages rather than guessing empty.

- [ ] **Write failing tests** with fixtures shaped like actual ETSI pages: an October 2026 week 1, April 2023 week 4 split into `ind2304D_1` and `ind2304D`, an older month-only page, a valid empty listing, pagination, masked sender, Unicode, relative and absolute links, and mixed timezones. Assert both split URLs survive, the month-page's message dated `2023-04-29` gets week `2023-04-W5`, masked sender stays `[log in to unmask]`, `2026-10-01T23:30:00-07:00` remains an aware datetime on October 1 with hour 23 and offset -07:00, timezone-less and date-only values retain their indicated precision, subject IDs are case-insensitively deduplicated, and a malformed/login document raises instead of returning empty.

  ```python
  from urllib.parse import parse_qs, urlsplit

  def test_archive_index_keeps_both_split_week_urls(split_index_html):
      pages = parse_archive_index(split_index_html, "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE", "EXAMPLE")
      assert {parse_qs(urlsplit(page.url).query)["A1"][0] for page in pages} >= {"ind2304D", "ind2304D_1"}
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/test_listserv_archive.py`; expect FAIL.
- [ ] **Implement:** Parse archive labels and listing table semantically, resolve links relative to source, derive week from the message day for month-only pages, preserve timezone and displayed date, and return page links without assuming label uniqueness. Promote the existing TDoc pattern into a shared helper if needed without changing CR behavior.
- [ ] **Run the same test:** expect PASS.

### Task 3: Restricted ETSI transport

**Files:** Create `src/doc3gpp/scraping/listserv_source.py`, `tests/unit/test_listserv_source.py`.

**Interfaces:** Produce `FetchedPage(text: str, content_type: str, final_url: str)`, `ListservSource.fetch(url: str) -> FetchedPage`, and `ListservSource.close() -> None` plus context-manager support in `src/doc3gpp/scraping/listserv_source.py`, and `archive_index_url(list_id: str) -> str`; injectable httpx client/transport is allowed. All fetched requests, including redirect hops, pass `validate_listserv_url(url: str) -> str` before transmission. Services that own a source expose `close()`/context-manager support; injected sources remain caller-owned.

- [ ] **Write failing tests** using an httpx mock transport: assert `archive_index_url("3gpp_tsg_ran_wg1")` targets the encoded `A0` parameter and fetch returns decoded UTF-8/declared-charset HTML and content type. Assert retryable failures follow existing retry limits; 401/403 give an actionable error. For initial URLs, redirects and iframe fetches, reject HTTP, `list.etsi.org.evil.example`, credentials, nonstandard ports, and external hosts without ever issuing a request to the rejected destination. A redirect loop fails cleanly and no response body is logged; login HTML is identified by the parsers, not transport.

  ```python
  def test_lookalike_host_rejected_before_fetch(recording_source):
      with pytest.raises(ValueError):
          recording_source.fetch("https://list.etsi.org.evil.example/scripts/wa.exe")
      assert recording_source.requests == []
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/test_listserv_source.py`; expect FAIL.
- [ ] **Implement:** Apply validation before each manual redirect hop and use bounded timeouts/retries; reject invalid or empty list IDs without an allowlist, and preserve Unicode. Do not fetch attachment targets. Test source ownership, idempotent close, and exception-path cleanup.
- [ ] **Run the same test:** expect PASS.

### Task 4: Detail, body, attachment, and TDoc parsing

**Files:** Create `src/doc3gpp/parsers/listserv_detail.py`, `tests/fixtures/listserv/detail.html`, `body.html`, `body.txt`, `tests/unit/test_listserv_detail.py`; modify shared TDoc-pattern location only if Task 2 extracted it.

**Interfaces:** Produce `parse_detail_page(html: str, base_url: str) -> tuple[str | None, tuple[ListservAttachment, ...], str | None]`, returning a body-part URL, genuine attachments, and inline fallback body; `parse_message_body(body: str, *, is_html: bool) -> str`; `extract_tdocs(*texts: str) -> tuple[str, ...]`.

- [ ] **Write failing tests** for iframe HTML and plain text, charsets, relative part URLs, text/plain, text/html and multipart/related alternatives versus named genuine attachments, and inline fallback. Assert recognized `<blockquote>` / reply and forward headers remove old TDoc `R1-111111` but keep uncertain prose, and escaped/unsafe embedded HTML cannot survive as executable markup. Assert a body ending in `R1-2610793`, repeated `r1-2610793`, `RP-2601234`, `R5s260009`, and an attachment URL ID produces unique IDs, including those after the eventual content limit.

  ```python
  def test_extract_tdocs_deduplicates_case_insensitively():
      ids = extract_tdocs("R1-2610793 r1-2610793 RP-2601234 R5s260009")
      assert {item.upper() for item in ids} == {"R1-2610793", "RP-2601234", "R5S260009"}
      assert len(ids) == 3
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/test_listserv_detail.py`; expect FAIL.
- [ ] **Implement:** Parse only body-representation URLs and real named attachments; no network or disk in the parser. Convert current message to safe Markdown, cut only recognizable previous-message boundaries, and use the shared TDoc identifier baseline.
- [ ] **Run the same test:** expect PASS.

### Task 5: Coverage-aware sync orchestration

**Files:** Create `src/doc3gpp/services/listserv_sync.py`, `tests/integration/test_listserv_sync_sqlite.py`; modify `src/doc3gpp/services/factory.py` and Task 1 protocol/storage only as needed for this interface.

**Interfaces:** Consume Tasks 1–3. Produce `ListservSyncService.sync(list_id: str, *, dates: Sequence[str] = (), force: bool = False, on_progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> ListservBatchResult` and `build_listserv_sync_service(settings: Settings | None = None) -> ListservSyncService`.

- [ ] **Write failing SQLite tests** with a recording fake source and a fixed clock. Assert no-date sync stores only preceding-year-through-today rows; explicit disjoint ranges never skip an uncovered hole; `!=` preserves the hole; changing predicates re-fetches when necessary; valid empty pages become covered; both split-week URLs are independently covered; every most-recent-week page re-fetches, historical covered pages do not, and `force=True` re-fetches all selected pages. For a month-only latest period, refresh conservatively. Assert an April page containing a May-dated message is scanned for a May query; pagination URLs are traversed and covered even when their parent is already covered; failed/malformed pages remain retryable without losing successful siblings; simulated transaction failure rolls back page messages, pagination links and coverage. Assert cancellation stops scheduling new pages (already-running work may finish), date validation precedes network/DB calls, and an updated subject replaces its subject-derived IDs.

  ```python
  def test_may_date_is_found_on_april_archive_page(sync_service, repository, april_may_url):
      result = sync_service.sync("EXAMPLE", dates=["= 2026-05-01"])
      assert not result.failures
      assert repository.get(april_may_url).message.date.date() == date(2026, 5, 1)
  ```
- [ ] **Run:** `python -m pytest -q tests/integration/test_listserv_sync_sqlite.py`; expect FAIL.
- [ ] **Implement:** Validate inputs first, discover actual page URLs, process page and pagination URLs independently with existing bounded concurrency conventions, traverse persisted pagination links even when their parent is skipped, compare date-set coverage, conservatively scan when labels do not prove date bounds, filter rows on displayed dates, and atomically commit each page and its links. Preserve successful work and return per-page failures; report progress/cancellation without logging bodies. Trigger source revision invalidation in the transaction, not only in a post-commit index hook. Test that in-flight fetches stay within the configured bound and cancellation stops scheduling new pages.
- [ ] **Run the same test:** expect PASS.

### Task 6: Explicit parse orchestration and stored-detail reads

**Files:** Create `src/doc3gpp/services/listserv_parse.py`, `src/doc3gpp/services/listserv_service.py`, `tests/integration/test_listserv_parse_sqlite.py`; modify `src/doc3gpp/services/factory.py`.

**Interfaces:** Consume Tasks 1, 3, 4. Produce `ListservParseService.parse(list_id: str, *, dates: Sequence[str] = (), force: bool = False, on_progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> ListservBatchResult`; `ListservService.list_ids() -> list[str]`, `ListservService.show(url: str) -> ListservRecord` (normal not-found error); `build_listserv_parse_service(settings: Settings | None = None) -> ListservParseService`, `build_listserv_service(settings: Settings | None = None) -> ListservService`.

- [ ] **Write failing SQLite tests** with fake source requests: exact list ID is mandatory and case-insensitive; no implicit sync, optional dates select stored messages, already parsed skip unless forced; iframe body fetch happens but attachment URLs are never fetched; TDoc beyond a 10-character body limit remains in detail IDs; ten Unicode characters round-trip as ten characters with `content_truncated=True` only if a character was removed. Assert detail/attachments/IDs/version replace atomically, failed force reparse retains prior detail, empty body is parsed not null, pagination/cancellation and partial failure behave correctly, and `show`/`list_ids` make no network calls. Reject a hostile persisted message/body URL before fetching it.

  ```python
  def test_parse_extracts_id_beyond_storage_limit(parse_service, repository, message_url):
      result = parse_service.parse("EXAMPLE")  # fixture body: ten characters followed by R1-2610793; limit=10
      detail = repository.get(message_url).detail
      assert not result.failures and detail.content_truncated
      assert len(detail.content) == 10 and "R1-2610793" in detail.tdocs
  ```
- [ ] **Run:** `python -m pytest -q tests/integration/test_listserv_parse_sqlite.py`; expect FAIL.
- [ ] **Implement:** Iterate all matching source records using bounded concurrency, fetch detail then validated body representation, select HTML versus plain parsing using its `FetchedPage.content_type`, parse current body, extract body/attachment IDs before character slicing, save compressed bounded Markdown atomically, and leave old detail on failure. Test the concurrency bound and cancellation before scheduling new items. Show returns stored data and unioned current subject/detail IDs, never the truncated-off source tail.
- [ ] **Run the same test:** expect PASS.

### Task 7: Source filters, FTS5, and search payloads

**Files:** Create `src/doc3gpp/storage/repositories/listserv_search_sql.py`, `src/doc3gpp/services/listserv_search.py`, `src/doc3gpp/models/listserv_payloads.py`, `tests/integration/test_listserv_search_sqlite.py`; modify `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/storage/repositories/listserv_sql.py`, `src/doc3gpp/services/factory.py`.

**Interfaces:** Produce `ListservSearchService.search(*, text: str | None = None, semantic: str | None = None, filters: ListservFilters = ListservFilters()) -> list[ListservSearchResult]`, `listserv_record_payload(record: ListservRecord) -> dict[str, object]`, `listserv_search_payload(result: ListservSearchResult) -> dict[str, object]`, and `build_listserv_search_service(settings: Settings | None = None) -> ListservSearchService`. Define an internal URL-stripping `indexable_text(record: ListservRecord) -> str` shared with Task 8. Task 7 implements filter-only and FTS; Task 8 adds semantic/hybrid to the same interface.

- [ ] **Write failing SQLite tests:** metadata-only subject `R1-2610793` is discoverable before parse; filters on each LIKE field and intersected date apply. Exact `tdocs="R1-2610793"` matches when either the subject-TDoc or detail-TDoc side is empty; `null`, `not-null`, and `!pattern` retain union filter semantics. Parse/store `"a" * 201` yields 200-char preview, `preview_truncated=True`, and independent storage flag; unparsed has both flags null and no snippet; empty parsed content has `""` and false flags. `SearchQueryBuilder` text and MATCH expressions work; no URL host/path token matches FTS though a TDoc found in an attachment URL does. Assert metadata, attachment names, week, date and body are indexed, current-subject TDoc changes affect filters, SQL parameters resist injection, and deterministic ties/page offsets do not duplicate or omit results. Assert same-day thread messages sort by time newest first; differently offset aware datetimes sort by instant, equal-score ranked ties use timestamp, and unknown-zone/day-only cases use the documented fallback without changing returned local values. Filter-only must work with vector and FTS unavailable and must never fetch network. Until Task 8 adds semantic/hybrid execution, any supplied `semantic` argument raises the existing semantic-unavailable error rather than returning an empty result or silently falling back.

  ```python
  def test_search_distinguishes_preview_from_storage_truncation(search_service):
      result, = search_service.search(filters=ListservFilters(subject="preview-example"))
      assert result.content_preview == "a" * 200 and result.preview_truncated is True
      assert result.record.detail.content_truncated is False  # stored content is 201 characters

  def test_same_day_thread_messages_are_newest_first(thread_search_service):
      results = thread_search_service.search(filters=ListservFilters(subject="thread-example"))
      assert [item.record.message.date.isoformat() for item in results] == [
          "2026-10-01T14:30:00+02:00", "2026-10-01T09:00:00+02:00"
      ]
  ```
- [ ] **Run:** `python -m pytest -q tests/integration/test_listserv_search_sqlite.py`; expect FAIL.
- [ ] **Implement:** Add dedicated FTS schema/repository and shared sanitized indexing text, source SQL filtering, metadata-only indexing, snippets/provenance, stable payload serializers and factory composition. Search payloads expose only the 200-character preview, not the complete `record.detail.content`; show payloads expose the complete stored content. Strip URL strings from body as well as dedicated URL fields, without modifying stored Markdown. Fetch details from SQL only; apply pagination after ranking/deduplication. Until Task 9 installs automatic hooks, test FTS by explicitly indexing fixture records.
- [ ] **Implement:** Add dedicated FTS schema/repository and shared sanitized indexing text, source SQL filtering, metadata-only indexing, snippets/provenance, stable payload serializers and factory composition. Search payloads expose only the 200-character preview, not the complete `record.detail.content`; show payloads expose the complete stored content. Strip URL strings from body as well as dedicated URL fields, without modifying stored Markdown. Correct `SQLAlchemyListservRepository._apply_tdocs_filter` so exact rich-LIKE values test subject/detail IDs independently while preserving null and negated-filter behavior. Fetch details from SQL only; apply pagination after ranking/deduplication. Until Task 8 adds semantic/hybrid execution and Task 9 installs automatic hooks, raise the existing semantic-unavailable error for semantic input and test FTS by explicitly indexing fixture records.
- [ ] **Run the same test:** expect PASS.

### Task 8: Vector search and hybrid fusion

**Files:** Create `src/doc3gpp/storage/repositories/listserv_vector_sql.py`, `src/doc3gpp/services/listserv_semantic.py`, `tests/integration/test_listserv_semantic_sqlite.py`; modify Task 7 service/factory and `src/doc3gpp/storage/db/migrate.py`.

**Interfaces:** Consume Tasks 1 and 7. Produce `ListservSemanticService.search(query: str, filters: ListservFilters) -> list[ListservSearchResult]`, `ListservSemanticService.index_record(record: ListservRecord) -> int` (returns the number of email chunks replaced in the vector index; Task 9 uses this after committed source changes), and `build_listserv_semantic_service(settings: Settings | None = None, *, embedder: Embedder | None = None) -> ListservSemanticService | None`; extend `ListservSearchService.search` for semantic-only and hybrid.

**Task 8 rulings:** Normalize whitespace-only queries to absent and dispatch filter-only/FTS-only/semantic-only/hybrid explicitly. Semantic-only works when FTS is unavailable; hybrid requires both services and never falls back; filter-only requires neither optional index. Expose positive cosine distance as the semantic-only `score` (lower is better), matching existing public semantic facades; expose positive email-level RRF score in hybrid (higher is better), and keep FTS-only BM25 score unchanged. Hybrid uses zero-based distinct-email ranks and `1/(k+rank_fts)*(1-W) + 1/(k+rank_vec)*W`, with `W=1-fts5_weight`; widen candidate windows, preserve best vector chunk text from the same vector read snapshot, deduplicate by email, tie-break by displayed datetime then URL, and apply offset/limit once after fusion. Vector retrieval must apply shared filters before final paging and widen KNN up to SQLite's 4096-row cap to account for chunk crowding. `index_record` embeds only Task 7 `indexable_text(record)` (stored content, metadata, attachment names and IDs; no URL strings and never truncated-off source content) using configured chunk settings. Keep vector sidecars email-keyed and isolated on the main DB; Task 9 owns lifecycle/rebuild.

- [ ] **Write failing tests** with assertion-level module-absence checks and a deterministic fake embedder/vector backend: `index_record` input contains neither message URL, attachment URL nor body URL, but does contain attachment names, effective TDocs, metadata, and only the stored (already capped) body. Assert vector-only and hybrid apply every LIKE/date filter before paging; multiple chunks for one email yield one hit with the closest same-snapshot chunk, `matched_source="content"`, and that chunk as the snippet. Assert semantic-only sorts by ascending positive distance; hybrid uses zero-based email RRF with `vector_weight=1-fts5_weight`, retains FTS provenance when present, tie-breaks by datetime/URL, deduplicates before nonzero-offset pagination, and widens KNN for chunk crowding. Assert semantic-only works without FTS, hybrid fails explicitly without either component, filter-only works without FTS/vector, and missing vector/embedding raises the existing actionable unavailable error. `index_record` replaces old chunks transactionally and indexes stored empty/metadata-only records without reading upstream source.

  ```python
  def test_embedding_input_excludes_urls(indexed_embedder):
      text = " ".join(indexed_embedder.inputs)
      assert "https://" not in text and "R1-2610793" in text
      assert "contribution.docx" in text
  ```
- [ ] **Run:** `python -m pytest -q tests/integration/test_listserv_semantic_sqlite.py`; expect FAIL.
- [ ] **Implement:** Build dedicated email-keyed vector and matched-chunk sidecars on the main engine, initialized from the main schema path and independent of FTS availability. Use the existing optional backend, chunker, embedder, filter and rank-fusion patterns; reuse Task 7 sanitization/payloads. Keep vector index writes atomic per email, expose `index_record` for Task 9, preserve same-snapshot chunk provenance, and expose explicit unavailability rather than returning an empty list.
- [ ] **Run the same test:** expect PASS (report optional backend skips explicitly).

### Task 9: Index lifecycle, incremental updates, and recovery

**Files:** Create `src/doc3gpp/services/listserv_index.py`, `tests/integration/test_listserv_index_sqlite.py`; modify `src/doc3gpp/services/{listserv_sync.py,listserv_parse.py,listserv_search.py,listserv_semantic.py,factory.py}`, `src/doc3gpp/storage/repositories/{listserv_search_sql.py,listserv_vector_sql.py}`, `src/doc3gpp/settings/schema.py`, `src/doc3gpp/data/doc3gpp.toml.example`, `tests/unit/test_settings_config_file.py`, `tests/unit/test_config_writer.py`, and `tests/integration/test_config_init_cli_sqlite.py`.

**Interfaces:** Produce `ListservIndexService.status() -> IndexStatus`, `ListservIndexService.rebuild(request: IndexRequest, *, quiet: bool = False, on_progress: Callable[[str], None] | None = None) -> IndexStatus | IndexRebuildResult`, `ListservIndexService.update_records(records: Sequence[ListservRecord], *, operation: Literal["sync", "parse"]) -> dict[str, str]`, and `build_listserv_index_service(settings: Settings | None = None, *, embedder: Embedder | None = None) -> ListservIndexService`. Inject the index service into `ListservSyncService` and `ListservParseService`; invoke `update_records` only after a page/detail source commit. Its return maps message URL to a safe component-failure summary; indexing failures never roll back or mark the source commit unsuccessful.

- [ ] **Write failing settings tests** first: `ListservSettings.auto_index_on_sync`, `auto_index_on_parse`, `auto_embed_on_sync`, and `auto_embed_on_parse` all default to `True`; each accepts `False`; packaged TOML emits all four values and loads to those defaults. Update canonical-template and config-init expected dictionaries.

  ```python
  def test_listserv_automatic_index_settings_default_on():
      settings = ListservSettings()
      assert settings.auto_index_on_sync is True
      assert settings.auto_index_on_parse is True
      assert settings.auto_embed_on_sync is True
      assert settings.auto_embed_on_parse is True
  ```

- [ ] **Write failing SQLite integration tests**: sync and parse call `update_records` only after committed source changes; the two `auto_index_*` flags gate FTS separately from the two `auto_embed_*` flags; disabled hooks leave matching source revisions stale; a post-commit FTS or vector failure retains source and reports a sanitized per-message failure; no-op source revisions do not invalidate or re-embed; component freshness/status is independent. Rebuild FTS, vector, or both according to `IndexRequest`; test batch size, stale-only selection, resume, empty and metadata-only records, model/chunk-setting changes, SQL-only operation, cancellation/progress and preservation of source after vector failure. Assert `reset_for_rebuild` clears old completion timestamps and only successful rebuild stamps fresh ones; no outbound ETSI requests.

  ```python
  def test_rebuild_repairs_failed_incremental_update(index_service, search_service):
      assert index_service.status().fts5.status.is_stale
      index_service.rebuild(IndexRequest(rebuild=True))
      assert not index_service.status().fts5.status.is_stale
      assert search_service.search(text="repairtoken")
  ```
- [ ] **Run:** `.venv/bin/python -m pytest -q -n auto tests/integration/test_listserv_index_sqlite.py tests/unit/test_settings_config_file.py tests/unit/test_config_writer.py tests/integration/test_config_init_cli_sqlite.py`; expect assertion-level RED.
- [ ] **Implement:** Add the four default-`True` `[listserv]` policy settings above. FTS auto updates are gated by `auto_index_on_sync`/`auto_index_on_parse` and `search.enabled`; vector auto updates are gated by `auto_embed_on_sync`/`auto_embed_on_parse` and `semantic_search.enabled` plus optional embedder/vector availability. After committed sync/parse source changes, update eligible components independently. Keep source commits authoritative: catch derived-index errors, return sanitized message-URL failures, and leave the affected component stale. Derive status independently from stored source/index revisions; advance component rebuild metadata only after its matching work commits. Implement `IndexRequest` rebuild modes with SQL-only source iteration, batches, resume/stale-only selection, progress and cancellation; an interrupted rebuild must not be marked successful. Clear `last_rebuild_at` and `last_indexed_at` when resetting the vector sidecar, then stamp them only after a successful rebuild.
- [ ] **Run focused tests:** the same command above; expect PASS.

### Task 10: SDK composition and CLI

**Files:** Create `src/doc3gpp/cli_listserv.py`, `tests/unit/test_cli_listserv.py`, `tests/integration/test_listserv_cli_sqlite.py`; modify `src/doc3gpp/cli.py`, `src/doc3gpp/services/factory.py`, and `src/doc3gpp/__init__.py` only if existing public SDK exports require it.

**Interfaces:** Consume Tasks 5–9. Export factory builders as SDK entry points. Register Typer group `listserv` with `sync LIST_ID`, `parse LIST_ID`, `list`, `search`, `show URL`, and `index` operations; index status/rebuild options mirror the existing index commands. Sync/parse accept repeatable `--date` and `--force`; search accepts repeatable `--date`, `--list-id`, `--from`, `--subject`, `--tdocs`, `--text`, `--semantic`, `--limit`, `--offset` and existing output-format conventions.

- [ ] **Write failing tests** using `CliRunner` and offline SQLite: help exposes all six operations and options; missing exact ID/date validation fails before source access; default and repeated dates reach services unchanged; IDs are not treated as SQL LIKE for sync/parse; list returns distinct stored IDs; search JSON matches shared payload including URL, 200-char preview and nullable flags; show returns all stored content or normal not-found; filter-only, FTS, semantic/hybrid, index status/rebuild and partial-failure exit/reporting are wired. Mock source to assert list/search/show/index never download.

  ```python
  def test_listserv_help_has_six_operations(cli_runner, app):
      result = cli_runner.invoke(app, ["listserv", "--help"])
      assert result.exit_code == 0
      assert all(name in result.output for name in ("sync", "parse", "list", "search", "show", "index"))
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/test_cli_listserv.py tests/integration/test_listserv_cli_sqlite.py`; expect FAIL.
- [ ] **Implement:** Register the thin Typer adapter, standard output/format handling and public factories; validate before initializing network/DB work where required and translate errors consistently with existing commands.
- [ ] **Run the same tests and** `python -m doc3gpp.cli listserv --help` (or installed `doc3gpp listserv --help` if the module lacks a `__main__`); expect PASS and all subcommands in help.

### Task 11: HTTP endpoints, job worker, and lifecycle

**Files:** Create `src/doc3gpp/web/routes/listserv.py`, `tests/integration/test_listserv_web_sqlite.py`, `tests/unit/web/test_listserv_jobs.py`; modify `src/doc3gpp/models/jobs.py`, `src/doc3gpp/web/workers/handlers.py`, `src/doc3gpp/web/workers/job_worker.py` if dispatch requires it, `src/doc3gpp/web/state.py`, `src/doc3gpp/web/app.py`, `src/doc3gpp/web/deps.py`, `src/doc3gpp/web/routes/__init__.py`.

**Interfaces:** Add HTTP `GET /api/listserv/lists`, `GET /api/listserv/search`, `GET /api/listserv/show` (exact URL query parameter), `GET /api/listserv/index/status`, and job-enqueuing `POST /api/listserv/sync`, `/parse`, `/index`; request fields mirror Task 10, and response read objects use Task 7 serializers. Add distinct `LISTSERV_SYNC`, `LISTSERV_PARSE`, `LISTSERV_INDEX` job kinds and shared enqueue helpers reusable by MCP.

- [ ] **Write failing tests** against the real ASGI app and isolated SQLite: compare HTTP list/search/show/status JSON with SDK payloads for parsed, empty, unparsed and not-found messages; test repeated date queries, bad IDs/filters, pagination and no implicit network. Assert mutating endpoints enqueue the correct kind/parameters, progress and partial failures are visible, and cancellation between pages/messages prevents later work. Use a blocked mocked handler to prove blocking work runs via `_to_thread` and stdio-owned worker shutdown waits for it before closing resources.

  ```python
  def test_http_show_matches_sdk_payload(client, sdk_service, message_url):
      response = client.get("/api/listserv/show", params={"url": message_url})
      assert response.status_code == 200
      assert response.json() == listserv_record_payload(sdk_service.show(message_url))
  ```
- [ ] **Run:** `python -m pytest -q tests/integration/test_listserv_web_sqlite.py tests/unit/web/test_listserv_jobs.py tests/unit/web/test_mcp_stdio_lifecycle.py`; expect new tests FAIL.
- [ ] **Implement:** Add shared state/dependencies and routes; use the existing queue and `_to_thread` for each blocking handler, propagate cancellation into service callbacks, and preserve the existing stdio drain/resource ownership contract. Close one-shot job-owned sources after work ends and shared sources only after their workers drain. Validate IDs/date expressions before creating jobs and expose partial failure rather than false success.
- [ ] **Run the same tests:** expect PASS.

### Task 12: MCP parity over HTTP and stdio

**Files:** Modify `src/doc3gpp/web/mcp_server.py`; create `tests/integration/test_listserv_mcp.py`, `tests/unit/web/test_listserv_mcp.py`.

**Interfaces:** Register `listserv_sync`, `listserv_parse`, `listserv_list`, `listserv_search`, `listserv_show`, `listserv_index_status`, and `listserv_index_rebuild` on the shared MCP server; arguments mirror Tasks 10–11. Read tools return the same JSON payloads as HTTP, and mutations enqueue the same job kinds.

- [ ] **Write failing tests** exercising the shared registered server over HTTP and stdio: verify tool schema requires exact `list_id` for sync/parse, accepts repeatable date filters and all search filters/modes, results equal HTTP payloads including both truncation flags and URLs, show uses exact URL, status/rebuild share index contract, errors are actionable, and mutation results contain a job ID. Verify blocked LISTSERV jobs drain under stdio and no read tools fetch remote content.

  ```python
  async def test_mcp_show_matches_http(mcp_client, http_client, message_url):
      tool_payload = await mcp_client.call_json("listserv_show", {"url": message_url})
      assert tool_payload == http_client.get("/api/listserv/show", params={"url": message_url}).json()
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/web/test_listserv_mcp.py tests/integration/test_listserv_mcp.py tests/integration/test_mcp_stdio_tool_shutdown.py`; expect new tests FAIL.
- [ ] **Implement:** Register tools once in `build_mcp_server`, reuse HTTP/shared serializers and enqueue helpers, and retain MCP enablement/transport/resource semantics. Do not duplicate queries or introduce a second worker.
- [ ] **Run the same tests:** expect PASS.

### Task 13: Web UI for list, search, detail, and jobs

**Files:** Create `src/doc3gpp/web/templates/listserv.html`, `src/doc3gpp/web/templates/listserv_show.html`, `src/doc3gpp/web/templates/partials/listserv_results.html`, `src/doc3gpp/web/static/js/listserv.js`, `tests/integration/test_listserv_ui.py`; modify `src/doc3gpp/web/routes/listserv.py` and `src/doc3gpp/web/templates/base.html`.

**Interfaces:** Serve `/listserv` and `/listserv/show` with forms/controls for sync, parse, list, search and index status/rebuild, job links, pagination and show-by-exact-URL; use Task 11 jobs and Task 7 read payloads, not separate database access.

- [ ] **Write failing tests:** render list IDs, metadata, 200-character preview, both truncation indicators and full stored show content; submit each operation and search mode with filters/dates; verify pagination and upstream/job links. With subject/body/attachment names containing `<script>`, unsafe links, and Markdown HTML, assert no executable script or unsafe URL is rendered. Show unparsed and parsed-empty as different states; no page render triggers sync/parse.

  ```python
  def test_show_escapes_upstream_subject(client, unsafe_message_url):
      response = client.get("/listserv/show", params={"url": unsafe_message_url})
      assert response.status_code == 200
      assert "<script>alert(1)</script>" not in response.text
      assert "&lt;script&gt;" in response.text
  ```
- [ ] **Run:** `python -m pytest -q tests/integration/test_listserv_ui.py`; expect FAIL.
- [ ] **Implement:** Follow existing Jinja/HTMX conventions with escaping and safe Markdown rendering, enqueue mutations through shared routes, and display partial job failures and truncation without implying access to discarded original content.
- [ ] **Run the same test:** expect PASS.

### Task 14: Documentation and whole-feature verification

**Files:** Modify `docs/cli.md`, `docs/config.md`, `docs/processes.md`, `docs/architecture.md`, `docs/code-map.md`, `docs/web-server.md`, `docs/mcp.md`, `docs/3gpp-knowledge.md`, `docs/known-constraints.md`, `AGENTS.md`; create `tests/unit/test_listserv_docs.py`; adjust `README.md` only if a short navigation entry is needed.

**Interfaces:** Document the exact SDK factories, CLI flags, HTTP routes, MCP tools, job behavior and field names delivered by Tasks 1–13. The approved spec remains the source of scope.

- [ ] **Write failing documentation/parity tests** where existing checks cannot cover the feature: assert the generated config template parses with `listserv.max_content_chars == 10000`, CLI help names all six commands, schema reporting matches actual LISTSERV ORM columns, documented relative links resolve, and equivalent HTTP/MCP/CLI JSON fields agree on `content_truncated`, `preview_truncated`, `url`, and `tdocs`.

  ```python
  def test_cli_guide_documents_sync():
      assert "doc3gpp listserv sync" in Path("docs/cli.md").read_text()
  ```
- [ ] **Run:** `python -m pytest -q tests/unit/test_listserv_docs.py`; expect FAIL for missing documentation/parity coverage.
- [ ] **Write documentation:** explain public-only access, actual-page/date coverage and force, default rolling year, datetime preservation, displayed-day filters, newest-first ordering and the unknown-zone/day-only fallback, separate sync/parse, body iframe vs attachments, best-effort quote boundaries, 10,000-character storage limit vs 200-character search preview, IDs extracted before truncation, search modes and URL exclusion, indexing freshness/rebuild, optional dependencies, job cancellation and stdio drain. Include working SDK/CLI/HTTP/MCP examples and the user-provided IDs as examples only, not a whitelist.
- [ ] **Run focused checks:** `ruff check .`; `python -m pytest -q tests/unit/test_listserv_dates.py tests/unit/test_listserv_archive.py tests/unit/test_listserv_source.py tests/unit/test_listserv_detail.py tests/unit/test_cli_listserv.py tests/unit/test_listserv_docs.py tests/unit/web/test_listserv_jobs.py tests/unit/web/test_listserv_mcp.py`; `python -m pytest -q tests/integration/test_listserv_repository_sqlite.py tests/integration/test_listserv_sync_sqlite.py tests/integration/test_listserv_parse_sqlite.py tests/integration/test_listserv_search_sqlite.py tests/integration/test_listserv_semantic_sqlite.py tests/integration/test_listserv_index_sqlite.py tests/integration/test_listserv_cli_sqlite.py tests/integration/test_listserv_web_sqlite.py tests/integration/test_listserv_mcp.py tests/integration/test_listserv_ui.py`; expect PASS, explicitly report skips.
- [ ] **Run broader regression:** `./scripts/test_sqlite.sh -n 0`; run existing configuration/template/schema, index, search, web and MCP lifecycle tests as appropriate. Record exact commands, totals, failures and skips. If live ETSI testing is attempted, use explicit `-m online` only and report connection restrictions instead of claiming live verification.
- [ ] **Review:** check every spec section against implementation and tests, run the plan's fresh-review gates under SDD, check `git status --short --branch` and `git diff --check`, preserve pre-existing untracked files, and report results without committing.
