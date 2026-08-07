# Calderilla web prototype

Minimal vertical prototype: one personal ledger, multiple accounts, tagged income/expense transactions, charts and Android-compatible CSV import/export, using FastAPI, PostgreSQL and a build-free frontend.

## Run

1. Optionally copy `.env.example` to `.env` and change the password.
2. Run `docker compose up --build`.
3. Open <http://localhost:8000> (API documentation: <http://localhost:8000/docs>).

Inspect PostgreSQL at <http://localhost:8080> with Adminer:

- System: `PostgreSQL`
- Server: `db`
- Username: `calderilla`
- Password: the `POSTGRES_PASSWORD` value (`calderilla` by default)
- Database: `calderilla`

Data is kept in the `postgres-data` Docker volume. This prototype creates its tables at startup; introduce Alembic before evolving the schema or keeping important data.
