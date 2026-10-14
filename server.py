import os
import time
import asyncio
import sqlite3
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
from config import SERVER_DB_PATH, API_KEYS, INTERVAL, DB_PATH, BASE_DIR


app = FastAPI(title="LinuxWatch Server")


def init_server_db():
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()
   cursor.executescript("""
       CREATE TABLE IF NOT EXISTS host_metrics (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           host TEXT NOT NULL,
           ts REAL NOT NULL,
           cpu REAL NOT NULL,
           ram REAL NOT NULL,
           disk REAL NOT NULL,
           swap REAL NOT NULL,
           load_1 REAL NOT NULL,
           net_in INTEGER NOT NULL,
           net_out INTEGER NOT NULL
       );
       CREATE INDEX IF NOT EXISTS idx_host_ts ON host_metrics (host, ts);
   """)
   conn.commit()
   conn.close()


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


def host_for_key(api_key):
   host = API_KEYS.get(api_key)
   if host is None:
      raise HTTPException(status_code=401, detail="Invalid API key")
   return host


@app.get("/")
def dashboard():
   return FileResponse(os.path.join(BASE_DIR, "dashboard.html"))


@app.get("/health")
def health():
   return {"message": "LinuxWatch server is running"}


@app.post("/metrics")
def receive_metrics(reading: Reading, x_api_key: str = Header(...)):
   host = host_for_key(x_api_key)
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()
   cursor.execute("""
       INSERT INTO host_metrics (host, ts, cpu, ram, disk, swap, load_1, net_in, net_out)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
   """, (host, reading.ts, reading.cpu, reading.ram, reading.disk, reading.swap,
         reading.load_1, reading.net_in, reading.net_out))
   conn.commit()
   conn.close()
   return {"ok": True, "host": host}


@app.get("/hosts")
def list_hosts():
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()
   cursor.execute("""
       SELECT host, MAX(ts), cpu, ram, disk
       FROM host_metrics
       GROUP BY host
       ORDER BY host
   """)
   rows = cursor.fetchall()
   conn.close()

   now = time.time()
   hosts = []
   for host, last_ts, cpu, ram, disk in rows:
      if now - last_ts < INTERVAL * 3:
         status = "up"
      else:
         status = "down"
      hosts.append({
         "host": host,
         "status": status,
         "last_seen_seconds_ago": round(now - last_ts),
         "cpu": cpu,
         "ram": ram,
         "disk": disk,
      })
   return hosts


@app.get("/metrics")
def get_metrics(host: str, minutes: int = 60):
   since = time.time() - minutes * 60
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()
   cursor.execute("""
       SELECT ts, cpu, ram, disk, swap, load_1, net_in, net_out
       FROM host_metrics
       WHERE host = ? AND ts >= ?
       ORDER BY ts
   """, (host, since))
   rows = cursor.fetchall()
   conn.close()

   results = []
   for ts, cpu, ram, disk, swap, load_1, net_in, net_out in rows:
      results.append({
         "ts": ts, "cpu": cpu, "ram": ram, "disk": disk, "swap": swap,
         "load_1": load_1, "net_in": net_in, "net_out": net_out,
      })
   return results


@app.get("/alerts")
def list_alerts(limit: int = 50):
   if not os.path.exists(DB_PATH):
      return []
   conn = sqlite3.connect(DB_PATH)
   cursor = conn.cursor()
   try:
      cursor.execute("""
          SELECT id, name, level, value, threshold, fired_at, resolved_at, resolved_value
          FROM alerts
          ORDER BY id DESC
          LIMIT ?
      """, (limit,))
      rows = cursor.fetchall()
   except sqlite3.OperationalError:
      rows = []
   conn.close()

   results = []
   for id, name, level, value, threshold, fired_at, resolved_at, resolved_value in rows:
      results.append({
         "id": id, "name": name, "level": level, "value": value, "threshold": threshold,
         "fired_at": fired_at, "resolved_at": resolved_at, "resolved_value": resolved_value,
      })
   return results


@app.websocket("/ws")
async def live_hosts(ws: WebSocket):
   await ws.accept()
   try:
      while True:
         await ws.send_json(list_hosts())
         await asyncio.sleep(5)
   except WebSocketDisconnect:
      pass
