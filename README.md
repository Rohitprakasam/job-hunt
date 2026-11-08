# job-agent

Local-first job hunt agent for Rohit Prakasam G. It finds jobs, ranks them against the profile,
sends a WhatsApp digest, prepares truthful tailored applications, fills application forms in a
headless browser (practice runs first, live only when you switch it on), tracks replies you import
and reports weekly. Everything personal stays on this PC; the cloud only sees counts.

**Budget is $0.** Gemini runs on the free tier only (requests per minute/day, reset at midnight
Pacific). When the quota is gone the agent keeps working without it (local similarity, profile-only
tailoring). WhatsApp sends plain text only, and only while the 24-hour window is open (you messaged
the RohitOS bot in the last 23.5 h); otherwise messages queue. No templates, ever.

## What runs when (`agent run`)

| Every | Stage | What it does |
|---|---|---|
| 1 h | discover ATS | Greenhouse / Lever / Ashby boards of the companies in `companies.yaml` |
| 3 h | discover boards | JobSpy: LinkedIn, Indeed, Naukri (roles x locations in `search.yaml`) |
| after each | score + urgent | Hard filters, fit score (embeddings, skills, Gemini rubric, seniority, freshness), 85+ alert |
| 08:00 | digest | Top 10 new matches with links, last 24 h counts, follow-up nudges |
| 30 min | prepare + apply | Tailored resume PDF + cover note for fit >= 70 on a supported ATS, the 7 gates, then a dry run (or live) |
| 2 h | prep packs | Interview prep file for every application that reached "interview" |
| 15 min | heartbeat | Heartbeat + sanitized snapshot to Mongo for the `/jobs` monitor |
| 10 min | flush | Sends queued WhatsApp messages once the window is open |
| 02:30 | backup | SQLite copy + config zip, 14 kept |
| Sun 19:00 | weekly | Weekly review file + 6-line WhatsApp summary |

Missed runs while the PC slept run once on wake.

## Where things live

| What | Where |
|---|---|
| Code, example configs | this repo |
| Real configs (`profile.yaml`, `settings.yaml`, `search.yaml`, `companies.yaml`, `contacts.csv`) | `config/`, gitignored |
| Database, logs, backups, resumes, screenshots, reports, prep packs | `%LOCALAPPDATA%\job-agent\` (override with `JOB_AGENT_HOME`) |
| Browser profile used for applying | `%LOCALAPPDATA%\job-agent\browser-profile` |
| Secrets | Windows Credential Manager, service `job-agent` |
| Cloud (counts only) | Atlas db `job_agent`: `heartbeat`, `control`, `snapshot` |

## Setup

```powershell
cd "C:\Users\dell\Documents\Job Hunting\job-agent"
python -m uv sync                                  # .venv with all dependencies
.venv\Scripts\Activate.ps1                         # then `agent` is on PATH
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\pre-commit install

# 1. Config (search.yaml, companies.yaml and settings.yaml are already copied and ready)
#    companies.yaml: 20 checked employers with jobs in India. Add more any time.

# 2. Secrets: Gemini + WhatsApp from the portfolio project (only these 5 are imported)
agent secrets import-env "C:\Users\dell\Desktop\pf\rohit-s-modern-profile\.env"

# 3. MongoDB: create the scoped Atlas user first (below), then
agent secrets set mongodb_uri
agent cloud init                                   # heartbeat, control, snapshot (safe to repeat)

# 4. Database
agent db migrate

# 5. Profile: `agent profile from-tex` (from config\resume.tex), fill the 4 TODOs, save as config\profile.yaml
agent answers seed                                 # answers that follow from profile.yaml

# 6. Check
agent doctor
agent doctor --send-test                           # WhatsApp test if the window is open
```

### Scoped Atlas user (before step 3)

Atlas -> cluster `port-me` -> **Database Access -> Add New Database User**:

- Username `job-agent`, password: autogenerate, copy it.
- **Specific Privileges**: `readWrite` on `job_agent`, `read` on `rohit_portfolio`, `read` on `portfolio`.
- URI: `mongodb+srv://job-agent:<password>@port-me.g4btuts.mongodb.net/?retryWrites=true&w=majority`

Don't use the portfolio's admin `MONGODB_URI`; `agent doctor` fails if the URI can write to the bot's database.

### WhatsApp (test number)

- Your number must be one of the test number's allowed recipients (max 5): Meta -> WhatsApp -> API Setup -> To.
- Send any message to the RohitOS bot each morning: that opens the free 24-hour window and the
  queued digest goes out within 10 minutes.

## Your LaTeX resume

`config/resume.tex` (gitignored) is the resume that gets sent, with `resume.engine: latex` in
`settings.yaml`. For each job the agent makes only small, truthful word changes and keeps your layout:

- the italic headline: the job's title first (`Backend Engineer | Python (FastAPI) | ...`), same number of parts;
- the summary's last sentence: `Seeking a Backend Engineer role at Zeta.`; with `llm_summary: true`
  Gemini may also reword a few summary words toward the job's terms. That edit is thrown away unless it
  keeps every number, adds no skill or term that isn't already in your resume, and stays valid LaTeX;
- the Technical Skills lines: the job's skills first, and spelled the way the job spells the same skill
  (PostgreSQL -> Postgres, React.js -> ReactJS, burp suite -> Burp Suite); most relevant line first.

Experience, projects, education and awards are never changed. If the tailored file runs over one page,
your template is used as written. Each application folder has `resume-changes.txt` with every change,
the job's ATS keywords you match and the ones you don't have (a learning list, never added).

Keep three shapes so the agent can find what to tailor: the `\noindent{\itshape...\selectfont ...}`
headline line, the `\sectionhead{Professional Summary}` block, and `\item \textbf{Category:} a, b, c`
lines under `\sectionhead{Technical Skills}`. `agent doctor` reports which ones it found.

**Needs a LaTeX engine** (one-time): install **MiKTeX** (miktex.org, "install missing packages
on the fly: Yes") or **Tectonic** (github.com/tectonic-typesetting/tectonic/releases, unzip
`tectonic.exe` to `%LOCALAPPDATA%\job-agent\tools\tectonic\`). The agent finds either; or set
`resume.latex_command`. Until then applications are skipped with "no LaTeX engine" (nothing is sent
with a different resume).

When you change `resume.tex`, run `agent profile from-tex` so `profile.yaml` (used for form answers like
"current title" and for the truth checks) matches it: it rebuilds `config/profile.draft.yaml`, keeps
your work authorization / notice period / salary, and saves the old draft as `.bak`.

## First week: from finding jobs to applying

1. **Run it:** `agent service install --yes` (starts the worker at logon, plus an hourly watchdog
   that restarts it if it stopped; a lock guarantees only one worker ever runs) and
   `powercfg /change standby-timeout-ac 0` so the PC doesn't sleep while plugged in. `agent run`
   runs it in a terminal instead. Logs: `%LOCALAPPDATA%\job-agent\logs\agent.log` (rotated).
2. **Check matches:** `agent jobs top`, `agent jobs stats` (why jobs were filtered),
   `agent console` -> http://127.0.0.1:8765. Tune `search.yaml` until 8 of 10 digest jobs are ones
   you'd apply to.
3. **Answer questions once:** dry runs put every question they couldn't answer on a list.
   `agent answers pending`, then `agent answers set "<question>" "<answer>"`. Legal questions
   (authorization, sponsorship, disability, criminal record, demographics) are only ever answered from
   these saved answers or profile.yaml, never by Gemini.
4. **Review dry runs:** each one saves `filled-dry-run.png` and `submitted-values.json` under
   `%LOCALAPPDATA%\job-agent\output\applications\<id>\` (also in the console).
5. **Go live per ATS** after 5 clean dry runs: set `apply.mode: live` in `settings.yaml`, then
   `agent apply go-live greenhouse` (and lever / ashby). `agent apply status` shows progress.
6. **Stop any time:** `agent stop`, a file named `STOP` in this folder, `/jobstop` to the bot, or the
   button on `/jobs`. Undo with `agent start` / `/jobstart <PIN>`.

**Job Boards (opt-in):** LinkedIn Easy Apply, Naukri, and Indeed can be enabled with `agent apply go-live <site>` (run via `deploy/instabox/go_live_boards.sh`).
*Account risk:* job board automation carries platform account risk (their terms forbid bots and accounts may be restricted).
*Human-in-the-loop:* every login, OTP, and captcha verification is routed strictly to the user personally through a secure, on-demand live view link sent via WhatsApp (with a 15-minute timeout). Passwords are typed directly by the user in the live browser and never saved on disk, database, or env.
*Strict caps:* at most 1 application per site per cycle, at most 10 per site per day, and at most 2 live view prompts per site per day. Any account warning or identity request permanently blocks that board and sends an alert. Jobs with external company ATS links (Greenhouse, Lever, Ashby) are automatically upgraded to direct ATS application. Unknown forms or closed listings are skipped.

## Replies (Gmail polling postponed)

Export emails from any Gmail connector as JSON (`[{id, from, subject, date, body}]`) or save them as
`.eml`, then `agent track import <file-or-folder>`. Only id, sender domain, date and status are kept,
never the body. Interviews, assessments and offers alert on WhatsApp at once. Applications you sent
yourself: `agent app add "<company>" "<title>" --url ... --date YYYY-MM-DD`; statuses by hand:
`agent app status <id> interview`.

## Commands

| Command | Does |
|---|---|
| `agent run` / `agent run-once <stage>` | Worker / one stage (`discover`, `score`, `digest`, `prepare-apply`, `heartbeat`, `weekly`, ...) |
| `agent doctor [--offline] [--send-test]` | Checks config, secrets, DB, redaction, browser, answers, Gemini, Mongo, WhatsApp |
| `agent jobs top / show <id> / stats` | Ranked jobs, score parts, filter counts |
| `agent app list / add / status` | Applications |
| `agent answers seed / set / list / pending` | Answer bank |
| `agent track import <path> / followups` | Reply import, follow-up check |
| `agent apply status / go-live <ats> / go-dry <ats> / resume` | Apply rollout |
| `agent boards status / login <site>` | Job boards live view login and limits |
| `agent stop` / `agent start` | Kill switch (local + Mongo) |
| `agent console` | Read-only console on 127.0.0.1:8765 |
| `agent review weekly / ab / refit / referrals` | Weekly report, resume A/B, re-fit weights (50+ outcomes), referral drafts from `config/contacts.csv` |
| `agent service install [--yes] / uninstall` | Start at Windows logon + hourly watchdog |
| `agent secrets ...`, `agent db ...`, `agent cloud init`, `agent llm usage`, `agent profile import` | Phase 0 tools |

## Monitor (`rohit-s-modern-profile`)

Added there (not deployed by this repo): `api/job-agent.ts` (12th and last Hobby function),
`lib/job-agent.ts`, page `/jobs`, the dead-man check inside `/api/cron?job=reminders` (GitHub Actions
calls it every minute; one "offline" alert after 45 min without a heartbeat, one "back online"), and
bot commands `/jobs`, `/jobstop`, `/jobstart <PIN>`. Before deploying, add `JOB_AGENT_PIN` to the
Vercel environment. The page asks for the existing `ADMIN_PASSWORD` and shows counts only.

## Production checklist

- [ ] `agent doctor` all OK (needs: secrets incl. `mongodb_uri`, `profile.yaml`, a LaTeX engine).
- [ ] `agent cloud init` done; `/jobs` monitor deployed with `JOB_AGENT_PIN` set on Vercel.
- [ ] `agent service install --yes`; PC set not to sleep on AC power.
- [ ] Message the RohitOS bot each morning (opens the free WhatsApp window for the digest).
- [ ] After the first dry runs: `agent answers pending` answered, screenshots checked in `agent console`.
- [ ] Only then: `apply.mode: live` + `agent apply go-live <ats>` per ATS.

Safety nets that are always on: single worker lock, crash restart, per-source circuit breakers,
Gemini quota pause, 25/day cap, 1 per company per 30 days, pause after 3 failed submits, kill switch
(`agent stop`, `STOP` file, `/jobstop`, `/jobs` page), nightly backups (14 kept), CI tests on GitHub.
When `search.yaml` changes, filtered jobs are re-checked; when the scoring formula changes, stored
scores are recomputed once without new Gemini calls.

## Development

```powershell
.venv\Scripts\python -m pytest          # offline; fake data, in-memory keychain, local form fixtures
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format .
```

Rules the tests enforce: only `agent/llm.py` imports `litellm`; nothing reads the portfolio's Mongo
`settings.whatsappToken`; WhatsApp code has no template sending; the dashboard snapshot can hold
nothing but allow-listed counts and codes; legal questions never use LLM answers; dry runs never click
submit.
