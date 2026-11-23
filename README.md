# Personal Web Research Agent (lite)

A self-hosted, multi-user web research assistant. You ask a question, an LLM plans a set of search queries, a tool-using "researcher" agent searches the web and reads pages, and a "synthesizer" writes a cited report. You can then ask follow-up questions about the finished report, export it as Markdown, or promote a follow-up into a new research run.

It is **bring-your-own-key**: every user saves their own LLM provider, model choices and API key (encrypted at rest), so the server never needs a shared LLM key.

- **Backend:** FastAPI + SQLAlchemy 2 (async) + PostgreSQL (asyncpg) + Alembic
- **Frontend:** static single-page app in `docs/` (vanilla HTML/JS/CSS, hash routing, deployable on GitHub Pages)
- **Python:** 3.13+, managed with [uv](https://docs.astral.sh/uv/)

---

## Table of contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Project structure](#project-structure)
4. [Getting started](#getting-started)
5. [Configuration](#configuration)
6. [Database & migrations](#database--migrations)
7. [Running the server](#running-the-server)
8. [API reference](#api-reference)
9. [Typical API workflow](#typical-api-workflow)
10. [LLM providers](#llm-providers)
11. [Job lifecycle](#job-lifecycle)
12. [Security design](#security-design)
13. [Frontend & deployment](#frontend--deployment)
14. [Design decisions & limitations](#design-decisions--limitations)
15. [Troubleshooting](#troubleshooting)

---

## Features

- **Plan → review → research → synthesize pipeline.** The planner proposes up to 5 search queries and a short title; you can edit the queries before the run starts (or use `auto_start` to skip review).
- **Tool-using researcher.** Searches the web (DuckDuckGo via `ddgs`) and fetches/parses pages (`httpx` + BeautifulSoup) for up to 6 reasoning iterations, running tool calls in parallel.
- **Numbered, inline citations.** Only pages that were actually read become sources; the report cites them as `[1]`, `[2][3]`, and the sources list is returned separately.
- **Background jobs with live progress.** A `stage` field (e.g. `researching (step 2/6)`, `reading sources`, `writing report`) is updated while a job runs.
- **Cooperative cancellation** of queued or running jobs.
- **Follow-up Q&A** on completed reports, answered only from the stored report and sources (one LLM call, no new search). If the report can't answer, the response includes a `suggested_query` so the UI can offer "Research this further".
- **Research threads.** Follow-ups can be promoted into child jobs via `parent_job_id`.
- **Job management.** Cursor-paginated listing with status filter and text search, rename, delete, and Markdown export.
- **Multi-provider LLM support.** Groq, OpenAI, Gemini and Anthropic, with a different model per stage (planner / researcher / synthesizer).
- **Full account system.** Registration, email verification (Brevo), login (JWT), resend verification, forgot/reset password.
- **Connection test.** `POST /settings/llm/test` sends a tiny prompt per stage so a bad key or model name is caught before a research run.

---

## How it works

```
                    ┌──────────────┐
  question ───────► │   Planner    │──► title + up to 5 search queries
                    └──────────────┘            │
                                      (user reviews / edits)
                                                ▼
                    ┌──────────────────────────────────────────┐
                    │  Researcher  (tool loop, ≤ 6 iterations)  │
                    │   • web_search (DuckDuckGo)               │
                    │   • fetch_url_content (HTML → text)       │
                    │   tool calls in a turn run in parallel    │
                    └──────────────────────────────────────────┘
                                                │ messages + numbered sources
                                                ▼
                    ┌──────────────┐
                    │ Synthesizer  │──► Markdown report with [n] citations
                    └──────────────┘
```

1. **`plan()`** makes one LLM call and parses a `{"title", "queries"}` JSON object (it also tolerates a bare JSON array, and falls back to the original question if parsing fails).
2. **`research()`** loops: the researcher model decides which tools to call; results are appended to the conversation. It stops when the model stops calling tools or the iteration cap is reached. Each page it fetches successfully is registered as a numbered `Source`.
3. **`synthesize()`** flattens the whole tool conversation into plain text, appends the numbered source list and citation rules, and asks the synthesizer model for the final report.

The pipeline is **synchronous/blocking** (it uses `httpx.Client` and `time.sleep` for retries). The service layer runs it in a worker thread via `asyncio.to_thread`, so the FastAPI event loop stays responsive.

### Request architecture

```
Routers (app/routers)           HTTP only: validation, status codes, response models
   │
Services (services/)            Business rules, no FastAPI imports
   │        ├─ auth_service            register / verify / login / reset
   │        ├─ llm_settings_service    per-user provider, models, encrypted key
   │        ├─ research_service        job lifecycle, background tasks, follow-ups
   │        ├─ email_service           Brevo transactional email
   │        └─ security                argon2, JWT, one-time tokens, Fernet
   │
Repositories (database/*_repo.py)  SQL only
   │
Models / session (database/)       SQLAlchemy models, async engine
   │
Agents (agents/)                 LLM client, pipeline, prompts, tools (no DB, no HTTP framework)
```

---

## Project structure

```
.
├── .github/workflows/migrations.yml   # CI for database migrations
├── alembic.ini
├── migrations/                        # Alembic environment + versions
│   └── versions/
│       ├── 4667248c39b0_creating_initial_tables.py
│       └── 649691d05224_research_message_table_added.py
├── docs/                              # Static frontend (index.html, app.js, styles.css)
├── pyproject.toml
├── uv.lock
├── .env.example
└── src/personal_web_research_agent_lite/
    ├── agents/
    │   ├── llm.py                     # Multi-provider chat client (OpenAI-compatible endpoints)
    │   ├── pipeline.py                # plan -> research -> synthesize
    │   ├── state.py                   # Plan, Source, Report dataclasses
    │   ├── prompts/                   # planner.py, researcher.py, synthesizer.py
    │   └── tools/                     # web_search_DDG.py, fetch_url.py
    ├── app/
    │   ├── server.py                  # FastAPI app, lifespan, CORS
    │   ├── deps.py                    # get_current_user, get_http_client
    │   └── routers/                   # auth.py, llm_settings.py, research.py
    ├── config/
    │   ├── settings.py                # pydantic-settings (PWRAL_ env vars)
    │   └── nepal_time.py              # current datetime injected into the planner prompt
    ├── database/
    │   ├── base.py                    # Declarative base + constraint naming convention
    │   ├── models.py                  # ORM models
    │   ├── session.py                 # Async engine + session factory
    │   ├── auth_repo.py               # users, tokens, LLM settings queries
    │   ├── job_repo.py                # research job queries and state transitions
    │   └── message_repo.py            # follow-up message queries
    └── services/
        ├── auth_service.py
        ├── email_service.py
        ├── llm_settings_service.py
        ├── research_service.py
        └── security.py
```

---

## Getting started

### Prerequisites

- Python **3.13+**
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- PostgreSQL **13+** (the schema uses `gen_random_uuid()`, built in from PG 13)
- An API key for at least one supported LLM provider (each user supplies their own through the app)
- *(Optional)* a [Brevo](https://www.brevo.com/) account for sending verification and reset emails

### Install

```bash
git clone <your-repo-url>
cd personal-web-research-agent-lite
uv sync
```

### Configure

```bash
cp .env.example .env
```

Generate the two secrets:

```bash
# JWT signing secret
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Fernet key used to encrypt users' LLM API keys
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Fill in `.env` (see [Configuration](#configuration)), then create the database and run migrations:

```bash
createdb research_agent                   # or create it however you prefer
uv run alembic upgrade head
```

Start the API:

```bash
uv run personal-web-research-agent-lite
# or
uv run fastapi dev        # uses [tool.fastapi] entrypoint, with auto-reload
```

Open the interactive docs at **http://localhost:8000/docs**.

---

## Configuration

All settings are read from environment variables prefixed with `PWRAL_` (or from a `.env` file). Unknown variables are ignored.

| Variable | Required | Default | Description |
|---|---|---|---|
| `PWRAL_DATABASE_URL` | yes | – | PostgreSQL URL. `postgresql://` and `postgres://` are automatically rewritten to `postgresql+asyncpg://`. |
| `PWRAL_JWT_SECRET_KEY` | yes | – | Secret used to sign access tokens. |
| `PWRAL_JWT_ALGORITHM` | no | `HS256` | JWT signing algorithm. |
| `PWRAL_ACCESS_TOKEN_EXPIRE_MINUTES` | no | `60` | Access-token lifetime. |
| `PWRAL_API_KEY_ENCRYPTION_KEY` | yes | – | Fernet key (URL-safe base64, 32 bytes) for encrypting users' LLM API keys. **Changing it makes all stored keys unreadable.** |
| `PWRAL_BREVO_API_KEY` | yes* | – | Brevo API key. See the note below. |
| `PWRAL_BREVO_SENDER_EMAIL` | no | `""` | Sender address; must be verified in Brevo. |
| `PWRAL_BREVO_SENDER_NAME` | no | `Personal Web Research Agent` | Sender display name. |
| `PWRAL_APP_BASE_URL` | yes | – | Public URL of this backend. |
| `PWRAL_FRONTEND_BASE_URL` | no | `""` | Origin of the frontend. Enables CORS for that origin and makes emails contain clickable links. |
| `PWRAL_FRONTEND_PATH` | no | `""` | Sub-path of the frontend (e.g. `repo-name` for GitHub Pages project sites). |
| `PWRAL_VERIFICATION_TOKEN_TTL_HOURS` | no | `24` | Email-verification link lifetime. |
| `PWRAL_VERIFICATION_RESEND_COOLDOWN_SECONDS` | no | `600` | Minimum gap between verification emails. |
| `PWRAL_PASSWORD_RESET_TOKEN_TTL_MINUTES` | no | `60` | Password-reset link lifetime. |

\* `brevo_api_key` has no default, so the variable must **exist**. For local development set it to an empty value (`PWRAL_BREVO_API_KEY=`). With an empty key, no email is sent and the verification/reset token (and link, if `FRONTEND_BASE_URL` is set) is written to the server log instead.

### Example `.env` for local development

```dotenv
PWRAL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/research_agent
PWRAL_JWT_SECRET_KEY=<generated secret>
PWRAL_API_KEY_ENCRYPTION_KEY=<generated Fernet key>

# Dev mode: tokens are printed to the server log instead of emailed
PWRAL_BREVO_API_KEY=

PWRAL_APP_BASE_URL=http://localhost:8000
PWRAL_FRONTEND_BASE_URL=http://localhost:5500
```

> **Never commit `.env`.** It contains secrets that protect every user's stored API key.

---

## Database & migrations

Schema changes are managed with Alembic (`alembic.ini`, `migrations/`). Constraint names follow an explicit naming convention (`base.py`), so autogenerate and downgrades stay clean.

```bash
uv run alembic upgrade head                              # apply all migrations
uv run alembic revision --autogenerate -m "describe it"  # create a new migration
uv run alembic downgrade -1                              # roll back one step
```

### Tables

| Table | Purpose |
|---|---|
| `users` | Accounts. Case-insensitive unique email (unique index on `lower(email)`), argon2 password hash, `role` (`user`/`admin`), `is_verified`, `is_active`, `last_login_at`. |
| `email_verification_tokens` | One-time verification tokens. Only the SHA-256 hash is stored. |
| `password_reset_tokens` | One-time reset tokens, same design. |
| `user_llm_settings` | One row per user: provider, planner/researcher/synthesizer model, Fernet-encrypted API key, and the last 4 characters for display. |
| `research_jobs` | One research run = one thread. Holds title, query, planned `queries` (JSONB), `status`, `stage`, `report`, `sources` (JSONB), `iterations`, `error`/`error_code`, timestamps and `parent_job_id`. |
| `research_messages` | Follow-up Q&A on completed jobs, with an optional `suggested_query` on assistant messages. |

Design notes:

- UUID primary keys generated by PostgreSQL.
- Enum-like columns (`role`, `provider`, `status`) are `VARCHAR` plus `CHECK` constraints, not native PG enums, so adding a value later is a cheap constraint change rather than an `ALTER TYPE` migration.
- Deleting a user cascades to their settings, jobs, messages and tokens.

> To promote a user to admin: `UPDATE users SET role = 'admin' WHERE email = '...';`

---

## Running the server

| Command | Use |
|---|---|
| `uv run fastapi dev` | Development, with auto-reload. |
| `uv run personal-web-research-agent-lite` | Runs uvicorn on `0.0.0.0:8000` with `proxy_headers=True` (no reload). |

### Lifespan behaviour

On **startup** the app creates a shared `httpx.AsyncClient` (used for Brevo emails) and marks any jobs left `queued`/`running` by a previous crash or restart as `failed` with the message *"Interrupted by a server restart. Start it again."*

On **shutdown** it cancels running background jobs, closes the HTTP client and disposes of the DB engine.

### CORS

If `PWRAL_FRONTEND_BASE_URL` is set, CORS is enabled for that origin only (scheme + host), allowing the `Authorization` and `Content-Type` headers.

---

## API reference

Interactive docs: `/docs` (Swagger UI) and `/redoc`. All endpoints marked 🔒 need `Authorization: Bearer <access_token>`.

### Auth

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/register` | Create an account (`email`, `password`) and send a verification email. |
| `POST` | `/auth/verify-email` | Verify with `{ "token": "..." }`. Idempotent for double submits. |
| `POST` | `/auth/resend-verification` | Resend the verification email (cooldown applies). Always returns a generic response. |
| `POST` | `/auth/login` | Returns `{ access_token, token_type, expires_in }`. |
| `POST` | `/auth/forgot-password` | Request a reset email. Always returns a generic response (no account enumeration). |
| `POST` | `/auth/reset-password` | `{ token, new_password }`. |
| `GET` 🔒 | `/auth/me` | Current user, including `is_verified` and `has_llm_settings`. |

### LLM settings

| Method | Path | Description |
|---|---|---|
| `GET` 🔒 | `/settings/llm/providers` | Supported providers with suggested model names (for dropdowns; not enforced). |
| `GET` 🔒 | `/settings/llm` | Current settings. The key is never returned, only `api_key_hint` (last 4 characters). |
| `PUT` 🔒 | `/settings/llm` | Create or update settings. Omit `api_key` to keep the stored key (a key is required on first setup or when switching provider). |
| `DELETE` 🔒 | `/settings/llm` | Remove saved settings and key. |
| `POST` 🔒 | `/settings/llm/test` | Sends a tiny prompt per stage and reports `ok`/`error` for each, so bad keys or model names show up immediately. |

### Research

| Method | Path | Description |
|---|---|---|
| `POST` 🔒 | `/research` | Plan queries and a title (one LLM call). Body: `query`, optional `auto_start`, optional `parent_job_id`. Returns `201` with the job. |
| `GET` 🔒 | `/research` | List jobs, newest first. Query params: `limit`, `cursor`, `status`, `q`. Returns summaries only; response has `items` and `next_cursor`. |
| `POST` 🔒 | `/research/{job_id}/start` | Queue the job. Optional body `{ "queries": [...] }` to override the planned queries (max 5). Non-blocking. |
| `POST` 🔒 | `/research/{job_id}/cancel` | Cancel a `queued` or `running` job. |
| `GET` 🔒 | `/research/{job_id}` | Full job: status, stage, report, sources, iterations, error info and timestamps. |
| `PATCH` 🔒 | `/research/{job_id}` | Rename (`{ "title": "..." }`). |
| `DELETE` 🔒 | `/research/{job_id}` | Delete the job (cancels it first if it is still running). |
| `GET` 🔒 | `/research/{job_id}/messages` | Follow-up conversation for the job. |
| `POST` 🔒 | `/research/{job_id}/messages` | Ask a follow-up (`{ "question": "..." }`). Only for `completed` jobs. Returns `user_message` and `assistant_message`. |
| `GET` 🔒 | `/research/{job_id}/export` | Download the completed report as a Markdown file with a numbered sources list. |

### Error codes on failed jobs

When a job fails, `error` holds a user-safe message and `error_code` a machine-readable value so the UI can offer the right action. Raw provider errors are logged, never stored or returned.

| `error_code` | Meaning |
|---|---|
| `llm_auth` | Provider rejected the API key. Update it in LLM settings. |
| `llm_rate_limit` | Provider rate-limited the request. Wait and retry. |
| `llm_error` | Other provider failure. |
| `interrupted` | Server restarted while the job was running. |
| `internal` | Unexpected server-side error. |

---

## Typical API workflow

```bash
BASE=http://localhost:8000

# 1. Register, then verify (in dev mode the token appears in the server log)
curl -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-strong-password"}'
curl -X POST $BASE/auth/verify-email -H 'Content-Type: application/json' \
  -d '{"token":"<token from email or log>"}'

# 2. Log in
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-strong-password"}' | jq -r .access_token)

# 3. Save your LLM settings
curl -X PUT $BASE/settings/llm -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"groq","api_key":"gsk_...","planner_model":"llama-3.3-70b-versatile",
       "researcher_model":"llama-3.3-70b-versatile","synthesizer_model":"llama-3.3-70b-versatile"}'

# 4. (Optional) verify the key and models actually work
curl -X POST $BASE/settings/llm/test -H "Authorization: Bearer $TOKEN"

# 5. Plan a research job
JOB=$(curl -s -X POST $BASE/research -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"What are the main approaches to grid-scale energy storage?"}' | jq -r .job_id)

# 6. Start it (optionally edit queries) and poll until status is completed
curl -X POST $BASE/research/$JOB/start -H "Authorization: Bearer $TOKEN"
curl -s $BASE/research/$JOB -H "Authorization: Bearer $TOKEN" | jq '{status, stage}'

# 7. Read the report, ask a follow-up, export
curl -s $BASE/research/$JOB -H "Authorization: Bearer $TOKEN" | jq -r .report
curl -X POST $BASE/research/$JOB/messages -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"question":"Which approach is cheapest per kWh?"}'
curl -OJ $BASE/research/$JOB/export -H "Authorization: Bearer $TOKEN"
```

---

## LLM providers

All four providers are called through **OpenAI-compatible `/chat/completions` endpoints**, so messages and tool-calling use one format everywhere.

| Provider | Endpoint | Suggested models |
|---|---|---|
| `groq` | `api.groq.com/openai/v1` | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b` |
| `openai` | `api.openai.com/v1` | `gpt-4o-mini`, `gpt-4o`, `gpt-4.1` |
| `gemini` | `generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.0-flash` |
| `anthropic` | `api.anthropic.com/v1` (OpenAI-compatibility layer) | `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` |

Model names are suggestions only; users can type any model ID their provider supports.

**Choosing models per stage.** The researcher model must support reliable **tool/function calling**. The planner and synthesizer are plain text calls, so a stronger model for synthesis and a cheaper, faster one for planning is a sensible split.

**Client behaviour (`agents/llm.py`):**

- A single pooled `httpx.Client` (60s timeout, 10s connect, 20 connections).
- Up to 3 retries with exponential backoff for `429`, `5xx` and Groq's `tool_use_failed` 400s, honouring `Retry-After` (capped at 30s).
- `401`/`403` fail immediately as an auth error.
- If a model rejects a custom `temperature` (e.g. some OpenAI reasoning models), the client drops it and retries.
- `LLMConfig.api_key` is declared `repr=False`, so keys never leak into logs or tracebacks.

> Anthropic's OpenAI-compatibility layer is documented by Anthropic as intended mainly for evaluation/testing. If you want Anthropic as a first-class provider, switch to the native `/v1/messages` API.

---

## Job lifecycle

```
PLANNED ──start──► QUEUED ──slot acquired──► RUNNING ──► COMPLETED
                      │                         │    └──► FAILED
                      └──────── cancel ─────────┴───────► CANCELLED
```

| Status | Meaning |
|---|---|
| `planned` | Queries generated; waiting for the user to review and start. |
| `queued` | Claimed and waiting for a free worker slot. |
| `running` | Pipeline executing; `stage` shows progress. |
| `completed` | Report and sources available. |
| `failed` | See `error` / `error_code`. Can be started again. |
| `cancelled` | Stopped by the user. |

How execution works (`research_service.py`):

- Jobs run as `asyncio` background tasks; a **semaphore (default 2)** limits concurrent jobs so bursts can't blow through provider rate limits.
- The blocking pipeline runs in a worker thread; progress callbacks are marshalled back to the event loop.
- **Cancellation is cooperative:** a `threading.Event` per job is checked between pipeline steps. A tool call already in flight can't be interrupted, so cancellation takes effect right after it returns.
- The service opens **short-lived DB sessions** and never holds a connection during an LLM call. (`get_current_user` also commits right after loading the user to release its connection early.)
- Starting uses an atomic claim (`claim_for_run`), so a job can't be started twice.

---

## Security design

- **Passwords:** argon2id (`argon2-cffi`), hashed in a worker thread so the event loop isn't blocked. Hashes are transparently upgraded if parameters change.
- **Timing safety:** logging in with an unknown email performs an equivalent dummy hash check so response time doesn't reveal which emails exist.
- **No account enumeration:** resend-verification and forgot-password always return the same generic response. Disabled/unverified status is only revealed after the password is proven correct.
- **Tokens:** access tokens are short-lived JWTs (`sub`, `iat`, `exp` required). Email-verification and reset tokens are random, single-use, expire, and only their **SHA-256 hash** is stored. Issuing a new token invalidates older unused ones.
- **LLM keys:** encrypted with Fernet at rest, decrypted only in memory for a pipeline run, and never returned by the API (only the last 4 characters).
- **Ownership checks:** every job query is scoped to the authenticated user's ID.
- **Cooldowns:** verification resend (default 10 minutes) and password-reset requests (60 seconds).
- **Safe errors:** provider exceptions are logged server-side; users only see classified, user-safe messages.

---

## Frontend & deployment

The `docs/` folder is a static SPA that uses **hash routing** (`#/verify?token=...`, `#/reset?token=...`), so it works on GitHub Pages without server-side routing. Email links are built from `PWRAL_FRONTEND_BASE_URL` and `PWRAL_FRONTEND_PATH`, e.g.:

```
PWRAL_FRONTEND_BASE_URL=https://username.github.io
PWRAL_FRONTEND_PATH=repo-name
→ https://username.github.io/repo-name/#/verify?token=...
```

For local development, serve it with any static server (the example config assumes port 5500):

```bash
cd docs && python -m http.server 5500
```

### Deploying the backend

- Run behind a TLS-terminating reverse proxy; `main()` already enables `proxy_headers`.
- Set all required `PWRAL_*` variables as real environment variables, with strong secrets.
- Run `alembic upgrade head` before starting a new version. `.github/workflows/migrations.yml` automates migrations in CI.
- Set `PWRAL_FRONTEND_BASE_URL` to your real frontend origin so CORS and email links work.
- Deploy **one process** (see limitations).

---

## Design decisions & limitations

- **Single-process assumption.** The concurrency semaphore, cancel events and startup recovery are all in-memory. Running multiple workers/replicas would break cancellation and could cause one instance to fail another's running jobs on startup. For horizontal scaling, move to a real job queue (e.g. Arq, Celery, or a Postgres-backed queue).
- **Jobs don't survive restarts.** Interrupted jobs are marked failed; the user restarts them.
- **No streaming.** Progress is polled via `GET /research/{id}`.
- **Follow-ups use no new search.** They answer from the stored report and sources only; unanswerable questions yield a `suggested_query` for a new research run.
- **Search/fetch quality.** DuckDuckGo results and page extraction vary; pages that can't be fetched don't become citable sources.
- **Citation fidelity depends on the model.** The synthesizer is instructed to cite only the numbered sources it was given, but smaller models may occasionally mis-cite.
- **Planner timezone.** The planner prompt receives the current date/time in Nepal time (`config/nepal_time.py`). Adjust it if you deploy elsewhere.
- **Quotas.** Users pay their own LLM costs, but the server has no per-user rate limit on requests or job creation.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| App won't start: `ValidationError` for `brevo_api_key` | The variable must be present. Set `PWRAL_BREVO_API_KEY=` (empty) for dev. |
| App won't start: Fernet error | `PWRAL_API_KEY_ENCRYPTION_KEY` isn't a valid 32-byte URL-safe base64 key. Regenerate it. |
| "Your saved API key can't be read; please save it again." | The encryption key changed since the key was stored. Users must re-enter their API key. |
| No verification email arrives | In dev mode the token/link is in the server log. In production check Brevo key, verified sender, and `PWRAL_BREVO_SENDER_EMAIL`. |
| Browser CORS errors | `PWRAL_FRONTEND_BASE_URL` must match the frontend's origin exactly. |
| Job fails with `llm_auth` | Provider rejected the key. Re-save it in LLM settings, and run `POST /settings/llm/test`. |
| Job fails with `llm_rate_limit` | Provider throttled you. Wait, use a model with higher limits, or lower concurrency. |
| Researcher errors on tool calls | The chosen researcher model may not support function calling well. Try another model. |
| Jobs stuck as `failed` / "Interrupted by a server restart" | Expected after a restart; start the job again. |
| `relation "users" does not exist` | Migrations haven't been run: `uv run alembic upgrade head`. |

---