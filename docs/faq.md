# FAQ

**Does it send proposals on its own?** No. It prepares a preview. You click.

**Is it allowed by Upwork?** Check Upwork's terms yourself. This project is not
affiliated with Upwork or Anthropic, and you must follow both terms.

**What does it cost?** The server, plus Claude use. With an OAuth token it uses
your Claude plan limits; with an API key you pay per use. See the costs page.

**Can it break my profile?** Profile changes are previews with versions and
rollback. Dry-run is on by default.

**What are the limits?** Upwork and Claude rate limits apply. The bridge caps
calls per run. Tunable values live in `config/defaults.toml` and settings.

**Does it support many users?** No. One freelancer per install.

**Where is my data?** In the `/data` volume on your server.

**A task failed. Where do I look?** The runs page, then container logs
(`LOG_LEVEL=debug` for more).
