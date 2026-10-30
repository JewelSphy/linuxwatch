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
from activity import log_event


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


class MuteIn(BaseModel):
   minutes: float = Field(default=60, gt=0, le=60 * 24 * 30)


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


def is_muted(conn, host, rule_id):
   row = conn.execute("SELECT until FROM alert_mutes WHERE host = ? AND rule_id = ?", (host, rule_id)).fetchone()
   return row is not None and row[0] > time.time()


def tell(conn, host, rule, text, info=False):
   """Log every alert change; send to Discord unless this host + rule is muted."""
   muted = is_muted(conn, host, rule["id"])
   (log.info if info else log.warning)(text + (" (muted)" if muted else ""))
   if not muted:
      notify(text)


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
   tell(conn, host, rule, text)
   if rule["metric"] == "offline":
      log_event(conn, "offline", f"went offline, nothing heard for {fmt_value('offline', value)}", host, rule["level"], ts)
   else:
      log_event(conn, "alert", f"{label} {fmt_value(rule['metric'], value)}, limit {fmt_value(rule['metric'], rule['threshold'])}", host, rule["level"], ts)


def resolve(conn, alert_id, host, rule, value, ts):
   conn.execute("UPDATE fleet_alerts SET resolved_at = ?, resolved_value = ? WHERE id = ?", (ts, value, alert_id))
   label = METRICS[rule["metric"]][0]
   text = f"RESOLVED: {host} {label} back to {fmt_value(rule['metric'], value)}"
   tell(conn, host, rule, text, info=True)
   if rule["metric"] == "offline":
      log_event(conn, "online", "back online", host, "ok", ts)
   else:
      log_event(conn, "resolved", f"{label} back to {fmt_value(rule['metric'], value)}", host, "ok", ts)


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
   sql = """
       SELECT a.*, m.until AS muted_until
       FROM fleet_alerts a
       LEFT JOIN alert_mutes m ON m.host = a.host AND m.rule_id = a.rule_id AND m.until > ?
   """
   args = [time.time()]
   if host:
      sql += " WHERE a.host = ?"
      args.append(host)
   sql += " ORDER BY a.fired_at DESC LIMIT ?"
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
         "acked_at": utc_text(r["acked_at"]),
         "muted_until": r["muted_until"],
      }
      for r in rows
   ]


def open_counts():
   """host -> {"count": open alerts, "level": worst level}, used by /hosts. Acked and muted alerts don't count."""
   with connect(SERVER_DB_PATH) as conn:
      rows = conn.execute("""
          SELECT a.host, COUNT(*), MAX(a.level = 'critical')
          FROM fleet_alerts a
          LEFT JOIN alert_mutes m ON m.host = a.host AND m.rule_id = a.rule_id AND m.until > ?
          WHERE a.resolved_at IS NULL AND a.acked_at IS NULL AND m.until IS NULL
          GROUP BY a.host
      """, (time.time(),)).fetchall()
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
      log_event(conn, "rule", "added rule: " + describe_rule(rule), rule.host)
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
      log_event(conn, "rule", ("changed" if rule.enabled else "turned off") + " rule: " + describe_rule(rule), rule.host)
      if not rule.enabled:
         close_rule_alerts(conn, rule_id)
   forget_breaches(rule_id)
   return {"ok": True}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, x_admin_token: Optional[str] = Header(None)):
   require_admin(x_admin_token)
   with connect(SERVER_DB_PATH) as conn:
      old = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
      if old is None:
         raise HTTPException(status_code=404, detail="No such rule")
      conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
      log_event(conn, "rule", "deleted rule: " + describe_rule(RuleIn(**{k: old[k] for k in ("metric", "threshold", "duration", "level", "host")})), old["host"])
      close_rule_alerts(conn, rule_id)
      conn.execute("DELETE FROM alert_mutes WHERE rule_id = ?", (rule_id,))
   forget_breaches(rule_id)
   return {"ok": True}


def describe_rule(rule):
   label = METRICS.get(rule.metric, (rule.metric, ""))[0]
   if rule.metric == "offline":
      text = f"{label} after {fmt_value('offline', rule.threshold)}"
   else:
      text = f"{label} at or above {fmt_value(rule.metric, rule.threshold)}"
      if rule.duration:
         text += f" for {round(rule.duration / 60, 1):g} min"
   return f"{text}, {rule.level}" + (f", only {rule.host}" if rule.host else "")


def close_rule_alerts(conn, rule_id):
   # a removed or disabled rule should not leave alerts open forever
   conn.execute("UPDATE fleet_alerts SET resolved_at = ? WHERE rule_id = ? AND resolved_at IS NULL", (time.time(), rule_id))


def forget_breaches(rule_id):
   for key in [k for k in breach_since if k[1] == rule_id]:
      breach_since.pop(key, None)


def fleet_alert(conn, alert_id):
   """Look up a fleet alert by its API id ("f12"); monitor.py alerts ("m3") can't be acked or muted."""
   if not alert_id.startswith("f") or not alert_id[1:].isdigit():
      raise HTTPException(status_code=400, detail="Only fleet alerts (id starting with f) can be acknowledged or muted")
   row = conn.execute("SELECT * FROM fleet_alerts WHERE id = ?", (int(alert_id[1:]),)).fetchone()
   if row is None:
      raise HTTPException(status_code=404, detail="No such alert")
   return row


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: str, x_admin_token: Optional[str] = Header(None)):
   require_admin(x_admin_token)
   with connect(SERVER_DB_PATH) as conn:
      row = fleet_alert(conn, alert_id)
      conn.execute("UPDATE fleet_alerts SET acked_at = COALESCE(acked_at, ?) WHERE id = ?", (time.time(), row["id"]))
      log_event(conn, "ack", f"acknowledged {METRICS.get(row['metric'], (row['metric'],))[0]} alert", row["host"])
   return {"ok": True}


@router.post("/alerts/{alert_id}/mute")
def mute_alert(alert_id: str, mute: MuteIn, x_admin_token: Optional[str] = Header(None)):
   """Silence Discord for this node + rule for a while; the alert itself still opens and resolves."""
   require_admin(x_admin_token)
   until = time.time() + mute.minutes * 60
   with connect(SERVER_DB_PATH) as conn:
      row = fleet_alert(conn, alert_id)
      conn.execute("INSERT OR REPLACE INTO alert_mutes (host, rule_id, until) VALUES (?, ?, ?)",
                   (row["host"], row["rule_id"], until))
      log_event(conn, "mute", f"muted {METRICS.get(row['metric'], (row['metric'],))[0]} alerts for {mute.minutes:g} min", row["host"])
   return {"ok": True, "muted_until": until}


@router.delete("/alerts/{alert_id}/mute")
def unmute_alert(alert_id: str, x_admin_token: Optional[str] = Header(None)):
   require_admin(x_admin_token)
   with connect(SERVER_DB_PATH) as conn:
      row = fleet_alert(conn, alert_id)
      conn.execute("DELETE FROM alert_mutes WHERE host = ? AND rule_id = ?", (row["host"], row["rule_id"]))
      log_event(conn, "mute", f"unmuted {METRICS.get(row['metric'], (row['metric'],))[0]} alerts", row["host"])
   return {"ok": True}
