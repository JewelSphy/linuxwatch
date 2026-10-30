import time
from typing import Optional
from fastapi import APIRouter
from config import SERVER_DB_PATH
from db import connect


# kind -> what happened; level picks the color in the dashboard (info, ok, warning, critical)
KINDS = ("joined", "offline", "online", "alert", "resolved", "rule", "ack", "mute", "ssh")

router = APIRouter()


def log_event(conn, kind, text, host=None, level="info", ts=None):
   """Add one line to the activity log, inside the caller's transaction."""
   conn.execute("INSERT INTO events (ts, kind, level, host, text) VALUES (?, ?, ?, ?, ?)",
                (ts or time.time(), kind, level, host, text))


def record(kind, text, host=None, level="info"):
   """Same as log_event but opens its own connection."""
   with connect(SERVER_DB_PATH) as conn:
      log_event(conn, kind, text, host, level)


@router.get("/events")
def events(limit: int = 50, host: Optional[str] = None):
   sql = "SELECT id, ts, kind, level, host, text FROM events"
   args = []
   if host:
      sql += " WHERE host = ?"
      args.append(host)
   sql += " ORDER BY id DESC LIMIT ?"
   args.append(min(limit, 500))
   with connect(SERVER_DB_PATH) as conn:
      return [dict(row) for row in conn.execute(sql, args)]
