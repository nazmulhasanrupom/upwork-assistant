# Setup with Coolify

1. In Coolify, add a new resource from your Git repo (Docker Compose build pack).
   Use `docker-compose.yml`. No host port is published; Coolify's proxy reaches
   the panel on container port 8000.
2. Set a domain, e.g. `https://your-domain.example`, on the `panel` service.
3. Set environment variables:
   - `PUBLIC_BASE_URL=https://your-domain.example`
   - `SECRET_KEY` (long random string)
   - `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`
   - `TZ`, `LOG_LEVEL` (optional)
4. Deploy. Check that `/healthz` returns 200.
5. Open the domain and finish the `/setup` wizard (admin login, TOTP, Upwork).
6. Keep dry-run ON until you trust the drafts.

The `data` volume must be persistent. Back it up.
