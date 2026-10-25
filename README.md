# AlertCheck

WhatsApp receipt verification and a merchant activity feed for Nigerian shops. This repository is an early implementation for the TSG Labs Chord build exercise. It includes the multi-tenant merchant portal, account setup, tenant-scoped activity, the hourly account-health calculation, a seeded mock transaction provider, provider-backed verification, and merchant-scoped MCP tools. Live Chord channel wiring and PDF reports described in [ARCHITECTURE.md](ARCHITECTURE.md) remain to be implemented.

The Chord documentation audit and live acceptance gates are in [CHORD_INTEGRATION_DECISIONS.md](CHORD_INTEGRATION_DECISIONS.md). A shop's WhatsApp number and MCP key do **not** mean its Chord channel is connected.

## Platform Chord key

MCP is installed by `uv sync --extra test` and runs in the project `.venv`. Configure the operator-owned Chord credential once for the deployment. Preferred on Railway: set `CHORD_API_KEY` and a long random `SESSION_SECRET`; optionally set `CHORD_BASE_URL` (default `https://api.chordhq.xyz`). This key identifies **one Chord tenant** and is shared by the platform integration, not handed to merchants. A dedicated Chord workspace/teammate and explicit MCP assignment per merchant are still required. A global API key alone does not provision them or connect WhatsApp.

Alternatively set `PLATFORM_ADMIN_PASSWORD` (at least 16 characters), then open `/platform/chord` and save the Chord API key there. Portal-saved keys are encrypted in Postgres using a key derived from `SESSION_SECRET`; keep that secret stable and backed up, or saved credentials become unreadable. An environment `CHORD_API_KEY` overrides a portal-saved key. The page never redisplays the key. Its **Test connection** action calls `GET /v1/teammates` and reports credential access only—not WhatsApp or MCP health. Keep the admin password out of merchant accounts and use HTTPS in deployment.

## Run locally

Requires Python 3.12+. Install the dependencies in `pyproject.toml`, then run:

```sh
uv sync --extra test
uv run uvicorn alertcheck.web:app --reload --env-file .env
```

Open `http://localhost:8000`. By default the app uses a local SQLite file for development. Set `DATABASE_URL` to a Postgres connection string to use Postgres. Never use production credentials in the repository. The `SESSION_SECRET` must be a long random value in Railway. See `.env.example` for variable names.

For local testing, create an ignored `.env` containing `SESSION_SECRET` and `PLATFORM_ADMIN_PASSWORD`; this workspace already has one for the local server. Do not commit `.env` or paste real Chord keys into `.env.example`.

Run the demo transaction feed separately with `MOCK_PROVIDER_TOKEN` set:

```sh
uv run uvicorn alertcheck.provider:app --port 8001
```

Set `MOCK_PROVIDER_URL=http://localhost:8001` and the same `MOCK_PROVIDER_TOKEN` for the portal service. Leave `DEMO_FEED_ENABLED` unset locally. Set it to `true` only on the mock-provider process when you want invented incoming credits. Once a merchant adds a primary account, the setup page can issue a one-time-visible, shop-specific Chord connection key. Connect Chord to the portal's Streamable HTTP MCP endpoint at `/mcp/` with `Authorization: Bearer <shop-key>`. Rotating the key revokes the old key. The tool surface is `verify_payment`, `customer_history`, `get_account_health`, and `report_summary`. `verify_payment` needs the account the money left and the amount the customer said. A name is required only the first time that sender account is confirmed. The shop receiving account is asked only when the shop has several accounts and the match is still ambiguous. A confirmed transfer is claimed and later checks return `already_claimed`. Chord must rediscover tools after a schema change, because it caches the previous input schema. These tools return message text and feed events; actual WhatsApp sending and delivery confirmation still require Chord channel wiring.

After creating a shop and its primary receiving account, seed invented provider records:

```sh
uv run python -m alertcheck.seed --merchant your-shop-slug --hourly
```

The numeric references `512345678901`, `712345678902`, `812345678903`, and `912345678904` represent success, failure, pending, and an amount mismatch. They are exact seeded records; an arbitrary reference beginning with those digits does not generate a status. The `--hourly` option adds 21 made-up attempts for the dashboard warning demonstration.

```sh
uv run pytest
```

## Railway

Create a Railway project with Postgres, a portal service, and a separate mock-provider service from this repository. Set both services' `DATABASE_URL` to the Postgres service's `DATABASE_URL` using a reference variable. Set a long `SESSION_SECRET` on the portal and the same long `MOCK_PROVIDER_TOKEN` on both app services. Set the portal's `MOCK_PROVIDER_URL` to the mock-provider's Railway internal URL. Set `DEMO_FEED_ENABLED=true` on the mock-provider service only. That feed is invented: about every 10 minutes (`DEMO_FEED_INTERVAL_SECONDS`, default 600) the provider inserts one successful incoming credit for each shop that has an active primary account. The portal Activity list still fills only after `verify_payment`. The portal start command is `uvicorn alertcheck.web:app --host 0.0.0.0 --port $PORT`; the provider start command is `uvicorn alertcheck.provider:app --host 0.0.0.0 --port $PORT`. Generate a public domain for the portal only. Schema creation runs at startup for this prototype; replace it with migrations before serving real merchants. Do not use the mock provider to represent real bank settlement.

After deploying a change to `verify_payment`, Chord must rediscover or update the Judas MCP server. Chord caches the tool schema, including an older schema that required `customer_chat_id`. Customers are not asked for a chat ID.

Set `PUBLIC_BASE_URL` to the portal's exact public HTTPS origin (for example `https://your-service.up.railway.app`); the MCP transport uses its host allowlist to reject unexpected Host headers. Railway's `RAILWAY_PUBLIC_DOMAIN` is used as a fallback if available.

## What works now

- Merchant signup and sign-in with hashed passwords and signed session cookies.
- Shop-scoped primary and backup receiving-account configuration.
- Shop-scoped payment activity and daily confirmed total.
- Rolling 60-minute account-health calculation: pending or failed attempts reach at least 50% across more than 20 distinct attempts.
- Backup-account recommendation only when its own last hour has more than 20 attempts and is below the warning threshold.
- External mock-provider API in `alertcheck.provider`, protected by `MOCK_PROVIDER_TOKEN`, and verification logic in `alertcheck.verification`.

## Demo data boundary

All receipts, accounts and transaction identifiers used in the Chord exercise will be invented. Numeric reference prefixes will select outcomes inside a seeded mock provider, not inside the agent. A receipt is never sufficient to confirm a payment. Public Nigerian receipt examples were consulted for layout and field names only; their personal details are not fixtures.

## AI use

Codex helped develop the architecture, portal UI and initial code. Integration decisions and observed failures will be documented in the submission's deployment field notes.
