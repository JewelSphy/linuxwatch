import os
import sys
import json
import time
import psutil
import urllib.request
from config import AGENT_SERVER_URL, AGENT_API_KEY, INTERVAL, BUFFER_PATH


last_net = None


def collect():
   global last_net
   net = psutil.net_io_counters()
   if last_net is None or net.bytes_recv < last_net.bytes_recv:
      net_in, net_out = 0, 0
   else:
      net_in = net.bytes_recv - last_net.bytes_recv
      net_out = net.bytes_sent - last_net.bytes_sent
   last_net = net

   return {
      "ts": time.time(),
      "cpu": psutil.cpu_percent(interval=1),
      "ram": psutil.virtual_memory().percent,
      "disk": psutil.disk_usage("/").percent,
      "swap": psutil.swap_memory().percent,
      "load_1": os.getloadavg()[0],
      "net_in": net_in,
      "net_out": net_out,
   }


def send(reading):
   data = json.dumps(reading).encode()
   req = urllib.request.Request(
      AGENT_SERVER_URL + "/metrics",
      data=data,
      headers={"Content-Type": "application/json", "X-API-Key": AGENT_API_KEY}
   )
   try:
      urllib.request.urlopen(req, timeout=10)
      return True
   except Exception as e:
      print("Send failed:", e)
      return False


def save_to_buffer(reading):
   with open(BUFFER_PATH, "a") as f:
      f.write(json.dumps(reading) + "\n")


def flush_buffer():
   if not os.path.exists(BUFFER_PATH):
      return True
   with open(BUFFER_PATH) as f:
      lines = f.readlines()

   left = []
   for i, line in enumerate(lines):
      if not send(json.loads(line)):
         left = lines[i:]
         break

   if left:
      with open(BUFFER_PATH, "w") as f:
         f.writelines(left)
      return False
   os.remove(BUFFER_PATH)
   print("Resent", len(lines), "buffered readings")
   return True


def main():
   if not AGENT_API_KEY:
      print("No api_key in [agent] section of config.ini")
      sys.exit(1)
   print("LinuxWatch agent sending to", AGENT_SERVER_URL, "every", INTERVAL, "s")
   while True:
      reading = collect()
      if flush_buffer() and send(reading):
         print(f"Sent: CPU {reading['cpu']}% RAM {reading['ram']}% Disk {reading['disk']}%")
      else:
         save_to_buffer(reading)
         print("Server unreachable, saved reading to buffer")
      time.sleep(max(0, INTERVAL - 1))


if __name__ == "__main__":
   main()
