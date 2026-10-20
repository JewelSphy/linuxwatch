import time
import json
import logging
import urllib.request
from config import INTERVAL, ALERT_DURATION, ALERT_COOLDOWN, DISCORD_WEBHOOK, DB_PATH
from db import connect

log = logging.getLogger("linuxwatch")

over_count = {}
last_sent = {}
active = {}


def stayed_high(name, value, threshold):
   if value >= threshold:
      over_count[name] = over_count.get(name, 0) + 1
   else:
      over_count[name] = 0
   needed = max(1, int(ALERT_DURATION / INTERVAL))
   return over_count[name] >= needed


def cooled_down(name):
   now = time.time()
   if name in last_sent and now - last_sent[name] < ALERT_COOLDOWN:
      return False
   last_sent[name] = now
   return True


def send_discord(text):
   if not DISCORD_WEBHOOK:
      return
   data = json.dumps({"content": text}).encode()
   req = urllib.request.Request(
      DISCORD_WEBHOOK,
      data=data,
      headers={"Content-Type": "application/json", "User-Agent": "LinuxWatch"}
   )
   try:
      urllib.request.urlopen(req, timeout=10)
   except Exception as e:
      log.error(f"Discord send failed: {e}")


def save_alert_fired(name, level, value, threshold):
   with connect(DB_PATH) as conn:
      cursor = conn.execute("""
          INSERT INTO alerts (name, level, value, threshold)
          VALUES (?, ?, ?, ?)
      """, (name, level, value, threshold))
      return cursor.lastrowid


def save_alert_resolved(alert_id, value):
   with connect(DB_PATH) as conn:
      conn.execute("""
          UPDATE alerts
          SET resolved_at = CURRENT_TIMESTAMP, resolved_value = ?
          WHERE id = ?
      """, (value, alert_id))


def check_metric(name, value, threshold, level, label):
   if stayed_high(name, value, threshold):
      if name not in active:
         active[name] = save_alert_fired(name, level, value, threshold)
      if cooled_down(name):
         text = f"{level.upper()}: {label} is {value}% (limit {threshold}%)"
         if level == "critical":
            log.critical(text)
         else:
            log.warning(text)
         send_discord(text)
   elif value < threshold and name in active:
      save_alert_resolved(active.pop(name), value)
      last_sent.pop(name, None)
      text = f"RESOLVED: {label} back to {value}% (limit {threshold}%)"
      log.info(text)
      send_discord(text)