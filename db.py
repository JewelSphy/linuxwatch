import sqlite3
import threading
from contextlib import contextmanager
from config import SERVER_DB_PATH


_local = threading.local()

SERVER_SCHEMA = """
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
   CREATE TABLE IF NOT EXISTS host_details (
       host TEXT PRIMARY KEY,
       ts REAL NOT NULL,
       data TEXT NOT NULL
   );
   CREATE TABLE IF NOT EXISTS alert_rules (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       metric TEXT NOT NULL,
       threshold REAL NOT NULL,
       duration REAL NOT NULL DEFAULT 0,
       level TEXT NOT NULL DEFAULT 'warning',
       host TEXT,
       enabled INTEGER NOT NULL DEFAULT 1
   );
   CREATE TABLE IF NOT EXISTS fleet_alerts (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       host TEXT NOT NULL,
       rule_id INTEGER,
       metric TEXT NOT NULL,
       level TEXT NOT NULL,
       value REAL NOT NULL,
       threshold REAL NOT NULL,
       fired_at REAL NOT NULL,
       resolved_at REAL,
       resolved_value REAL
   );
   CREATE INDEX IF NOT EXISTS idx_fleet_open ON fleet_alerts (host, rule_id, resolved_at);
   CREATE TABLE IF NOT EXISTS events (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       ts REAL NOT NULL,
       kind TEXT NOT NULL,
       level TEXT NOT NULL DEFAULT 'info',
       host TEXT,
       text TEXT NOT NULL
   );
   CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
   CREATE TABLE IF NOT EXISTS ssh_failures (
       host TEXT NOT NULL,
       ts REAL NOT NULL,
       ip TEXT NOT NULL,
       user TEXT,
       count INTEGER NOT NULL
   );
   CREATE INDEX IF NOT EXISTS idx_ssh_ts ON ssh_failures (ts);
   CREATE TABLE IF NOT EXISTS alert_mutes (
       host TEXT NOT NULL,
       rule_id INTEGER NOT NULL,
       until REAL NOT NULL,
       PRIMARY KEY (host, rule_id)
   );
"""


def _open(path):
   conn = sqlite3.connect(path, timeout=10)
   conn.row_factory = sqlite3.Row
   conn.execute("PRAGMA journal_mode=WAL")
   conn.execute("PRAGMA synchronous=NORMAL")
   return conn


@contextmanager
def connect(path):
   """Yield this thread's cached connection to `path`; commit on success, roll back on error."""
   conns = _local.__dict__.setdefault("conns", {})
   conn = conns.get(path)
   if conn is None:
      conn = conns[path] = _open(path)
   try:
      yield conn
      conn.commit()
   except BaseException:
      conn.rollback()
      raise


def add_column(conn, table, column, decl):
   """Add a column to an existing table (older databases) if it is missing."""
   columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
   if column not in columns:
      conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_server_db():
   with connect(SERVER_DB_PATH) as conn:
      conn.executescript(SERVER_SCHEMA)
      add_column(conn, "fleet_alerts", "acked_at", "REAL")
