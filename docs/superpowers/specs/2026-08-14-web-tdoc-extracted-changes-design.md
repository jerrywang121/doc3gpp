# Web tdoc detail — Extracted Changes Sections

**Date:** 2026-08-14
**Branch:** main
**Status:** design

## Goal

The web tdoc detail page (`GET /tdocs/{id}` → `tdoc_show.html`) currently
exposes the cover-page sidecar and the TTCN sidecar's overview fields, but
never surfaces the structured extraction results that the parser already
produces and persists:

- For TTCN CRs: the **`required_changes`** list (one entry per TTCN
  correction: function/template name, module, reason-for-change,
  summary-of-change, MCC160 comment).
- For non-TTCN CRs: the **body-derived change blocks** captured by
  `extract_body_changes` — each block carries its attributed clause
  labels and the captured text (with `<ins>` / `<del>` markers, gap
  bridges, and context padding).

The CLI's `tdoc show` already renders both shapes in its markdown
renderer (`_render_tdoc_show_markdown_full` in `src/doc3gpp/cli.py`,
`## TTCN Details` and `## Change Details` sections). The web page just
doesn't. This spec closes that gap so a human visiting
`/tdocs/<id>` in a browser sees the same information the CLI emits.

The MCP `tdoc_show` tool and the `?format=json` HTTP route are already
complete (the route delegates to `to_jsonable(record)`, which already
serialises `ttcn.required_changes` and `changes`). This work touches
the HTML template only.

## Non-Goals

- No new repo reads, no new model fields, no new service-layer code.
- No changes to the CLI renderers (already complete).
- No changes to the by-URL route (`GET /tdocs/.../content?format=html`,
  which renders the cached markdown bytes — a separate render path).
- No changes to the search subsystem, search hits, or MCP tool
  descriptions.
- No changes to the cached markdown / zip paths.

## Design

### Composition

The data is already in the `TDocShowRecord` composed by
`TDocShowRecord.from_tdoc_id(...)` in
`src/doc3gpp/models/tdoc_show.py:75`. The two new cards render fields
that today are silently dropped by `tdoc_show.html`:

- `record.ttcn.required_changes: list[dict[str, Any]]` — populated only
  when `is_ttcn_tdoc(record.tdoc.tdoc_id)` is `True` AND the TTCN
  parser recognised at least one correction.
- `record.changes: TDocCRChangeDetails | None` — populated only for
  non-TTCN CRs (TTCN CRs write `changes=None` per
  `TTCNCRParser.parse` in `src/doc3gpp/parsers/cr/cr_parsers.py:238`).
  Carries `clauses: tuple[str, ...]` and
  `changes: tuple[ChangeBlock, ...]`, where each `ChangeBlock` is
  `{"clauses": list[str], "text": str}`.

The two cards are mutually exclusive by construction: a TTCN CR never
has `record.changes` and a non-TTCN CR never has `record.ttcn`. We
rely on that invariant rather than re-checking the tdoc-id shape in
the template.

### Template

Add two new `{% if %}` blocks to `src/doc3gpp/web/templates/tdoc_show.html`,
appended in this order, placed between the existing TTCN card (line 109)
and the existing "Extracted at" card (line 111):

1. **"Required changes"** — renders when
   `record.ttcn is not None and record.ttcn.required_changes`.
2. **"Extracted changes"** — renders when `record.changes is not None`.

The cards reuse the existing `card` / `kv` / `placeholder` classes and
the existing `<dl class="kv">` pattern. One new class — `change-block`
on the `<pre>` element — is added so any future styling (max-height,
scrollbar, code-block background) can target the block text without
rippling into the rest of the page. The first release ships with no
extra CSS rules; the project stylesheet's existing `<pre>` defaults
apply.

#### Card 1 — "Required changes" (TTCN)

```
<section class="card">
  <h2>Required changes</h2>
  {% for change in record.ttcn.required_changes %}
    <article class="change-entry">
      <dl class="kv">
        {% if change.function_name %}<dt>Function name</dt><dd><code>{{ change.function_name }}</code></dd>{% endif %}
        {% if change.ttcn_module %}<dt>TTCN module</dt><dd><code>{{ change.ttcn_module }}</code></dd>{% endif %}
        {% if change.reason_for_change %}<dt>Reason for change</dt><dd>{{ change.reason_for_change }}</dd>{% endif %}
        {% if change.summary_of_change %}<dt>Summary of change</dt><dd>{{ change.summary_of_change }}</dd>{% endif %}
        {% if change.mcc160_comment %}<dt>MCC160 comment</dt><dd>{{ change.mcc160_comment }}</dd>{% endif %}
      </dl>
    </article>
  {% endfor %}
</section>
```

Notes:
- The existing `Changed functions` aggregate stays inside the TTCN
  card exactly as it is today (`tdoc_show.html:97-106`).
- Fields absent on a given entry are skipped entirely (no `—`
  placeholder per field) — keeps each entry compact.
- One entry per `change`, separated by a blank line (no extra CSS
  in the first release). The `change-entry` class is a hook for
  future styling (border-top, padding) without changing the markup
  shape.
- The `required_changes` list is preserved as-is from the JSON blob
  the parser produced. We render the five known fields explicitly
  and skip any key the template doesn't enumerate (forward-compat
  for parser additions). `function_name` is the only field that can
  be a `'.<function>'` or `'<module>.'` partial-recovery sentinel
  (see the `changed_functions` derivation contract in
  `src/doc3gpp/parsers/cr/ttcn_functions.py`); we render whatever
  string is in the entry verbatim — no special-case UI.

#### Card 2 — "Extracted changes" (non-TTCN)

```
<section class="card">
  <h2>Extracted changes</h2>
  <p class="meta">
    <span>Clauses: <code>{{ record.changes.clauses | join(', ') or '—' }}</code></span>
    <span>{{ record.changes.changes | length }} block(s)</span>
  </p>
  {% for block in record.changes.changes %}
    <article class="change-block-entry">
      <h3>Block {{ loop.index }}{% if block.clauses %} · clauses: {{ block.clauses | join(', ') }}{% endif %}</h3>
      <pre><code class="change-block">{{ block.text }}</code></pre>
    </article>
  {% endfor %}
</section>
```

Notes:
- The header line mirrors `_render_tdoc_show_markdown_full`'s
  `## Change Details` opener (clauses + block count).
- The `<pre>` keeps the captured text byte-faithful: newlines,
  `<ins>` / `<del>` marker lines, gap-window bridges, and
  context-padding plain lines all survive. Long blocks scroll
  inside the `<pre>` rather than expanding the page; if a future
  release needs a hard cap, the `change-block` class is the hook.
- An empty `record.changes.changes` tuple is a parser-level
  edge case the data model permits; we render the card with the
  header line and an empty body (no "no blocks" placeholder — the
  header already shows `0 block(s)`).

### Empty / Missing Data

The cards are gated on `record.ttcn.required_changes` /
`record.changes`, so the visible states are:

| State | Card 1 (Required changes) | Card 2 (Extracted changes) |
| --- | --- | --- |
| TTCN CR, no corrections | Omitted entirely | Omitted entirely (TTCN CRs always have `changes=None`) |
| TTCN CR, with corrections | Rendered | Omitted |
| Non-TTCN CR, no body changes | Omitted entirely | Omitted entirely |
| Non-TTCN CR, with body changes | Omitted entirely (non-TTCN CRs always have `ttcn=None`) | Rendered |
| TDoc not yet extracted (no `cover`) | Omitted (no `ttcn` row exists) | Omitted (no `changes` row exists) |

No "no changes" placeholder for either card — absence of the
extraction is the natural state of an unparsed TDoc, and the
existing "Cover page" placeholder already points the operator at
`doc3gpp tdoc parse --tdoc <id>`. Adding a second placeholder would
be noise.

### JSON / MCP Consistency

`GET /tdocs/{id}?format=json` already serialises both fields via
`to_jsonable(record)`. No change. The MCP `tdoc_show` tool
delegates to the same JSON envelope. No change.

The by-URL route (`GET /tdocs/{tdoc_id}?ftp_url=...`) is also
unaffected — the `TDocShowRecordByUrl` model carries the same
`ttcn` and `changes` fields, and the by-URL route's template path
does not exist today (the by-URL selector is documented as a
CLI-only / JSON-only selector; see `src/doc3gpp/cli.py:_tdoc_show_by_ftp_url`).

## Files Touched

| File | Change |
| --- | --- |
| `src/doc3gpp/web/templates/tdoc_show.html` | Add two `{% if %}` blocks between the existing TTCN card and the "Extracted at" card. ~40 lines added. |
| `tests/unit/test_tdoc_show_web_template.py` | New file. Four cases (see Testing). |
| `AGENTS.md` | One-line addition to the "Where to look" table noting the two new cards. |
| `docs/web-server.md` | One-line addition to the tdoc detail page description. |

No changes to `cli.py`, `web/routes/tdocs.py`, `web/render.py`,
`web/filters.py`, `models/tdoc_show.py`, `models/tdoc_cr.py`,
`models/tdoc_cr_change_details.py`, or any storage repo.

## Testing

A new `tests/unit/test_tdoc_show_web_template.py` covers four cases
by invoking `Jinja2Templates.TemplateResponse` against a hand-built
`TDocShowRecord` and asserting on the rendered HTML:

1. **TTCN CR with corrections** — `record.ttcn` populated,
   `record.ttcn.required_changes` has 2 entries with all five fields
   populated on the first entry and only `function_name` on the
   second. Assert:
   - The "Required changes" `<h2>` appears exactly once.
   - Two `<article class="change-entry">` elements appear.
   - The first entry renders all five `<dt>`/`<dd>` pairs; the
     second entry renders exactly one.
   - The existing TTCN card's "Changed functions" list is still
     present (regression guard).
   - The "Extracted changes" card is **not** present (TTCN CRs
     never have `record.changes`).

2. **Non-TTCN CR with body changes** — `record.changes` populated
   with 2 clauses and 2 change blocks (block 1 has clauses + text;
   block 2 has clauses only, empty `text`). Assert:
   - The "Extracted changes" `<h2>` appears exactly once.
   - The header line shows the comma-joined clauses string and
     `2 block(s)`.
   - Two `<article class="change-block-entry">` elements appear.
   - Two `<pre><code class="change-block">` elements appear, with
     the captured text byte-faithful.
   - The "Required changes" card is **not** present (non-TTCN CRs
     never have `record.ttcn`).

3. **Empty record (no sidecars)** — both `record.ttcn` and
   `record.changes` are `None`. Assert:
   - Neither new card is in the output.
   - The existing "Cover page" placeholder is still present
     (regression guard).

4. **TTCN CR with empty `required_changes`** — `record.ttcn`
   populated, `record.ttcn.required_changes == []`. Assert:
   - The "Required changes" card is **not** present.
   - The existing TTCN card is still present (regression guard).

Snapshot-style HTML assertions (substring + element-count) — no
full-HTML diff. Keeps the test robust against unrelated whitespace
or attribute-order changes. The test does not touch a database or
a network: the `TDocShowRecord` is constructed directly in-memory
with a `TDocShowRepos` carrying stub repositories (the same pattern
`tests/unit/test_tdoc_show_record.py` already uses).

## Implementation Order

1. Add the two `{% if %}` blocks to `tdoc_show.html`.
2. Add `tests/unit/test_tdoc_show_web_template.py` with the four
   cases. Run the new tests; iterate on the template until green.
3. Run the full sqlite suite (`./scripts/test_sqlite.sh`) to confirm
   no regression in the existing tdoc-show tests.
4. Run `ruff check .` to confirm no lint regressions.
5. Update `AGENTS.md` "Where to look" table — one row, one line.
6. Update `docs/web-server.md` — one sentence in the tdoc detail
   section.
7. Commit on the current branch (no new branch — the work is small
   and follows the established template-extension pattern; the user
   will say if they want a feature branch).

## Open Questions

None. The data is in the model, the CLI already renders it, the
template just needs to follow suit.
