# LISTSERV catalog filters and single-message parsing

## Intent and scope

Allow users to filter the LISTSERV catalog by list ID and TSG from the CLI,
REST API, web portal, and MCP. On the portal, give the catalog its own panel
above “Search stored messages” and link each displayed list ID to its ETSI
archive. Add an explicit `parse-item` operation on all four surfaces that
parses one email by its ETSI detail URL, including an email and list that have
not previously been synced.

Existing unfiltered catalog responses, list-wide sync and parse, stored reads,
message search, and index ownership remain compatible. The new operation does
not sync a whole list or imply that an archive page has been synced.

## Catalog filters and presentation

Extend the catalog repository protocol, SQL implementation, and service with
optional `list_id` and `tsg` text filters. Apply the existing parameterized rich
SQL LIKE grammar separately to the catalog columns and intersect the filters.
Plain values match exactly; `%` and `_` are SQL wildcards; `!` negates a LIKE
pattern; `null` and `not-null` test SQL nullability. SQL three-valued logic
applies to negation on nullable TSGs. Preserve token handling and validation
used by other rich filters, and do not uppercase patterns in a way that changes
the grammar. No filter means the current ordered catalog result.

Expose these filters as `doc3gpp listserv list --list-id ... --tsg ...`, query
parameters on `GET /api/listserv/lists`, and optional arguments on the MCP
`listserv_list` tool. Both JSON arrays, `lists` and the compatibility
`list_ids`, reflect the same filtered, list-ID-ordered records. The operations
are SQL-only and require no network access.

On `/listserv`, put the catalog in its own panel immediately before “Search
stored messages”. Add list-ID and TSG filters with the same rich SQL LIKE
behavior. Filtering the catalog must not change the separate message-search
filters. Render each list ID as an escaped link to
`https://list.etsi.org/scripts/wa.exe?A0=<list_id>`, using the existing safe
archive-URL construction and HTML escaping. Display catalog TSG and description
where the current presentation already provides them; keep message search and
the existing selector usable.

## Single-item operation

Expose `doc3gpp listserv parse-item URL [--force]`, a
`POST /api/listserv/parse-item` request with `url` and optional `force`, a
`listserv_parse_item` MCP tool with equivalent arguments, and a portal form with
the same inputs. CLI runs the service directly; REST and MCP enqueue a job and
the portal uses the same job-monitoring conventions as existing LISTSERV jobs.
The service is also available to SDK callers. A previously parsed item is
skipped unless forced, matching list-wide parse. An unparsed item is parsed.

Accept the public detail form
`https://list.etsi.org/scripts/wa.exe?A2=<list_id>;<item_id>.<year_month_wk_group_id>`
with optional empty `&S=`. Both variants identify the same item. Validate the
scheme, host, port, path, list ID, A2 structure and optional suffix before any
network or database work; reject other parameters, credentials, fragments,
malformed components, unsafe hosts and attachment URLs. Reuse the existing
public-host-only transport and redirect validation; do not download attachment
bytes. Normalize the two accepted forms to the app's detail-URL identity and
resolve already stored equivalent URL forms without creating duplicates or
destructively rewriting existing data.

For a known message, retain existing synced metadata and reuse the detail
parser. For an unknown message, fetch its A2 page once, extract and validate
its subject, sender, and displayed date from the detail headers, derive its
week consistently with the existing LISTSERV model, and reuse the same fetched
page for detail/body parsing. If required metadata is missing or malformed,
report a parse failure rather than inventing a message. Reuse existing body
conversion, content bounds, TDoc extraction, attachment-metadata handling,
parser version, and timestamp behavior.

After the entire item is parsed successfully, commit its catalog registration,
message metadata, and parsed detail together in one source transaction. If the
list ID is not yet in the database, register it in that transaction using the
existing discovered-list TSG inference and null-description rules, without
overwriting an existing list's metadata. A failed parse or failed source commit
must not leave a newly registered list or partial item, and a failed forced
reparse must retain the previous good detail. This is a new, explicit successful
single-item discovery path; list-wide sync's date-matching registration and
archive coverage rules remain unchanged. No archive-page coverage or pagination
state is written for an item-only parse.

After committing, update configured derived indexes using the existing
best-effort path and distinguish source failures from index failures in the job
summary. Index failure does not roll back committed source data. Keep worker
threading, cooperative cancellation, shared embedder ownership, and resource
shutdown consistent with existing LISTSERV jobs, including the shared worker
`_to_thread` path for blocking operations.

## Errors and compatibility

Reject invalid input at adapter boundaries with the surfaces' existing error
conventions and validate again at the service boundary. Report inaccessible,
login-only, malformed, or unparseable pages as failures without partial writes.
Preserve normal progress, queued-job, and index-repair reporting. Existing
snapshot reads stay SQL-only and never implicitly fetch or parse. The feature
does not add attachment downloads or a database schema migration.

## Verification and documentation

Add focused offline unit and SQLite integration coverage for rich filters on
both fields, their intersection, wildcards, negation, nulls, and unfiltered
compatibility across CLI, REST, MCP, and portal. Test panel ordering, independent
search filters, safe archive links, and SQL-only listing.

Test both URL spellings and equivalence to stored URLs; malformed and hostile
URLs; missing and invalid headers; an unsynced item on an unknown list; an
unsynced item on a known list; existing unparsed, parsed, and forced items;
atomic rollback and failed reparse preservation; date/week and TDoc metadata;
no fabricated archive coverage; no attachment download; source and index failure
separation; job cancellation and ownership; and CLI/REST/MCP/portal parity.
Mock transports in deterministic tests and exercise the real database and shared
serializers where appropriate.

Update maintained CLI, processes, web, MCP, and contributor documentation for
the new filters, item operation, discovery rule, and examples. Run relevant
targeted tests, the SQLite suite with xdist when available, Ruff, command-help
checks, and documentation/link checks; report any unavailable dependencies or
verification limitations accurately.
