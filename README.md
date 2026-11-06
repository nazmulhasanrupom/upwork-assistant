# Upwork Assistant

A self-hosted bot for one Upwork freelancer. It finds jobs that fit you, drafts
proposals, reviews your sent proposals, and tracks your profile. You stay in
control: it never changes anything on Upwork without your click.

## What it does

- Finds new jobs and scores how well they fit you.
- Drafts a proposal for the good ones.
- Reviews proposals after you send them.
- Builds your profile notes (USER.md) from your Upwork profile.
- Keeps a daily digest, weekly strategy, monthly plan, and cost page.
- Learns over time (memory, playbook, skills) with versions you can roll back.

## Safety model

- Dry-run is ON by default. Nothing is written to Upwork until you turn it off.
- Every write is a preview first. It runs only after you click confirm.
- Upwork access goes through a Claude bridge that can only use read-only tools.
- Secrets stay on your server in the data volume.

## Quick start

1. Get a Claude token (`CLAUDE_CODE_OAUTH_TOKEN`) or an `ANTHROPIC_API_KEY`.
2. Copy `.env.example` to `.env` and fill in the values.
3. Run `docker compose up -d --build`.
4. Open the panel and finish the `/setup` wizard.

Full guides: [Docker](docs/setup-docker.md), [Coolify](docs/setup-coolify.md).

## Settings (environment)

| Variable | Meaning |
|---|---|
| `PUBLIC_BASE_URL` | The URL you open the panel at |
| `SECRET_KEY` | Long random string for sessions |
| `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | How Claude is paid for |
| `TZ` | Your time zone, e.g. `Europe/Berlin` |
| `LOG_LEVEL` | `info` by default |
| `DATA_DIR` | Data folder, `/data` in the image |

## More

[How it works](docs/how-it-works.md) · [FAQ](docs/faq.md) ·
[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [License](LICENSE)

## Disclaimer

This project is not made by, endorsed by, or linked to Upwork or Anthropic.
Upwork and Claude are trademarks of their owners. You must follow Upwork's and
Anthropic's terms of service. You are responsible for what you send and how you
use this tool. It comes with no warranty (see the license).
