import os
import time
import psutil
from twisted.internet import task, reactor, threads
from ssh_watch import ask_ssh_permission, get_ssh_failed_interval
from config import CPU_WARNING, CPU_CRITICAL, RAM_WARNING, DISK_WARNING, INTERVAL, SERVICES, DB_PATH
from log_setup import setup_logging
from alerts import check_metric
from db import connect


log = setup_logging()

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

SAMPLE_SECONDS = 1
TOP_PROCESSES = 5

last_counters = {}


def init_db():
   with open(SCHEMA_PATH) as f:
      schema = f.read()
   with connect(DB_PATH) as conn:
      conn.executescript(schema)


def counter_delta(key, current):
   """Bytes since the previous call; 0 on the first call or when the counter reset."""
   previous = last_counters.get(key)
   last_counters[key] = current
   if previous is None or current < previous:
      return 0
   return current - previous


def get_cpu_temp():
   if not hasattr(psutil, "sensors_temperatures"):
      return None
   temps = psutil.sensors_temperatures()
   for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
      if name in temps and temps[name]:
         return temps[name][0].current
   return None


def get_open_connections():
   """Count ESTABLISHED TCP sockets from /proc, which is far cheaper than psutil scanning every pid's fds."""
   count = 0
   try:
      for path in ("/proc/net/tcp", "/proc/net/tcp6"):
         with open(path) as f:
            next(f, None)
            for line in f:
               fields = line.split()
               if len(fields) > 3 and fields[3] == "01":
                  count += 1
      return count
   except OSError:
      try:
         conns = psutil.net_connections(kind="tcp")
      except psutil.AccessDenied:
         return 0
      return sum(1 for c in conns if c.status == psutil.CONN_ESTABLISHED)


def start_process_sample():
   procs = []
   for p in psutil.process_iter(["pid", "name"]):
      try:
         p.cpu_percent(None)
         procs.append(p)
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass
   return procs


def finish_process_sample(procs):
   """Return (top processes by CPU, set of running process names)."""
   names = set()
   usage = []
   for p in procs:
      try:
         names.add(p.info["name"])
         usage.append((p.cpu_percent(None), p))
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass

   usage.sort(key=lambda u: u[0], reverse=True)
   top = []
   for cpu_percent, p in usage[:TOP_PROCESSES]:
      try:
         top.append((p.pid, p.info["name"], cpu_percent, p.memory_percent()))
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass
   return top, names


def collect():
   """Read every metric over one shared SAMPLE_SECONDS window instead of one sleep per metric."""
   procs = start_process_sample()
   psutil.cpu_percent(None)
   psutil.cpu_percent(None, percpu=True)
   io_start = psutil.disk_io_counters()

   time.sleep(SAMPLE_SECONDS)

   cpu = psutil.cpu_percent(None)
   cores = psutil.cpu_percent(None, percpu=True)
   io_end = psutil.disk_io_counters()
   top, names = finish_process_sample(procs)

   if io_start and io_end:
      read_speed = io_end.read_bytes - io_start.read_bytes
      write_speed = io_end.write_bytes - io_start.write_bytes
   else:
      read_speed = write_speed = 0

   net = psutil.net_io_counters()
   network_in = counter_delta("net_in", net.bytes_recv)
   network_out = counter_delta("net_out", net.bytes_sent)
   load1, load5, load15 = os.getloadavg()

   return {
      "cpu": cpu,
      "cores": cores,
      "ram": psutil.virtual_memory().percent,
      "disk": psutil.disk_usage("/").percent,
      "swap": psutil.swap_memory().percent,
      "uptime": time.time() - psutil.boot_time(),
      "read_speed": read_speed,
      "write_speed": write_speed,
      "network_in": network_in,
      "network_out": network_out,
      "net_in_speed": network_in / INTERVAL,
      "net_out_speed": network_out / INTERVAL,
      "load": (load1, load5, load15),
      "top": top,
      "cpu_temp": get_cpu_temp(),
      "connections": get_open_connections(),
      "services": [(name, name in names) for name in SERVICES],
      "ssh_failed": get_ssh_failed_interval(),
   }


def save_metrics(m):
   load1, load5, load15 = m["load"]
   with connect(DB_PATH) as conn:
      cursor = conn.execute("""
          INSERT INTO server_metrics
                        (cpu_usage, ram_usage, disk_usage, swap_usage, read_speed, write_speed, uptime, network_in, network_out, net_in_speed, net_out_speed, load_1, load_5, load_15, cpu_temp, open_connections)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """, (m["cpu"], m["ram"], m["disk"], m["swap"], m["read_speed"], m["write_speed"], m["uptime"],
            m["network_in"], m["network_out"], m["net_in_speed"], m["net_out_speed"],
            load1, load5, load15, m["cpu_temp"], m["connections"]))
      metric_id = cursor.lastrowid

      conn.executemany(
         "INSERT INTO cpu_cores (metric_id, core, usage) VALUES (?, ?, ?)",
         [(metric_id, i, usage) for i, usage in enumerate(m["cores"])])
      conn.executemany(
         "INSERT INTO top_processes (metric_id, pid, name, cpu_percent, memory_percent) VALUES (?, ?, ?, ?, ?)",
         [(metric_id, *row) for row in m["top"]])
      conn.executemany(
         "INSERT INTO service_status (metric_id, service, running) VALUES (?, ?, ?)",
         [(metric_id, name, running) for name, running in m["services"]])
      if m["ssh_failed"] is not None:
         conn.execute("INSERT INTO ssh_failed (metric_id, count) VALUES (?, ?)", (metric_id, m["ssh_failed"]))


def check_alerts(cpu, ram, disk):
   check_metric("cpu_critical", cpu, CPU_CRITICAL, "critical", "CPU usage")
   check_metric("cpu", cpu, CPU_WARNING, "warning", "CPU usage")
   check_metric("ram", ram, RAM_WARNING, "warning", "RAM usage")
   check_metric("disk", disk, DISK_WARNING, "warning", "Disk usage")


def tick():
   m = collect()
   log.info(f"CPU {m['cpu']}% | RAM {m['ram']}% | Swap {m['swap']}% | Disk {m['disk']}%")
   log.info(f"Network In Speed: {m['net_in_speed']:.0f} bytes/s")
   log.info(f"Network Out Speed: {m['net_out_speed']:.0f} bytes/s")
   check_alerts(m["cpu"], m["ram"], m["disk"])
   save_metrics(m)


def recheck_time():
   # runs in a worker thread so the 1 s sample never blocks the reactor; LoopingCall waits for the Deferred
   d = threads.deferToThread(tick)
   d.addErrback(lambda failure: log.error("Monitor tick failed", exc_info=failure.value))
   return d


def main():
   ask_ssh_permission()
   init_db()
   task.LoopingCall(recheck_time).start(INTERVAL)
   reactor.run()


if __name__ == "__main__":
   main()
