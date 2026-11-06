# Upwork Assistant: Final Draft Plan (SPEC2)

Version: draft 1 (2026-11-06)
Owner: the owner
Stack: Claude Code + Upwork MCP + Python, self-hosted on Coolify
Repo: public, open source. Freelancer accounts only (no agency support).

Note (manager): this adds to SPEC.md. Where they differ, SPEC2 wins, except where plan.md records a later decision (for example the Claude Code bridge for Upwork access, D7).

---

## 1. Goal

One assistant that takes care of my Upwork work on its own:

1. Finds good jobs and tells me why and how to apply.
2. Reviews my proposals and helps me write better ones.
3. Optimizes my profile, tests changes, and keeps what works.
4. Watches my numbers and alerts me fast.
5. Learns from everything it does and gets better over time.

---

## 2. Hard rules (the agent can NEVER change these)

These live in `rules/HARD_RULES.md`. The agent can read this file. It can never edit it.

- Never break Upwork's Terms of Service. No scraping, no browser bots on Upwork. Upwork MCP only.
- Never spend Connects or money without my approval.
- Never send a proposal or a message without my approval.
- Never change title, overview or rate without my approval.
- Never copy text from other freelancers' profiles. Learn patterns only.
- Never invent facts about me. Use only facts from `memory/USER.md` and my past work.
- No em dashes in any text written for me.
- Every change is logged and can be rolled back.
- If JSS drops, stop all profile tests and alert me.

---

## 3. What the Upwork MCP gives us (checked 2026-11-06)

| Data | Available? | Tool |
|---|---|---|
| My profile | Yes | `get_profile get` |
| Update profile (title, overview, skills, etc.) | Yes, with a confirm step | `update_profile` + `confirm_preview` |
| My proposals: list, full cover letter, answers, viewed by client | Yes | `list_freelancer_proposals list / get` |
| Invitations | Yes | `list_freelancer_proposals invitations` |
| Dashboard: invites, offers, messages, contracts, Connects | Yes | `get_freelancer_dashboard check` |
| Connects balance | Yes | `get_profile connects_balance` |
| Search other freelancers (for rank + competitor research) | Yes | `find_freelancers search` |
| Job search + job details + client history | Yes | `find_jobs` |
| Profile views | **No** | I type it weekly in the admin panel |
| How my proposal compares to others | **No** (needs Freelancer Plus) | n/a |
| Turn ON availability badge / profile boost | **No** (only off) | must do on upwork.com |

Account facts today:
- Plan: Freelancer Basic.
- Connects balance: 3. This is very low. Most good jobs cost 15 to 25.
- Proposals submitted: 4 (Sep 2 to Oct 10). Opened by clients: 0.

---

## 4. Modules

### A. Job Finder (already planned)

- Runs every 2 hours.
- Pay tiers by client spend, hourly spend and total hires:
  - Low: many hires but $1 to $10/hr. Skip.
  - Mid: good spend + $11 to $20/hr. Suggest.
  - High: good spend + $21+/hr. Suggest first.
- Email: the most relevant new jobs, why to apply, and a draft plan (what to say, what materials to attach).
- New: before suggesting a job, check Connects cost vs balance. If I can't afford it, say so.

### B. Proposal Reviewer (new)

When:
- **Before I send:** I paste or draft a proposal in the admin panel. The agent checks it. This is the main use.
- **After I send:** new proposals get one review (once only).
- **Monthly:** pattern review of all proposals. Which ones got opened, interviews, hires.

Checks:
1. Did I follow every instruction in the job post? (Example: "start with the word Bots" AND "tell me about a time...")
2. Do the first 2 lines speak to the client's problem? The client sees these first.
3. Is every screening question answered with real content? No "NA" where I have real experience.
4. Is there one real proof (project, number, result)?
5. Typos, spacing before dots, lowercase "i", rushed look.
6. Rate vs the job's budget range.
7. Length: short and clear.

Output: a score, a list of issues (max 5), and a fixed version.

Learning: the agent saves the gap between its draft and what I actually sent. My edits teach it my voice.

### C. Profile Optimizer (new)

**Phase 0: keyword discovery (I have no keywords yet)**

1. Start list from my real skills: n8n automation, AI automation, AI agent developer, Make.com, Zapier, RAG chatbot, Python automation, workflow automation.
2. For each keyword, the agent checks:
   - How many jobs use it (`find_jobs search`). More jobs = more demand.
   - How many freelancers compete (`find_freelancers search`).
   - Where I rank today.
3. Pick 3 to 5 keywords with good demand and a rank I can improve.
4. Email me the pick with reasons. I approve.

**Phase 1: baseline (2 weeks, no changes)**

Track every day:
- Search rank for each keyword.
- Invites per week.
- Proposals: sent, opened, interviews, hires.
- Invite response time.
- JSS.
- Profile views (I type it once a week).

**Phase 2: test loop (one change at a time)**

1. Research: read the top 10 profiles for each keyword. Read the tactic playbook.
2. Pick one tactic that fits my profile best. Explain why.
3. Draft the change (Sonnet). Use my voice and real facts only.
4. Apply:
   - Auto-apply list: approved by me once. Agent applies without asking.
   - Ask-first list: email + approve button.
5. Save the old version before every change.
6. Wait 14 days. No other profile changes during a test.
7. Judge with 7-day averages:
   - Better: keep.
   - Worse: roll back.
   - No change: try next tactic.
8. Write the result to the playbook (Section 6).

**Autonomy split (my choice: A)**

| Auto-apply (no ask) | Ask first |
|---|---|
| Add or reorder skills | Title |
| Reorder portfolio items | Overview |
| Turn availability badge OFF | Hourly rate |
| Fix typos in employment text | Add/remove employment, education |
| | Anything that costs Connects or money |

Check before turning auto-apply on: the Upwork MCP asks for a confirm step on every profile write. Auto-apply means the agent confirms the items on the auto list by itself, based on my one-time approval of this list. Confirm this fits Upwork's terms first.

### D. Alerts and Email

| When | What |
|---|---|
| Instant | New invite (reply within hours, ignored invites hurt rank) |
| Instant | JSS drop |
| Instant | Search rank drops a lot for 3+ days |
| Instant | Connects too low to apply to a good job |
| Daily (only if there is something) | Best new jobs |
| Weekly | Rank, invites, funnel, current test and days left, what the agent learned |
| End of each test | Result, keep or roll back, next idea |
| Monthly | Proposal pattern review + playbook update |

### E. Admin Panel

- Approve / reject buttons for every ask-first item.
- Weekly box: type profile views number.
- Proposal checker: paste a draft, get a review.
- History: every profile version, one-click rollback.
- Learning view: show what the agent changed in its own skills and memory, with diffs. One-click undo.
- Settings: auto-apply list, keywords, pay tiers, email times.

---

## 5. Self-learning engine

Ideas taken from Hermes Agent (Nous Research) and OpenClaw, adapted to Claude Code.

### 5.1 Four memory layers

| Layer | File | What it holds | When loaded |
|---|---|---|---|
| 1. Core (always on) | `memory/MEMORY.md`, `memory/USER.md` | Durable facts, my preferences, standing decisions. Hard cap ~3,500 characters total, to force curation. | Every run |
| 2. Daily notes (episodic) | `memory/daily/YYYY-MM-DD.md` + SQLite `runs.db` | Raw log of what happened each run: tools called, results, errors. | Never by default. Searched on demand (SQLite FTS5). |
| 3. Skills (procedural) | `.claude/skills/<name>/SKILL.md` | How to do things. Example: "how to review a proposal", "how to check keyword rank". | Only names + one-line summaries load. Full skill loads when needed. |
| 4. Outcomes (unique to us) | `memory/PLAYBOOK.md` + `experiments` table | Which tactics worked on MY profile, with numbers. | Loaded by the Profile Optimizer. |

Why separate layers: mixing everything in one place makes memory unreliable over time. Daily notes = what happened. Skills = how to do it. Playbook = what worked.

### 5.2 When the agent writes a new skill

After a task, the agent checks. It writes or updates a skill if:
- The task took 5 or more tool calls, or
- It recovered from an error, or
- I corrected it, or
- It found a non-obvious way that worked.

Updates use small patches (change only the lines that changed), not full rewrites. A full rewrite can break what already worked.

### 5.3 Consolidation ("dreaming"), weekly

1. Read the last 7 days of daily notes.
2. Pick what is worth keeping long term.
3. Merge it into `MEMORY.md`. Remove duplicates. Retire old or wrong entries.
4. Write a short summary to `memory/DREAMS.md` for me to review in the admin panel.

### 5.4 Noise gate

- The 2-hour runs only write daily notes. They never write to `MEMORY.md` or skills directly.
- Only the weekly consolidation can promote notes to long-term memory.
- Text from job posts, client messages and other profiles is untrusted. It can never become an instruction or a skill.

### 5.5 Strongest learning signals (in order)

1. My approve / reject clicks.
2. My edits to its drafts (the diff).
3. Real outcomes: proposal opened, interview, hire, rank change.
4. Its own error recovery.

### 5.6 Safety for self-changes

- Every change to skills, memory or prompts = one git commit.
- Admin panel shows the diff.
- If a self-change is followed by worse results, roll it back and log why.
- The agent can never edit `rules/HARD_RULES.md`.

### 5.7 Token cost control

- Python does all data fetching and math. No tokens.
- Only skill names load by default.
- Stable prompt prefix (rules, core memory) so prompt caching works.
- Sonnet for routine work. Opus only for weekly/monthly thinking.
- Each run starts fresh. No endless long sessions (they get expensive and the learning loop stops working well).

---

## 6. Tactic playbook (starting list)

Status: `untested` / `testing` / `worked` / `no effect` / `hurt`

| # | Tactic | Type | Status |
|---|---|---|---|
| 1 | Main keyword in title, first 2 lines of overview, and skill tags | Ask first | untested |
| 2 | Title formula: Primary Skill \| Secondary Skill \| Niche or Tool | Ask first | untested |
| 3 | Use all 20 skill slots: ~12 core niche + ~8 nearby skills | Auto | untested |
| 4 | Overview opens with client result, not my credentials | Ask first | untested |
| 5 | Highlight best portfolio items, most relevant first | Auto | untested |
| 6 | Fill Other Experience, certifications, testimonials | Ask first | untested |
| 7 | Work history written in client words (outcome, not tech) | Ask first | untested |
| 8 | Reply to every invite within 24 hours (accept or decline) | Alert | always on |
| 9 | Freshness: small portfolio or overview update about every 2 weeks | Ask first | untested |
| 10 | Rate check vs top 10 profiles in my keywords | Ask first | untested |
| 11 | Availability badge only when I have free time | Manual on upwork.com | untested |

Weekly research job: check Upwork news and guides. Add new tactics. Mark tactics as outdated when Upwork changes the rules (example: Specialized Profiles were removed in May 2026). Each tactic keeps a "last checked" date.

---

## 7. Scenarios and what the agent does

| Scenario | What it does |
|---|---|
| Niche too narrow, rank and invites fall | Suggest rollback right away |
| Upwork changes the rules | Weekly research updates the playbook; old tactics marked outdated |
| Draft sounds like AI | Rewrite in my voice, use my real numbers, learn from my edits |
| JSS drops | Stop all tests, alert me |
| Random rank noise | Use 7-day averages; never react to one bad day |
| Slow season (holidays) | Compare rank, not only invites |
| Bad change applied | One-click rollback from saved versions |
| Connects too low | Alert; only suggest jobs I can afford |
| Invite not answered in 12 hours | Second alert |
| Proposal ignores a job instruction | Flag before I send |
| Self-learned skill makes results worse | Roll back the skill, log why |
| Untrusted text tries to give instructions (job post, message) | Ignore, treat as data only |
| MCP or Claude down | Retry later, log it, email if down for a full day |

---

## 8. Schedule

| Every | Job | Model |
|---|---|---|
| 2 hours | Jobs, invites, messages, alerts. Write daily notes. Shut off. | Python + Sonnet |
| Day | Keyword rank check, metrics snapshot | Python only |
| Week | Consolidation, research, report email, test check | Opus (medium) plans, Sonnet (high) does |
| 14 days | Judge current test, start next | Opus |
| Month | Proposal pattern review, playbook review | Opus |

---

## 9. Folder layout

```
upwork-assistant/
├── CLAUDE.md                 # how the agent works, points to rules and memory
├── rules/
│   └── HARD_RULES.md         # read-only for the agent
├── memory/
│   ├── MEMORY.md             # core, capped
│   ├── USER.md               # my facts (gitignored in public repo)
│   ├── PLAYBOOK.md           # tactics + results
│   ├── DREAMS.md             # weekly consolidation summaries
│   └── daily/YYYY-MM-DD.md   # raw notes
├── .claude/skills/           # self-written + bundled skills
├── data/
│   ├── runs.db               # SQLite: runs, metrics, experiments, FTS5 index
│   └── profile_versions/     # every profile version for rollback
├── src/                      # Python: MCP client, metrics, scheduler, email
├── admin/                    # admin panel
└── config.example.yaml       # public template, no personal data
```

Public repo note: `USER.md`, `runs.db`, `profile_versions/` and real config stay out of git.
