# Website Outreach Pipeline

A Python pipeline that finds local-business website improvement opportunities, evaluates sites in a browser, builds redesign previews, and prepares outreach for review. A bundled fixture demo exercises the workflow without real prospect data or credentials.

## Engineering highlights

- A restartable orchestrator coordinates discovery, deduplication, crawling, scoring, and build stages.
- Playwright checks desktop/mobile rendering, layout overflow, request failures, and console errors.
- Deterministic scoring filters prospects before optional, bounded AI analysis.
- SQLAlchemy and SQLite persist campaign state, work results, suppression records, and provider usage.
- Browser QA gates generated previews; feature flags control discovery, publishing, sending, and reply processing.

## Stack

Python · SQLAlchemy / Alembic · SQLite · Playwright · Pydantic · Typer · Jinja2 · pytest

## Run the fixture demo

Requires Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m playwright install chromium
cp .env.example .env
python -m outreach init-db
python -m outreach run --vertical dentist --city Phoenix --state AZ --dry-run
```

Run tests with `pytest`. The pipeline is in `outreach/`, test websites in `outreach/fixtures/`, and schema changes in `migrations/`.

The example configuration leaves external discovery, AI calls, publishing, and email sending disabled. This portfolio copy excludes prospect databases, OAuth tokens, generated previews, and runtime artifacts. External integrations require your own accounts and configuration.
