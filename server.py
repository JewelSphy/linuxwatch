import os
import json
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from typing import Optional
from pydantic import BaseModel
from config import SERVER_DB_PATH, API_KEYS, INTERVAL, DB_PATH, BASE_DIR
from db import connect, init_server_db
import storage
import detect
import fleet_alerts


MAX_BATCH = 1000
HOSTS_CACHE_SECONDS = 2

INSERT_READING = """
    INSERT INTO host_metrics (host, ts, cpu, ram, disk, swap, load_1, net_in, net_out)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@asynccontextmanager
async def lifespan(app):
   stop = storage.start_cleanup()
   stop_watch = fleet_alerts.start_offline_watch()
   yield
   stop.set()
   stop_watch.set()


app = FastAPI(title="LinuxWatch Server", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(storage.router)
app.include_router(detect.router)
app.include_router(fleet_alerts.router)

init_server_db()


class Reading(BaseModel):
   ts: float
   cpu: float
   ram: float
   disk: float
   swap: float
   load_1: float
   net_in: int
   net_out: int
   details: Optional[dict] = None   # live snapshot (cores, processes, services...); never buffered


class Batch(BaseModel):
   readings: list[Reading]


def host_for_key(api_key):
   host = API_KEYS.get(api_key)
   if host is None:
      raise HTTPException(status_code=401, detail="Invalid API key")
   return host


def reading_row(host, r):
   return (host, r.ts, r.cpu, r.ram, r.disk, r.swap, r.load_1, r.net_in, r.net_out)


@app.get("/")
def dashboard():
   return FileResponse(os.path.join(BASE_DIR, "dashboard.html"))


@app.get("/health")
def health():
   return {"message": "LinuxWatch server is running"}


@app.post("/metrics")
def receive_metrics(reading: Reading, x_api_key: str = Header(...)):
   host = host_for_key(x_api_key)
   with connect(SERVER_DB_PATH) as conn:
      conn.execute(INSERT_READING, reading_row(host, reading))
      if reading.details:
         conn.execute(
            "INSERT OR REPLACE INTO host_details (host, ts, data) VALUES (?, ?, ?)",
            (host, reading.ts, json.dumps(reading.details)))
   fleet_alerts.evaluate(host, reading.model_dump(exclude={"details"}), reading.ts)
   return {"ok": True, "host": host}


@app.post("/metrics/batch")
def receive_batch(batch: Batch, x_api_key: str = Header(...)):
   host = host_for_key(x_api_key)
   if len(batch.readings) > MAX_BATCH:
      raise HTTPException(status_code=413, detail=f"At most {MAX_BATCH} readings per batch")
   with connect(SERVER_DB_PATH) as conn:
      conn.executemany(INSERT_READING, [reading_row(host, r) for r in batch.readings])
   return {"ok": True, "host": host, "stored": len(batch.readings)}


def list_hosts():
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT host, MAX(ts), cpu, ram, disk
          FROM host_metrics
          GROUP BY host
          ORDER BY host
      """).fetchall()

   now = time.time()
   open_alerts = fleet_alerts.open_counts()
   return [
      {
         "host": host,
         "status": "up" if now - last_ts < INTERVAL * 3 else "down",
         "last_seen_seconds_ago": round(now - last_ts),
         "cpu": cpu,
         "ram": ram,
         "disk": disk,
         "open_alerts": open_alerts.get(host, {}).get("count", 0),
         "alert_level": open_alerts.get(host, {}).get("level"),
      }
      for host, last_ts, cpu, ram, disk in rows
   ]


@app.get("/hosts")
def hosts():
   return list_hosts()


@app.get("/metrics")
def get_metrics(host: str, minutes: int = 60):
   since = time.time() - minutes * 60
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT ts, cpu, ram, disk, swap, load_1, net_in, net_out
          FROM host_metrics
          WHERE host = ? AND ts >= ?
          ORDER BY ts
      """, (host, since)).fetchall()
   return [dict(row) for row in rows]


@app.get("/hosts/{host}/details")
def host_details(host: str):
   with connect(SERVER_DB_PATH) as conn:
      row = conn.execute("SELECT ts, data FROM host_details WHERE host = ?", (host,)).fetchone()
   if row is None:
      raise HTTPException(status_code=404, detail="No details yet (agent too old or not reporting)")
   return dict(json.loads(row["data"]), host=host, ts=row["ts"], age=round(time.time() - row["ts"]))


@app.get("/alerts")
def list_alerts(limit: int = 50, host: Optional[str] = None):
   """Fleet alerts from every agent, plus monitor.py's local alerts (shown as host "local")."""
   rows = fleet_alerts.fleet_alert_rows(limit, host)
   if host in (None, "local"):
      rows += monitor_alerts(limit)
   rows.sort(key=lambda a: a["fired_at"] or "", reverse=True)
   return rows[:limit]


def monitor_alerts(limit):
   if not os.path.exists(DB_PATH):
      return []
   with connect(DB_PATH) as conn:
      try:
         rows = conn.execute("""
             SELECT id, name, level, value, threshold, fired_at, resolved_at, resolved_value
             FROM alerts
             ORDER BY id DESC
             LIMIT ?
         """, (limit,)).fetchall()
      except Exception:
         # no alerts table yet (monitor.py has not created one)
         return []
   return [dict(row, id="m" + str(row["id"]), source="monitor", host="local", unit="%", acked_at=None, muted_until=None) for row in rows]


# one query feeds every websocket client instead of one query per client per push
_hosts_cache = {"at": 0.0, "data": []}


async def hosts_snapshot():
   if time.monotonic() - _hosts_cache["at"] > HOSTS_CACHE_SECONDS:
      _hosts_cache["data"] = await run_in_threadpool(list_hosts)
      _hosts_cache["at"] = time.monotonic()
   return _hosts_cache["data"]


@app.websocket("/ws")
async def live_hosts(ws: WebSocket):
   await ws.accept()
   try:
      while True:
         await ws.send_json(await hosts_snapshot())
         await asyncio.sleep(5)
   except (WebSocketDisconnect, RuntimeError):
      pass
