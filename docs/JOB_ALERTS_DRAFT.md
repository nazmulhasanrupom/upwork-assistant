# Instant job alerts

Status: built.

## What it does

Every 15 minutes the app:

1. Gets the newest jobs from Upwork.
2. Removes jobs it has already seen.
3. Checks the new jobs against your profile and your filters.
4. For a good job:
   - It saves the job to /jobs.
   - It sends you one email: "Apply now".
   - It writes a draft, the same way the daily run does.

It only reads from Upwork. It never sends a proposal by itself. The existing auto-send countdown rules still apply as they are. They are off in dry-run and when the kill switch is on.

## Rules check

### Project rules (rules/HARD_RULES.md)

- Upwork MCP only, with no scraping. OK: it uses `find_jobs` reads through the bridge.
- It spends no Connects and sends nothing without approval. OK: it only reads and sends email.

### Upwork MCP rate limits

These numbers are from the Upwork help article "Rate limits for Upwork MCP" (support.upwork.com/hc/en-us/articles/56577997707923):

| Category | Per minute | Per hour | Per day |
|---|---|---|---|
| Search and discovery (job search, browsing job listings) | 15 | 150 | 750 |
| Other read operations | 20 | 250 | 750 |

- If you stay far above these limits, Upwork can slow down, suspend, or end your MCP access.
- The Upwork API and MCP Terms of Use (Section 7) apply too.

How this fits:

- Every 15 minutes is 96 runs a day.
- Each run makes 1 search call and at most 3 job-detail calls, so 4 calls at most.
- 96 x 4 = 384 calls a day at most, and fewer on quiet runs.
- That is about half of the 750 a day. It leaves room for the daily run, keywords, rank, and your own clicks.
- A daily cap in the app stops alerts before the limit. See the settings below.

### Things to watch

- **Claude cost.** Each run starts one headless Claude call to reach Upwork, even when there are no new jobs. That is about 96 small calls a day. The monthly spend cap still applies. Quiet hours lower the count.
- **Job detail cooldown.** Upwork may block job-detail opens for about a minute after several denials in a row (error `JOB_BROWSE_COOLDOWN`). When that happens, alerts stop opening jobs for that run.
- **Caching.** An older Upwork API help article ("What are the API requests limits?") says caching API answers for more than 24 hours is not allowed. The app already keeps jobs longer than that. This is not new, but it is worth checking against the current terms.

## How one run works

1. **Check if it may run.** Skip the run when:
   - Alerts are off.
   - The kill switch is on.
   - It is in quiet hours.
   - The daily call cap is reached.
   - Another task holds the lock.
2. **Search (1 call).** `find_jobs smart_search`, with `mode=most_recent`, `days_posted=1` and `limit=10`. This is Upwork's own "Most Recent" feed matched to your profile.
3. **Drop known jobs (no call).** Remove job ids that are already saved.
4. **Quick filters (no call).** Run the same prefilter and activity filter as the daily run. HIDE jobs are dropped.
5. **Details (at most 3 calls).** Run `find_jobs get` for the best remaining jobs, newest first.
6. **Score.** Use the same tier and red flags as the daily run. Then Claude judges the job (fit score), the same as the daily run.
7. **Alert.** A job is "good" when its tier is HIGH and its fit score is at least `job_alerts.fit_min`. For a good job:
   - Save it to /jobs as a candidate.
   - Send one email, once per job.
   - Write the draft, the same way as the daily run.
8. **Log.** Save one row in Runs: calls used, jobs seen, alerts sent and cost.

If Upwork answers "too many requests", alerts skip the next 2 runs.

## Settings (config/defaults.toml, section [job_alerts])

| Setting | Default | Meaning |
|---|---|---|
| enabled | false | Turns alerts on. You turn this on in Settings. |
| interval_minutes | 15 | How often a run happens. The scheduler already ticks every 5 minutes. |
| search_limit | 10 | Jobs asked from the "Most Recent" feed. |
| max_details_per_run | 3 | Job-detail calls per run, at most. |
| max_calls_per_day | 400 | Alerts stop for the day after this many calls. |
| fit_min | 75 | Lowest fit score that sends an email. |
| email_tiers | ["HIGH"] | Only these tiers send an email. |
| days_posted | 1 | Only jobs posted within this many days. |
| quiet_hours | "" | Hours with no runs, for example "22:00-07:00", in your time zone. |
| backoff_runs | 2 | Runs skipped after a "too many requests" answer. |

## The email

- **Subject:** "Apply now: <job title>"
- **Body:** fit score, budget, client spend and rating, the number of proposals, how long ago the job was posted, the main reasons from the judgement, and a link to the job page in the app.
- It is plain text, with one email per job and no repeats.

## Where it lives

- `app/tasks/job_alerts.py`: the run.
- It reuses `app/upwork/jobs.py` (the search and detail parsers), `app/filters/*`, `app/llm/judging.py` and `app/mail.py`.
- The scheduler (`app/tasks/scheduler.py`) starts it when it is due.
- The Settings page shows the new [job_alerts] section.
- Home shows the "Last alert check" time and today's call count.

## Choices made

1. **Daily run job search.** When alerts are on, the daily run skips its job search. Reports and the rest of the daily run stay.
2. **Email threshold.** Email only for tier HIGH with fit 75 or more (`job_alerts.fit_min = 75`, `email_tiers = ["HIGH"]`).
