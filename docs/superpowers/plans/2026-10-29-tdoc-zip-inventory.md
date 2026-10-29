# TDoc ZIP Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every original file entry in a supported TDoc ZIP, even if later parsing fails, and expose its stored inventory on all requested surfaces.

**Architecture:** Enumerate the outer ZIP central directory independently of DOCX conversion and persist an ordered, URL-keyed inventory in a separate main-database table. Carry inventory and any subsequent parsing error to the existing serialized writer; compose detail and content values from that repository without inventory-triggered network or parsing.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, SQLite, Typer, FastAPI/Jinja2, MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-tdoc-zip-inventory-design.md`

## Global Constraints

- Work on `feat/tdoc-zip-inventory` in the current checkout; preserve the pre-existing web fix in `src/doc3gpp/web/templates/tdoc_show.html` and `tests/unit/test_web_routes.py`.
- Do not commit: `docs/conventions.md` forbids automatic commits. Review each task's uncommitted diff instead.
- Follow TDD: add the task's tests, run them and observe the expected failures before production changes; re-run after implementation. Subagents must receive the full relevant task, these constraints, and the spec.
- Only CR, WID new/revised, LS in/out, and discussion TDoc types get an inventory; do not extend parser eligibility. Every file type *inside* their ZIPs is included.
- `zip_files` is `null` when unknown, `[]` for a known empty archive, and otherwise an ordered list retaining duplicates and original decoded names. It is separate from auxiliary `files` and converted `documents`.
- Do not extract inventory members, recurse into ZIPs, normalize filenames, or interpret filenames as filesystem paths or commands. Escape filenames in HTML and safely render them in Markdown/terminal output.
- Inventory collection follows existing download and source-size limits; later validation, conversion, or limit errors retain their original exception and failed/skipped classification. No partial inventory replaces a known one.
- When collecting an inventory, reuse cached ZIP bytes only with verified provenance identifying the actual candidate URL; unknown legacy, malformed, missing, or mismatched provenance requires re-download. Failed re-download leaves old cache and inventory intact.
- All batch and web-job inventory SQL writes occur in the existing single-writer lane. Preserve cancellation, atomic content replacement, and existing indexing hooks. Synchronous extraction persists the same results.
- Canonical 3GPP URL parsing persists only for stored eligible TDocs and the actual ZIP URL; local paths and non-3GPP URLs remain in-memory only. Do not treat a DOCX's internal ZIP as a TDoc ZIP.
- Keep CLI `--format raw` unchanged. The inventory lookup never fetches or parses; the existing optional `sync.auto_sync` metadata behavior for CLI `tdoc show --tdoc` remains unchanged.
- Schema changes are additive and idempotent through `doc3gpp db init --scope main`; do not reset existing data or bulk-backfill. Old inventories stay unknown until forced re-parse.
- Use `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH` for the established full-suite environment when available; run tests offline.

## Review Focus

1. A ZIP contains duplicate, unsafe-looking, Unicode, hidden, and nested entries: preserve each file name in directory order without reading any member (Task 1).
2. The ZIP directory is readable but conversion or a limit fails: save its inventory on the writer and retain the original failure/skip (Tasks 2–3).
3. A ZIP cannot be read, a fallback URL is used, or cached provenance is missing/stale: never publish a partial or misattributed inventory; re-download when provenance is unknown (Tasks 2–3).
4. An old URL, empty inventory, unknown inventory, or parse failure is viewed: distinguish all cases without asserting that content was parsed or suggesting unsupported parses (Tasks 4–5).
5. Attacker-controlled filenames or concurrent/cancelled preparation reach a surface: render inert names and keep SQL writes serialized while preserving cancellation (Tasks 2 and 5).

## File Map

- `src/doc3gpp/parsers/tdoc_zip.py`: metadata-only outer-ZIP enumeration.
- `scraping/tdoc_zip_source.py`, `scraping/cache.py`: source-provenance-aware ZIP cache reads and writes while preserving unrelated cache behavior.
- `src/doc3gpp/models/tdoc_zip_inventory.py`, `repository/protocols.py`, `storage/db/models.py`, `storage/repositories/tdoc_zip_inventory_sql.py`, `models/schema_info.py`, `services/factory.py`: domain value, repository, table, schema registry, and construction.
- `services/tdoc_zip_inventory.py`, `services/tdoc_cr_service.py`, `services/tdoc_family_service.py`: carry inventories through database-mode and direct URL preparation to the writer, including parse failures.
- `models/tdoc_show.py`, `models/tdoc_content.py`, `services/tdoc_content_service.py`: repository-backed typed detail and content reads.
- `cli.py`, `web/render.py`, `web/routes/tdocs.py`, `web/mcp_server.py`, `web/templates/tdoc_show.html`: CLI, HTTP, MCP, and HTML presentation.
- Tests under `tests/unit/` and `tests/integration/`; `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`, and `docs/architecture.md` document the resulting behavior.

---

### Task 1: Enumerate and store inventories

**Files:**
- Create: `src/doc3gpp/models/tdoc_zip_inventory.py`, `src/doc3gpp/storage/repositories/tdoc_zip_inventory_sql.py`, `tests/integration/test_tdoc_zip_inventory_sqlite.py`
- Modify: `src/doc3gpp/parsers/tdoc_zip.py`, `src/doc3gpp/repository/protocols.py`, `src/doc3gpp/storage/db/models.py`, `src/doc3gpp/models/schema_info.py`, `src/doc3gpp/services/factory.py`, `tests/unit/test_tdoc_zip.py`, `tests/unit/test_schema_info.py`

**Interfaces:**
- Produces `enumerate_zip_files(zip_bytes: bytes) -> tuple[str, ...]` in `parsers/tdoc_zip.py`.
- Produces frozen `TDocZipInventory(ftp_url: str, tdoc_id: str, zip_files: tuple[str, ...])` in `models/tdoc_zip_inventory.py`.
- Produces `TDocZipInventoryRepository.get_by_url(ftp_url: str) -> TDocZipInventory | None` and `upsert(inventory: TDocZipInventory) -> None`, implemented by `SQLAlchemyTDocZipInventoryRepository(session_factory=None)` and constructed by `build_tdoc_zip_inventory_repository()`.
- Produces `TDocZipInventoryOrm` / table `tdoc_zip_inventory`: normalized `ftp_url` PK, indexed `tdoc_id` FK to `tdocs` with cascade, and non-null `zip_files` text containing a lossless JSON array. Register all three fields under the TDoc schema.

- [ ] **Step 1: Write failing enumeration and storage tests.** In `test_tdoc_zip.py`, use an in-memory archive with the entries below in this order; include both a slash directory and a DOS-separator directory, and use duplicate `dup.pdf` entries. Patch `ZipFile.open` to raise if called while enumerating:

  ```python
  def test_zip_inventory_preserves_outer_file_entries_without_reading_members():
      assert enumerate_zip_files(archive) == (
          "main.docx", "old.doc", "support.pdf", "sheet.xlsx", "__MACOSX/._main",
          "~$lock.docx", "nested.zip", "日本語/文書.pdf", r"a\b.txt",
          "../unsafe.txt", "dup.pdf", "dup.pdf",
      )

  def test_zip_inventory_empty_and_unreadable_directory():
      assert enumerate_zip_files(empty_zip) == ()
      with pytest.raises(zipfile.BadZipFile):
          enumerate_zip_files(b"not a ZIP")
  ```

  In `test_tdoc_zip_inventory_sqlite.py`, seed a TDoc and exercise the real repository and main schema; add tests `test_inventory_round_trips_order_duplicates_and_normalized_urls`, `test_inventory_revisions_empty_and_unknown_are_distinct`, `test_inventory_survives_cache_eviction`, and `test_main_schema_upgrade_is_additive_and_idempotent`. Assert exact tuple/order/duplicates, full versus relative URL lookup equality, two independent URLs for one ID, `None` versus `()`, retained pre-existing rows, and successful repeated `create_schema("main")` against an existing database without the new table. Extend `test_registry_matches_orm_columns` to include the new table.
- [ ] **Step 2: Run the new tests and confirm missing-function/table failures.** Run `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH python -m pytest tests/unit/test_tdoc_zip.py tests/integration/test_tdoc_zip_inventory_sqlite.py tests/unit/test_schema_info.py -q`.
- [ ] **Step 3: Implement the interfaces.** Enumerate `ZipFile.infolist()` only, using the existing directory detection (including DOS separators and directory mode) without the DOCX candidate validator. Use `normalize_ftp_path` for repository keys and JSON encoding/decoding for the filename sequence; create no inventory row for unknown. Add the ORM to existing `Base.metadata` and registry, not a destructive migration. Add the factory without exposing ORM instances.
- [ ] **Step 4: Re-run the Step 2 command.** Expect all tests to pass, including the repeatable upgrade and registry checks.

### Task 2: Carry inventories through database-anchored parsing

**Files:**
- Create: `src/doc3gpp/services/tdoc_zip_inventory.py`, `tests/integration/test_tdoc_zip_inventory_parse_sqlite.py`
- Modify: `src/doc3gpp/scraping/tdoc_zip_source.py`, `src/doc3gpp/scraping/cache.py`, `src/doc3gpp/services/tdoc_cr_service.py`, `src/doc3gpp/services/tdoc_family_service.py`, `src/doc3gpp/services/factory.py`, `tests/unit/test_tdoc_zip_source.py`, `tests/unit/test_tdoc_cache.py`, `tests/unit/test_tdoc_pipeline.py`, `tests/unit/test_pipeline_handlers.py`, `tests/unit/test_tdoc_family_service.py`

**Interfaces:**
- Consumes Task 1's `enumerate_zip_files` and `TDocZipInventoryRepository`.
- Extend `DownloadedZip` with `payload: bytes | None = None` and `download_tdoc_zip(..., require_provenance: bool = False) -> DownloadedZip`; when required, `payload` contains the exact ZIP-byte snapshot and `url` identifies its actual download candidate, including on cache hits. Keep the existing default behavior and fields for unrelated callers.
- Add cache operations `get_zip_with_provenance(key: str) -> tuple[bytes, str] | None` and `put_zip_with_provenance(key: str, data: bytes, source_url: str) -> Path` to `TDocCache` / `TDocCacheLike`. Store an atomically replaced, versioned sidecar containing the actual URL and SHA-256 of the bytes; validate both the digest and candidate URL before reuse. Read the bytes and provenance as one synchronized snapshot, and have inventory preparation consume those same returned bytes rather than re-reading a mutable path. Coordinate concurrent operations for the same key across cache instances/processes; interrupted or inconsistent writes must be detected as unknown, never trusted.
- Any existing `put_bytes(key, data, "zips")` call must invalidate provenance for that key unless it writes through `put_zip_with_provenance`; perform the invalidation under the same per-key synchronization. Markdown cache writes are unchanged.
- Produces `PreparedInventoryFailure(inventory: TDocZipInventory, error: Exception)` in `services/tdoc_zip_inventory.py` and an optional `inventory: TDocZipInventory | None = None` on `PreparedTDocExtract` and `PreparedFamilyExtract`.
- Extend `TDocCrService.prepare_extract(...)` / `TDocFamilyService.prepare_extract(...)` to return `PreparedInventoryFailure` after successful enumeration followed by a parse exception; extend `TDocCrService.persist_prepared_extract(...)` to accept it, persist the inventory, then raise its original `error`.
- Existing `ExtractResult` / `FamilyExtractResult` remain complete cache-hit results; a failure envelope is never considered complete by pipeline `is_complete` callbacks.

- [ ] **Step 1: Write failing real-service tests.** Build small ZIPs and seed supported CR and discussion TDocs. Add the following tests in `test_tdoc_zip_inventory_parse_sqlite.py`, using the actual SQL repository, cache, parser/service and existing fixture patterns:

  ```python
  def test_supported_cr_and_discussion_record_all_zip_entries():
      assert cr_inventory.zip_files == ("main.docx", "support.pdf")
      assert discussion_inventory.zip_files == ("main.docx", "support.pdf")
      assert cr_extract is not None and discussion_extract is not None

  def test_pdf_only_failed_parse_records_inventory_without_extract():
      assert inventory.zip_files == ("only.pdf",)
      assert extract is None
      assert outcome.status == "failed" and original_error_name in outcome.reason

  def test_limit_skip_records_inventory_without_changing_skip_reason():
      assert inventory.zip_files == ("one.docx", "two.docx")
      assert outcome.status == "skipped" and "TDocZipLimitError" in outcome.reason
  ```

  Also test `test_validation_failure_records_duplicate_and_unsafe_names`, `test_unreadable_or_missing_zip_keeps_previous_inventory`, `test_complete_cached_parse_does_not_backfill_but_force_uses_verified_cached_zip`, and `test_resolved_fallback_url_owns_inventory_for_cr_and_family`. Assert original validation/download error classification; no new/overwritten row on unreadable/download failure; a normal complete cache hit leaves unknown unchanged; forced extraction records the source without a network call when the ZIP has verified provenance; the actual fallback URL, not the attempted URL, owns the inventory. Check extracted document identity remains consistent with its actual URL.
- [ ] **Step 2: Write failing cache-provenance tests.** In `test_tdoc_zip_source.py` and `test_tdoc_cache.py` add `test_legacy_cached_zip_redownloads_before_inventory`, `test_fallback_download_cache_hit_preserves_actual_source`, `test_missing_malformed_wrong_candidate_or_wrong_digest_provenance_redownloads`, `test_unknown_provenance_download_failure_preserves_cache`, `test_cached_provenance_does_not_bypass_source_size_limit`, `test_oversized_provenance_redownload_preserves_existing_cache`, `test_plain_zip_cache_write_invalidates_provenance`, and `test_concurrent_cache_processes_never_pair_bytes_with_wrong_url`. Assert no inventory receives an assumed URL, exact fallback URL survives later cache reuse, source-size guards still apply before replacing old bytes, failed/oversized download leaves pre-existing ZIP bytes unchanged, a plain ZIP-cache write makes provenance unavailable, and concurrent process reads never combine bytes with another download's provenance. Test cache purge/eviction and an interrupted sidecar write: orphaned metadata is not trusted and cannot recreate an evicted ZIP. Use two different ZIP payloads and two candidate URLs in the concurrency test.
- [ ] **Step 3: Write failing writer/cancellation tests.** In pipeline/handler tests add `test_inventory_writes_are_serialized_for_concurrent_success_failure_and_skip` and `test_cancelled_preparation_does_not_publish_inventory`. Instrument a repository wrapping real writes with thread IDs and an active-writer counter, use concurrent preparations in both `run_sync` and `run_async`, and assert `max_active_writers == 1`, no preparation thread writes, correct ordered outcomes/reasons, and cancellation still propagates without publishing a not-yet-submitted result. Add `test_sync_extract_persists_inventory_before_reraising_original_error` asserting exception type/message and stored inventory.
- [ ] **Step 4: Run the added tests and confirm they fail for missing inventory/provenance behavior.** Run `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH python -m pytest tests/integration/test_tdoc_zip_inventory_parse_sqlite.py tests/unit/test_tdoc_zip_source.py tests/unit/test_tdoc_cache.py tests/unit/test_tdoc_pipeline.py tests/unit/test_pipeline_handlers.py tests/unit/test_tdoc_family_service.py -q`.
- [ ] **Step 5: Implement cache provenance.** Bind the URL and bytes as specified above; ensure cache reads and all writes/removals affecting the ZIP/sidecar are synchronized even across cache instances/processes, and pair the resulting byte snapshot with its URL through preparation. For required provenance, treat invalid/absent metadata as a cache miss, perform the existing candidate downloads with source-size checks, and only replace the ZIP and sidecar after successful download. Failed or oversized download leaves existing bytes intact. Any plain `put_bytes(..., "zips")` invalidates that key's provenance under the same lock. Sidecars participate in normal cache size accounting and purge/eviction; missing ZIPs or metadata remain cache misses. Other callers retain their existing legacy-cache behavior.
- [ ] **Step 6: Implement preparation and persistence.** Read the verified source bytes once after existing download/size checks; attempt outer-ZIP enumeration before DOCX validation/conversion. On a readable directory, attach the inventory to a successful prepared result or carry the exact `Exception` in `PreparedInventoryFailure`. Do not catch cancellation or `BaseException`. If the directory is unreadable, do not persist a new inventory and let the existing parser report its original error. At the writer, upsert inventory before the existing atomic bundle transaction, then run unchanged success hooks; for a failure envelope upsert and re-raise the original error so the pipeline classifies it normally. If the inventory write itself fails on that error path, log it and re-raise the original parsing error rather than masking it. Apply actual resolved download URL consistently in family and CR paths. Do not add a separate writer or mutate the database in preparation.
- [ ] **Step 7: Re-run the Step 4 command.** Expect all tests to pass with no changed failed/skipped or cancellation behavior.

### Task 3: Support direct canonical 3GPP ZIP URLs without new side effects

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py`, `tests/integration/test_tdoc_parse_direct_sqlite.py`, `tests/integration/test_tdoc_family_direct_sqlite.py`, `tests/unit/test_tdoc_cr_service_ftp_batch.py`, `tests/unit/test_pipeline_handlers.py`

**Interfaces:**
- Consumes Task 2's `PreparedInventoryFailure` and inventory repository.
- Add `inventory: TDocZipInventory | None = None` to `PreparedDirectTDoc`; use `PreparedDirectFamily.prepared.inventory` for families. Extend `prepare_from_url(...)` and `persist_prepared_url(...)` to carry and persist the same failure envelope.

- [ ] **Step 1: Write failing direct-mode tests.** Use real SQL/cache and 3GPP URL fixtures to add these tests and assertions:

  ```python
  def test_known_eligible_3gpp_zip_success_and_parse_failure_both_store_inventory():
      assert successful_url_inventory.zip_files == ("main.docx", "support.pdf")
      assert failed_url_inventory.zip_files == ("only.pdf",)
      assert failed_url_extract is None
      assert error_type_and_message == original_error_type_and_message

  def test_direct_docx_does_not_inventory_its_internal_zip():
      assert inventory_repo.get_by_url(docx_url) is None

  def test_local_non_3gpp_unknown_id_and_ineligible_types_do_not_persist():
      assert all(inventory_repo.get_by_url(url) is None for url in attempted_urls)
  ```

  Add `test_direct_url_batch_and_web_job_preserve_failure_skip_and_writer_lane`, covering both failure and conversion-limit skip through synchronous URL batch and asynchronous web job. Add `test_direct_legacy_cache_redownloads_and_failed_download_preserves_it`, and check a subsequent verified cache hit needs no network. Include an unknown or unsupported stored type and assert existing direct parsing/type-detection behavior is unchanged even though inventory collection requires an eligible stored type.
- [ ] **Step 2: Run the direct-mode tests and confirm missing-inventory failures.** Run `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH python -m pytest tests/integration/test_tdoc_parse_direct_sqlite.py tests/integration/test_tdoc_family_direct_sqlite.py tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_pipeline_handlers.py -q`.
- [ ] **Step 3: Implement canonical ZIP URL collection.** Restrict inventory enumeration to an actual outer `.zip` for a stored eligible TDoc; retain the existing in-memory and direct DOCX paths. In the direct canonical-URL path, use Task 2's provenance-aware cache methods with the exact canonical URL requested (do not route this flow through `download_tdoc_zip`'s stored-TDoc fallback candidates); re-download unverified legacy entries and use the exact bytes/URL snapshot. Attach inventories before conversion to both direct prepared variants and return failure envelopes when later work fails. Make `persist_prepared_url` upsert and re-raise via the same writer semantics as Task 2; retain `_DirectPipelineError` behavior for the single-URL API and existing batch/job classification. Preserve other direct cache/download rules and output.
- [ ] **Step 4: Re-run the Step 2 command.** Expect all direct, folder-batch, and web-job tests to pass.

### Task 4: Expose typed inventory and JSON snapshots

**Files:**
- Modify: `src/doc3gpp/models/tdoc_show.py`, `src/doc3gpp/models/tdoc_content.py`, `src/doc3gpp/services/tdoc_content_service.py`, `src/doc3gpp/services/factory.py`, `src/doc3gpp/cli.py`, `src/doc3gpp/web/render.py`, `src/doc3gpp/web/routes/tdocs.py`, `src/doc3gpp/web/mcp_server.py`
- Test: `tests/unit/test_tdoc_show_record.py`, `tests/unit/test_tdoc_content_service.py`, `tests/unit/test_tdoc_content_surfaces.py`, `tests/integration/test_mcp_end_to_end.py`, `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes Task 1's URL-keyed repository.
- Add `zip_files: tuple[str, ...] | None = None` to `TDocShowRecord`, `TDocShowRecordByUrl`, and `TDocContent`; inject `TDocZipInventoryRepository` into `TDocShowRepos` and `TDocContentService` using existing factory/composition conventions.
- Keep `TDocShowRecord.from_tdoc_id(tdoc_id, repos)`, `TDocShowRecordByUrl.from_ftp_url(ftp_url, repos)`, `TDocContentService.get(...)`, and `tdoc_content_to_dict(content)` as the public read interfaces.

- [ ] **Step 1: Write failing typed and JSON contract tests.** With real repository-backed records, assert the following; exercise both detail selectors and both content selectors:

  ```python
  def test_detail_and_content_expose_unknown_empty_and_ordered_files():
      assert unknown.zip_files is None
      assert empty.zip_files == ()
      assert known.zip_files == ("a.docx", "support.pdf", "support.pdf")
      assert content.zip_files == known.zip_files

  def test_detail_json_keeps_null_empty_duplicates_and_auxiliary_files_separate():
      assert unknown_payload["zip_files"] is None
      assert empty_payload["zip_files"] == []
      assert known_payload["zip_files"] == ["a.docx", "support.pdf", "support.pdf"]
      assert known_payload["files"][0]["type"] == "review"

  def test_detail_and_content_http_mcp_json_are_byte_identical():
      assert http_detail_bytes == mcp_detail_bytes
      assert http_content_bytes == mcp_content_bytes
  ```

  Add `test_by_id_uses_current_url_and_by_url_finds_old_inventory`, including a historic URL with no matching current `tdocs.ftp_url` and a known empty inventory; `test_failed_parse_inventory_does_not_set_parsed_or_extracted_at`; and `test_inventory_lookup_does_not_download_or_parse`. Guard ZIP download and parser calls with fail-fast spies for HTTP, MCP and CLI with auto-sync disabled; separately assert the existing CLI metadata auto-sync still runs when enabled. Verify source-file filtering changes `documents` only, not `zip_files`. Add a no-content case showing detail still returns inventory while the content endpoint retains its existing unavailable error.
- [ ] **Step 2: Run the targeted tests and confirm missing-field/composition failures.** Run `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH python -m pytest tests/unit/test_tdoc_show_record.py tests/unit/test_tdoc_content_service.py tests/unit/test_tdoc_content_surfaces.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -q`.
- [ ] **Step 3: Implement typed lookup and JSON serialization.** Read by the current normalized `tdoc.ftp_url` for ID mode and the exact normalized selected URL for URL mode; never infer from converted documents. Wire the repository into CLI, web, MCP, and content-service constructors. Treat a URL inventory, including `()`, as a matching row in URL-mode not-found checks. Make only these TDoc detail dataclasses retain `zip_files` when null or empty in `web/render.py`; preserve all other omit-when-null rules and existing JSON key order. Add the same always-present field to CLI `_build_show_payload` and `tdoc_content_to_dict`; ensure compact HTTP/MCP bytes remain identical. Keep absent cover/sidecars, extraction state, `files`, source selection, and raw content semantics unchanged.
- [ ] **Step 4: Re-run the Step 2 command.** Expect all typed SDK and JSON parity tests to pass.

### Task 5: Present the inventory and document the upgrade

**Files:**
- Modify: `src/doc3gpp/cli.py`, `src/doc3gpp/web/templates/tdoc_show.html`, `tests/unit/test_tdoc_content_surfaces.py`, `tests/unit/test_web_routes.py`, `tests/unit/test_compact_helpers.py`, `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`, `docs/architecture.md`

**Interfaces:**
- Consumes Task 4's `record.zip_files` for ID and URL detail, along with `record.tdoc.type` and the existing content-family eligibility registry. Raw output and `--source-file` retain their existing contract.

- [ ] **Step 1: Write failing presentation tests.** Exercise table, full Markdown, compact Markdown, and web HTML for both selectors:

  ```python
  def test_known_zip_files_are_separate_from_auxiliary_files():
      assert "ZIP files" in rendered
      assert "support.pdf" in rendered and "review.zip" in auxiliary_section

  def test_unknown_supported_has_force_hint_but_unsupported_does_not():
      assert "unavailable" in supported.lower() and "--force" in supported
      assert "unavailable" in unsupported.lower() and "--force" not in unsupported

  def test_empty_zip_and_failed_parse_states_are_honest():
      assert "no files" in empty.lower()
      assert "only.pdf" in failed_parse and "already been parsed" not in failed_parse

  def test_html_escapes_untrusted_zip_names():
      assert "&lt;script&gt;" in html
      assert "<script>alert(1)</script>" not in html
  ```

  Add `test_zip_names_cannot_inject_markdown_or_terminal_control_sequences`, using a name containing Markdown link/image syntax, HTML and a terminal escape, and assert it is rendered as inert text. Add `test_raw_output_is_byte_identical_with_and_without_inventory` and an exact `--source-file` regression. Ensure the pre-existing parsed-discussion/no-cover web regression remains intact.
- [ ] **Step 2: Run the presentation tests and confirm missing-section failures.** Run `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH python -m pytest tests/unit/test_tdoc_content_surfaces.py tests/unit/test_web_routes.py tests/unit/test_compact_helpers.py -q`.
- [ ] **Step 3: Implement distinct ZIP-files sections.** Render names in stored order, with duplicates; make unknown, known empty, and known nonempty states distinct. Provide `doc3gpp tdoc parse --tdoc <id> --force` for supported ID records and `doc3gpp tdoc parse --from-url <url> --force` for supported URL records; do not offer a parse hint when type/eligibility is unavailable or unsupported. Escape output as required without altering stored/JSON filenames. Keep auxiliary sections and the existing cover-page regression fix.
- [ ] **Step 4: Update documentation.** Explain `zip_files` versus `files`/`documents`; supported types and all member extensions; null versus empty; ZIP metadata-only safety and no nested traversal; failure and cache-eviction independence; current versus historical URL; all CLI/HTTP/MCP/SDK surfaces and the existing CLI auto-sync exception; `--force` re-parse for existing entries, including re-download of old ZIP caches with unknown provenance; and the required additive `doc3gpp db init --scope main` before using an existing database with the new version. Update registry/table counts and affected diagrams or examples where present.
- [ ] **Step 5: Re-run the Step 2 command, lint, and the full offline suite.** Run `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH python -m pytest tests/unit/test_tdoc_content_surfaces.py tests/unit/test_web_routes.py tests/unit/test_compact_helpers.py -q`, `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH ruff check .`, `PATH=/tmp/opencode/doc3gpp-venv/bin:$PATH ./scripts/test_sqlite.sh`, and `git diff --check`. Expect exit code 0 for each; report the actual pass/skip/warning counts.

## Final review

- [ ] After each task, perform the SDD spec-compliance and code-quality reviews before dispatching the next task. Review the uncommitted working tree, including untracked files, without staging or committing.
- [ ] Request a fresh whole-branch review after Task 5, resolve actionable findings, and re-run affected tests and the full suite if fixes change code. Confirm the pre-existing web fix remains present and report any remaining limitations.
