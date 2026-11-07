# Setup with Docker

You need Docker with Compose and a Claude account. No `.env` file is needed.

1. Clone the repo.
2. Start it: `docker compose up -d`
   (on a laptop: `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build`).
3. Open `http://localhost:8000` and create your account (email and password).
4. Follow the setup bar at the top. The Claude step shows how to make a token:
   install Claude Code on your own computer, run `claude setup-token`, sign in
   in the browser, and paste the token.
5. Leave dry-run ON until you trust the drafts.

Optional: copy `.env.example` to `.env` to set `SECRET_KEY`, `PUBLIC_BASE_URL`,
`TZ`, or the Claude token yourself. 2-step login is optional; turn it on in Settings.

Two services run: `panel` (web) and `scheduler` (cron tasks). Both share the
`data` volume at `/data`: database, tokens, run logs, memory, skills.

Back up the `data` volume. It holds `secret_key`, which unlocks the saved tokens. Update with `git pull` and `docker compose up -d --build`.

Run the tests in the image: `docker run --rm upwork-assistant:local pytest -q`.
