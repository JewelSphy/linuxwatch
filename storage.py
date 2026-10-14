import time
import sqlite3
import logging
import threading
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from config import SERVER_DB_PATH, INTERVAL, config


log = logging.getLogger("linuxwatch")

RAW_DAYS = config.getfloat("storage", "raw_days", fallback=7)
HOURLY_DAYS = config.getfloat("storage", "hourly_days", fallback=90)
CLEANUP_EVERY = config.getfloat("storage", "cleanup_every", fallback=3600)

router = APIRouter()


def init_storage():
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.executescript("""
       CREATE TABLE IF NOT EXISTS host_metrics_hourly (
           host TEXT NOT NULL,
           hour REAL NOT NULL,
           samples INTEGER NOT NULL,
           cpu_avg REAL,
           cpu_max REAL,
           ram_avg REAL,
           ram_max REAL,
           disk_avg REAL,
           disk_max REAL,
           swap_avg REAL,
           load_avg REAL,
           net_in_sum INTEGER,
           net_out_sum INTEGER,
           PRIMARY KEY (host, hour)
       );
       CREATE INDEX IF NOT EXISTS idx_host_ts ON host_metrics (host, ts);
   """)


   conn.commit()
   conn.close()


def roll_up_hours():
   this_hour = int(time.time() // 3600) * 3600
   since = this_hour - RAW_DAYS * 86400

   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.execute("""
       INSERT OR REPLACE INTO host_metrics_hourly
       (host, hour, samples, cpu_avg, cpu_max, ram_avg, ram_max, disk_avg, disk_max, swap_avg, load_avg, net_in_sum, net_out_sum)
       SELECT host,
              CAST(ts / 3600 AS INTEGER) * 3600 AS hour,
              COUNT(*),
              AVG(cpu), MAX(cpu),
              AVG(ram), MAX(ram),
              AVG(disk), MAX(disk),
              AVG(swap),
              AVG(load_1),
              SUM(net_in), SUM(net_out)
       FROM host_metrics
       WHERE ts >= ? AND ts < ?
       GROUP BY host, hour
   """, (since, this_hour))
   hours = cursor.rowcount


   conn.commit()
   conn.close()
   return hours


def delete_old():
   now = time.time()

   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.execute("DELETE FROM host_metrics WHERE ts < ?", (now - RAW_DAYS * 86400,))
   raw_deleted = cursor.rowcount

   cursor.execute("DELETE FROM host_metrics_hourly WHERE hour < ?", (now - HOURLY_DAYS * 86400,))
   hourly_deleted = cursor.rowcount


   conn.commit()
   conn.close()
   return raw_deleted, hourly_deleted


def run_cleanup():
   hours = roll_up_hours()
   raw_deleted, hourly_deleted = delete_old()
   log.info(f"Storage: rolled up {hours} host-hours, deleted {raw_deleted} raw rows, {hourly_deleted} hourly rows")


def cleanup_loop():
   while True:
      try:
         run_cleanup()
      except Exception:
         log.exception("Storage cleanup failed")
      time.sleep(CLEANUP_EVERY)


@router.get("/history")
def get_history(host: str, days: int = 30):
   since = time.time() - days * 86400

   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.execute("""
       SELECT hour, samples, cpu_avg, cpu_max, ram_avg, ram_max, disk_avg, disk_max, swap_avg, load_avg, net_in_sum, net_out_sum
       FROM host_metrics_hourly
       WHERE host = ? AND hour >= ?
       ORDER BY hour
   """, (host, since))
   rows = cursor.fetchall()


   conn.close()

   results = []
   for hour, samples, cpu_avg, cpu_max, ram_avg, ram_max, disk_avg, disk_max, swap_avg, load_avg, net_in_sum, net_out_sum in rows:
      results.append({
         "hour": hour, "samples": samples,
         "cpu_avg": cpu_avg, "cpu_max": cpu_max,
         "ram_avg": ram_avg, "ram_max": ram_max,
         "disk_avg": disk_avg, "disk_max": disk_max,
         "swap_avg": swap_avg, "load_avg": load_avg,
         "net_in_sum": net_in_sum, "net_out_sum": net_out_sum,
      })
   return results


@router.get("/prometheus", response_class=PlainTextResponse)
def prometheus():
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.execute("""
       SELECT host, MAX(ts), cpu, ram, disk, swap, load_1
       FROM host_metrics
       GROUP BY host
   """)
   rows = cursor.fetchall()


   conn.close()

   now = time.time()
   metrics = [
      ("linuxwatch_up", "1 if the host reported recently, else 0"),
      ("linuxwatch_last_seen_seconds", "Seconds since the host last reported"),
      ("linuxwatch_cpu_percent", "CPU usage percent"),
      ("linuxwatch_ram_percent", "RAM usage percent"),
      ("linuxwatch_disk_percent", "Disk usage percent"),
      ("linuxwatch_swap_percent", "Swap usage percent"),
      ("linuxwatch_load1", "1 minute load average"),
   ]

   values = {}
   for name, help_text in metrics:
      values[name] = []

   for host, last_ts, cpu, ram, disk, swap, load_1 in rows:
      label = '{host="' + host.replace('"', '') + '"}'
      age = now - last_ts
      if age < INTERVAL * 3:
         up = 1
      else:
         up = 0
      values["linuxwatch_up"].append(label + " " + str(up))
      values["linuxwatch_last_seen_seconds"].append(label + " " + str(round(age)))
      values["linuxwatch_cpu_percent"].append(label + " " + str(cpu))
      values["linuxwatch_ram_percent"].append(label + " " + str(ram))
      values["linuxwatch_disk_percent"].append(label + " " + str(disk))
      values["linuxwatch_swap_percent"].append(label + " " + str(swap))
      values["linuxwatch_load1"].append(label + " " + str(load_1))

   lines = []
   for name, help_text in metrics:
      lines.append("# HELP " + name + " " + help_text)
      lines.append("# TYPE " + name + " gauge")
      for v in values[name]:
         lines.append(name + v)
   return "\n".join(lines) + "\n"


def attach(app):
   init_storage()
   app.include_router(router)
   t = threading.Thread(target=cleanup_loop, daemon=True)
   t.start()
