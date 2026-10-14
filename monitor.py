import os
import shutil
import psutil
import time
from twisted.internet import task, reactor
import sqlite3
import subprocess
import getpass
import sys
from ssh_watch import ask_ssh_permission, get_ssh_failed_interval
from config import CPU_WARNING, CPU_CRITICAL, RAM_WARNING, DISK_WARNING, INTERVAL, SERVICES, DB_PATH
from log_setup import setup_logging
from alerts import stayed_high, cooled_down

log = setup_logging()

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# per-core usage
def get_cpu_per_core():
    usage = psutil.cpu_percent(interval=1, percpu=True)
    return usage
 
def load_average():
   num_cores = psutil.cpu_count(logical=True)
   load1, load5, load15 = os.getloadavg()
   print(f"1 min: {load1:.2f} ({(load1/num_cores)*100:.2f}% of capacity)")
   print(f"5 min: {load5:.2f} ({(load5/num_cores)*100:.2f}% of capacity)")
   print(f"15 min: {load15:.2f} ({(load15/num_cores)*100:.2f}% of capacity)")
   
   return load1, load5, load15

def init_db():
   conn = sqlite3.connect(DB_PATH)
   cursor = conn.cursor()


   with open(SCHEMA_PATH) as f:
      cursor.executescript(f.read())


   conn.commit()
   conn.close()


def get_cpu_usage():
   cpu_usage= psutil.cpu_percent(interval=1)
   return cpu_usage


def get_ram_usage():
   ram= psutil.virtual_memory()
   print(f"Total: {ram.total / (1024**3):.2f} GB")
   print(f"Available: {ram.available / (1024**3):.2f} GB")
   print(f"Used: {ram.used / (1024**3):.2f} GB")
   print(f"Usage: {ram.percent}%")
   return ram.percent


def get_disk_usage():
   disk_usage=psutil.disk_usage("/")
   return disk_usage.percent

def read_write_speed():
   IO1= psutil.disk_io_counters()
   time.sleep(1)
   IO2= psutil.disk_io_counters()
   read_speed= IO2.read_bytes - IO1.read_bytes
   write_speed= IO2.write_bytes - IO1.write_bytes
   return read_speed, write_speed

def get_uptime():
   boot_time = psutil.boot_time()
   current_time = time.time()
   return current_time - boot_time


def get_network_in():
   network = psutil.net_io_counters()
   return network.bytes_recv


def get_network_out():
   network_out = psutil.net_io_counters()
   return network_out.bytes_sent

def get_swap_usage():
   swap= psutil.swap_memory()
   return swap.percent


last_network_in = None
last_network_out = None


def get_network_in_interval():
   global last_network_in
   current = get_network_in()
   if last_network_in is None or current < last_network_in:
       last_network_in = current
       return 0
   interval = current - last_network_in
   last_network_in = current
   return interval


def get_network_out_interval():
   global last_network_out
   current = get_network_out()
   if last_network_out is None or current < last_network_out:
       last_network_out = current
       return 0
   interval = current - last_network_out
   last_network_out = current
   return interval


def save_metrics():
   cpu = get_cpu_usage()
   ram = get_ram_usage()
   disk = get_disk_usage()
   uptime = get_uptime()
   network_in = get_network_in_interval()
   network_out = get_network_out_interval()
   swap = get_swap_usage()
   read_speed, write_speed = read_write_speed()
   cores = get_cpu_per_core()
   load1, load5, load15 = os.getloadavg()
   net_in_speed, net_out_speed = get_network_speed(network_in, network_out)
   top = get_top_processes()
   cpu_temp = get_cpu_temp()
   connections = get_open_connections()
   service_status = get_service_status()
   ssh_failed = get_ssh_failed_interval()

   conn = sqlite3.connect(DB_PATH)
   cursor = conn.cursor()
   
   cursor.execute("""
       INSERT INTO server_metrics
                     (cpu_usage, ram_usage, disk_usage, swap_usage, read_speed, write_speed, uptime, network_in, network_out, net_in_speed, net_out_speed, load_1, load_5, load_15, cpu_temp, open_connections)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
   """, (cpu, ram, disk, swap, read_speed, write_speed, uptime, network_in, network_out, net_in_speed, net_out_speed, load1, load5, load15, cpu_temp, connections))
   metric_id = cursor.lastrowid


   for i, usage in enumerate(cores):
       cursor.execute("""
           INSERT INTO cpu_cores (metric_id, core, usage)
           VALUES (?, ?, ?)
       """, (metric_id, i, usage))

   for pid, name, cpu_percent, memory_percent in top:
       cursor.execute("""
           INSERT INTO top_processes (metric_id, pid, name, cpu_percent, memory_percent)
           VALUES (?, ?, ?, ?, ?)
       """, (metric_id, pid, name, cpu_percent, memory_percent))

   for name, running in service_status:
       cursor.execute("""
           INSERT INTO service_status (metric_id, service, running)
           VALUES (?, ?, ?)
       """, (metric_id, name, running))
       
   if ssh_failed is not None:
       cursor.execute("""
           INSERT INTO ssh_failed (metric_id, count)
           VALUES (?, ?)
       """, (metric_id, ssh_failed))

   conn.commit()
   conn.close()
   
 
def check_alerts(cpu, ram, disk):
   if stayed_high("cpu_critical", cpu, CPU_CRITICAL):
       log.critical(f"CPU usage is too high! ({cpu}%)")
   elif stayed_high("cpu", cpu, CPU_WARNING):
       log.warning(f"CPU usage is high! ({cpu}%)")


   if stayed_high("ram", ram, RAM_WARNING):
       log.warning(f"RAM usage is high! ({ram}%)")


   if stayed_high("disk", disk, DISK_WARNING):
       log.warning(f"Disk space is running low! ({disk}%)")
 

def dashboard():
    cpu = get_cpu_usage()
    ram = get_ram_usage()
    swap = get_swap_usage()
    disk = get_disk_usage()
    uptime = get_uptime()
    network_in = get_network_in()
    network_out = get_network_out()
    cores = get_cpu_per_core()
    load = load_average()
    read_speed, write_speed = read_write_speed()
    service_status = get_service_status()
    

    print("CPU Usage:", cpu)
    print("RAM Used:", ram)
    print("Swap Used:", swap)
    print("Disk Used:", disk)
    print("Uptime:", uptime)
    print("Network In:", network_in)
    print("Network Out:", network_out)
    print("Read Speed:", read_speed)
    print("Write Speed:", write_speed)
    print("Per-Core Usage:")
    log.info(f"CPU {cpu}% | RAM {ram}% | Swap {swap}% | Disk {disk}%")
    for i, usage in enumerate(cores):
        print(f"Core {i}: {usage}%")

    check_alerts(cpu, ram, disk)
    save_metrics()
    
    
def get_network_speed(network_in, network_out):
   in_speed = network_in / INTERVAL
   out_speed = network_out / INTERVAL
   log.info(f"Network In Speed: {in_speed:.0f} bytes/s")
   log.info(f"Network Out Speed: {out_speed:.0f} bytes/s")
   return in_speed, out_speed


def get_top_processes():
   procs = []
   for p in psutil.process_iter(['pid', 'name']):
      try:
         p.cpu_percent(None)
         procs.append(p)
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass

   time.sleep(1)

   results = []
   for p in procs:
      try:
         results.append((p.pid, p.info['name'], p.cpu_percent(None), p.memory_percent()))
      except (psutil.NoSuchProcess, psutil.AccessDenied):
         pass

   results.sort(key=lambda r: r[2], reverse=True)
   return results[:5]


def get_cpu_temp():
   if not hasattr(psutil, "sensors_temperatures"):
      return None
   temps = psutil.sensors_temperatures()
   for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
      if name in temps and temps[name]:
         return temps[name][0].current
   return None


def get_open_connections():
   try:
      conns = psutil.net_connections(kind="tcp")
   except psutil.AccessDenied:
      return 0
   count = 0
   for c in conns:
      if c.status == psutil.CONN_ESTABLISHED:
         count = count + 1
   return count

def service_is_running(name):
   for p in psutil.process_iter(['name']):
      if p.info['name'] == name:
         return True
   return False

def get_service_status():
   services = SERVICES
   results = []
   for name in services:
      results.append((name, service_is_running(name)))
   return results
#Monitoring Loop


def recheck_time():
   try:
       dashboard()
   except Exception as e:
      log.exception("Monitor tick failed")

ask_ssh_permission()
init_db()
looping_call = task.LoopingCall(recheck_time) 
looping_call.start(INTERVAL)
reactor.run()


