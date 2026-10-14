# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

LinuxWatch: self-hosted Linux monitoring. Flat Python project (no package, no build step, no tests, no linter config). Python 3.12, FastAPI, psutil, Twisted, SQLite, one vanilla-JS `dashboard.html`.

## Commands

```bash
bash setup.sh                      # apt python3-venv, creates ./venv, pip installs fastapi uvicorn psutil twisted, prints 2 random API keys
cp config.example.ini config.ini   # paste keys; config.ini is gitignored

./venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000   # central server + dashboard at /, API docs at /docs
./venv/bin/python agent.py                                  # per-machine agent -> POST /metrics
./venv/bin/python monitor.py                                # standalone local collector (Twisted reactor)
```

No test suite, no lint command. Scripts import `config` from cwd-relative siblings, so run from repo root.

## Architecture

Two independent pipelines that share only `config.py` and, loosely, the dashboard:

1. **Distributed pipeline (agent -> server).** `agent.py` samples a small reading (cpu, ram, disk, swap, load_1, net deltas), POSTs to `server.py` with `X-API-Key`. Server maps key -> host name via `[api_keys]` and inserts into `host_metrics` in `server.db` (`SERVER_DB_PATH`). Agent appends to `buffer.jsonl` when server unreachable and flushes oldest-first on next success. Host "up" = last reading newer than `INTERVAL * 3`.
2. **Local pipeline (`monitor.py`).** Full metric set (per-core, processes, temp, connections, services, SSH failures via `ssh_watch.py`) written to `linuxwatch.db` (`DB_PATH`) using `schema.sql` (`server_metrics` + child tables). Does NOT talk to the server.

Bridge: `server.py` `GET /alerts` reads the `alerts` table straight out of the local `linuxwatch.db` (returns `[]` if file or table missing), so server and `monitor.py` must run on the same box for alert history to show.

`server.py` also serves `dashboard.html` at `/`, `/hosts`, `GET /metrics`, `/health`, and `/ws` (pushes `/hosts` every 5 s). Bottom of `server.py` calls `storage.attach(app)`, which registers the router (`/history`, `/prometheus`), runs `init_storage()` and starts a daemon thread (`cleanup_loop`) that rolls raw rows into `host_metrics_hourly` then deletes expired raw/hourly rows (`[storage]` config).

`detect.py` (z-score `/anomalies`, `/anomalies/now`, linear-fit disk `/forecast`, `[detect]` config) exposes `attach(app)` but **server.py never calls it** — routes are not live until wired in. Dashboard uses `apiSoft()` for these optional routes so missing ones return empty without triggering demo mode.

`config.py` is the single config loader (`configparser` over `config.ini`, every value has a fallback, so missing config still imports). Other modules import constants from it; `storage.py`/`detect.py` also read their own sections via the exported `config` object.

`dashboard.html`: self-contained SPA, `api()` falls back to built-in demo data on any fetch failure, `file:` protocol, claude.ai, or `*.vercel.app` host (`vercel.json` rewrites `/` to the HTML for the static demo deploy). Change demo behavior via `enterDemo()` and the `demo` object near the top of the script.

## Gotchas

- `alerts.py` `check_metric()` (Discord, cooldown, DB history, RESOLVED) is **not called** by `monitor.py`; `monitor.check_alerts()` only uses `stayed_high` and logs. Also `schema.sql` has no `alerts` table, which `alerts.py` and `/alerts` expect. Alert history/Discord path is incomplete.
- `alerts.py` state (`over_count`, `last_sent`, `active`) is in-memory module globals; lost on restart.
- `storage.py` creates `idx_host_ts` on `host_metrics`; table is created in `server.py` at import. Import order matters: `init_server_db()` must run before `storage.attach`.
- Code style: 3-space indentation in most modules (monitor.py mixes 3 and 4). Match surrounding file.
- `config.ini`, `*.db`, `buffer.jsonl`, logs are gitignored; never commit them.
