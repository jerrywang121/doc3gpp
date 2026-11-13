# LISTSERV list catalog design

## Purpose and scope

Provide a persistent catalog of known LISTSERV list IDs, their optional 3GPP
group, and their optional description. Populate it with the reference data below
and with previously unknown lists discovered by successful sync. Keep existing
LISTSERV data and upgrade its relationships without a database reset. The CLI,
HTTP, and MCP list operations return catalog entries with their IDs, TSGs and
descriptions, including entries without stored messages. Existing HTTP/MCP
clients can continue to use their `list_ids` field.

This changes source metadata and initialization, not the LISTSERV transport,
date filtering, message parsing, indexing, attachment policy, or public-host
security model.

## Schema and bootstrap

Add the main database table `listserv_list`:

| Column | Constraint |
| --- | --- |
| `list_id` | Primary key, normalized uppercase LISTSERV ID |
| `tsg` | Nullable foreign key to `tsgs.short_name` |
| `description` | Nullable text |

Add foreign keys from `listserv_messages.list_id` and
`listserv_archive_pages.list_id` to `listserv_list.list_id`. Details retain
their existing FK to `listserv_messages.url`, so they are linked to the catalog
indirectly. FTS and vector tables are derived indexes, not authoritative list
tables; SQLite virtual tables cannot carry these relational FKs. Keep existing
indexes and relationships intact and synchronize `models/schema_info.py` with
the new ORM schema.

Add the historical TSG reference record with `tsg_name = RAN WG6`,
`short_name = R6`, description
`(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access (UTRA)`, and
`url = null`. The existing TSG seed composes missing
URLs, so it must explicitly preserve this null URL, including on repeated
explicit TSG seeding. Other TSG URLs retain their existing behavior.

Main schema initialization must insert missing required TSG references before
inserting catalog seeds without overwriting existing TSG metadata, including on
a fresh database reached through
`listserv list` or `listserv sync`, which currently call `create_schema` rather
than `db init`'s separate TSG seed. Repeated initialization is idempotent and
must not overwrite already stored catalog metadata. Populate the reference
catalog with the exact IDs, TSGs and descriptions in the seed data below;
`3GPP_TSG_RAN_WG6_DRAFTS` is deliberately not a seed. Its ID is not banned from
later discovery. The predefined associations are authoritative and are not
recomputed by the discovery heuristic.

## Existing databases

`doc3gpp db init --scope main` and the existing main-schema initialization
entry point upgrade old databases non-destructively. Insert reference seeds
first, then backfill IDs present in either the old message or archive-page
table but absent from the catalog, with null `tsg` and `description`. This
includes page-only IDs: preserving existing archive coverage requires a parent
row even if no message was ever stored. Such migration backfill is distinct
from discovery of a new ID during sync.

For SQLite, creating ORM tables does not add constraints to existing tables;
perform a controlled table rebuild when necessary. Preserve messages, details,
archive-page coverage and next URLs, existing indexes, and derived index data.
Account for the detail-to-message FK when rebuilding the message table. Use a
real database transaction for DDL and data movement, verify integrity before
commit, restore any temporarily changed connection settings, and leave the old
data usable if an upgrade fails. Avoid triggering `ON DELETE CASCADE` on stored
details. The upgrade must be safe to repeat and must not silently report
success if it could not establish the requested foreign keys. Document the
supported migration path and any backend limitations rather than claiming that
`create_all` altered old constraints.

## Sync and discovery

Normalize input IDs using the existing rules. A list absent from the catalog is
registered only after a successfully parsed archive page produces at least one
message matching the requested date filters. Insert the parent row, matched
message rows, page coverage, and pagination links as one atomic page commit. A
failed parse or failed commit must not create a list. A successful upstream
index alone or a page with no matching messages does not register it.

For an unknown list, retain successful zero-match page results temporarily so
their coverage and links can be committed after a later page registers the
list. Continue traversing discovered pagination links in the meantime. If the
sync ends or is cancelled without a match, discard the temporary coverage and
links; later syncs may fetch those pages again. Once a list exists, preserve the
current per-page atomicity, successful-empty-page coverage, retry, cancellation,
and best-effort derived-index semantics. Failed pages remain retryable. Report
the result consistently with actual committed pages; do not claim discarded
coverage was persisted. Do not retain successful empty-page buffers longer than
the sync or persist them outside the catalog FK.

For a newly discovered ID, infer `tsg` from the normalized ID:

- `3GPP_TSG_RAN_WG<digits>` and suffixes beginning at the next `_` map to
  `R<digits>`; SA maps to `S<digits>` and CT maps to `C<digits>`.
- A recognized family with no immediately following `WG<digits>` segment maps
  to its plenary: `RP`, `SP`, or `CP`. Recognize the family at the end of the
  ID or followed by `_`; do not treat unrelated prefixes as a family.
- IDs outside that pattern map to null. A generated code not present in
  `tsgs.short_name` also maps to null to preserve FK validity; do not substitute
  the plenary for an unknown WG.
- New descriptions are null. Inserting a discovered ID must not replace the
  TSG or description of an existing seed or other catalog row, including when
  concurrent sync attempts race.

## Read interfaces

The repository and service expose ordered catalog records containing
`list_id`, `tsg`, and `description`, without leaking ORM entities. Preserve
`list_ids()` as a compatibility method returning IDs from the catalog.

CLI `listserv list` renders the three fields as columns, with `-` for a null
value. The HTTP `/api/listserv/lists` and MCP `listserv_list` responses use a
shared serializer and return `{ "list_ids": [...], "lists": [...] }`, where
each `lists` item has `list_id`, `tsg`, and `description` keys and nullable
values are JSON null. Retain `list_ids` for existing consumers, and keep the web
search list selector functional. All reads are SQL-only and ordered by list ID.

## Error handling and verification

- Test every seed tuple, exact descriptions and associations, absence of the
  excluded seed, the R6 record including its null URL, and repeat initialization
  and explicit TSG seed behavior.
- Exercise actual SQLite FK enforcement on fresh and upgraded schemas, including
  orphan inserts and invalid TSG codes. Check schema reporting against the ORM.
- Upgrade a realistic legacy database containing message records, detail blobs,
  page-only IDs, coverage and next URLs, and search/index state. Assert all
  values and indexes survive, FKs are present, running the upgrade twice is
  safe, and injected migration failures leave original data intact.
- Test discovery with matching messages, date-filtered zero matches, empty
  pages before a match, no matches anywhere, failures and cancellation. Verify
  page transaction rollback leaves no newly registered ID. Test recognized WG,
  plenary, unknown WG and non-3GPP IDs, suffixes, normalization, preservation
  of existing mappings, and repeated/concurrent insert behavior.
- Test CLI/HTTP/MCP list fields, null handling, cross-surface consistency and
  retained `list_ids` compatibility for preseeded, migrated, and discovered
  entries, including lists without messages. Update
  maintained process, CLI, schema, and MCP/HTTP documentation as affected.

## Reference seed data

Columns are `list_id | tsg | description`. Store descriptions exactly as
shown, preserving their case, punctuation, and Unicode.

```text
3GPP_TSG_CT | CP | 3GPP_TSG_CT - Core Network and Terminals
3GPP_TSG_CT_IETF-COORD | CP | 3GPP_TSG_CT_IETF-Coord
3GPP_TSG_CT_WG1 | C1 | 3GPP_TSG_CT_WG1 - Core Network and Terminals WG 1
3GPP_TSG_CT_WG1_122E_5G | C1 | 3GPP_TSG_CT_WG1_122e_5G Closed 15/06/2020 done by TGV by request #2020-102749
3GPP_TSG_CT_WG1_E_IMS | C1 | 3GPP_TSG_CT_WG1_E_IMS
3GPP_TSG_CT_WG1_E_MAIN | C1 | 3GPP_TSG_CT_WG1_E_main
3GPP_TSG_CT_WG1_E_MC | C1 | 3GPP_TSG_CT_WG1_E_MC
3GPP_TSG_CT_WG1_E_SERVICES | C1 | 3GPP_TSG_CT_WG1_E_services
3GPP_TSG_CT_WG3 | C3 | 3GPP_TSG_CT_WG3 - Core Network and Terminals WG 3
3GPP_TSG_CT_WG3_108E_MAIN | C3 | 3GPP_TSG_CT_WG3_108e_main Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066
3GPP_TSG_CT_WG3_108E_NON_PCC | C3 | 3GPP_TSG_CT_WG3_108e_Non_PCC Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066
3GPP_TSG_CT_WG3_108E_PCC | C3 | 3GPP_TSG_CT_WG3_108e_PCC Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066
3GPP_TSG_CT_WG3_MAIN | C3 | 3GPP_TSG_CT_WG3_main
3GPP_TSG_CT_WG3_NON_PCC | C3 | 3GPP_TSG_CT_WG3_Non_PCC
3GPP_TSG_CT_WG3_PCC | C3 | 3GPP_TSG_CT_WG3_PCC
3GPP_TSG_CT_WG4 | C4 | 3GPP_TSG_CT_WG4 - Core Network and Terminals WG 4
3GPP_TSG_CT_WG4_E_MEETING | C4 | 3GPP_TSG_CT_WG4_E_Meeting - Core Network and Terminals WG 4 E_Meeting
3GPP_TSG_CT_WG6 | C6 | 3GPP_TSG_CT_WG6 - Core Network and Terminals WG 6
3GPP_TSG_RAN | RP | 3gpp_tsg_ran: tsg radio access network group
3GPP_TSG_RAN_ADHOC | R2 | 3gpp_tsg_ran_adhoc: tsg ran ad hoc meetings
3GPP_TSG_RAN_AHG1 | RT | 3gpp_tsg_ran_ahg1: tsg ran ad-hoc group on itu co-ordination
3GPP_TSG_RAN_DRAFTS | RP | 3GPP_TSG_RAN_DRAFTS
3GPP_TSG_RAN_WG1 | R1 | 3gpp_tsg_ran_wg1: tsg ran working group 1
3GPP_TSG_RAN_WG1_CHANNELMODEL | R1 | 3GPP_TSG_RAN_WG1_ChannelModel
3GPP_TSG_RAN_WG1_EUL_AH | R1 | 3GPP_TSG_RAN_WG1_EUL_AH
3GPP_TSG_RAN_WG1_HSPA | R1 | 3GPP_TSG_RAN_WG1_HSPA
3GPP_TSG_RAN_WG1_LTE | R1 | 3GPP_TSG_RAN_WG1_LTE
3GPP_TSG_RAN_WG1_NR | R1 | 3GPP_TSG_RAN_WG1_NR
3GPP_TSG_RAN_WG1_WCDMA_MIMO | R1 | 3GPP_TSG_RAN_WG1_WCDMA_MIMO
3GPP_TSG_RAN_WG2 | R2 | 3GPP_TSG_RAN_WG2 list
3GPP_TSG_RAN_WG2_MBMS_RRC | R2 | 3GPP_TSG_RAN_WG2_MBMS_RRC List list closed 26th August 2009
3GPP_TSG_RAN_WG2_ROHC | R2 | 3GPP_TSG_RAN_WG2_ROHC : 3GPP_TSG_RAN_WG2_ROHC List Closed 26th August 2009
3GPP_TSG_RAN_WG2_UMTS_DRAFTS | R2 | Closed 05/03/2025 Done by PKA by request of Joern K ticket 2025-101007
3GPP_TSG_RAN_WG3 | R3 | 3gpp_tsg_ran_wg3: tsg ran working group 3
3GPP_TSG_RAN_WG4 | R4 | 3gpp_tsg_ran_wg4: tsg ran working group 4
3GPP_TSG_RAN_WG4_AAS | R4 | 3GPP_TSG_RAN_WG4_AAS
3GPP_TSG_RAN_WG4_BANDS | R4 | 3GPP_TSG_RAN_WG4_Bands : New UMTS frequency bands list closed
3GPP_TSG_RAN_WG4_CA | R4 | 3GPP_TSG_RAN_WG4_CA
3GPP_TSG_RAN_WG4_DIVERSITY_OFF | R4 | 3gpp_tsg_ran_wg4_DIVERSITY_OFF: tsg ran working group 4 Diversity off list closed
3GPP_TSG_RAN_WG4_DRAFTS | R4 | 3GPP_TSG_RAN_WG4_DRAFTS
3GPP_TSG_RAN_WG4_EMEETING | R4 | 3GPP_TSG_RAN_WG4_eMeeting
3GPP_TSG_RAN_WG4_HSDPA | R4 | 3GPP_TSG_RAN_WG4_HSDPA : HSDPA PERFORMANCE REQUIREMENTS
3GPP_TSG_RAN_WG4_IOT | R4 | 3GPP_TSG_RAN_WG4_IoT
3GPP_TSG_RAN_WG4_LTE | R4 | 3GPP TSG RAN WG4 Long Term Evolution
3GPP_TSG_RAN_WG4_MIMO-OTA | R4 | 3GPP_TSG_RAN_WG4_MIMO-OTA
3GPP_TSG_RAN_WG4_NR_BANDS | R4 | 3GPP_TSG_RAN_WG4_NR_band_and_combo
3GPP_TSG_RAN_WG4_NR-MIMO-OTA | R4 | 3GPP_TSG_RAN_WG4_NR-MIMO-OTA
3GPP_TSG_RAN_WG4_TRP-TRS-OTA | R4 | 3GPP_TSG_RAN_WG4_TRP-TRS-OTA
3GPP_TSG_RAN_WG5 | R5 | 3GPP_TSG_RAN_WG5 : Testing
3GPP_TSG_RAN_WG5_5G_NR | R5 | 3GPP_TSG_RAN_WG5_5G_NR
3GPP_TSG_RAN_WG5_6G_SPEC_MODERN | R5 | 3GPP_TSG_RAN_WG5_6G_Spec_Modern
3GPP_TSG_RAN_WG5_AIML | R5 | 3GPP_TSG_RAN_WG5_AIML
3GPP_TSG_RAN_WG5_ANALYTICS | R5 | 3GPP_TSG_RAN_WG5_ANALYTICS
3GPP_TSG_RAN_WG5_APPLICABILITY | R5 | 3GPP_TSG_RAN_WG5_APPLICABILITY
3GPP_TSG_RAN_WG5_FR2_MU | R5 | 3GPP_TSG_RAN_WG5_FR2_MU
3GPP_TSG_RAN_WG5_IMSCC | R5 | 3GPP TSG RAN WG5 IMS Call Control mailing
3GPP_TSG_RAN_WG5_IOT | R5 | 3GPP_TSG_RAN_WG5_IOT
3GPP_TSG_RAN_WG5_LTE-CDMA2000 | R5 | 3GPP_TSG_RAN_WG5_LTE-CDMA2000
3GPP_TSG_RAN_WG5_MCX | R5 | 3GPP_TSG_RAN_WG5_MCX
3GPP_TSG_RAN_WG5_RF | R5 | 3GPP_TSG_RAN_WG5_RF : Radio Frequency
3GPP_TSG_RAN_WG5_SIG | R5 | 3GPP_TSG_RAN_WG5_SIG: Signalling
3GPP_TSG_RAN_WG5_TRP-TRS-OTA | R5 | 3GPP_TSG_RAN_WG5_TRP-TRS-OTA
3GPP_TSG_RAN_WG6 | R6 | 3GPP_TSG_RAN_WG6 Close 14/08/2020 JMS ticket 2020-103474
3GPP_TSG_SA | SP | 3gpp_tsg_sa: tsg system aspects group
3GPP_TSG_SA_DRAFTS | S2 | 3GPP_TSG_SA_DRAFTS
3GPP_TSG_SA_ITUT_AHG | SP | 3gpp_tsg_sa_ITUT_AHG: TSG SA ad-hoc group on ITU-T co-ordination
3GPP_TSG_SA_WG1 | S1 | 3gpp_tsg_sa_wg1: tsg sa services
3GPP_TSG_SA_WG2 | S2 | 3gpp_tsg_sa_wg2: tsg sa architecture
3GPP_TSG_SA_WG2_DISCUSSIONS | S2 | 3GPP_TSG_SA_WG2_DISCUSSIONS
3GPP_TSG_SA_WG2_ELCS | S2 | 3GPP_TSG_SA_WG2_eLCS
3GPP_TSG_SA_WG2_EMEET | S2 | 3GPP_TSG_SA_WG2_EMEET
3GPP_TSG_SA_WG2_MTCE | S2 | 3GPP_TSG_SA_WG2_MTCe
3GPP_TSG_SA_WG2_PROSE_GCSE_LTE | S2 | 3GPP_TSG_SA_WG2_ProSe_GCSE_LTE
3GPP_TSG_SA_WG2_UPCON | S2 | 3GPP_TSG_SA_WG2_UPCON
3GPP_TSG_SA_WG3 | S3 | 3gpp_tsg_sa_wg3: tsg sa security
3GPP_TSG_SA_WG3_LI | S3 | 3gpp_tsg_sa_wg3_li: (tsg sa wg3 lawful interception group)
3GPP_TSG_SA_WG4 | S4 | 3gpp_tsg_sa_wg4: tsg sa codec
3GPP_TSG_SA_WG4_AUDIO | S4 | 3GPP_TSG_SA_WG4_AUDIO
3GPP_TSG_SA_WG4_EVS-IVAS | S4 | 3GPP_TSG_SA_WG4_EVS-IVAS
3GPP_TSG_SA_WG4_MBS | S4 | 3GPP_TSG_SA_WG4_MBS
3GPP_TSG_SA_WG4_MTSI | S4 | 3GPP_TSG_SA_WG4_MTSI Closed 31/03/2-#done by YLC-By Request of Jayeeta Saha-ticket n°2022-101512
3GPP_TSG_SA_WG4_RTC | S4 | 3GPP_TSG_SA_WG4_RTC
3GPP_TSG_SA_WG4_SQ | S4 | 3GPP_TSG_SA_WG4_SQ-Closed 31/03/22-#done by YLC-by request of Jayeeta Saha-ticket n°2022-101512
3GPP_TSG_SA_WG4_VIDEO | S4 | 3GPP_TSG_SA_WG4_VIDEO
3GPP_TSG_SA_WG5 | S5 | 3gpp_tsg_sa_wg 5: tsg sa5 telecoms management
3GPP_TSG_SA_WG5_CHARGING | S5 | 3GPP_TSG_SA_WG5_Charging: Charging Management
3GPP_TSG_SA_WG5_OAM | S5 | 3GPP_TSG_SA_WG5_OAM : OAM
3GPP_TSG_SA_WG5_SWGA | S5 | 3GPP_TSG_SA_WG5_SWGA : Closed 13/12/2019 done by Guillaume T by request Ticket n°2019-103870
3GPP_TSG_SA_WG5_SWGB | S5 | 3GPP_TSG_SA_WG5_SWGB : Charging CLOSED 270407
3GPP_TSG_SA_WG5_SWGC | S5 | 3GPP_TSG_SA_WG5_SWGC : OAM&P Interface Definition Closed 270407
3GPP_TSG_SA_WG5_SWGD | S5 | 3GPP_TSG_SA_WG5_SWGD : OAM&P Data Definition CLOSED 270407
3GPP_TSG_SA_WG6 | S6 | 3gpp_tsg_sa_wg6: mission-critical applications
3GPP_TSG_SA1_EMEET | S1 | 3GPP_TSG_SA1_eMeet
3GPP_TSG_SA4_FS_6G_MED | S4 | 3GPP_TSG_SA4_FS_6G_MED
3GPP_TSG_SA4_FS_MED | S4 | Closed 21/09/2026 #done by PKA by request of Andrijana Brekalo ticket n°2026-104353
3GPP_TSG_SA4_IVAS_BASOP | S4 | 3GPP_TSG_SA4_IVAS_BASOP
3GPP_TSG_SA4_IVAS_PUBLIC_CLBRTN | S4 | 3gpp_tsg_sa_wg4_IVAS_Public_Collaboration@list.etsi.org
```
