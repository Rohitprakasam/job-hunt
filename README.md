# job-agent

Local-first job hunt agent for Rohit Prakasam G. Phase 0 (foundation) is built: config and profile,
local SQLite, keychain secrets, a Gemini wrapper that always redacts and respects free-tier quotas,
the MongoDB Atlas bridge (shared with `rohit-s-modern-profile`), $0 WhatsApp, the kill switch and
`agent doctor`. Gmail tracking is postponed.

**Budget is $0.** Gemini runs on the free tier only (limits are requests per minute/day, reset at
midnight Pacific). WhatsApp sends plain text only, and only while the 24-hour window is open
(Rohit messaged the RohitOS bot in the last 23.5 h); otherwise messages queue. No templates, ever.

## Where things live

| What | Where |
|---|---|
| Code, example configs | this repo |
| Real configs (`profile.yaml`, `settings.yaml`, `search.yaml`, `companies.yaml`) | `config/`, gitignored |
| Database, logs, backups | `%LOCALAPPDATA%\job-agent\` (override with `JOB_AGENT_HOME`) |
| Secrets | Windows Credential Manager, service `job-agent` |
| Cloud (non-personal only) | Atlas db `job_agent`: `heartbeat`, `control` |

## Setup

```powershell
cd "C:\Users\dell\Documents\Job Hunting\job-agent"
python -m uv sync                     # creates .venv with all dependencies
.venv\Scripts\Activate.ps1            # then `agent` is on PATH
.venv\Scripts\pre-commit install      # blocks personal files and secrets at commit time

# 1. Config
copy config\settings.example.yaml config\settings.yaml
copy config\search.example.yaml config\search.yaml
copy config\companies.example.yaml config\companies.yaml

# 2. Secrets: Gemini + WhatsApp from the portfolio project (only these 5 are imported)
agent secrets import-env "C:\Users\dell\Desktop\pf\rohit-s-modern-profile\.env"

# 3. MongoDB: create the scoped Atlas user first (below), then
agent secrets set mongodb_uri
agent cloud init

# 4. Database
agent db migrate

# 5. Profile: draft from the portfolio, review it, save as profile.yaml
agent profile import            # or: --from-file "C:\Users\dell\Desktop\pf\rohit-s-modern-profile\src\data\portfolio.ts"
# edit config\profile.draft.yaml -> fill every TODO -> save as config\profile.yaml

# 6. Check everything
agent doctor
agent doctor --send-test        # sends a WhatsApp text only if the window is open
```

### Scoped Atlas user (do this before step 3)

Atlas -> cluster `port-me` -> **Database Access -> Add New Database User**:

- Username `job-agent`, password: autogenerate, copy it.
- **Specific Privileges**: `readWrite` on `job_agent`, `read` on `rohit_portfolio`, `read` on `portfolio`.
- Network Access: make sure this PC can connect (the cluster may already allow `0.0.0.0/0`).
- URI: `mongodb+srv://job-agent:<password>@port-me.g4btuts.mongodb.net/?retryWrites=true&w=majority`

Don't use the portfolio's admin `MONGODB_URI`; `agent doctor` fails if the URI can write to the bot's database.

### WhatsApp (test number)

- Rohit's number must be one of the test number's allowed recipients (max 5): Meta -> WhatsApp -> API Setup -> To.
- If `agent doctor` warns the token expires, create a System User token with no expiry
  (Business Settings -> System users -> Generate token, permissions `whatsapp_business_messaging`,
  `whatsapp_business_management`), then `agent secrets set whatsapp_token` and update the portfolio's
  Vercel env too.

## Commands

| Command | Does |
|---|---|
| `agent doctor [--offline] [--send-test]` | Checks config, secrets, DB, redaction, Gemini, Mongo, WhatsApp, git hygiene |
| `agent secrets list / set <name> / import-env <path> / purge` | Keychain (values are never printed) |
| `agent db migrate / info` | Local database |
| `agent cloud init` | Create the validated `job_agent` collections (safe to repeat) |
| `agent llm usage` | Free-tier requests today per model, 429s, pause state |
| `agent profile import [--from-file] [--yes]` | Write `config/profile.draft.yaml`; never touches `profile.yaml` |

Stop applying (later phases): create a file named `STOP` in this folder, or set `stop: true` in `job_agent.control`.

## Development

```powershell
.venv\Scripts\python -m pytest          # offline; uses fake data and an in-memory keychain
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format .
```

Rules the tests enforce: only `agent/llm.py` imports `litellm`; nothing reads the portfolio's Mongo
`settings.whatsappToken` (its `/api/settings` has no auth); WhatsApp code has no template sending.
