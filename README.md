# CORAL

Digital signage: a dashboard (FastAPI + SQLite) and Raspberry Pi players (Python agent + mpv).

- `dashboard/` FastAPI app, SQL migrations, templates, tests
- `player/` player agent, fallback asset, tests
- `tools/` `signagectl` (provision/revoke players)
- `deploy/` systemd units and Caddyfile
- `docs/` specification and installation guides (Portuguese)

Development:

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
