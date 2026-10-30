import os
import time
import socket
import platform
import psutil
from collections import Counter
from config import SERVICES
from ssh_watch import get_ssh_failures_interval


TOP_PROCESSES = 8


def os_name():
   if hasattr(platform, "freedesktop_os_release"):
      try:
         return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
      except OSError:
         pass
   if platform.system() == "Darwin":
      return "macOS " + platform.mac_ver()[0]
   return platform.system() + " " + platform.release()


def system_info():
   """Facts that rarely change: OS, kernel, sizes."""
   return {
      "hostname": socket.gethostname(),
      "os": os_name(),
      "kernel": platform.release(),
      "arch": platform.machine(),
      "cpu_count": psutil.cpu_count(),
      "ram_total": psutil.virtual_memory().total,
      "swap_total": psutil.swap_memory().total,
      "disk_total": psutil.disk_usage("/").total,
      "boot_time": psutil.boot_time(),
   }


def cpu_temp():
   if not hasattr(psutil, "sensors_temperatures"):
      return None
   try:
      temps = psutil.sensors_temperatures()
   except Exception:
      return None
   for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
      if temps.get(name):
         return round(temps[name][0].current, 1)
   return None


def open_connections():
   """Count ESTABLISHED TCP sockets; /proc is cheap on Linux, psutil is the fallback."""
   try:
      count = 0
      for path in ("/proc/net/tcp", "/proc/net/tcp6"):
         with open(path) as f:
            next(f, None)
            count += sum(1 for line in f if line.split()[3:4] == ["01"])
      return count
   except OSError:
      try:
         return sum(1 for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_ESTABLISHED)
      except (psutil.AccessDenied, OSError):
         return None   # macOS needs root for this


def listening_ports():
   """TCP ports with something listening; public = reachable from other machines (not 127.0.0.1 / ::1)."""
   ports = {}
   try:
      for path in ("/proc/net/tcp", "/proc/net/tcp6"):
         with open(path) as f:
            next(f, None)
            for line in f:
               fields = line.split()
               if len(fields) > 3 and fields[3] == "0A":   # 0A = LISTEN
                  addr, port_hex = fields[1].rsplit(":", 1)
                  local = addr in ("0100007F", "00000000000000000000000001000000")
                  port = int(port_hex, 16)
                  ports[port] = ports.get(port, False) or not local
   except OSError:
      try:
         for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr:
               local = c.laddr.ip in ("127.0.0.1", "::1")
               ports[c.laddr.port] = ports.get(c.laddr.port, False) or not local
      except (psutil.AccessDenied, OSError):
         return None   # macOS needs root for this
   return [{"port": p, "public": pub} for p, pub in sorted(ports.items())]


def ssh_attempts():
   """Failed SSH logins this interval grouped by (ip, user); None when the auth log can't be read."""
   failures = get_ssh_failures_interval()
   if failures is None:
      return None
   counts = Counter((ip, user) for user, ip in failures)
   return [{"ip": ip, "user": user, "count": n} for (ip, user), n in counts.most_common(50)]


def start_process_sample():
   procs = []
   for p in psutil.process_iter(["pid", "name", "username"]):
      try:
         p.cpu_percent(None)
         procs.append(p)
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass
   return procs


def finish_process_sample(procs):
   """Return (top processes as dicts, set of running process names)."""
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
   for cpu, p in usage[:TOP_PROCESSES]:
      try:
         top.append({
            "pid": p.pid,
            "name": p.info["name"],
            "user": p.info["username"],
            "cpu": round(cpu, 1),
            "mem": round(p.memory_percent(), 1),
         })
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass
   return top, names


def sample(seconds=1):
   """Measure CPU, per-core CPU and processes over one shared window; return (cpu, details)."""
   procs = start_process_sample()
   psutil.cpu_percent(None)
   psutil.cpu_percent(None, percpu=True)

   time.sleep(seconds)

   cpu = psutil.cpu_percent(None)
   cores = psutil.cpu_percent(None, percpu=True)
   top, names = finish_process_sample(procs)
   vm = psutil.virtual_memory()
   du = psutil.disk_usage("/")

   attempts = ssh_attempts()
   details = system_info()
   details.update({
      "uptime": time.time() - details["boot_time"],
      "load": [round(x, 2) for x in os.getloadavg()],
      "cores": cores,
      "ram_used": vm.total - vm.available,
      "disk_used": du.used,
      "temp": cpu_temp(),
      "connections": open_connections(),
      "ports": listening_ports(),
      "services": [{"name": s, "running": s in names} for s in SERVICES],
      "processes": top,
      "ssh_failed": None if attempts is None else sum(a["count"] for a in attempts),
      "ssh_attempts": attempts,
   })
   return cpu, details
