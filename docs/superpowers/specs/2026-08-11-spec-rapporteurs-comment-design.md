# Spec rapporteurs + drop comment — design

**Date:** 2026-08-11
**Status:** Approved

## 1. Goal

Two changes to the spec storage / parsing pipeline:

1. **Remove the `comment` field** from `spec_versions`. It is parsed from
   the version row's `.lblRemarkText` cell but is not being extracted /
   used, so it should be dropped end-to-end.
2. **Add a `rapporteurs` field** to `specs`, extracted from the spec
   detail page's rapporteurs grid. It concatenates all company names from
   the rapporteurs table, comma-joined (mirroring how `wis` is stored).
   For 38.508-1 the single rapporteur company is `Ericsson LM`.

## 2. Rapporteurs extraction

The detail page carries a Telerik grid with id
`specificationRapporteurs_rdGridRapporteurs_ctl00`. Each `tr.rgRow` /
`tr.rgAltRow` has a `td.companyStyleColumn` whose inner `<div>` holds the
company name:

```html
<table class="rgMasterTable rgClipCells" id="specificationRapporteurs_rdGridRapporteurs_ctl00">
  <tbody>
    <tr class="rgRow" id="specificationRapporteurs_rdGridRapporteurs_ctl00__0">
      <td class="hyperlinkStyleColumn"><a ...><strong>Bo</strong> <strong>Jönsson</strong></a></td>
      <td class="companyStyleColumn"><div>Ericsson LM</div></td>
      <td style="display:none;">76116</td>
    </tr>
  </tbody>
</table>
```

Extraction rules (mirror `_extract_related_wis`):

- Locate the grid by id `specificationRapporteurs_rdGridRapporteurs_ctl00`.
- Iterate `tr` rows with class containing `rgRow` or `rgAltRow`.
- For each row, read the `td.companyStyleColumn` cell, normalize its text
  (collapse whitespace via `_normalize`).
- Skip empty / duplicate values; comma-join the survivors.
- Return `None` when the grid is absent or no company names are found.

For 38.508-1 this yields `"Ericsson LM"`.

## 3. Schema changes

### 3.1 `specs` — add `rapporteurs`

| Column        | Type          | Notes |
| ------------- | ------------- | ----- |
| `rapporteurs` | `String(512)` | Comma-joined company names from the detail page rapporteurs grid. Nullable. |

### 3.2 `spec_versions` — drop `comment`

Remove the `comment` column (`String(256)`). New databases will not
create it; existing databases get it dropped via migration (see §5).

## 4. Code changes

### 4.1 `models/spec.py`

- `Spec`: add `rapporteurs: str | None = None` (after `wis`).
- `SpecVersion`: remove `comment: str | None = None` and its docstring
  line.

### 4.2 `storage/db/models.py`

- `SpecORM`: add `rapporteurs: Mapped[str | None] = mapped_column(String(512), nullable=True)`.
- `SpecVersionORM`: remove the `comment` column.

### 4.3 `parsers/spec_parser.py`

- `parse_spec_detail`: extract `rapporteurs` via a new
  `_extract_rapporteurs(soup)` helper and set it on the header `Spec`.
- `_parse_version_row`: remove the `.lblRemarkText` comment extraction
  and the `comment=` kwarg.

### 4.4 `storage/repositories/spec_sql.py`

- `upsert`: persist `existing.rapporteurs = spec.rapporteurs` (update
  branch) and `rapporteurs=spec.rapporteurs` (insert branch).
- `_orm_to_spec`: map `rapporteurs=row.rapporteurs`.
- `upsert_versions`: remove `existing.comment = v.comment` and
  `comment=v.comment`.
- `_orm_to_version`: remove `comment=row.comment`.

### 4.5 `storage/db/migrate.py`

Add `_migrate_spec_rapporteurs()` and `_migrate_spec_versions_drop_comment()`
following the `_migrate_tsg_spec_last_sync` probe pattern:

- `_migrate_spec_rapporteurs`: if `specs` exists and `PRAGMA table_info(specs)`
  lacks `rapporteurs`, `ALTER TABLE specs ADD COLUMN rapporteurs VARCHAR(512)`.
- `_migrate_spec_versions_drop_comment`: if `spec_versions` exists and
  `PRAGMA table_info(spec_versions)` has `comment`,
  `ALTER TABLE spec_versions DROP COLUMN comment`. SQLite 3.35+ supports
  `DROP COLUMN`; the runtime sqlite is 3.45.1. Guard with a `try/except`
  so an older sqlite (no `DROP COLUMN`) degrades to leaving the orphan
  column rather than failing schema bootstrap.

Call both from `create_schema()` before `Base.metadata.create_all`.

### 4.6 `cli.py` (`spec show`)

- `header_fields`: add `"rapporteurs"` (after `"wis"`).
- `version_fields`: remove `"comment"`.

### 4.7 `settings/schema.py`

- `OutputSettings.fields.spec` default: add `"rapporteurs"` (after `"wis"`),
  so `spec list` shows it by default.

### 4.8 `web/routes/specs.py`

- `_SPEC_DEFAULT_FIELDS`: add `"rapporteurs"`.
- `_VERSION_FIELDS`: remove `"comment"`.

### 4.9 `web/mcp_server.py`

- `_SPEC_FIELDS`: add `"rapporteurs"`.
- `_VERSION_FIELDS`: remove `"comment"`.

### 4.10 `web/templates/spec_show.html`

- Add a `Rapporteurs` `<dt>/<dd>` row (guarded by `{% if spec.rapporteurs %}`).
- Remove the `Comment` `<th>` and `<td>` from the versions table.

## 5. Migration behaviour

- **New DB:** `create_all` builds `specs` with `rapporteurs` and
  `spec_versions` without `comment`.
- **Existing DB:** `_migrate_spec_rapporteurs` adds the column;
  `_migrate_spec_versions_drop_comment` drops it. Both are idempotent
  (probe `PRAGMA table_info` first). If `DROP COLUMN` is unsupported the
  orphan column is left in place and the ORM simply stops reading/writing
  it.

## 6. Tests

- `tests/unit/test_spec_parser.py`: assert `header.rapporteurs` on the
  portal fixture (38.508-1 → `"Ericsson LM"`); remove the
  `v0.comment == "Some comment here"` assertion.
- `tests/fixtures/spec_pages/R5_detail_portal.html`: add a rapporteurs
  grid (single `Ericsson LM` row) so the parser test has a target.
- `tests/unit/test_spec_model.py`: add `rapporteurs` to the fields test;
  remove `v.comment is None` from the optional-fields test.
- `tests/integration/test_spec_sql.py`: add `rapporteurs` to the
  upsert/get round-trip; remove `comment` from the dedupe tests.
- `tests/integration/test_spec_cli.py`: remove `comment="-"` from the
  `spec show` JSON test.
- `tests/unit/test_web_routes.py`: `FakeSpecService` / assertions updated
  for the new field (no `comment` in version rows).

## 7. Docs

Update `docs/architecture.md`, `docs/cli.md`, `docs/3gpp-knowledge.md`,
`docs/code-map.md`, and the historical spec
`docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md` per the
AGENTS.md doc-sync convention. `doc3gpp.toml.example` gains a commented
`rapporteurs` entry in the `[output] fields.spec` block.

## 8. Acceptance criteria

- `spec sync` stores `rapporteurs` on the `specs` row and no longer
  stores `comment` on `spec_versions`.
- `spec list` / `spec show` / `/specs` / MCP `list_specs` / `get_spec`
  surface `rapporteurs` and no longer surface `comment`.
- Existing databases migrate cleanly (add `rapporteurs`, drop `comment`).
- Full sqlite test suite passes (`./scripts/test_sqlite.sh`).
