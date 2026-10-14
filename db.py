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


def init_server_db():
   with connect(SERVER_DB_PATH) as conn:
      conn.executescript(SERVER_SCHEMA)
