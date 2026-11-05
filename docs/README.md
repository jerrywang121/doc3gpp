# Documentation

These guides describe the current implementation, including changes on the
working branch. Each topic has one primary home; other guides link to it rather
than repeat its full reference.

## Using doc3gpp

- [Project overview and quick start](../README.md)
- [CLI reference](cli.md): command syntax, selectors, filters, and output.
- [Configuration](config.md): settings discovery, precedence, defaults,
  dependencies, and secret handling.
- [Processes](processes.md): what sync, parse, cache, chunk, index, query, and
  background jobs actually do; skip rules and recovery.
- [Web server](web-server.md): operation, web pages, HTTP routes, and monitoring.
- [MCP](mcp.md): connect an agent, discover tools, read content, and enqueue jobs.
- [3GPP knowledge](3gpp-knowledge.md): upstream conventions and domain semantics.
- [Known constraints](known-constraints.md): limits and operational caveats.

## Python SDK

Install `doc3gpp` without extras for metadata services. Configure the database
as described in [configuration](config.md), then compose a service through its
factory:

```python
from doc3gpp.services.factory import build_meeting_service
from doc3gpp.storage.db.migrate import create_schema

create_schema("main")  # additive bootstrap of the configured main database
service = build_meeting_service()
for meeting in service.list_recent(limit=10):
    print(meeting.meeting_id, meeting.name, meeting.end_date)
```

This reads stored rows; it does not fetch a calendar or run CLI auto-sync.
Sync and extraction are explicit service operations. Services accept repository
interfaces for custom composition and tests; see [architecture](architecture.md)
and the [code map](code-map.md). Detail/content DTOs retain the same ZIP inventory
and source-identity semantics as the CLI, HTTP, and MCP payloads.

## Developing doc3gpp

- [Architecture](architecture.md): boundaries and storage ownership.
- [Code map](code-map.md): symbols and modules to change.
- [Conventions](conventions.md): testing, filtering, commits, and doc maintenance.
- [Agent guide](../AGENTS.md): compact contributor entry point.
- [TODO](TODO.md): engineering work, not a feature or command reference.
- [Packaged configuration example](../src/doc3gpp/data/doc3gpp.toml.example):
  editable template, interpreted by the current settings schema.
- [Packaged agent skill](../src/doc3gpp/data/skills/doc3gpp/SKILL.md): task-oriented
  agent guidance, also returned by MCP `get_skill`.

## Historical design records

Files under [`superpowers/specs/`](superpowers/specs/) and
[`superpowers/plans/`](superpowers/plans/) record designs and implementation
plans at particular points in time. They can describe superseded behavior or
work that was never shipped. Use the guides above and current source for the
supported interface; consult historical records for rationale, not defaults.
