# Migrations

Schema is bootstrapped via `Base.metadata.create_all` from
`doc3gpp.storage.db.migrate.create_schema`, invoked by `doc3gpp db init`
and (idempotently) by `meeting sync`, `wi sync`, and `tsg seed`. Alembic
is installed as a dependency but is not wired up — schema changes ship as
ORM model updates plus a one-time `doc3gpp db reset --yes` (SQLite) or a
backend-native migration (MySQL / PostgreSQL).
