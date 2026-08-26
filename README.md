## Wooloo


Wooloo is an OCI-compatible container registry for storing, managing, and distributing container images and artifacts.

Its goal is to provide a secure, reliable, and extensible foundation for container image distribution, enabling organizations to manage repositories, artifacts, manifests, and image lifecycles through standard OCI workflows.

Wooloo is designed to support modern cloud-native environments, automation pipelines, and container orchestration platforms while remaining compliant with open standards.

## Prerequisites

- **Python 3.14+** — the version is pinned in `.python-version`.
- **[uv](https://docs.astral.sh/uv/)** — used for dependency management and for running every
  command in this README.
- **Docker** (with Compose v2) — used to run the local PostgreSQL instance.

### Getting Started

#### Running with Make

##### 1. Install Dependencies

```bash
uv sync
```

##### 2. Configure Environment Variables

Copy the example file:

```bash
cp .env.example .env
```

Review the values and adjust them if necessary.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | Async SQLAlchemy DSN for PostgreSQL, e.g. `postgresql+asyncpg://wooloo:wooloo@localhost:5432/wooloo`. |
| `LOG_LEVEL` | No | `INFO` | Verbosity of the application's own logs: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`, accepted in any case. Third-party libraries stay floored at `INFO` so raising this does not fan `DEBUG` out across every installed dependency. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | `http://localhost:4317` | OTLP gRPC endpoint trace spans are exported to — by default the Jaeger service in `docker-compose.yml`. Must start with `http://` or `https://`; the scheme is validated at startup because it is what decides whether the exporter opens a TLS or a plaintext channel, and a scheme-less value drops every span silently. |
| `OTEL_CONSOLE_EXPORT_ENABLED` | No | `true` | Whether spans are *also* printed to stdout. Set to `false` wherever stdout is shipped to a log store — spans still reach Jaeger over OTLP, they just stop inflating the log stream. |
| `STORAGE_BACKEND` | No | `filesystem` | Which blob storage backend to build: `filesystem`, or `s3`, accepted in any case. Only `filesystem` is implemented; the other three are recognised names that fail when the storage factory builds them, not at settings validation. An unrecognised name fails at startup. |
| `STORAGE_ROOT` | No | `/var/lib/wooloo` | Directory the filesystem backend stores blobs under. Ignored by the other backends. Deliberately not under `/tmp`: that is cleared on reboot on most systems and is world-writable, so any local unprivileged user could pre-create the tree — or plant a symlink at a digest path, which is predictable from public content. Not validated at startup; whether the directory is usable is reported by the storage health check instead. |
| `MAX_UPLOAD_BYTES` | No | `5368709120` | Largest request body accepted on any endpoint, in bytes — 5 GiB by default, a plausible ceiling for a single OCI layer. Must be positive; there is no "unlimited" setting, because stored blobs are never reclaimed and an oversized upload that succeeds costs that disk permanently rather than transiently. |

All seven are read from the process environment first, falling back to `.env`. An absent
`DATABASE_URL`, an unrecognised `LOG_LEVEL` or `STORAGE_BACKEND`, a non-positive
`MAX_UPLOAD_BYTES`, or a scheme-less `OTEL_EXPORTER_OTLP_ENDPOINT` fails the
application at startup rather than at first use. An OTLP endpoint that is well-formed
but unreachable does *not* — tracing degrades to dropped spans rather than taking the
process down. Neither does a `STORAGE_ROOT` that does not exist yet — see
[Storage](#storage).

##### 3. Start the Backing Services

```bash
docker compose up -d
```

This starts two containers: `wooloo-postgres` (PostgreSQL 17) and `wooloo-jaeger`
(Jaeger all-in-one, receiving traces on OTLP and serving a trace UI). Both are bound
to the loopback interface only.

Verify both are healthy:

```bash
docker compose ps
```

```text
NAME              SERVICE    STATUS
wooloo-jaeger     jaeger     Up (healthy)
wooloo-postgres   postgres   Up (healthy)
```

The Jaeger UI is then available at:

```text
http://localhost:16686
```

See [Distributed Tracing](#distributed-tracing) for what lands in it.

##### 4. Run Database Migrations

```bash
uv run alembic upgrade head
```

Check the current revision:

```bash
uv run alembic current
```

##### 5. Start the API

```bash
uv run uvicorn wooloo.main:asgi_app --reload
```

Serve `asgi_app`, not `app`. The served object is the FastAPI application wrapped in
the request-logging middleware, and serving `app` directly silently drops request
correlation from every response — see [Request Correlation](#request-correlation).

The API will be available at:

```text
http://127.0.0.1:8000
```

Interactive API documentation:

```text
http://127.0.0.1:8000/docs
```

## Health Checks

```http
GET /api/v1/healthz
```

A single endpoint serves as both liveness and readiness probe — reaching the handler
at all proves the process is alive, and the database verdict rides in the body rather
than the status code. The response is always HTTP 200; a probe must read the `status`
field, not rely on the status code alone.

Example response, database reachable:

```json
{
  "status": "ok",
  "database": "up"
}
```

When the database is unreachable:

```json
{
  "status": "degraded",
  "database": "down"
}
```

## Running Tests

Run the complete test suite:

```bash
uv run pytest
```

Run a specific suite:

```bash
uv run pytest tests/unit
uv run pytest tests/integration
```

## Database Migrations

Create a new migration:

```bash
uv run alembic revision -m "migration_name"
```

Apply pending migrations:

```bash
uv run alembic upgrade head
```

Rollback the most recent migration:

```bash
uv run alembic downgrade -1
```

### License
Copyright © 2026
