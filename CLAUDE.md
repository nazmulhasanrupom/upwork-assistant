# Upwork Assistant (developer notes)

Self-hosted bot for one Upwork freelancer. Design: `SPEC.md`. Build steps: `plan.md`.

## Rules
- Code does the work. Claude thinks and writes. The only Upwork access is `app/upwork/bridge.py`: headless Claude makes exact READ-ONLY tool calls (`READ_TOOLS`) and Python reads the raw results. `client.py`/`oauth.py` are an unused direct path (Upwork allow list).
- Every write is preview, then `confirm_preview` after a user click. The one exception is the owner-approved auto-send path (`app/auto_send.py`): after its countdown ends it may confirm with a recorded system consent (never before the countdown ends, never in dry-run or with the kill switch on). No other code calls `confirm_preview` on its own.
- Dry-run is ON by default.
- Tunable values live in `config/defaults.toml` and the `settings` table. No hard-coded numbers.
- Nothing personal in the repo (names, emails, IDs, domains).
- Never read or commit `.env` or `test_keys.txt`.
- No real Upwork or Claude calls in tests. Use fakes in `tests/`.
- Never use `claude --bare` (it ignores `CLAUDE_CODE_OAUTH_TOKEN`).

## Commands
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`
- Test: `.venv/bin/pytest -q`
- Lint: `.venv/bin/ruff check .`
- Image: `docker build -t upwork-assistant:dev . && docker run --rm upwork-assistant:dev pytest -q`

## Layout
`app/` code, `app/migrations/` SQL, `prompts/` Claude prompts, `config/` defaults,
`tests/` tests, `/data` runtime volume (db, tokens, runs).
