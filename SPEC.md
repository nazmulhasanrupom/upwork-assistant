# Upwork Assistant: Draft Plan

Status: DRAFT v0.2 (6 Nov 2026)
Author: Nazmul Hasan Rupom
Type: public, open-source, self-hosted

---

## 1. Goal

An open-source bot that any Upwork freelancer can run on their own server. It:

- Finds new jobs every 2 hours.
- Keeps only mid and high paying jobs.
- Explains why each job fits, and drafts the proposal.
- Watches trends, and drafts profile changes.
- Makes weekly and monthly plans.
- Shows everything in an admin panel. The user approves with one click.
- Emails the user about urgent things, and about things it cannot do itself.

Main rule: **code does the work, Claude only thinks and writes.** Most runs use zero tokens.

---

## 2. Decisions made

| Topic | Decision |
|---|---|
| Distribution | Public GitHub repo. Each person runs their own copy |
| Users per copy | One freelancer per copy (single-user) |
| Agency accounts | Not supported |
| Hosting | Docker. Docs for Coolify and plain Docker Compose |
| Claude auth | User picks: subscription token or API key |
| Who talks to Upwork | Python, directly to the Upwork MCP |
| Thinking model (default) | Opus 5.5, medium effort |
| Writing model (default) | Sonnet 5.5, high effort |
| Approvals | Admin panel, Approve button |
| Daily cycle | Every 2 hours, then the task stops |
| Setup | First-run setup wizard in the panel. No code edits needed |

### 2.1 Why self-hosted, not one shared service

- Each user logs in to their own Upwork and their own Claude. Nobody shares accounts.
- Sharing one Claude subscription with other people would break Anthropic's terms. Self-hosting avoids this.
- Each user's Upwork data stays on their own server.
- No central server to secure or pay for.

---

## 3. Research findings

### 3.1 Upwork accounts

`list_accounts` returns every account a user has, with a role: `TALENT` (freelancer), `CLIENT`, or `FL_AGENCY`.
The app uses only `TALENT`. If a user has more than one, the setup wizard asks which one.

### 3.2 Data the MCP gives for each job

From `find_jobs` (search / smart_search):
- Job budget (hourly range or fixed), job type, experience level, duration.
- Proposals tier (for example "15 to 20", "50+").
- Client: country, rating, total reviews, total hires, total spent, payment verified.

From `find_jobs action=get` (one job, full detail):
- `client_record`: `spend_total`, `hours_total`, `hire_rate_percent`, `jobs_posted`, `contracts_total`, `contracts_active`, `feedback_score`.
- Latest client reviews with comments. These often name the client contact.
- Screening questions.
- `connects_cost` for this job, and the user's `connects_balance`.
- The user's plan (for example "Freelancer Basic").

Not given: "average hourly rate paid". We estimate it:

```
rate_paid = spend_total / hours_total
```

Warning: `spend_total` also includes fixed-price work. So the estimate can be a bit high. We also check the job's own budget.

Real example (5 Nov 2026, a Slack AI agents job):
- $32,547 / 1,918 hours = about $17/hr, so MID.
- Job budget $8 to $20/hr.
- One 1-star review from a past freelancer. The bot must flag this.

### 3.3 Things every user may hit

- **Low Connects.** In testing, the author's account had 3 Connects and a job cost 27. The bot must track Connects and warn.
- **Freelancer Basic plan** hides bid stats (what others bid). Freelancer Plus shows them. The app must work on both.

### 3.4 Write rules (very important)

- Every write (proposal, profile edit, portfolio, invite reply) works in 2 steps:
  1. The tool makes a **preview** with a `preview_id`.
  2. `confirm_preview` makes it live. This needs the user's real "yes".
- The "always allow" setting does **not** skip the preview step.
- These can only be done by the user on upwork.com (the tool returns a link):
  - Accept an offer.
  - Fund or approve a milestone.
  - Start a profile boost, or turn ON the availability badge.
  - Change a contract's weekly hour limit.
- Proposal rules from the tool (must follow):
  - Before `create`, check `list_freelancer_proposals` for an existing invite or proposal. If an invite exists, use `accept_invitation`.
  - Always show `connects_cost` and `connects_balance`.
  - Always ask about file attachments.
  - Offer highlights (portfolio, certificates, past jobs) from `get_profile action=list_highlights`.
  - Cover letter max 5,000 characters. Use only real history. Never invent clients, numbers, or praise.
  - Address the client by name only if a review or invite states the name.
  - Boost is a Connects bid. Only offer it if `boost.available` is true.

### 3.5 What the bot can change on a profile

Through `update_profile`: title, overview, video, availability, skills, employment, education, languages, other experience.
Through other tools: portfolio projects, Project Catalog items.

### 3.6 Upwork login (tested)

Probed `https://mcp.upwork.com/mcp` with no login. Result:

- Returns 401 with `resource_metadata` = `https://mcp.upwork.com/.well-known/oauth-protected-resource/mcp`.
- Auth server metadata:
  - `authorization_endpoint`: `https://www.upwork.com/ab/account-security/oauth2/authorize`
  - `token_endpoint`: `https://www.upwork.com/api/v3/oauth2/token`
  - `revocation_endpoint`: `https://www.upwork.com/api/v3/oauth2/token/revoke`
  - Grants: `authorization_code`, `refresh_token`, `client_credentials`
  - PKCE: `S256`
  - `token_endpoint_auth_methods_supported` includes `none` (public app, no secret)
  - `client_id_metadata_document_supported: true`
- `registration_endpoint` points to the normal user sign-up page. So no automatic app sign-up.

Meaning for a public repo: **every copy registers itself.** Each copy serves its own metadata file on its own domain. That URL is its app ID. No shared app ID, no secret in the repo, no approval from Upwork needed.

Requirement: Upwork must be able to fetch that file. So each copy needs a public HTTPS domain (or a tunnel, for example Cloudflare Tunnel).

### 3.7 Claude Code facts

- Headless mode: `claude -p "..."`. Runs, prints output, exits.
- Useful flags: `--model`, `--output-format json`, `--max-turns`, `--max-budget-usd`, `--strict-mcp-config`, `--tools`.
- Auth: `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`.
- `--bare` does NOT read `CLAUDE_CODE_OAUTH_TOKEN`. So we do not use `--bare`.
- claude.ai connectors do not load in headless mode. Not a problem: Claude needs no MCP here.
- If a subscription hits its session limit, the run fails with a 429 error. The code retries next cycle.

### 3.8 Token-saving repos (reviewed)

| Repo | Use it? | Why |
|---|---|---|
| `nadimtuhin/claude-token-optimizer` | Yes, as a dev tool | Keeps CLAUDE.md small, measures startup tokens |
| `SingggggYee/claude-usage-analyzer` | Optional | Token use by run and model. Our own cost log covers most of it |
| `rtk-ai/rtk` | No | Our scripts print short output anyway |
| `Madhan230205/token-reducer` | No | For code search in big codebases |
| `Balki01/code-nav-mcp` | No | For code navigation |
| `awesomo913/Claude-Token-Saver` | No | For code context |

Rule: do not ship third-party plugins or hooks inside the image. Each copy holds a user's Upwork login.

The biggest savings come from our design (section 7), not from repos.

---

## 4. Making it usable by anyone

### 4.1 Nothing personal in the repo

- No account IDs, domains, emails, names, or skills in code.
- All of that comes from env vars or the setup wizard, and lives in `/data`.
- `context/me.md` is not in the repo. The wizard builds it per user.

### 4.2 Setup wizard (first run)

Opens automatically when `/data` is empty.

1. **Admin account.** Create password. Scan TOTP code with an authenticator app.
2. **Claude check.** Runs `claude -p "say ok"`. Shows clear errors if auth fails.
3. **Connect Upwork.** OAuth login. Pick the freelancer account if there is more than one.
4. **Build profile context.** Python reads `get_profile` and `list_highlights`. Sonnet drafts `me.md` (skills, best projects, proof points). User edits and saves.
5. **Search keywords.** Pre-filled from the profile's skills. User edits.
6. **Pay rules.** Defaults from section 8. User edits.
7. **Writing style.** Pick defaults (plain English, short sentences, no em dashes) or write your own.
8. **Email.** Any SMTP server. Optional. Test button.
9. **Schedules and time zone.** Defaults shown. User edits.
10. **Start in dry-run mode.** On by default. User turns it off when happy.

### 4.3 Configuration

Env vars are only for secrets and the domain. Everything else is in Settings.

`.env.example`:
```
# Required
PUBLIC_BASE_URL=https://upwork.example.com
SECRET_KEY=change-me-long-random-string

# Claude: set ONE of these
CLAUDE_CODE_OAUTH_TOKEN=
ANTHROPIC_API_KEY=

# Optional
TZ=UTC
LOG_LEVEL=info
```

`SECRET_KEY` encrypts Upwork tokens and signs panel sessions.

### 4.4 Model choice

- Defaults: Opus 5.5 (thinking), Sonnet 5.5 (writing).
- Users can change models per task in Settings (for example, Sonnet for everything to save money).
- Model names are config, never hard-coded.

### 4.5 Deploy options (documented)

- **Coolify:** public Git repo app, Dockerfile build pack, `/data` volume, Scheduled Tasks.
- **Docker Compose:** one `docker-compose.yml` with the panel and a small scheduler (supercronic) in the same image.
- Both need a public HTTPS domain for the Upwork login (section 3.6).

### 4.6 Public repo files

- `README.md`: what it does, screenshots, quick start.
- `docs/setup-coolify.md`, `docs/setup-docker.md`
- `docs/how-it-works.md`: flow, tiers, token plan.
- `docs/faq.md`: Connects, plans, costs, limits.
- `SECURITY.md`: how to report issues. What the app stores.
- `CONTRIBUTING.md`
- `LICENSE` (to decide, see section 16).
- `.env.example`
- `.github/workflows/`: tests, lint, Docker image build.
- Disclaimer: not made by or linked to Upwork or Anthropic. Users must follow Upwork's and Anthropic's terms.

---

## 5. Best guesses (test on day 1)

| Unknown | Guess | How we handle it |
|---|---|---|
| Preview lifetime | Short, about 30 minutes | "Prepare" makes the preview only when the user opens the item. If Approve fails as expired, the panel makes a new one |
| Upwork rate limits | Unknown | Max 40 MCP calls per run, 1 call per second, back off on errors |
| "Good spending" default | $5,000+ total spent | Editable in Settings |
| `get_rate_insights` for freelancers | Maybe client-only | Try it. If it fails, use our own trend data |
| Effort flag in headless mode | Set per run | Confirm exact flag or setting on day 1 |
| Default time zone | `UTC` | Set in the wizard |

---

## 6. Architecture

### 6.1 One container

```
Container: upwork-assistant
  - Python 3.12
  - Claude Code (installed in Dockerfile)
  - FastAPI admin panel (always on)
  - Task scripts (started by Coolify Scheduled Tasks or supercronic)

Volume:  /data
  - app.db          SQLite: settings, jobs, drafts, approvals, runs, costs
  - tokens.enc      Upwork login tokens (encrypted with SECRET_KEY)
  - context/        me.md, style.md (built by the wizard)
  - notes/          daily, weekly, monthly notes (Markdown)
  - runs/<run_id>/  input and output files for each Claude call
  - logs/
```

The `/data` volume is a must. Without it, a redeploy deletes the Upwork login.

### 6.2 Data flow (every 2 hours)

```
scheduler → python -m app.tasks.daily
  1. Python: refresh Upwork login
  2. Python: get jobs, invites, messages, Connects       (0 tokens)
  3. Python: drop jobs already seen                      (0 tokens)
  4. Python: pre-filter on search data                   (0 tokens)
  5. Python: find_jobs get for survivors only            (0 tokens)
  6. Python: apply pay tier rule and red flags           (0 tokens)
  7. Nothing new and good? Write notes, exit.            (0 tokens)
  8. Opus: judge fit, "why apply", proposal plan         (small JSON in)
  9. Sonnet: cover letter, screening answers, materials  (Opus plan in)
 10. Python: save to panel, send email if urgent, write notes, exit
```

### 6.3 Why Claude needs no Upwork access

Python does all Upwork reads and writes. Claude only gets a file and returns a file. Benefits:
- Fewer tools loaded, so fewer tokens.
- Job posts can contain tricks aimed at AI (prompt injection). Claude has no tools that can act on Upwork, so a trick can do nothing.

---

## 7. Model split and token plan

### 7.1 Who does what

| Task | Who |
|---|---|
| Fetch, filter, dedupe, tiers, emails, notes, approvals | Python |
| Fit score, why apply, proposal plan | Thinking model (Opus) |
| Cover letter, screening answers, materials list | Writing model (Sonnet) |
| Profile text drafts, wizard `me.md` draft | Writing model (Sonnet) |
| Weekly trends, profile strategy | Thinking model (Opus) |
| Monthly report and plan | Thinking model (Opus) |

Python runs the two models as two separate calls. No subagents.

### 7.2 Claude calls (draft commands)

Thinking call:
```bash
claude -p "$(cat /app/prompts/judge.md)" \
  --model "$THINK_MODEL" \
  --output-format json \
  --strict-mcp-config --mcp-config /app/config/empty-mcp.json \
  --tools "Read,Write" \
  --max-turns 6 \
  --max-budget-usd 1.00
```

Writing call:
```bash
claude -p "$(cat /app/prompts/write_proposal.md)" \
  --model "$WRITE_MODEL" \
  --output-format json \
  --strict-mcp-config --mcp-config /app/config/empty-mcp.json \
  --tools "Read,Write" \
  --max-turns 6 \
  --max-budget-usd 0.50
```

`THINK_MODEL` and `WRITE_MODEL` come from Settings. Defaults: `claude-opus-5-5`, `claude-sonnet-5-5`.
Each call runs inside `/data/runs/<run_id>/`. It reads `input.json` and writes `output.json`.

### 7.3 Token rules

1. No Claude call if nothing new passed the filter.
2. Never send raw MCP output to Claude. Python cuts each job to about 15 fields.
3. Never judge the same job twice (store job IDs).
4. Max 5 jobs per thinking call. Max 3 proposals per writing call.
5. Short CLAUDE.md. Rules and profile facts live in small files, read only when needed.
6. Stable text (rules, profile) goes first in each prompt, so caching works.
7. Weekly and monthly tasks read short notes, never raw logs.
8. Every call has `--max-turns` and `--max-budget-usd`.
9. Save `usage` and cost from each JSON result. Show it in the panel.
10. Monthly budget cap in Settings. When reached, Claude calls stop and the panel warns.

---

## 8. Job filter rules (defaults, all editable)

### 8.1 Search queries (each run)

- `smart_search` mode `most_recent`, `days_posted=1`
- `smart_search` mode `best_match`
- `search` with `title` for each keyword from Settings (built from the user's skills)
- Always `verified_payment_only=true`

### 8.2 Step 1: pre-filter (search data only, no extra calls)

Drop the job if:
- Payment not verified.
- Client `total_spent` < $5,000 (good spending threshold).
- Hourly job with budget max < $11.
- Fixed job with budget < $300.
- Already seen.

### 8.3 Step 2: tier rule (after `find_jobs get`)

```
rate_paid = spend_total / hours_total      (if hours_total > 0)

LOW   if rate_paid <= 10
      or (total_hires is high and rate_paid <= 10)
MID   if spend_total >= 5000 and 11 <= rate_paid <= 20
HIGH  if spend_total >= 5000 and rate_paid >= 21
```

Extra checks:
- If `hours_total` = 0 (fixed-price only client): use job budget only. Mark tier as "estimated".
- Hourly job budget max must also be >= $11.
- Keep only MID and HIGH.

### 8.4 Red flags (shown, not auto-dropped)

- Client `feedback_score` < 4.5 with 3+ reviews.
- Any review with score 1 or 2 in the latest reviews.
- `hire_rate_percent` < 40.
- Proposals tier "50+".
- `connects_cost` > `connects_balance`.

### 8.5 Fit score (thinking model)

Input: the cut-down job, `me.md`, the user's best past work. Output:

```json
{
  "job_id": "2118413555956864697",
  "fit_score": 0-100,
  "tier": "MID",
  "why_apply": "3 short reasons",
  "risks": "short list",
  "proposal_plan": "angle, proof to show, questions to ask",
  "materials": ["portfolio item X", "past job Y"],
  "suggested_rate": 25
}
```

Only jobs with `fit_score` >= 65 go to the writing model (editable).

---

## 9. Schedules (defaults, in the user's time zone)

| Task | Cron | What it does |
|---|---|---|
| Daily cycle | `0 */2 * * *` | Jobs, invites, messages, Connects, drafts, notes |
| Daily digest email | `0 21 * * *` | One summary email of the day |
| Weekly | `0 6 * * 1` | Trends, proposal results, profile change drafts |
| Monthly | `0 7 1 * *` | Earnings, full profile audit, next month plan |

The scheduler runs every 5 minutes and reads the schedules from Settings. So users change times in the panel, not in Coolify.

### 9.1 Daily cycle (every 2 hours)

- Everything in section 6.2.
- Check invites. Invites go to the top of the panel and trigger an email.
- Check unread client messages. Trigger an email.
- Check Connects. Warn if below the cost of the top job.
- Write a few lines to `/data/notes/daily/YYYY-MM-DD.md`:
  - Jobs seen, kept, tier counts.
  - Top skills seen in all jobs (kept or not).
  - Anything odd.

### 9.2 Weekly

Python first (zero tokens):
- Count skills across all jobs seen this week (even dropped ones).
- Median budget per skill. Change versus last week.
- Proposal results: sent, viewed, replied, hired (from `list_freelancer_proposals`).
- Try `get_rate_insights` for the user's main skills.

Then the thinking model:
- Reads the weekly stats and daily notes.
- Writes the trend report.
- Proposes profile changes: title, overview, skills, portfolio, catalog ideas.

Then the writing model:
- Writes the final profile text.

Result: profile changes wait in the Approvals queue.

### 9.3 Monthly

- Python: earnings and Connects spend (`get_freelancer_financials`), win rate, hours.
- Thinking model: full profile audit and next month's plan (goals, skills to add, rate advice).
- Email the report. Save it in the panel.

---

## 10. Admin panel

Stack: FastAPI, Jinja templates, HTMX, SQLite. One user per copy.

### 10.1 Pages

**Setup wizard** (first run only, section 4.2)

**Dashboard**
- Connects balance, with a warning color when low.
- Last run time and status of each task.
- Counts: new MID jobs, new HIGH jobs, items waiting for approval.
- Token cost today, this week, this month, and the monthly cap.
- Alerts (login expired, run failed, session limit hit, cap reached).

**Jobs**
- Card per job: title, tier badge, `rate_paid`, client spend, hires, rating, red flags, fit score, why apply.
- Job detail: full draft cover letter, screening answers, materials list, suggested rate.
- Buttons: `Edit draft`, `Prepare`, `Approve`, `Skip`, `Save to Upwork`.
- Filters: tier, fit score, date, status.

**Approvals queue**
- Everything waiting for a "yes": proposals, profile changes, invite replies.
- Profile changes show a before and after view.
- Each item: `Approve`, `Edit`, `Reject`.

**Invites and messages**
- New invites (with a drafted reply).
- Unread client messages (with a link to Upwork).

**Trends**
- Skill demand chart over weeks.
- Median budget per skill.
- Latest weekly report.

**Plans and reports**
- Weekly and monthly reports, with history.

**Profile context**
- Edit `me.md` and `style.md`.
- `Rebuild from Upwork` button.

**Runs and costs**
- Every run: start, end, status, MCP calls used, Claude tokens, cost.
- Link to the run's log.

**Settings**
- Pay rules: good spending, tier limits, fixed-price minimum, fit score minimum.
- Search keywords.
- Schedules and time zone.
- Models and effort per task. Monthly cost cap.
- Email server and on/off per event type.
- **Dry-run mode**: everything runs, but nothing is sent to Upwork.
- **Kill switch**: stops all tasks at once.
- Export data (JSON) and delete all data.

**Upwork connection**
- `Connect Upwork` / `Reconnect` button.
- Login status and token expiry.
- `Disconnect` (revokes the token).

### 10.2 Proposal approve flow

1. Open a job. Edit the draft if needed.
2. Click **Prepare**.
   - Python checks for an existing invite or proposal on this job.
   - Python creates the preview with `manage_proposals create` (or `accept_invitation`).
   - Panel shows: Connects cost, balance, boost options (if any), unmet client preferences.
   - Panel asks: attach files? Pick highlights? (Upwork's rules require this.)
3. Click **Approve**.
   - Python calls `confirm_preview type=proposal`.
   - Zero Claude tokens.
4. Status moves to "Sent". The weekly task tracks the result.

If the preview expired, the panel makes a new one and asks again.

### 10.3 Profile change flow

1. Weekly task puts the change in Approvals (before and after).
2. Click **Approve**.
3. Python makes the preview (`update_profile ...`), then calls `confirm_preview`.

### 10.4 Panel security

- HTTPS (Coolify Traefik, or any reverse proxy).
- Password plus TOTP code.
- Login rate limit. Lock after 5 wrong tries.
- Session cookie: Secure, HttpOnly, SameSite=Strict.
- CSRF token on every button that writes.
- Upwork tokens encrypted at rest with `SECRET_KEY`.
- App refuses to start if `SECRET_KEY` is missing or still the example value.
- Optional: Tailscale or Cloudflare Access in front.

---

## 11. Emails

Any SMTP server (Gmail app password, Resend, Mailgun, and so on). Optional.

| Event | When |
|---|---|
| New HIGH job, fit 65+ | Right away |
| New invite | Right away |
| Unread client message | Right away |
| Connects too low for top job | Right away, max once a day |
| Login expired / run failed 3 times | Right away |
| Thing the bot cannot do (see 3.4) | Right away, with the Upwork link |
| Daily digest (MID jobs, counts, cost) | Daily, default 21:00 |
| Weekly report | Weekly |
| Monthly report | Monthly |

Each job email has: title, tier, why apply, risks, and an `Open in panel` link. The full draft lives in the panel.

---

## 12. Data model (SQLite)

```
settings(key, value)
jobs(
  job_id PK, first_seen, title, url, job_type, budget_min, budget_max,
  client_spend, client_hours, client_hires, client_rating, client_reviews,
  rate_paid, tier, tier_estimated, proposals_tier, connects_cost,
  red_flags JSON, status  -- seen|dropped|candidate|judged|drafted|prepared|sent|skipped
)
skills_seen(job_id, skill, week)
judgements(job_id, run_id, fit_score, why_apply, risks, plan JSON, suggested_rate)
drafts(id, job_id, cover_letter, answers JSON, materials JSON, edited_by_user BOOL)
approvals(id, kind, ref_id, payload JSON, preview_id, preview_created, status, decided_at)
invites(invite_id, job_id, received, status, draft_reply)
messages(room_id, last_seen_story, unread, notified)
runs(run_id, task, started, ended, status, mcp_calls, error)
claude_calls(id, run_id, model, input_tokens, output_tokens, cost_usd)
emails(id, event, sent_at, ref_id)
schema_version(version)
```

Migrations run on start, so upgrades keep user data.

---

## 13. Repo layout

```
upwork-assistant/
  README.md
  LICENSE
  SECURITY.md
  CONTRIBUTING.md
  .env.example
  Dockerfile
  docker-compose.yml
  CLAUDE.md                 (short, for people developing the repo)
  .github/workflows/        tests, lint, image build
  docs/
    setup-coolify.md
    setup-docker.md
    how-it-works.md
    faq.md
  app/
    main.py                 FastAPI panel
    auth.py                 panel login, TOTP
    setup/                  first-run wizard
    upwork/
      oauth.py              PKCE login, refresh, token storage
      metadata.py           serves /oauth/client-metadata.json from PUBLIC_BASE_URL
      client.py             MCP client: tools/call, rate limit, retries
      jobs.py               fetch and cut-down
      writes.py             previews and confirm_preview
    filters/
      tiers.py              pay tier rule
      flags.py              red flags
    llm/
      runner.py             runs claude -p, saves usage and cost
    tasks/
      scheduler.py          reads schedules from settings
      daily.py
      weekly.py
      monthly.py
      digest.py
    mail.py
    db.py
    migrations/
    templates/
    static/
  prompts/
    judge.md                thinking: fit score and plan
    write_proposal.md       writing: cover letter and answers
    weekly_strategy.md      thinking
    write_profile.md        writing
    monthly_plan.md         thinking
    build_me.md             writing: wizard profile context
  config/
    empty-mcp.json          {"mcpServers": {}}
    defaults.toml           default settings
  tests/
```

### 13.1 OAuth client metadata (generated per copy)

Served at `${PUBLIC_BASE_URL}/oauth/client-metadata.json`:

```json
{
  "client_id": "${PUBLIC_BASE_URL}/oauth/client-metadata.json",
  "client_name": "Upwork Assistant (self-hosted)",
  "redirect_uris": ["${PUBLIC_BASE_URL}/oauth/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

### 13.2 Dockerfile (outline)

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME ["/data"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Scheduler (one task, every 5 minutes, it decides what is due):
```
python -m app.tasks.scheduler
```

---

## 14. Safety rules

1. **Nothing goes live without a click.** The bot never calls `confirm_preview` on its own.
2. **Dry-run is on by default** for new installs.
3. **Job text is untrusted.** Claude gets it marked as untrusted data. Claude has no Upwork tools, so a trick in a job post cannot act.
4. **No invented facts.** Drafts use only `me.md` and real Upwork history.
5. **One user per copy.** Docs tell users never to share their copy or their Claude login with others.
6. **Spend caps** on every Claude call, plus a monthly cap.
7. **Terms:** only the official Upwork MCP. No scraping. No browser automation on Upwork.
8. **No telemetry.** The app sends no data to the author.

---

## 15. Build phases

| Phase | What | Done when |
|---|---|---|
| 0 | Dockerfile, Coolify app, `/data` volume, Claude Code login test | `claude -p "say ok"` works in the container |
| 1 | Panel login, Upwork OAuth, fetch jobs, tier filter, Jobs page (no Claude) | Real MID/HIGH jobs show in the panel |
| 1b | Day-1 tests: preview lifetime, rate limits, `get_rate_insights` | Guesses in section 5 replaced with facts |
| 2 | Thinking and writing calls, cost log, emails | Drafts appear, emails arrive |
| 3 | Prepare and Approve, invites, messages | A real proposal sent from the panel |
| 4 | Weekly and monthly tasks, Trends page, profile approvals | First weekly report and profile drafts |
| 5 | Setup wizard, settings for everything, no personal data in code | A fresh install works with only `.env` filled |
| 6 | Public release: docs, CI, image on GHCR, first tagged version | A second person installs it from the README alone |

Build it for the author first (phases 0 to 4), but keep all values in Settings from day 1. This makes phase 5 small.

---

## 16. Open questions and risks

- **License:** MIT or AGPL-3.0 (to decide).
- Preview lifetime (guess: about 30 min).
- Upwork rate limits (guess: 40 calls per run).
- Refresh token lifetime. If it expires, the user logs in again. Panel and email tell them.
- Subscription session limits can stop a run. The bot retries next cycle.
- Upwork may change MCP tool names or fields. Pin the expected shape in tests and fail with a clear message.
- Users need a public HTTPS domain for the login.
- Users need enough Connects for this to pay off.

---

## 17. Later ideas

- 30-minute code-only check for HIGH jobs.
- Learn from wins: compare sent drafts with replies and hires.
- Project Catalog ideas from trend data.
- Milestone reminders and submit drafts for active contracts.
- Meeting recap summaries.
- More languages for the panel.
