import time
import hmac
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from config import (SERVER_DB_PATH, INTERVAL, ADMIN_TOKEN, ALERT_DURATION,
                    CPU_WARNING, CPU_CRITICAL, RAM_WARNING, DISK_WARNING)
from db import connect
from alerts import send_discord


log = logging.getLogger("linuxwatch")

# metric -> (label, unit); "offline" is seconds since the host last reported
METRICS = {
   "cpu": ("CPU", "%"),
   "ram": ("RAM", "%"),
   "disk": ("Disk", "%"),
   "swap": ("Swap", "%"),
   "load_1": ("Load (1 min)", ""),
   "offline": ("Offline", "s"),
}
LEVELS = ("warning", "critical")
CHECK_EVERY = 15

router = APIRouter()

# (host, rule_id) -> ts the value first went over the limit; resets on restart, which only re-arms the wait
breach_since = {}
started_at = time.time()


class RuleIn(BaseModel):
   metric: str
   threshold: float = Field(ge=0)
   duration: float = Field(default=0, ge=0)
   level: str = "warning"
   host: Optional[str] = None
   enabled: bool = True


def require_admin(token):
   if ADMIN_TOKEN and not hmac.compare_digest(token or "", ADMIN_TOKEN):
      raise HTTPException(status_code=401, detail="Admin token required")


def check_rule_input(rule):
   if rule.metric not in METRICS:
      raise HTTPException(status_code=422, detail=f"metric must be one of {', '.join(METRICS)}")
   if rule.level not in LEVELS:
      raise HTTPException(status_code=422, detail="level must be warning or critical")
   rule.host = (rule.host or "").strip() or None


def seed_rules():
   """First start only: copy the config.ini thresholds into editable rules."""
   with connect(SERVER_DB_PATH) as conn:
      if conn.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0]:
         return
      conn.executemany(
         "INSERT INTO alert_rules (metric, threshold, duration, level) VALUES (?, ?, ?, ?)",
         [
            ("cpu", CPU_WARNING, ALERT_DURATION, "warning"),
            ("cpu", CPU_CRITICAL, ALERT_DURATION, "critical"),
            ("ram", RAM_WARNING, ALERT_DURATION, "warning"),
            ("disk", DISK_WARNING, ALERT_DURATION, "warning"),
            ("offline", INTERVAL * 3, 0, "critical"),
         ])


def load_rules(host=None):
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("SELECT * FROM alert_rules ORDER BY metric, threshold").fetchall()
   rules = [dict(r) for r in rows]
   if host is None:
      return rules
   return [r for r in rules if r["enabled"] and r["host"] in (None, host)]


def fmt_value(metric, value):
   label, unit = METRICS.get(metric, (metric, ""))
   if metric == "offline":
      return f"{round(value)}s"
   return f"{round(value, 1)}{unit}"


def notify(text):
   # Discord can take seconds; never hold up an agent's POST for it
   threading.Thread(target=send_discord, args=(text,), daemon=True).start()


def open_alert_id(conn, host, rule_id):
   row = conn.execute(
      "SELECT id FROM fleet_alerts WHERE host = ? AND rule_id = ? AND resolved_at IS NULL",
      (host, rule_id)).fetchone()
   return row[0] if row else None


def fire(conn, host, rule, value, ts):
   conn.execute("""
       INSERT INTO fleet_alerts (host, rule_id, metric, level, value, threshold, fired_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
   """, (host, rule["id"], rule["metric"], rule["level"], value, rule["threshold"], ts))
   label = METRICS[rule["metric"]][0]
   text = f"{rule['level'].upper()}: {host} {label} is {fmt_value(rule['metric'], value)} (limit {fmt_value(rule['metric'], rule['threshold'])})"
   log.warning(text)
   notify(text)


def resolve(conn, alert_id, host, rule, value, ts):
   conn.execute("UPDATE fleet_alerts SET resolved_at = ?, resolved_value = ? WHERE id = ?", (ts, value, alert_id))
   label = METRICS[rule["metric"]][0]
   text = f"RESOLVED: {host} {label} back to {fmt_value(rule['metric'], value)}"
   log.info(text)
   notify(text)


def check_rule(conn, host, rule, value, ts):
   key = (host, rule["id"])
   alert_id = open_alert_id(conn, host, rule["id"])
   if value >= rule["threshold"]:
      since = breach_since.setdefault(key, ts)
      if alert_id is None and ts - since >= rule["duration"]:
         fire(conn, host, rule, value, ts)
   else:
      breach_since.pop(key, None)
      if alert_id is not None:
         resolve(conn, alert_id, host, rule, value, ts)


def evaluate(host, values, ts):
   """Run every rule for `host` against one live reading."""
   rules = load_rules(host)
   with connect(SERVER_DB_PATH) as conn:
      for rule in rules:
         if rule["metric"] == "offline":
            check_rule(conn, host, rule, 0, ts)   # the host just reported, so it is back
         elif values.get(rule["metric"]) is not None:
            check_rule(conn, host, rule, values[rule["metric"]], ts)


def check_offline():
   now = time.time()
   if now - started_at < INTERVAL * 3:
      return   # give agents time to reconnect after a server restart
   with connect(SERVER_DB_PATH) as conn:
      last_seen = conn.execute("SELECT host, MAX(ts) FROM host_metrics GROUP BY host").fetchall()
   for host, last_ts in last_seen:
      for rule in load_rules(host):
         if rule["metric"] != "offline":
            continue
         with connect(SERVER_DB_PATH) as conn:
            age = now - last_ts
            if age >= rule["threshold"] and open_alert_id(conn, host, rule["id"]) is None:
               fire(conn, host, rule, age, now)


def offline_loop(stop):
   while not stop.wait(CHECK_EVERY):
      try:
         check_offline()
      except Exception:
         log.exception("Offline check failed")


def start_offline_watch():
   """Start the background offline checker; set the returned event to stop it."""
   seed_rules()
   stop = threading.Event()
   threading.Thread(target=offline_loop, args=(stop,), daemon=True).start()
   return stop


def utc_text(ts):
   if ts is None:
      return None
   return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fleet_alert_rows(limit, host=None):
   sql = "SELECT * FROM fleet_alerts"
   args = []
   if host:
      sql += " WHERE host = ?"
      args.append(host)
   sql += " ORDER BY fired_at DESC LIMIT ?"
   args.append(limit)
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute(sql, args).fetchall()
   return [
      {
         "id": "f" + str(r["id"]),
         "source": "fleet",
         "host": r["host"],
         "name": r["metric"],
         "level": r["level"],
         "value": r["value"],
         "threshold": r["threshold"],
         "unit": METRICS.get(r["metric"], ("", ""))[1],
         "fired_at": utc_text(r["fired_at"]),
         "resolved_at": utc_text(r["resolved_at"]),
         "resolved_value": r["resolved_value"],
      }
      for r in rows
   ]


def open_counts():
   """host -> {"count": open alerts, "level": worst level}, used by /hosts."""
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT host, COUNT(*), MAX(level = 'critical')
          FROM fleet_alerts
          WHERE resolved_at IS NULL
          GROUP BY host
      """).fetchall()
   return {host: {"count": n, "level": "critical" if crit else "warning"} for host, n, crit in rows}


@router.get("/rules")
def get_rules():
   return {"metrics": {k: {"label": v[0], "unit": v[1]} for k, v in METRICS.items()},
           "protected": bool(ADMIN_TOKEN),
           "rules": load_rules()}


@router.post("/rules")
def add_rule(rule: RuleIn, x_admin_token: Optional[str] = Header(None)):
   require_admin(x_admin_token)
   check_rule_input(rule)
   with connect(SERVER_DB_PATH) as conn:
      cursor = conn.execute(
         "INSERT INTO alert_rules (metric, threshold, duration, level, host, enabled) VALUES (?, ?, ?, ?, ?, ?)",
         (rule.metric, rule.threshold, rule.duration, rule.level, rule.host, int(rule.enabled)))
   return {"ok": True, "id": cursor.lastrowid}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, rule: RuleIn, x_admin_token: Optional[str] = Header(None)):
   require_admin(x_admin_token)
   check_rule_input(rule)
   with connect(SERVER_DB_PATH) as conn:
      changed = conn.execute(
         "UPDATE alert_rules SET metric = ?, threshold = ?, duration = ?, level = ?, host = ?, enabled = ? WHERE id = ?",
         (rule.metric, rule.threshold, rule.duration, rule.level, rule.host, int(rule.enabled), rule_id)).rowcount
      if not changed:
         raise HTTPException(status_code=404, detail="No such rule")
      if not rule.enabled:
         close_rule_alerts(conn, rule_id)
   forget_breaches(rule_id)
   return {"ok": True}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, x_admin_token: Optional[str] = Header(None)):
   require_admin(x_admin_token)
   with connect(SERVER_DB_PATH) as conn:
      if not conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,)).rowcount:
         raise HTTPException(status_code=404, detail="No such rule")
      close_rule_alerts(conn, rule_id)
   forget_breaches(rule_id)
   return {"ok": True}


def close_rule_alerts(conn, rule_id):
   # a removed or disabled rule should not leave alerts open forever
   conn.execute("UPDATE fleet_alerts SET resolved_at = ? WHERE rule_id = ? AND resolved_at IS NULL", (time.time(), rule_id))


def forget_breaches(rule_id):
   for key in [k for k in breach_since if k[1] == rule_id]:
      breach_since.pop(key, None)
