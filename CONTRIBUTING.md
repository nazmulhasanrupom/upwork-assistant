# Contributing

Thanks for helping. Please keep changes small and tested.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

## Checks

    .venv/bin/pytest -q -n auto
    .venv/bin/ruff check .
    .venv/bin/ruff format --check .

## Rules

- No real Upwork or Claude calls in tests. Use fakes in `tests/`.
- Every Upwork write is preview, then confirm after a user click.
- Dry-run stays ON by default.
- Tunable values go in `config/defaults.toml` or the settings table.
- No personal data in the repo (names, emails, IDs, domains). Never commit `.env`.
- Keep English plain and short.

Open a pull request with a short description and the test result.
