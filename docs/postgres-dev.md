# Postgres dev service

The compose stacks ship a Postgres 16 service (`db`) on
`127.0.0.1:5433` (mapped to container `5432`) with database `holzi`
and superuser `holzi_owner` / `holzi_owner_dev_pw`. Data persists in
the named volume `holzi-pg`. Nothing in the app talks to it yet — this
is the foundation for the SQLite → Postgres + RLS port (§1 of the
SaaS plan); deps, DSN config, alembic, schema, and RLS land in later
tasks.

## Bring it up

```bash
docker compose -f docker-compose.local.yml up -d db
docker compose -f docker-compose.local.yml exec db pg_isready -U holzi_owner -d holzi
# expected: ... accepting connections
```

Connect from the host:

```bash
psql postgresql://holzi_owner:holzi_owner_dev_pw@127.0.0.1:5433/holzi
```

To reset the data volume:

```bash
docker compose -f docker-compose.local.yml down
docker volume rm holzi_holzi-pg  # or whatever `docker volume ls` shows
```
