# Security

## Report a problem

Please do not open a public issue for a security bug. Use GitHub's private
vulnerability reporting on this repository ("Security" tab, "Report a
vulnerability"). We will reply as soon as we can.

## What the app stores

In the `/data` volume on your server only:

- SQLite database (jobs, drafts, previews, settings, run logs).
- Upwork and Gmail tokens, and the Claude config.
- Memory and skill files, with git history.
- Admin password hash, and the TOTP secret if you turn on 2-step login (optional, with a
  reminder after 7 days).
- `secret_key`: made on first start. It encrypts the saved tokens and signs sessions. Back it
  up with the volume; without it saved tokens cannot be read.
- Claude token and API key, if you paste them in the app: stored encrypted, never shown back.

Nothing is sent to the project authors. Claude and Upwork get only what each
call needs. Keep `/data` (and `.env`, if you use one) private and back them up safely.

## Safety design

Dry-run is on by default, Upwork reads are read-only, and every write needs a
preview and a click.
