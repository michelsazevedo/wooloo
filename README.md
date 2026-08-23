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

##### 3. Start PostgreSQL

```bash
docker compose up -d
```

Verify the database is healthy:

```bash
docker compose ps
```

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
uv run uvicorn wooloo.main:app --reload
```

The API will be available at:

```text
http://127.0.0.1:8000
```

Interactive API documentation:

```text
http://127.0.0.1:8000/docs
```

## Health Checks

### Liveness

```http
GET /healthz
```

Example response:

```json
{
  "status": "ok"
}
```

### Readiness

```http
GET /healthz
```

Example response:

```json
{
  "status": "ok",
  "database": "up"
}
```

When dependencies are unavailable:

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
