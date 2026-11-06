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
- Admin password hash and TOTP secret.

Nothing is sent to the project authors. Claude and Upwork get only what each
call needs. Keep `.env` and `/data` private and back them up safely.

## Safety design

Dry-run is on by default, Upwork reads are read-only, and every write needs a
preview and a click.
