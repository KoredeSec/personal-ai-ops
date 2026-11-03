# Personal AI Ops

A self-hosted personal operations platform with two agents behind one private Telegram bot:

1. **Chief of Staff**: tasks, reminders, follow-ups, daily briefing, weekly review, memory.
2. **Cybersecurity, Career & Research**: RSS and source monitoring, technical digests, scholarship and opportunity tracking.

Built in Python with Gemini (`google-genai`), python-telegram-bot, SQLAlchemy/Alembic and SQLite (PostgreSQL-ready). It needs no paid services.

## Status

| Milestone | Scope | State |
|---|---|---|
| 1 | Structure, config, database + migrations, Gemini client, Telegram bot core, Docker | **Done** (offline tests pass; live Telegram not yet verified, see below) |
| 2 | Chief of Staff: tasks, reminders, follow-ups, briefings, memory, NL routing | Next |
| 3 | Research agent: RSS ingestion, article processing, opportunities, digests | Planned |
| 4 | Integration tests, security review, deployment, backup/restore, docs | Planned |

## Quick start (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in the values below
alembic upgrade head        # creates data/app.db
python -m pytest            # offline test suite
python -m app.main          # start the bot
```

Required `.env` values:

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message **@BotFather** → `/newbot` |
| `TELEGRAM_ALLOWED_USER_IDS` | Your numeric ID, e.g. from **@userinfobot** |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey (free tier) |

Model names are set with `GEMINI_MODEL` (reasoning and summaries) and `GEMINI_MODEL_LIGHT` (classification and routing). Check https://ai.google.dev/gemini-api/docs/models for the current free-tier model IDs.

## Verifying Milestone 1 on your machine

```bash
python -m scripts.check_gemini              # one text call + one structured-JSON call
python -m scripts.check_telegram            # validates the token (getMe)
python -m scripts.check_telegram --send     # DMs you (press Start in the bot chat first)
python -m app.main
```

Then, in Telegram:
- `/start` should reply "Initialised" with the current Lagos time.
- `/status` should show ✅ Database, ✅ Schema 0001_initial, ✅ Gemini key, and the `housekeeping` job.
- `/aitest` makes one small Gemini call and shows the model, tokens and latency.
- A message from any other Telegram account gets **no reply**, and an `access_denied` row is written to `audit_logs`.

## Docker

```bash
cp .env.example .env   # fill in; set APP_UID/APP_GID to `id -u` / `id -g`
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

The entrypoint runs `alembic upgrade head` and then starts the bot. The container runs as your uid with a read-only root filesystem, no Linux capabilities and `no-new-privileges`. Only `./data` is writable.

## Architecture

```
app/
  core/        config, logging (secret redaction), security, llm (Gemini), scheduler, timeutils
  database/    models, UTC datetime type, session, Alembic migrations
  bot/         Telegram application, gates (auth, rate limit), handlers, formatting
  services/    audit, users, health, LLM usage store/cache
  agents/      chief_of_staff/ and research/ (Milestones 2–3)
  integrations/ RSS, safe HTTP fetcher, search providers (Milestone 3)
scripts/       live connectivity checks (later: backup/restore)
tests/         offline tests, including full bot flows over a fake Telegram transport
```

Key decisions:
- **Modular monolith, no agent framework.** Agents are plain Python modules. Deterministic work (CRUD, date maths, listing, scheduling) never calls the LLM. Gemini is used only for language understanding, summarisation and classification.
- **One Gemini abstraction** (`app/core/llm.py`) handles timeouts and exponential backoff. It retries per-minute rate limits but fails fast when the daily quota is exhausted. It also enforces a local daily request budget and a prompt size cap, validates structured JSON with Pydantic, provides an opt-in response cache, logs usage per call to `agent_execution_logs`, and gives clear user-facing failure messages.
- **Times are stored in UTC and displayed in Africa/Lagos.** A custom column type rejects naive datetimes, so local/UTC mix-ups fail loudly.
- **Durable scheduling.** Reminders and digests live in the database, and periodic jobs dispatch them. Restarts lose nothing, and `notification_log.dedupe_key` (unique) prevents duplicate sends.
- **Migrations only.** The schema is created by Alembic and never by `create_all`. A test fails if the models drift from the migrations. The app refuses to start on an out-of-date schema.
- **Sync SQLAlchemy.** For a single user, SQLite queries take well under a millisecond. To move to PostgreSQL, change `DATABASE_URL` and add the `psycopg` driver.

## Security model

- **Allowlist.** Updates from unlisted Telegram user IDs are dropped silently and audited. Only private chats are served, never groups.
- **Rate limiting** per user, with one warning per burst.
- **Secrets** are only read from the environment, typed as `SecretStr`, and redacted from all logs, including tracebacks and the bot-token-bearing URLs that httpx logs. `/status` shows no secrets.
- **Errors** show the user a reference ID only. Details go to the server logs and the audit log.
- **Untrusted content.** External content is fenced with `wrap_untrusted()`: spoofed fence tags and invisible characters are stripped, the text is truncated, and a standing system policy tells the model never to follow instructions inside it. Injection heuristics are flagged for audit.
- **SSRF.** `validate_public_url()` allows only http/https on standard ports with no URL credentials. Every resolved IP must be public, which blocks loopback, RFC1918, link-local/cloud metadata, CGNAT, IPv6 ULA and IPv4-mapped tricks. The Milestone 3 fetcher re-validates every redirect hop.
- **No autonomous external actions.** The system never sends emails, messages or applications. Anything outward-facing needs explicit approval through a confirmation button.

## Dependencies and why

| Package | Why |
|---|---|
| `google-genai` | Official Gemini SDK (required) |
| `python-telegram-bot[job-queue]` | Mature async Telegram client. The job-queue extra adds APScheduler, so the bot and background jobs share one event loop. |
| `SQLAlchemy` + `alembic` | ORM with a portable SQLite → PostgreSQL path, plus real migrations |
| `pydantic-settings` | Typed, validated env config with secret types (Pydantic already comes with google-genai) |
| `httpx` | HTTP client, already a dependency of both SDKs |
| `tzdata` | Timezone database for `zoneinfo` in slim containers |
| `pytest`, `pytest-asyncio` | Tests only |

Planned for later milestones, to be confirmed before adding: `dateparser` (relative dates such as "next Monday", M2), `feedparser` (RSS parsing, M3) and possibly `trafilatura` (main-text extraction from web pages, M3).

## Database tables

`users`, `preferences`, `memory_items`, `projects`, `tasks`, `reminders`, `followups`, `research_sources`, `research_articles`, `opportunities`, `opportunity_deadlines`, `conversation_summaries`, `agent_execution_logs`, `audit_logs`, `llm_cache`, `notification_log`.

Opportunities record `verification_status`, a per-field `field_verification` map and `is_verified` on each deadline. This keeps verified facts separate from assumptions.

New schema change:

```bash
alembic revision --autogenerate -m "describe change"
```

Review the generated file, then run:

```bash
alembic upgrade head
```
