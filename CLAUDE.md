# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

No tests, linter, or build step exist (pytest/CI are roadmap items only).

```bash
bash setup.sh                      # apt python3-venv, creates ./venv, installs fastapi uvicorn psutil twisted; prints 2 API keys
cp config.example.ini config.ini   # then paste keys; config.ini is gitignored

./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000   # central server + dashboard + /docs
./venv/bin/python agent.py         # per-machine agent -> POST /metrics
./venv/bin/python monitor.py       # local collector + alert engine (Twisted reactor)
```

## Architecture

Two independent pipelines that only meet in `server.py`:

1. **Agent -> server (fleet metrics).** `agent.py` collects a small reading (cpu/ram/disk/swap/load_1/net_in/net_out) and POSTs it to `/metrics` with `X-API-Key`; failed sends are buffered to `buffer.jsonl` and flushed oldest-first via `POST /metrics/batch` (falls back to single posts if the server lacks the batch route). `server.py` maps key -> hostname via `[api_keys]` (`host_for_key`) and stores rows in `server.db` (`host_metrics`). `/ws` pushes a shared, briefly cached `/hosts` snapshot every 5 s.
2. **Local collector (`monitor.py`).** Richer metric set (per-core CPU, processes, temp, services, SSH fails via `ssh_watch.py`) taken in one 1 s sample window inside a worker thread, saved to a *separate* DB, `linuxwatch.db` (`schema.sql`, which also defines `alerts`). `alerts.check_metric` implements duration + cooldown + Discord webhook + fired/resolved history. `server.py` `GET /alerts` reads that DB directly (returns `[]` if file/table missing), so alerts only appear when `monitor.py` runs on the same box as the server.

`db.py` owns SQLite access: `connect(path)` is a context manager over a per-thread cached WAL connection (commit on exit); `init_server_db()` creates server tables. Use it instead of raw `sqlite3.connect`.

`config.py` is the single settings module: parses `config.ini` with `fallback=` defaults for everything, so scripts run with no config file. New tunables go there (or `config.getX(..., fallback=)` as `storage.py`/`detect.py` do for their own sections).

### Route modules

`storage.py` (`/history`, `/prometheus`, incremental hourly rollups + retention cleanup thread started from the FastAPI lifespan) and `detect.py` (`/anomalies`, `/anomalies/now`, `/forecast`) each expose an `APIRouter` that `server.py` mounts. Add new optional feature routes the same way.

### Dashboard

`dashboard.html` is a single-file vanilla JS + SVG SPA, served by `GET /` (and by the `vercel.json` rewrite for the static Vercel demo). If the API is unreachable it flips `state.demo = true` and renders generated sample data (`demo` object). Optional-feature routes (`/history`, `/anomalies`, `/forecast`) soft-fail to empty instead of triggering demo mode; keep that distinction when adding routes.

## Conventions

- Code uses 3-space indentation in `server.py`/`storage.py`/`detect.py`/`config.py`/`agent.py` (monitor.py uses 4); match the file you edit.
- `*.db`, `buffer.jsonl`, `*.log`, `config.ini`, `venv/` are gitignored; never commit them.
