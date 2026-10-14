import time
import logging
import threading
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from config import SERVER_DB_PATH, INTERVAL, config
from db import connect


log = logging.getLogger("linuxwatch")

RAW_DAYS = config.getfloat("storage", "raw_days", fallback=7)
HOURLY_DAYS = config.getfloat("storage", "hourly_days", fallback=90)
CLEANUP_EVERY = config.getfloat("storage", "cleanup_every", fallback=3600)
# after the first full pass, only recent hours are re-aggregated (late buffered readings still land)
ROLLUP_LOOKBACK = config.getfloat("storage", "rollup_lookback_hours", fallback=24) * 3600

router = APIRouter()


def hour_floor(ts):
   return int(ts // 3600) * 3600


def roll_up_hours(full=False):
   this_hour = hour_floor(time.time())
   window = RAW_DAYS * 86400 if full else min(ROLLUP_LOOKBACK, RAW_DAYS * 86400)
   since = hour_floor(this_hour - window)

   with connect(SERVER_DB_PATH) as conn:
      cursor = conn.execute("""
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
      return cursor.rowcount


def delete_old():
   now = time.time()
   # whole hours only, so a partly-deleted hour is never re-aggregated into a skewed row
   raw_cutoff = hour_floor(now - RAW_DAYS * 86400)

   with connect(SERVER_DB_PATH) as conn:
      raw_deleted = conn.execute("DELETE FROM host_metrics WHERE ts < ?", (raw_cutoff,)).rowcount
      hourly_deleted = conn.execute(
         "DELETE FROM host_metrics_hourly WHERE hour < ?", (now - HOURLY_DAYS * 86400,)
      ).rowcount
   return raw_deleted, hourly_deleted


def run_cleanup(full=False):
   hours = roll_up_hours(full)
   raw_deleted, hourly_deleted = delete_old()
   log.info(f"Storage: rolled up {hours} host-hours, deleted {raw_deleted} raw rows, {hourly_deleted} hourly rows")


def cleanup_loop(stop):
   full = True
   while True:
      try:
         run_cleanup(full)
         full = False
      except Exception:
         log.exception("Storage cleanup failed")
      if stop.wait(CLEANUP_EVERY):
         return


def start_cleanup():
   """Start the background cleanup thread; set the returned event to stop it."""
   stop = threading.Event()
   threading.Thread(target=cleanup_loop, args=(stop,), daemon=True).start()
   return stop


@router.get("/history")
def get_history(host: str, days: int = 30):
   since = time.time() - days * 86400

   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT hour, samples, cpu_avg, cpu_max, ram_avg, ram_max, disk_avg, disk_max,
                 swap_avg, load_avg, net_in_sum, net_out_sum
          FROM host_metrics_hourly
          WHERE host = ? AND hour >= ?
          ORDER BY hour
      """, (host, since)).fetchall()
   return [dict(row) for row in rows]


PROMETHEUS_METRICS = [
   ("linuxwatch_up", "1 if the host reported recently, else 0"),
   ("linuxwatch_last_seen_seconds", "Seconds since the host last reported"),
   ("linuxwatch_cpu_percent", "CPU usage percent"),
   ("linuxwatch_ram_percent", "RAM usage percent"),
   ("linuxwatch_disk_percent", "Disk usage percent"),
   ("linuxwatch_swap_percent", "Swap usage percent"),
   ("linuxwatch_load1", "1 minute load average"),
]


@router.get("/prometheus", response_class=PlainTextResponse)
def prometheus():
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT host, MAX(ts), cpu, ram, disk, swap, load_1
          FROM host_metrics
          GROUP BY host
      """).fetchall()

   now = time.time()
   values = {name: [] for name, _ in PROMETHEUS_METRICS}
   for host, last_ts, cpu, ram, disk, swap, load_1 in rows:
      label = '{host="' + host.replace('"', '') + '"}'
      age = now - last_ts
      values["linuxwatch_up"].append(f"{label} {1 if age < INTERVAL * 3 else 0}")
      values["linuxwatch_last_seen_seconds"].append(f"{label} {round(age)}")
      values["linuxwatch_cpu_percent"].append(f"{label} {cpu}")
      values["linuxwatch_ram_percent"].append(f"{label} {ram}")
      values["linuxwatch_disk_percent"].append(f"{label} {disk}")
      values["linuxwatch_swap_percent"].append(f"{label} {swap}")
      values["linuxwatch_load1"].append(f"{label} {load_1}")

   lines = []
   for name, help_text in PROMETHEUS_METRICS:
      lines.append(f"# HELP {name} {help_text}")
      lines.append(f"# TYPE {name} gauge")
      lines.extend(name + v for v in values[name])
   return "\n".join(lines) + "\n"
