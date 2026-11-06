# Setup with Docker

You need Docker with Compose, and a Claude token or API key.

1. Clone the repo.
2. `cp .env.example .env` and set:
   - `SECRET_KEY`: a long random string (`openssl rand -hex 32`).
   - `PUBLIC_BASE_URL`: e.g. `http://localhost:8000`.
   - `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`.
   - `TZ`: your time zone.
3. Start it on your laptop:
   `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build`
4. Open `http://localhost:8000`. The `/setup` wizard asks you to make an admin
   login and a TOTP code, then connect Upwork.
5. Leave dry-run ON until you trust the drafts.

Two services run: `panel` (web) and `scheduler` (cron tasks). Both share the
`data` volume at `/data`: database, tokens, run logs, memory, skills.

Back up the `data` volume. Update with `git pull` and `docker compose up -d --build`.

Run the tests in the image: `docker run --rm upwork-assistant:local pytest -q`.
