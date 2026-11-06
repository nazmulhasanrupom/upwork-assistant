Size: big

# Upwork Assistant: Build Plan

## Goal

Build a self-hosted, open-source bot for one Upwork freelancer.
It finds MID/HIGH paying jobs every 2 hours, drafts proposals with Claude, and shows everything in an admin panel.
Nothing goes live on Upwork without a click in the panel.

The full design is in `SPEC.md` (same folder). **Read the sections named in your step.** SPEC.md is the source of truth for rules, numbers, fields, and file layout.

## Key facts (short form of SPEC.md)

- One Docker container: FastAPI + Jinja + HTMX panel, SQLite in `/data/app.db`, Claude Code CLI installed in the image.
- Python does all Upwork calls (official Upwork MCP at `https://mcp.upwork.com/mcp`, OAuth 2 + PKCE, public client, no secret). Claude gets no tools except Read/Write in a run folder.
- Each copy is its own OAuth client: it serves `${PUBLIC_BASE_URL}/oauth/client-metadata.json` (SPEC 13.1).
- Claude runs as `claude -p ... --output-format json` with `--max-turns` and `--max-budget-usd` (SPEC 7.2). Never use `--bare`.
- Model names come from settings. Defaults: `claude-opus-5-5` (think), `claude-sonnet-5-5` (write).
- Every Upwork write = preview, then `confirm_preview` only after a user click (SPEC 3.4, 14).
- Dry-run is ON by default. Nothing personal in the repo (SPEC 4.1).
- Env vars: only `PUBLIC_BASE_URL`, `SECRET_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, `TZ`, `LOG_LEVEL`, plus `DATA_DIR` (default `/data`).

## Rules for every builder

- Work in `/home/naz/Documents/upwork_assistant`. The repo root is this folder (not a subfolder).
- **Never read, print, copy, or commit `test_keys.txt` or `.env`.** They hold secrets.
- Do not run `git commit` or `git push`. Do not publish anything.
- No real Upwork calls and no real Claude calls in tests. Use fakes and fixtures in `tests/`.
- Keep all tunable values in settings (`config/defaults.toml` + `settings` table), never hard-coded.
- Local tests: `.venv/bin/pytest -q` (make the venv with `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`). Image tests: `docker build -t upwork-assistant:dev . && docker run --rm upwork-assistant:dev pytest -q`.
- Log to `progress.md` in the format from your agent rules (`STARTED:` / `DONE:` / `BLOCKED:` per step).

---

## Phase A: Skeleton (no secrets needed)

A1. Repo base files.
- Create: `.gitignore` (must list `.env`, `test_keys.txt`, `data/`, `.venv/`, `__pycache__/`, `*.db`), `.env.example` (SPEC 4.3, add `DATA_DIR=/data`), `requirements.txt` (fastapi, uvicorn[standard], jinja2, python-multipart, httpx, cryptography, pyotp, argon2-cffi, itsdangerous, croniter, tomli-w if needed), `requirements-dev.txt` (pytest, pytest-asyncio, respx, ruff), `pyproject.toml` (ruff + pytest config), short `CLAUDE.md` for developers (max 30 lines), `config/empty-mcp.json` = `{"mcpServers": {}}`.
- Run `git init` (no commit).
- Done when: files exist, `git status` works, `.venv` installs with no error.

A2. Config and database.
- `config/defaults.toml`: all defaults from SPEC 5, 7.2, 7.3, 8, 9, 11 (thresholds, schedules, models, budgets, fit minimum, dry_run=true, kill_switch=false, max MCP calls per run=40, 1 call/sec).
- `app/config.py`: reads env. Refuses to start if `SECRET_KEY` is missing, shorter than 32 chars, or equals `change-me-long-random-string`.
- `app/db.py` + `app/migrations/001_init.sql`: all tables in SPEC 12. Migration runner applies new files on start and records `schema_version`.
- `app/settings.py`: get/set with fallback to `defaults.toml`. Typed values (JSON in the `value` column).
- Done when: `tests/test_db.py`, `tests/test_settings.py`, `tests/test_config.py` pass (migrations idempotent, defaults load, bad SECRET_KEY refused).

A3. FastAPI app shell + Dockerfile.
- `app/main.py`: app factory, runs migrations on start, `GET /healthz` returns `{"ok": true}`, base Jinja layout in `app/templates/base.html` with HTMX from `app/static/` (vendor the file, no CDN).
- `Dockerfile` per SPEC 13.2, plus `supercronic` binary, non-root user `app` with home dir, `DATA_DIR=/data`, Claude installed for that user. `docker-compose.yml`: one service `panel` (uvicorn) and one service `scheduler` (supercronic running `python -m app.tasks.scheduler` every 5 min), shared `/data` volume, `env_file: .env`.
- Stub `app/tasks/scheduler.py` that just logs "tick".
- Done when: local pytest passes; `docker build -t upwork-assistant:dev .` works; `docker run --rm -e SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_hex(32))') -p 8000:8000 upwork-assistant:dev` serves `/healthz`; `docker run --rm upwork-assistant:dev claude --version` prints a version.

## Phase B: Claude runner

B1. `app/llm/runner.py`.
- Function `run_claude(task, prompt_file, input_obj, model, max_turns, max_budget_usd) -> dict`.
- Makes `/data/runs/<run_id>/`, writes `input.json`, runs the command in SPEC 7.2 with `cwd` = run folder, `--strict-mcp-config --mcp-config config/empty-mcp.json --tools "Read,Write"`.
- Parses JSON result, reads `output.json`, saves a `claude_calls` row (model, input/output tokens, `total_cost_usd`).
- Checks monthly cap before calling; raises `BudgetCapReached`. Detects 429 / session limit and raises `ClaudeRateLimited`.
- Job text is wrapped in `<untrusted_job_post>` tags in input (SPEC 14.3).
- Find the right headless effort setting: run `claude --help` in the image and record the result in progress.md. If there is no effort flag, leave effort as a setting that is passed only if supported.
- Tests use a fake `claude` script on PATH (`tests/fakes/claude`).
- Done when: `tests/test_runner.py` passes (success, cap reached, 429, bad JSON).

B2. Claude check command.
- `python -m app.tools.claude_check` runs `claude -p "say ok" --output-format json --max-turns 1` and prints OK or a clear error (no token, bad token, rate limit).
- Done when: test with fake passes. **Real check needs a human (decision D1).**

## Phase C: Panel login and security

C1. `app/auth.py`: one admin user. Password hashed with argon2. TOTP with pyotp (QR as data URI or the otpauth URL text). Signed session cookie (Secure, HttpOnly, SameSite=Strict). CSRF token on every POST. Lock after 5 wrong tries for 15 min.
C2. Temporary first-user page `/setup/admin` (only works when no admin exists). Login page. Logout. All pages except `/healthz`, `/login`, `/setup/admin`, `/oauth/*` need login.
- Done when: `tests/test_auth.py` passes (login, wrong TOTP, lockout, CSRF missing = 403, protected page redirects).

## Phase D: Upwork connection

D0. Setup code. `/setup/admin` must ask for a one-time setup code. On start, if no admin exists, the app makes a random code, stores its hash in the DB, and prints the code to the container log (`SETUP CODE: ...`). Without the right code, setup fails. Reason: the panel is on a public domain before the admin exists.
- Done when: test shows setup fails with no code or a wrong code, and works with the right code.

D1. (`app/crypto.py` exists from Phase C, reuse it.) `app/upwork/oauth.py`: store `state` and the PKCE verifier in the DB keyed by `state`, not in the session cookie (SameSite=Strict drops the cookie on the cross-site redirect from Upwork). `/oauth/callback` checks `state` from the DB (one use, 10 min max), saves tokens, then renders a small page that links to `/upwork` (no 302 that needs the cookie). PKCE S256, state check, authorize URL, code exchange, refresh, revoke, token storage in `/data/tokens.enc`. Endpoints from SPEC 3.6. `client_id` = `${PUBLIC_BASE_URL}/oauth/client-metadata.json`.
D2. `app/upwork/metadata.py`: serves `/oauth/client-metadata.json` (SPEC 13.1). Routes `/oauth/start`, `/oauth/callback`, `/upwork/disconnect`. Page `/upwork` shows status and token expiry.
D3. `app/upwork/client.py`: MCP client over Streamable HTTP (JSON-RPC: `initialize`, `notifications/initialized`, `tools/call`; handle `Mcp-Session-Id` header and SSE responses). Bearer token, auto refresh on 401, 1 call/sec, max calls per run from settings, backoff on 429/5xx. Counts calls into the `runs` row.
- Done when: `tests/test_oauth.py` and `tests/test_mcp_client.py` pass with respx mocks. **Real login needs a public HTTPS domain (decision D2).**

## Phase E: Jobs pipeline (no Claude)

E0. DONE by manager: real samples in `tests/fixtures/upwork/` (read its README.md for field facts). Note the MCP tools take `{action, org_uid, params:{...}}`.
E1. `app/upwork/jobs.py`: run the searches in SPEC 8.1, merge, dedupe against `jobs` table, cut each job to about 15 fields.
E2. `app/filters/tiers.py` (SPEC 8.2, 8.3) and `app/filters/flags.py` (SPEC 8.4). Pure functions. All thresholds from settings.
- Test case from SPEC 3.2: spend 32547, hours 1918 → rate about 17 → MID; 1-star review → flag.
E3. `app/tasks/daily.py` steps 1 to 7 of SPEC 6.2, plus Connects check and daily note (SPEC 9.1). Respects kill switch.
E4. Jobs page `/jobs` (cards, filters) and job detail page `/jobs/<id>`. Dashboard `/` with counts, Connects, last runs.
- Done when: tests pass on fixtures; daily task on fixtures fills the DB; pages render in a test client.

## Phase U: UI polish (added 2026-11-06, user request)

U1. Minimal, clean, easy-to-read UI for all existing pages (login, setup, dashboard, jobs, job detail, upwork). One hand-written `app/static/app.css` (no framework, no CDN, no build step). System font stack, good spacing, max-width content, clear type sizes. Light and dark mode via `prefers-color-scheme`. Top nav with the current page marked. Tier badges (HIGH / MID / LOW colors), flag chips, stat cards on the dashboard, job cards in a responsive grid, clear primary/secondary buttons, readable forms, visible focus states, works on a phone. Shared Jinja macros in `app/templates/_macros.html` (badge, card, stat, button, flash) so later phases reuse them.
- Done when: all tests pass; screenshots of each page (light + dark, desktop + phone width) taken with headless Chromium or Playwright from a local run with fixture data, saved in `/tmp` scratch and listed in the report.

## Phase F: Day-1 probes (human present)

F0. Deploy to Coolify (human + manager). Coolify needs the code from a git repo. Plan: a PRIVATE GitHub repo now (public only at D6). Human sets in Coolify: Dockerfile build pack, `/data` volume, domain, env vars (`PUBLIC_BASE_URL`, `SECRET_KEY`, Claude token), a Scheduled Task `python -m app.tasks.scheduler` every 5 min. Then human reads `SETUP CODE` in the log, makes the admin, clicks Connect Upwork.
- Done when: `https://<domain>/healthz` is ok and `/upwork` shows connected.

F1. With the real login: run the daily task for real. Measure rate limits. Try `get_rate_insights`. Make one proposal preview (no confirm) and test how long it stays valid. Update SPEC 5 table with facts.
- Done when: real MID/HIGH jobs show in the panel; facts written to SPEC.md section 5.

## Phase G: Claude thinking and writing

G1. Prompts: `prompts/judge.md`, `prompts/write_proposal.md` (SPEC 7, 8.5, 3.4 proposal rules, style from `/data/context/style.md`). Stable text first.
G2. Daily steps 8 to 10: batch 5 jobs per judge call, 3 per write call, fit minimum from settings. Save `judgements` and `drafts`. Never judge a job twice.
G3. `app/mail.py` (SMTP, on/off per event, dedupe via `emails` table) and `app/tasks/digest.py`. Events in SPEC 11.
G4. Runs and costs page `/runs`. Cost totals on dashboard. Cap warning.
- Done when: tests pass with fake claude and a fake SMTP server.

## Phase H: Prepare, Approve, invites, messages

H1. `app/upwork/writes.py`: preview create, `confirm_preview`. Check existing proposal/invite first. Dry-run blocks the confirm step and logs it.
H2. Job detail: Edit draft, Prepare (shows Connects cost, balance, boost, highlights, attachment question), Approve, Skip. Re-make expired previews.
H3. Approvals queue `/approvals`. Invites and messages page `/inbox` with draft replies and Upwork links.
- Done when: tests pass with mocks. **Sending one real proposal needs a human (decision D4).**

## Phase I: Weekly and monthly

I1. `app/tasks/weekly.py` (SPEC 9.2) with `prompts/weekly_strategy.md`, `prompts/write_profile.md`. Profile changes go to approvals with before/after.
I2. `app/tasks/monthly.py` (SPEC 9.3) with `prompts/monthly_plan.md`.
I3. Pages `/trends` (skill chart, median budget) and `/reports`.
I4. `app/tasks/scheduler.py`: real version. Reads cron + time zone from settings, runs due tasks, uses a lock so two runs never overlap.
- Done when: tests pass on fixtures.

## Phase J: Setup wizard and settings

J1. Wizard `/setup` (SPEC 4.2, all 10 steps). Replaces `/setup/admin`. Opens when no admin exists.
J2. `prompts/build_me.md`. Profile context page `/context` with Rebuild button.
J3. Settings page `/settings` for every value in `defaults.toml`. Dry-run, kill switch, export JSON, delete all data.
J4. Grep check: no personal names, emails, IDs, or domains in the repo.
- Done when: fresh `/data` + only `.env` filled → wizard runs end to end in tests.

## Phase K: Public release

K1. `README.md`, `docs/setup-coolify.md`, `docs/setup-docker.md`, `docs/how-it-works.md`, `docs/faq.md`, `SECURITY.md`, `CONTRIBUTING.md`, disclaimer (SPEC 4.6).
K2. `.github/workflows/`: ruff, pytest, Docker build, push to GHCR on tag.
K3. `LICENSE` (decision D5). First tag. **Push and publish only after decision D6.**
- Done when: docs complete, CI file valid (`actionlint` if available), a clean clone builds.

---

## Decision points (human must choose)

- D1 (any time before Phase F; does not block C to E): Put a real `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in `.env` for the real "say ok" test.
- D2: DECIDED 2026-11-06: Coolify domain. Before Phase F the human deploys on Coolify (public git or local push needed — see F0) and gives the domain.
- D3 (Phase F): OK to make a real proposal preview (no send) for the lifetime test.
- D4 (end of Phase H): OK to send one real proposal (costs Connects).
- D5 (Phase K): License: MIT or AGPL-3.0.
- D6 (Phase K): OK to create the public GitHub repo, push, and publish the image to GHCR.
