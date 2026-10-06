# Developer guide

This guide runs the custom D365 Finance Assistant on Windows, macOS or Linux. No Docker, D365 database access, Dataverse copy, X++ customization or Visual Studio D365 project is required. Live integration uses the standard Finance & Operations OData endpoint.

## Architecture and repository

The React frontend calls FastAPI over JSON and a fetch-readable server-sent event stream. A bounded LangGraph assistant uses typed application tools; the model cannot provide raw OData URLs. The finance service normalizes ERP records and calculates totals with `Decimal`, separately for each currency. The D365 client handles server-side Entra authentication, metadata discovery, safe reads and supported writes. SQLite stores conversation history, tool evidence, pending actions and audit records.

```text
frontend/                 React/TypeScript, Vite and frontend tests
  src/                    Components, API client, state and voice UI
  .env.example            Browser-safe settings
  package-lock.json       Reproducible npm dependency resolution
backend/
  app/api/                API routes and stream protocol
  app/agent/              LangGraph assistant and typed tool catalog
  app/core/               Settings, errors and request/session policy
  app/db/                 Async SQLAlchemy session infrastructure
  app/models/             Application persistence models
  app/schemas/            Validated API and finance structures
  app/services/           Conversations, confirmation, voice and finance workflows
  app/integrations/d365/   OAuth, OData, metadata, finance and mock provider
  app/llm/                Azure OpenAI adapter
  alembic/                Database migrations
  tests/                  Deterministic backend tests
  requirements.txt        Direct dependency declarations
  requirements.lock       Fully pinned, hash-verified installation lock
  .env.example            Server configuration placeholders
  data/                   Runtime SQLite database; ignored by Git
scripts/                  Setup and local development launchers
.github/workflows/ci.yml   Independent backend and frontend checks
```

The frontend is a single application and the backend a single service. The backend reuses async HTTP connections. Transactional balances are retrieved when requested rather than used as a long-lived financial cache. Evidence includes the company, currency, source entity, retrieval time and record references.

## Prerequisites

- Python **3.13.3** with `venv` and `pip`. On Windows, install the Python launcher (`py`) as well. Use this exact patch version; the setup and dev scripts reject other versions, including an existing older virtual environment. Official release installers are available at https://www.python.org/downloads/release/python-3133/.
- Node.js **22 LTS** with npm. The frontend requires Node 20.19 or newer; use Node 22 for the standard workflow.
- Git and a terminal. Windows PowerShell 5.1+ works for the included scripts.
- Outbound HTTPS to package registries during installation. Live operation additionally needs your Entra tenant token endpoint, D365 host, Azure OpenAI host and optionally `api.groq.com`.

Check installations:

```powershell
py -3.13 --version  # Must report Python 3.13.3.
node --version
npm.cmd --version
git --version
```

## Quick Windows startup

From the cloned repository root:

```powershell
.\scripts\setup.ps1
.\scripts\dev.ps1 -Mock
```

The setup script validates Python 3.13.3, creates `backend/.venv`, installs hash-locked Python requirements and the locked npm dependencies, creates local `.env` files only if absent, and applies Alembic migrations. An existing virtual environment is checked before any dependency installation; it is not silently reused with a different Python version. The dev script also verifies Python, checks the backend port and health before starting Vite and runs migrations before startup. `-Mock` temporarily sets `D365_MOCK_MODE=true` for the launched backend; it does not rewrite your `.env` file. Stop with **Ctrl+C**.

If your personal PowerShell policy blocks locally downloaded scripts, use a process-scoped policy for the terminal you control:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Managed corporate policies may override this. The manual commands below also work without executing our `.ps1` files.

URLs:

| Component | Address |
| --- | --- |
| Frontend | http://localhost:5173 |
| Backend | http://localhost:8000 |
| Interactive API documentation | http://localhost:8000/docs |
| Health | http://localhost:8000/api/health |

## Manual backend setup: Windows PowerShell

Run from the repository root. If `.venv` already exists with a different interpreter, follow [the recreation procedure](#mismatched-virtual-environment) first. This activation command is convenient, but all commands can instead use `.\.venv\Scripts\python.exe` directly.

```powershell
cd backend
py -3.13 ..\scripts\check_backend.py --check-python
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.13.3 before continuing." }
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe ..\scripts\check_backend.py --check-python --existing-venv
if ($LASTEXITCODE -ne 0) { throw "Recreate the virtual environment with Python 3.13.3." }
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements.lock
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

For a credentials-free demonstration, set `D365_MOCK_MODE=true` in `.env` or run `$env:D365_MOCK_MODE = "true"` before starting Uvicorn. For live use keep it `false` and set the server credentials described below. Environment variables override `.env`; remove a shell override with `Remove-Item Env:D365_MOCK_MODE -ErrorAction SilentlyContinue` when switching back to live mode.

## Manual backend setup: macOS/Linux

If `.venv` already exists with a different interpreter, follow [the recreation procedure](#mismatched-virtual-environment) first.

```bash
cd backend
python3.13 ../scripts/check_backend.py --check-python
python3.13 -m venv .venv
.venv/bin/python ../scripts/check_backend.py --check-python --existing-venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
test -f .env || cp .env.example .env
# Edit .env using your preferred editor.
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Both version checks must succeed before continuing the manual commands. `python3.13` must resolve to Python 3.13.3; if another 3.13 patch version is selected, install/select the pinned interpreter first. For mock mode, prefix startup with `D365_MOCK_MODE=true`. Use `unset D365_MOCK_MODE` to remove a persistent shell override. If Python's `venv` module is unavailable, install the matching operating-system venv package; do not install the app into your system Python.

The bundled launchers are also available:

```bash
bash scripts/setup.sh
bash scripts/dev.sh --mock
```

## Mismatched virtual environment

A virtual environment keeps the interpreter used when it was created. Installing Python 3.13.3 does not upgrade an existing `backend/.venv`; setup and dev launchers reject it if it uses a different patch version. Stop the dev servers, deactivate the environment, then recreate only this generated directory. Keep `backend/.env`, `backend/data/` and the source files.

Windows PowerShell, from the repository root:

```powershell
py -3.13 .\scripts\check_backend.py --check-python
if ($LASTEXITCODE -ne 0) { throw "Install/select Python 3.13.3 before recreating the environment." }
if (Get-Command deactivate -ErrorAction SilentlyContinue) { deactivate }
if (Test-Path .\backend\.venv) { Remove-Item -Recurse -Force .\backend\.venv }
.\scripts\setup.ps1
```

macOS/Linux, from the repository root, after stopping servers and running `deactivate` if the old environment is active:

```bash
python3.13 scripts/check_backend.py --check-python
# Continue only when the exact-version check succeeds.
rm -rf -- backend/.venv
bash scripts/setup.sh
```

If `py -3.13` or `python3.13` selects another patch release, correct your installed interpreter/PATH before recreating the environment. The setup script checks the base interpreter and the new venv again; it never silently accepts another version. Deleting `.venv` removes installed Python packages, not local chat data or credentials.

## Frontend setup

In a second terminal, from the repository root:

```powershell
cd frontend
npm.cmd ci
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm.cmd run dev -- --host localhost --port 5173 --strictPort
```

macOS/Linux:

```bash
cd frontend
npm ci
test -f .env || cp .env.example .env
npm run dev -- --host localhost --port 5173 --strictPort
```

Use `npm ci` for repeatable installations from the committed lockfile. `npm install` is suitable when intentionally changing dependencies. Keep the backend running in its own terminal.

## Configuration files

**`backend/.env`** contains server secrets and configuration. **`frontend/.env`** contains browser-safe settings only. Both are ignored by Git; their `.env.example` templates are committed. Restart the backend after changes; restart Vite after frontend environment changes. Start backend commands inside `backend/` so `.env` and the default relative database path resolve consistently.

Never put secrets in a variable beginning with `VITE_`: Vite embeds those variables in the browser bundle. Never paste secrets into chat, commit a real `.env`, or store tokens in the application database. Values in `.env.example` are placeholders or non-secret configuration, not working credentials.

### Application settings

| Setting | Default / purpose |
| --- | --- |
| `APP_ENV` | `development`; configure deployment environment policy. |
| `LOG_LEVEL` | `INFO`; structured server logs. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/finance_assistant.db`. |
| `FRONTEND_ORIGIN` | `http://localhost:5173`; allowed browser origin. |
| `APP_SESSION_SECRET` | Signs local session cookies. The development placeholder triggers a generated local signing key; configure a strong value for deployment. |
| `SESSION_COOKIE_SECURE` | `false` on local HTTP; use `true` with deployment HTTPS. |
| `MAX_REQUEST_MB` | `16`; general request-size limit. |
| `CHAT_RATE_LIMIT_PER_MINUTE` | `20`; local request throttle. |
| `ACTION_EXPIRY_MINUTES` | `10`; confirmation validity. |
| `AGENT_MAX_ITERATIONS` | `6`; bounded tool loop. |

Generate a session secret locally and place the result in your own `.env`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Treat this value as a secret. Changing it invalidates existing signed browser sessions; preserve it across restarts when you want to retain the same workspace identity. With the development placeholder, the backend creates a persistent random key at `backend/data/.session_secret` (ignored by Git). Production startup requires an explicitly configured secret. Clearing browser cookies creates a new local workspace identity; it does not erase old database records.

### Dynamics 365 settings

| Setting | Purpose / default |
| --- | --- |
| `D365_BASE_URL` | `https://org1a43c536.operations.dynamics.com`; HTTPS server root. |
| `D365_TENANT_ID` | Entra tenant ID; required for live authentication. |
| `D365_CLIENT_ID` | Application/client ID; required for live authentication. |
| `D365_CLIENT_SECRET` | Server-only client secret; required for live authentication. |
| `D365_DEFAULT_COMPANY` | `usmf`; legal entity for queries and supported writes. |
| `D365_MOCK_MODE` | `false`; opt into synthetic data with `true`. |
| `D365_WRITE_ACTIONS_ENABLED` | `true`; set `false` to remove supported mutation tools from the assistant. Confirmation remains required when enabled. |
| `D365_TIMEOUT_SECONDS` | `30`; request timeout. |
| `D365_MAX_RETRIES` | `2`; bounded transient read retry policy. |
| `D365_DELETE_ALLOWED_PREFIXES` | `TEST-,DEMO-,CHAT-`; restrict test-customer deletion. |
| `D365_PAYMENT_JOURNAL_NAME` | `CustPay`; customer payment journal configuration. |
| `D365_PAYMENT_BANK_ACCOUNT` | `USMF OPER`; bank offset account. |
| `D365_PAYMENT_METHOD` | `CHECK`; payment method. |
| `D365_CUSTOMER_POSTING_PROFILE` | `GEN`; customer posting profile. |
| `D365_REVENUE_ACCOUNT` | `401100`; draft invoice line revenue account. |

The observed journal/account defaults are starting configuration, not proof that a different environment permits them. Live business state and supported entity fields must pass validation before a write is offered or executed.

Entity collection configuration:

```dotenv
D365_CUSTOMERS_ENTITY=CustomersV3
D365_CUSTOMER_GROUPS_ENTITY=CustomerGroups
D365_CURRENCIES_ENTITY=Currencies
D365_MAIN_ACCOUNTS_ENTITY=MainAccounts
D365_FREE_TEXT_HEADERS_ENTITY=CDSFreeTextInvoiceHeaders
D365_FREE_TEXT_LINES_ENTITY=CDSFreeTextInvoiceLines
D365_PAYMENT_HEADERS_ENTITY=CustomerPaymentJournalHeaders
D365_PAYMENT_LINES_ENTITY=CustomerPaymentJournalLines
D365_CUSTOMER_TRANSACTIONS_ENTITY=auto
D365_OPEN_TRANSACTIONS_ENTITY=auto
```

### Azure OpenAI settings

```dotenv
AZURE_OPENAI_ENDPOINT=https://voiceagentdemo-resource.cognitiveservices.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4.1-mini
AZURE_OPENAI_API_VERSION=2025-03-01-preview
AZURE_OPENAI_API_KEY=
```

Set the key only in `backend/.env` or your secret store. The deployment name is the Azure deployment identifier, not an assumption about a public OpenAI model endpoint. The assistant uses low-temperature tool calling and streaming. It does not use the conversational deployment to transcribe audio.

In live mode a missing or unavailable Azure configuration produces a useful error while retaining the user's message for retry. Mock mode without an Azure key uses a deterministic demonstration responder and needs no LLM key; it supports the documented example flows rather than unrestricted natural-language intelligence. If you configure an Azure key in mock mode, the LangGraph assistant calls Azure while its ERP tools still use the mock adapter. A successful offline mock response does not verify Azure connectivity.

### Groq voice settings

```dotenv
GROQ_API_KEY=
GROQ_WHISPER_MODEL=whisper-large-v3-turbo
VOICE_MAX_UPLOAD_MB=15
VOICE_MAX_DURATION_SECONDS=120
```

Groq handles speech-to-text only. Its model can be changed through configuration. Without a Groq key, microphone transcription is disabled with an explanatory capability message; text chat continues to work. The server validates audio uploads and does not persist raw audio. Transcribed text becomes conversation history only when sent. If you increase `VOICE_MAX_UPLOAD_MB`, also leave enough headroom in `MAX_REQUEST_MB` for the multipart request overhead.

Assistant audio output uses browser `SpeechSynthesis`, independent of Groq and Azure. Settings offer browser voice, rate and automatic readout controls where supported by the browser. Available voices depend on the operating system/browser. This provider boundary allows another TTS adapter to be added later.

### Frontend settings

```dotenv
VITE_API_BASE_URL=/api
API_PROXY_TARGET=http://127.0.0.1:8000
```

The normal development setup uses same-origin `/api`, proxied by Vite to the backend. `API_PROXY_TARGET` configures the Vite server and is not exposed as a browser `VITE_` variable. For deployment, configure the reverse proxy to serve the built frontend and route `/api` to FastAPI. `vite preview` alone does not replace that production proxy.

## D365 authentication and connection verification

The backend obtains an Entra service-to-service access token with client credentials. For the supplied environment the scope is:

```text
https://org1a43c536.operations.dynamics.com/.default
```

The scope follows `D365_BASE_URL` when you configure another environment. The Entra application must be mapped to the intended Finance & Operations user under **System administration → Setup → Microsoft Entra applications**. That D365 user needs permissions to the legal entity and relevant data entities. A valid Entra token alone does not grant ERP entity access.

Tokens are cached only in backend memory and refreshed before expiry. A D365 401 forces one refresh and one retry; repeated failure is reported as disconnected. Do not use personal passwords or send the client secret from the browser.

After entering credentials and restarting the backend:

1. Open the frontend and click **Reconnect** in the D365 header or settings.
2. Inspect `GET /api/integrations/d365/status` in Swagger or the browser network tools.
3. Confirm a successful connection and inspect the resolved capabilities before asking for balances or offering writes.
4. Use entity diagnostics if transaction discovery is unavailable.

The status includes non-secret health information such as company, last success, error summary, metadata readiness, capabilities and latency. The frontend refreshes status periodically and when focus returns. Missing configuration or lost connectivity must never turn into an invented financial answer. Saved conversations remain viewable while ERP access is disconnected.

API calls use a signed local session cookie. An API client should retain cookies across requests, just as the browser does. Swagger is convenient for inspecting schemas and trying endpoints on your local machine; this local cookie is not an enterprise authentication system.

## Metadata and transaction entity discovery

Reconnect loads `GET /data/$metadata`, parses public entity sets and their fields, and evaluates candidates for customer transactions and open transactions. Deterministic scoring uses semantic field coverage: customer account, invoice/reference, transaction/due dates, currency, amount, remaining balance, voucher and company. The selected candidate is tested with a safe `$top=1` read rather than trusted from its name alone.

Names differ across D365 environments. The resolver must not infer a current open balance solely from an original invoice amount. When a reliable remaining-balance source is unavailable, balance/overdue capabilities are unavailable and the app explains the limitation. Other verified features can remain available.

Incomplete source fields remain explicit: an unavailable original amount is not replaced with a guessed value. An invoice without a verified due date cannot be classified as overdue; the backend reports the incomplete due-date coverage alongside the invoices it can classify. Remaining balances are read from the resolved open-transaction source rather than calculated as original invoice amounts minus guessed payments.

Inspect:

```text
GET /api/integrations/d365/diagnostics/entities
```

Diagnostics show candidate sets and relevant metadata fields without credentials. Use an entity that actually exists in your tenant, exposes the required semantics and permits reads for the mapped D365 user. Override names in `backend/.env`, then restart/reconnect:

```dotenv
D365_CUSTOMER_TRANSACTIONS_ENTITY=YourVerifiedCustomerTransactionEntity
D365_OPEN_TRANSACTIONS_ENTITY=YourVerifiedOpenTransactionEntity
```

These example identifiers are instructions to substitute your verified names, not built-in entity names. An explicit override does not bypass schema/capability validation.

## Mock development and manual live acceptance

Mock mode is an opt-in synthetic ERP provider for demonstrations and repeatable tests:

```powershell
.\scripts\dev.ps1 -Mock
```

It never calls the live D365 tenant. The interface identifies demo data, and mock results do not establish live authentication, metadata or write compatibility. The isolated mock adapter includes the requested Asterion fixture (`AST-001`, `FTI-00000022` with INR 35,000 remaining and `FTI-00000021` with INR 75,000 remaining). Those fixture values are confined to explicit mock mode, never inserted into live business logic or used as fallback when D365 is disconnected. Mock customer/invoice/payment writes live in memory and reset when the backend restarts; application chats and audits still persist in SQLite.

The following **manual live acceptance** requires real D365 and Azure credentials. It is not a claimed result of automated tests:

1. Disable mock mode, reconnect and inspect open-transaction capability.
2. Ask **“What is the outstanding balance for Asterion?”** Verify the resolved customer is `AST-001`, company `USMF`, and the underlying invoices are read from the live ERP.
3. For the supplied snapshot, `FTI-00000022` had INR 35,000 remaining and `FTI-00000021` had INR 75,000 remaining, totaling INR 110,000. Those values are expectations for that snapshot only. If live records change, trust D365.
4. Ask **“Which Asterion invoices are overdue as of 5 October 2026?”** In that snapshot, the first invoice was due 30 September 2026 and overdue; the second was due 15 October 2026 and not overdue.
5. Inspect evidence cards for invoice references, original versus remaining amount, company, currency and retrieval time.
6. Test any live mutation only against an approved disposable test record, review confirmation and verify the result in D365. Do not use acceptance amounts as seed data or expected live application responses.

## SQLite, migrations and persistence

The default database is **`backend/data/finance_assistant.db`** when you run from `backend/`. Its directory is created automatically. The SQLite configuration uses foreign keys, WAL mode and a busy timeout. Conversation messages, summaries, tool evidence, pending confirmations and audit events survive browser refreshes and backend restarts.

Apply migrations explicitly before startup:

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

macOS/Linux:

```bash
cd backend
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Migrations are the schema lifecycle; the app does not rely solely on SQLAlchemy `create_all`. Add a migration deliberately when changing database models. The setup/dev scripts apply the existing migrations but do not create new ones.

Conversation lifecycle includes creation, search, rename, archive, delete and resumption. Conversation deletion and clearing archived conversations are soft deletes: they hide records and cancel pending actions, but do not physically erase messages or audit history from the database. Use the reset procedure below when deliberately removing all local application data. The LLM receives the latest 14 messages plus relevant tool identifiers and a rolling deterministic summary; summaries start after 24 messages and refresh periodically. Export options preserve conversation content for the user. Stored evidence is historical: its retrieval timestamp matters, and a previous answer is not a fresh live balance.

### Reset local data safely

Resetting loses local chats, pending actions and audits; it does not delete D365 data. Prefer export/archive/delete controls when you only want to manage selected conversations. For a full local reset:

1. Stop the backend and any process using the database.
2. Back up the database and its `-wal` / `-shm` companions together if they exist. Keep backups outside Git and protect them as finance data.
3. Rename the database files, then recreate the schema with Alembic.

PowerShell, from `backend/`:

```powershell
$BackupDir = Join-Path $env:TEMP ("finance-assistant-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $BackupDir | Out-Null
foreach ($Name in @("finance_assistant.db", "finance_assistant.db-wal", "finance_assistant.db-shm")) {
    $Path = Join-Path "data" $Name
    if (Test-Path $Path) { Move-Item $Path $BackupDir }
}
.\.venv\Scripts\python.exe -m alembic upgrade head
```

For a custom `DATABASE_URL`, identify its actual path before resetting. Never apply this recipe to a remote database.

## Tests, lint and build

The committed `backend/requirements.lock` pins transitive dependencies with artifact hashes. Install it with `--require-hashes`; do not disable verification to work around an installation failure. `requirements.txt` is the direct dependency declaration for intentional updates. When changing dependencies, regenerate and review the lock with the team's pinned tooling before committing it.

Backend, from `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
```

macOS/Linux use `.venv/bin/python` for the same commands. Tests use temporary databases and mock external services; no live ERP, Azure or Groq key is required. Apply migrations locally with `python -m alembic upgrade head` to verify your installation and schema in addition to tests.

Frontend, from `frontend/`:

```powershell
npm.cmd run test -- --run
npm.cmd run lint
npm.cmd run format:check
npm.cmd run build
```

`npm run test:watch` starts interactive Vitest. `npm run format` applies Prettier intentionally to frontend source. `npm run build` includes TypeScript checks and writes the production bundle to ignored `frontend/dist/`.

GitHub Actions installs Python 3.13.3 and Node 22, runs backend lint/format/tests/migrations, frontend lint/format/tests/build and separate mock Chromium smoke tests. The workflow has read-only repository contents permissions and requires no external secrets. Automated mock tests and the manual live acceptance procedure answer different questions: the former validates deterministic application behavior; the latter validates compatibility with your actual tenant and credentials.

### Browser smoke tests

Run setup first so the backend venv and frontend dependencies exist, then from `frontend/`:

```powershell
npx.cmd playwright install chromium
npx.cmd playwright test
```

macOS/Linux use `npx playwright install chromium` and `npx playwright test`. On Linux, `npx playwright install --with-deps chromium` can also install browser operating-system prerequisites where your machine policy permits it.

The Playwright configuration starts its own mock backend on **8010** and Vite on **5174**, using **`backend/data/e2e.db`** and a separate session key. Leave those ports free. The tests exercise streamed finance answers, evidence, confirmation/cancellation, audit visibility and history after a browser refresh. They need no D365, Azure or Groq key and do not use the normal local chat database. The mock ERP state resets when the test backend restarts; the isolated test database can be reset with the same stop/back-up/migrate principles described above.

If browser downloads are blocked but a compatible Chromium is already installed, configure its executable instead of disabling verification. For the cloud machine's system Chromium:

```bash
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium npx playwright test
```

For an existing Windows installation, set `$env:PLAYWRIGHT_CHROMIUM_EXECUTABLE` to its actual executable path before running the same test command. Failed-run traces are saved in ignored `frontend/test-results/`. CI retains synthetic-data failure traces for seven days.

## API and stream protocol

Swagger at **http://localhost:8000/docs** is the authoritative list of request and response schemas. Useful routes include health, settings capabilities, conversation history, D365 status/reconnect/entity diagnostics, confirmation/cancellation, chat streaming and voice transcription.

The chat endpoint uses `POST /api/chat/stream`. The frontend reads server-sent events using `fetch` so it can submit a JSON body and include credentials. Events represent message start/deltas, tool activity, evidence, pending actions, integration state, errors and completion. Tool activity exposes concise operation descriptions, not private model reasoning. Stopping generation aborts the request; inspect an existing pending action or ERP audit rather than assuming an interrupted connection cancelled a confirmed ERP write.

Errors include a stable code, useful message, request ID and retryability. Use the request ID when correlating browser failures with structured backend logs. Logs and API responses must not expose authorization headers or secret values.

## Example prompts

With live credentials use your real customer identifiers. In mock mode use the synthetic customer suggestions shown in the application.

- “What is the outstanding balance for Asterion?”
- “Show me the invoices behind that amount.”
- “Which Asterion invoices are overdue as of 5 October 2026?”
- “How much remains on FTI-00000022?”
- “Show Asterion's payment history.”
- “Draft a collection reminder for the overdue invoice.”
- “Create a test customer called TEST-ACME-001.”
- “Update the payment terms for TEST-ACME-001 to NET30.”
- “Delete TEST-ACME-001.”
- “Create a draft INR 5,000 invoice for AST-001 due on 30 October 2026.”
- “Change the due date of that draft invoice.”
- “Create an unposted customer payment journal, then add a payment line for AST-001.”

The assistant asks for clarification when a required field is missing or multiple customers match. For a payment, journal header creation and line addition are separate supported actions; provide the returned journal reference and an explicit line number when adding a line. Reminders are saved drafts only; the application does not send email.

## ERP write safety

Supported mutation tools prepare pending actions; they do not execute on the model's first request. The confirmation card displays the target, proposed changes, company, financial impact and expiry. **Confirm** submits the server action; **Cancel** rejects it. An expired action cannot be executed. The backend rechecks applicable current state before mutation and records the attempted outcome in an audit event.

Repeated confirmation of an already executed action returns its stored result; concurrent confirmations claim the action only once. Cancelled, expired, failed and uncertain actions do not rerun. When an outcome requires verification, check any returned partial identifiers in D365 before preparing another action.

Customer creation/update uses `CustomersV3`. Deletion is restricted to configured test prefixes and must verify there are no financial transactions; a customer such as `AST-001` is not eligible. Safe-field schemas constrain updates.

Draft invoice operations use `CDSFreeTextInvoiceHeaders` and `CDSFreeTextInvoiceLines`. The backend checks the unposted state before updating or deleting. Invoice lines use `MainAccountDisplayValue`, not an invented `MainAccount` property. Posted invoices are immutable because changing posted accounting records would bypass ledger controls and audit integrity.

Payment preparation uses `CustomerPaymentJournalHeaders` and `CustomerPaymentJournalLines`, with explicit line numbers. The reusable account-display helper escapes segment delimiters such as the hyphen in `AST-001`. Configuration and metadata are checked rather than treating observed tenant defaults as universally valid.

Live writes also need sufficient public setup metadata/data to verify their business configuration. Depending on the action, the backend looks for compatible journal-name, bank-account, customer-payment-mode, customer-posting-profile and payment-term entities, and verifies the revenue account permits posting. If those entities/fields are unavailable to the mapped D365 user, the write fails closed with a capability diagnostic; the assistant does not substitute mock configuration. A connected read workflow therefore does not prove every mutation is available in a particular tenant.

**Automatic posting and settlement are not enabled.** Open the created unposted customer payment journal in D365, review the customer/bank accounts, amount/currency, reference and invoice settlement, then validate and post using standard Finance & Operations controls and your approved permissions. A prepared payment line is not proof of posted payment or a reduced live customer balance.

The HTTP layer never blindly retries an ambiguous financial write. If the network fails after a request may have reached D365, inspect the ERP record and audit/reference before trying again. Multi-step invoice/journal preparation is not a distributed transaction; any partial outcome needs explicit review. A returned confirmation is not a claim that the ERP write succeeded.

## Troubleshooting

### D365 401 or token failure

Check tenant/client identifiers, secret validity/expiry and the base URL/scope. A 401 triggers one backend token refresh and retry. Click Reconnect after correcting configuration; repeated failure stays disconnected. A login token for a different resource cannot be reused as a D365 token.

### D365 403 or permission failure

Verify the service principal's Entra mapping inside D365, the mapped user's roles, legal entity access and entity permissions. Metadata can be readable while a specific data entity is forbidden. Do not grant unnecessarily broad administrative access just to make a check pass.

### Missing entity or unavailable balance capability

Inspect diagnostics and your tenant's `$metadata`. Correct collection names or configure verified transaction entity overrides. A selected entity must expose the required fields and support a safe read. Do not use invoice original amount as a substitute for remaining/open balance. Restart/reconnect after changing `.env`.

### Rate limits or D365 5xx

Read requests use limited retries and respect retry guidance. Wait before retrying an interactive read, and avoid repeated reconnect clicks. Financial writes are handled conservatively to avoid duplicates; review an uncertain outcome rather than resubmitting it blindly.

### Network failure or timeout

Check DNS, outbound HTTPS, proxy settings and reachability for `login.microsoftonline.com`, the configured D365 host and the Azure endpoint. Check configured timeouts and tenant health. Retain TLS verification. The frontend stays usable for history when ERP access is disconnected.

### Azure OpenAI unavailable

Check endpoint, deployment name, API version, key and tenant firewall access. The deployment must support the selected chat/tool-calling API. Missing keys do not become synthetic live financial responses. Retained user messages can be retried after the backend is corrected. For an offline demo, explicitly enable mock mode.

### Voice input/output

Missing `GROQ_API_KEY` disables transcription without affecting text chat. Check microphone permission, `MediaRecorder` support, supported upload MIME type, recording size and duration limits. Microphone APIs require a secure context (localhost is allowed; remote HTTP usually is not). Groq failures should display the provider error safely; no audio is retained permanently.

Browser speech voices can arrive asynchronously and differ by platform. Select a supported voice and use the browser's audio controls. Some browsers require an explicit user interaction before audio starts. Speech output does not require a Groq key and does not send audio to Azure OpenAI.

### SQLite locked, missing table or missing directory

Start from `backend/`, check `DATABASE_URL`, confirm write permissions and run `python -m alembic upgrade head`. Close obsolete backend processes when testing. SQLite supports this local workflow; concurrent multi-user deployment should use PostgreSQL and an appropriate migration plan. Back up data before resetting or attempting a schema repair.

### CORS, cookie or connection failures

Use the recommended same-origin Vite proxy (`VITE_API_BASE_URL=/api`). Confirm `API_PROXY_TARGET` matches the running backend. Production CORS uses the exact `FRONTEND_ORIGIN` allowlist. Development also permits the corresponding `localhost` / `127.0.0.1` alias on the configured port, but cookies still belong to their original host: keep frontend and API browser requests on the same host or use the proxy. Requests must include credentials. Local HTTP needs `SESSION_COOKIE_SECURE=false`; HTTPS deployment needs `true`. Do not solve cookie failures by exposing secrets in frontend configuration.

### Port already in use / Windows environment

Stop old dev servers or deliberately update both port configuration and proxy/CORS settings. The scripts use strict port 5173 rather than silently moving the frontend. Use `npm.cmd` if PowerShell's script policy blocks `npm.ps1`. Use the venv Python executable directly if activation is blocked. Re-run setup when dependency manifests change.

## Production deployment

This repository provides a runnable local single-user finance application and integration foundation. The automatically created signed local cookie identifies a local workspace; it does not authenticate a company employee. Do not expose the dev servers as a public multi-user finance service.

Before deploying against production finance data:

- Put the frontend and API behind TLS and an authenticated reverse proxy or implement verified Entra SSO. Define a trusted identity boundary and remove any path that accepts unauthenticated public access.
- Add user/role/company authorization to conversation ownership, diagnostics, audit views and every finance read/write. The current single-local-user policy is not a tenant isolation mechanism.
- Set `APP_ENV=production`, configure strong session signing, enable secure cookies and review CSRF/origin policy for the final hosting topology. Review request limits and rate limiting for multiple workers.
- Use PostgreSQL for concurrent workloads. Add the async driver and a reviewed migration/configuration path before changing `DATABASE_URL`; the bundled installation is tested with SQLite.
- Use a managed secret store (for example Azure Key Vault) and rotated least-privilege service credentials. Never place a production client secret in source, the frontend bundle, a container layer or SQLite.
- Build the frontend with `npm run build`, serve static assets through the proxy and route `/api` to a production ASGI process. Disable reload, restrict network access, configure health monitoring and test graceful restarts.
- Centralize sanitized logs/metrics, protect finance history and backups, define retention, and alert on authentication failures, degraded ERP capability, uncertain write outcomes and audit failures.
- Validate metadata, permissions, configured journal/account defaults and every supported write against a nonproduction D365 environment before enabling production writes.
- Keep posting/settlement manual until a supported standard public action has been specifically verified and implemented with the same confirmation/audit safeguards.

Redis, background workers or other infrastructure can be added for a concrete scaling need; they are not prerequisites for local startup. Cloud setup should use the existing isolated checkout under `/workspace`, not create a separate Git worktree unless explicitly requested.

## Initial implementation verification

| Check | Result |
| --- | --- |
| Backend pytest | 74 passed; external HTTP calls mocked |
| Frontend Vitest / Testing Library | 35 passed |
| Chromium browser smoke | 2 passed against isolated mock backend; streaming, evidence, confirmation, cancellation, persisted history and settings |
| Ruff lint and formatting | Passed |
| ESLint and Prettier | Passed |
| TypeScript and Vite production build | Passed |
| Alembic upgrade and schema drift check | Passed |
| Hash-locked setup script and Linux development launcher | Executed successfully |
| Frontend dependency audit | No reported vulnerabilities |
| Windows PowerShell scripts | Reviewed statically; not executed on Windows |
| Live D365, Azure OpenAI and Groq acceptance | Unrun: credentials were not supplied |

These results describe the initial local implementation. GitHub Actions is configured to rerun the automated checks after changes; its remote outcome is separate from local verification. Dependency advisory checks are a point-in-time check, not a complete security assessment. Live transaction entity mappings, write permissions, and public setup capabilities must still be verified in your D365 environment.
