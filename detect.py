import time
import math
from bisect import bisect_left
from itertools import groupby
from operator import itemgetter
from fastapi import APIRouter
from config import SERVER_DB_PATH, INTERVAL, config
from db import connect


WINDOW = config.getint("detect", "window", fallback=30)
Z_LIMIT = config.getfloat("detect", "z_limit", fallback=3)
MIN_JUMP = config.getfloat("detect", "min_jump", fallback=10)
FORECAST_DAYS = config.getfloat("detect", "forecast_days", fallback=7)

METRICS = ["cpu", "ram"]

router = APIRouter()


def score(mean, variance, value):
   """Return the anomaly details if `value` is far outside the window's mean, else None."""
   std = max(1.0, math.sqrt(max(0.0, variance)))
   z = (value - mean) / std
   if abs(z) >= Z_LIMIT and abs(value - mean) >= MIN_JUMP:
      return {
         "z": round(z, 1),
         "mean": round(mean, 1),
         "low": round(max(0, mean - Z_LIMIT * std), 1),
         "high": round(min(100, mean + Z_LIMIT * std), 1),
      }
   return None


def check_point(window_values, value):
   n = len(window_values)
   mean = sum(window_values) / n
   variance = sum((v - mean) ** 2 for v in window_values) / n
   return score(mean, variance, value)


def rolling_hits(values, first):
   """Score values[i] for i >= first against the WINDOW values before it, in O(n) via running sums."""
   total = sum(values[:WINDOW])
   total_sq = sum(v * v for v in values[:WINDOW])
   for i in range(WINDOW, len(values)):
      if i >= first:
         mean = total / WINDOW
         hit = score(mean, total_sq / WINDOW - mean * mean, values[i])
         if hit:
            yield i, hit
      old, new = values[i - WINDOW], values[i]
      total += new - old
      total_sq += new * new - old * old


@router.get("/anomalies")
def anomalies(host: str, minutes: int = 60):
   since = time.time() - minutes * 60
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT ts, cpu, ram
          FROM host_metrics
          WHERE host = ? AND ts >= ?
          ORDER BY ts
      """, (host, since - WINDOW * INTERVAL)).fetchall()

   timestamps = [row[0] for row in rows]
   first = bisect_left(timestamps, since)
   found = []
   for m, metric in enumerate(METRICS):
      values = [row[m + 1] for row in rows]
      for i, hit in rolling_hits(values, first):
         hit["ts"] = timestamps[i]
         hit["metric"] = metric
         hit["value"] = values[i]
         found.append(hit)
   return found


@router.get("/anomalies/now")
def anomalies_now():
   since = time.time() - (WINDOW + 5) * INTERVAL
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT host, ts, cpu, ram
          FROM host_metrics
          WHERE ts >= ?
          ORDER BY host, ts
      """, (since,)).fetchall()

   found = []
   for host, group in groupby(rows, key=itemgetter(0)):
      host_rows = list(group)
      if len(host_rows) <= WINDOW:
         continue
      for m, metric in enumerate(METRICS):
         values = [row[m + 2] for row in host_rows]
         hit = check_point(values[-WINDOW - 1:-1], values[-1])
         if hit:
            hit["host"] = host
            hit["metric"] = metric
            hit["value"] = values[-1]
            hit["ts"] = host_rows[-1][1]
            found.append(hit)
   return found


@router.get("/forecast")
def forecast():
   now = time.time()
   since = now - FORECAST_DAYS * 86400

   # least-squares sums computed inside SQLite, so no raw rows are loaded into Python
   with connect(SERVER_DB_PATH) as conn:
      hosts = [row[0] for row in conn.execute("SELECT DISTINCT host FROM host_metrics ORDER BY host")]
      stats = {row[0]: row for row in conn.execute("""
          SELECT host, COUNT(*), MIN(ts), MAX(ts),
                 SUM((ts - :now) / 86400.0),
                 SUM(disk),
                 SUM((ts - :now) / 86400.0 * disk),
                 SUM((ts - :now) / 86400.0 * (ts - :now) / 86400.0)
          FROM host_metrics
          WHERE ts >= :since
          GROUP BY host
      """, {"now": now, "since": since})}
      latest = {row[0]: row[1] for row in conn.execute("""
          SELECT host, disk, MAX(ts)
          FROM host_metrics
          WHERE ts >= ?
          GROUP BY host
      """, (since,))}

   results = []
   for host in hosts:
      disk_now = latest.get(host)
      if host not in stats:
         results.append({"host": host, "status": "learning", "disk_now": None})
         continue

      _, n, first_ts, last_ts, sx, sy, sxy, sxx = stats[host]
      if n < 30 or last_ts - first_ts < 6 * 3600:
         results.append({"host": host, "status": "learning", "disk_now": disk_now})
         continue

      bottom = sxx - sx * sx / n
      slope = (sxy - sx * sy / n) / bottom if bottom else 0

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
