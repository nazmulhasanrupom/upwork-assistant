# Setup with Coolify

1. In Coolify, add a new resource from your Git repo (Docker Compose build pack).
   Use `docker-compose.yml`. No host port is published; Coolify's proxy reaches
   the panel on container port 8000.
2. Set a domain, e.g. `https://your-domain.example`, on the `panel` service.
3. No environment variables are required. Optional: `TZ`, `LOG_LEVEL`, `PUBLIC_BASE_URL`,
   `SECRET_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` (env wins over Settings).
4. Deploy. Check that `/healthz` returns 200.
5. Open the domain, create your account, and follow the setup bar
   (Claude token, Upwork, and the rest).
6. Keep dry-run ON until you trust the drafts.

The `data` volume must be persistent. Back it up: it holds `secret_key`.
