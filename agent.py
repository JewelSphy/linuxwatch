import os
import sys
import json
import time
import psutil
import urllib.error
import urllib.request
from config import AGENT_SERVER_URL, AGENT_API_KEY, INTERVAL, BUFFER_PATH
from details import sample


BATCH_SIZE = 500

last_net = None
batch_supported = True


def collect():
   """Return (reading, details). Only the reading is buffered; details are a live snapshot."""
   global last_net
   cpu, details = sample(1)
   net = psutil.net_io_counters()
   if last_net is None or net.bytes_recv < last_net.bytes_recv:
      net_in, net_out = 0, 0
   else:
      net_in = net.bytes_recv - last_net.bytes_recv
      net_out = net.bytes_sent - last_net.bytes_sent
   last_net = net

   reading = {
      "ts": time.time(),
      "cpu": cpu,
      "ram": psutil.virtual_memory().percent,
      "disk": psutil.disk_usage("/").percent,
      "swap": psutil.swap_memory().percent,
      "load_1": os.getloadavg()[0],
      "net_in": net_in,
      "net_out": net_out,
   }
   return reading, details


def post(path, payload):
   req = urllib.request.Request(
      AGENT_SERVER_URL + path,
      data=json.dumps(payload).encode(),
      headers={"Content-Type": "application/json", "X-API-Key": AGENT_API_KEY}
   )
   urllib.request.urlopen(req, timeout=10).close()


def send(reading):
   try:
      post("/metrics", reading)
      return True
   except Exception as e:
      print("Send failed:", e)
      return False


def send_batch(readings):
   """Send readings in one request; return how many were accepted."""
   global batch_supported
   if batch_supported:
      try:
         post("/metrics/batch", {"readings": readings})
         return len(readings)
      except urllib.error.HTTPError as e:
         if e.code != 404:
            print("Send failed:", e)
            return 0
         batch_supported = False   # older server without /metrics/batch
      except Exception as e:
         print("Send failed:", e)
         return 0

   sent = 0
   for reading in readings:
      if not send(reading):
         break
      sent += 1
   return sent


def save_to_buffer(reading):
   with open(BUFFER_PATH, "a") as f:
      f.write(json.dumps(reading) + "\n")


def load_buffer():
   readings = []
   with open(BUFFER_PATH) as f:
      for line in f:
         try:
            readings.append(json.loads(line))
         except ValueError:
            pass   # half-written line from a crash
   return readings


def flush_buffer():
   if not os.path.exists(BUFFER_PATH):
      return True
   pending = load_buffer()

   sent = 0
   while sent < len(pending):
      chunk = pending[sent:sent + BATCH_SIZE]
      accepted = send_batch(chunk)
      sent += accepted
      if accepted < len(chunk):
         break

   if sent < len(pending):
      if sent:
         with open(BUFFER_PATH, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in pending[sent:])
      return False
   os.remove(BUFFER_PATH)
   print("Resent", sent, "buffered readings")
   return True


def main():
   if not AGENT_API_KEY:
      print("No api_key in [agent] section of config.ini")
      sys.exit(1)
   print("LinuxWatch agent sending to", AGENT_SERVER_URL, "every", INTERVAL, "s")
   next_run = time.monotonic()
   while True:
      reading, details = collect()
      if flush_buffer() and send(dict(reading, details=details)):
         print(f"Sent: CPU {reading['cpu']}% RAM {reading['ram']}% Disk {reading['disk']}%")
      else:
         save_to_buffer(reading)
         print("Server unreachable, saved reading to buffer")
      # fixed schedule: collect()'s 1 s sample and slow sends no longer stretch the period
      next_run = max(next_run + INTERVAL, time.monotonic())
      time.sleep(max(0, next_run - time.monotonic()))


if __name__ == "__main__":
   main()
