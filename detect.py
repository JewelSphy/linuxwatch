import time
import math
import sqlite3
from fastapi import APIRouter
from config import SERVER_DB_PATH, INTERVAL, config


WINDOW = config.getint("detect", "window", fallback=30)
Z_LIMIT = config.getfloat("detect", "z_limit", fallback=3)
MIN_JUMP = config.getfloat("detect", "min_jump", fallback=10)
FORECAST_DAYS = config.getfloat("detect", "forecast_days", fallback=7)

METRICS = ["cpu", "ram"]

router = APIRouter()


def mean_and_std(values):
   n = len(values)
   mean = sum(values) / n
   total = 0
   for v in values:
      total = total + (v - mean) ** 2
   std = math.sqrt(total / n)
   return mean, std


def check_point(window_values, value):
   mean, std = mean_and_std(window_values)
   if std < 1:
      std = 1
   z = (value - mean) / std
   if abs(z) >= Z_LIMIT and abs(value - mean) >= MIN_JUMP:
      low = max(0, mean - Z_LIMIT * std)
      high = min(100, mean + Z_LIMIT * std)
      return {"z": round(z, 1), "mean": round(mean, 1), "low": round(low, 1), "high": round(high, 1)}
   return None


def get_rows(host, since):
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.execute("""
       SELECT ts, cpu, ram, disk
       FROM host_metrics
       WHERE host = ? AND ts >= ?
       ORDER BY ts
   """, (host, since))
   rows = cursor.fetchall()


   conn.close()
   return rows


def get_hosts():
   conn = sqlite3.connect(SERVER_DB_PATH)
   cursor = conn.cursor()


   cursor.execute("SELECT DISTINCT host FROM host_metrics ORDER BY host")
   hosts = []
   for (host,) in cursor.fetchall():
      hosts.append(host)


   conn.close()
   return hosts


@router.get("/anomalies")
def anomalies(host: str, minutes: int = 60):
   since = time.time() - minutes * 60
   rows = get_rows(host, since - WINDOW * INTERVAL)

   found = []
   for m, metric in enumerate(METRICS):
      values = []
      for row in rows:
         values.append(row[m + 1])
      for i in range(WINDOW, len(rows)):
         if rows[i][0] < since:
            continue
         hit = check_point(values[i - WINDOW:i], values[i])
         if hit:
            hit["ts"] = rows[i][0]
            hit["metric"] = metric
            hit["value"] = values[i]
            found.append(hit)
   return found


@router.get("/anomalies/now")
def anomalies_now():
   since = time.time() - (WINDOW + 5) * INTERVAL
   found = []
   for host in get_hosts():
      rows = get_rows(host, since)
      if len(rows) <= WINDOW:
         continue
      for m, metric in enumerate(METRICS):
         values = []
         for row in rows:
            values.append(row[m + 1])
         hit = check_point(values[-WINDOW - 1:-1], values[-1])
         if hit:
            hit["host"] = host
            hit["metric"] = metric
            hit["value"] = values[-1]
            hit["ts"] = rows[-1][0]
            found.append(hit)
   return found


def fit_line(xs, ys):
   n = len(xs)
   mean_x = sum(xs) / n
   mean_y = sum(ys) / n
   top = 0
   bottom = 0
   for i in range(n):
      top = top + (xs[i] - mean_x) * (ys[i] - mean_y)
      bottom = bottom + (xs[i] - mean_x) ** 2
   if bottom == 0:
      return 0, mean_y
   slope = top / bottom
   intercept = mean_y - slope * mean_x
   return slope, intercept


@router.get("/forecast")
def forecast():
   now = time.time()
   since = now - FORECAST_DAYS * 86400
   results = []
   for host in get_hosts():
      rows = get_rows(host, since)
      if len(rows) < 30 or rows[-1][0] - rows[0][0] < 6 * 3600:
         results.append({"host": host, "status": "learning", "disk_now": rows[-1][3] if rows else None})
         continue

      xs = []
      ys = []
      for row in rows:
         xs.append((row[0] - now) / 86400)
         ys.append(row[3])

      slope, intercept = fit_line(xs, ys)
      disk_now = ys[-1]

      if slope <= 0.05:
         results.append({"host": host, "status": "stable", "disk_now": disk_now, "slope_per_day": round(slope, 3)})
         continue

      days_to_full = (100 - disk_now) / slope
      results.append({
         "host": host,
         "status": "filling",
         "disk_now": disk_now,
         "slope_per_day": round(slope, 3),
         "days_to_full": round(days_to_full, 1),
         "full_at": now + days_to_full * 86400,
      })

   results.sort(key=lambda r: r.get("days_to_full", 99999))
   return results


def attach(app):
   app.include_router(router)
