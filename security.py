import json
import time
from fastapi import APIRouter
from config import SERVER_DB_PATH, INTERVAL, config
from db import connect
from activity import log_event


# one IP failing this many times in one reading gets its own activity line (at most once an hour)
SSH_BURST = config.getint("security", "ssh_burst", fallback=10)
BURST_QUIET = 3600

router = APIRouter()

last_burst = {}   # (host, ip) -> ts of the last burst event


def record(conn, host, ts, details):
   """Store this reading's failed SSH logins and log bursts from a single IP."""
   attempts = details.get("ssh_attempts") or []
   conn.executemany(
      "INSERT INTO ssh_failures (host, ts, ip, user, count) VALUES (?, ?, ?, ?, ?)",
      [(host, ts, a.get("ip") or "unknown", a.get("user"), int(a.get("count") or 0)) for a in attempts])

   per_ip = {}
   for a in attempts:
      per_ip[a.get("ip") or "unknown"] = per_ip.get(a.get("ip") or "unknown", 0) + int(a.get("count") or 0)
   for ip, n in per_ip.items():
      key = (host, ip)
      if n >= SSH_BURST and ts - last_burst.get(key, 0) >= BURST_QUIET:
         last_burst[key] = ts
         log_event(conn, "ssh", f"{n} failed SSH logins from {ip} in {round(INTERVAL)} s", host, "warning", ts)


def hourly_counts(conn, since, now):
   rows = dict(conn.execute("""
       SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS hour, SUM(count)
       FROM ssh_failures
       WHERE ts >= ?
       GROUP BY hour
   """, (since,)).fetchall())
   first = int(since // 3600) * 3600
   return [{"hour": h, "count": rows.get(h, 0)} for h in range(first, int(now) + 1, 3600)]


def open_ports(conn):
   result = []
   for host, data in conn.execute("SELECT host, data FROM host_details ORDER BY host"):
      ports = json.loads(data).get("ports")
      result.append({"host": host, "ports": ports})
   return result


@router.get("/security")
def security(hours: int = 24):
   now = time.time()
   since = now - hours * 3600
   with connect(SERVER_DB_PATH) as conn:
      total, ips, hosts = conn.execute(
         "SELECT COALESCE(SUM(count), 0), COUNT(DISTINCT ip), COUNT(DISTINCT host) FROM ssh_failures WHERE ts >= ?",
         (since,)).fetchone()
      top_ips = [dict(r) for r in conn.execute("""
          SELECT ip, SUM(count) AS count, COUNT(DISTINCT host) AS hosts, MAX(ts) AS last_seen,
                 GROUP_CONCAT(DISTINCT user) AS users
          FROM ssh_failures
          WHERE ts >= ?
          GROUP BY ip
          ORDER BY count DESC
          LIMIT 8
      """, (since,))]
      top_users = [dict(r) for r in conn.execute("""
          SELECT user, SUM(count) AS count
          FROM ssh_failures
          WHERE ts >= ? AND user IS NOT NULL
          GROUP BY user
          ORDER BY count DESC
          LIMIT 6
      """, (since,))]
      by_host = dict(conn.execute(
         "SELECT host, SUM(count) FROM ssh_failures WHERE ts >= ? GROUP BY host", (since,)).fetchall())
      return {
         "hours": hours,
         "total": total,
         "unique_ips": ips,
         "hosts_targeted": hosts,
         "hourly": hourly_counts(conn, since, now),
         "top_ips": top_ips,
         "top_users": top_users,
         "by_host": by_host,
         "ports": open_ports(conn),
      }
