import os
import configparser


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

config = configparser.ConfigParser()
config.optionxform = str
config.read(CONFIG_PATH)


CPU_WARNING = config.getfloat("thresholds", "cpu_warning", fallback=80)
CPU_CRITICAL = config.getfloat("thresholds", "cpu_critical", fallback=95)
RAM_WARNING = config.getfloat("thresholds", "ram_warning", fallback=85)
DISK_WARNING = config.getfloat("thresholds", "disk_warning", fallback=90)

INTERVAL = config.getfloat("monitor", "interval", fallback=60)

SERVICES = [s.strip() for s in config.get("monitor", "services", fallback="sshd, cron, nginx").split(",") if s.strip()]

DB_PATH = os.path.join(BASE_DIR, config.get("paths", "database", fallback="linuxwatch.db"))
LOG_PATH = os.path.join(BASE_DIR, config.get("paths", "log_file", fallback="linuxwatch.log"))
SERVER_DB_PATH = os.path.join(BASE_DIR, config.get("paths", "server_database", fallback="server.db"))
BUFFER_PATH = os.path.join(BASE_DIR, config.get("paths", "agent_buffer", fallback="buffer.jsonl"))

ALERT_DURATION = config.getfloat("alerts", "duration", fallback=300)
ALERT_COOLDOWN = config.getfloat("alerts", "cooldown", fallback=1800)

DISCORD_WEBHOOK = config.get("discord", "webhook_url", fallback="").strip()

AGENT_SERVER_URL = config.get("agent", "server_url", fallback="http://localhost:8000").rstrip("/")
AGENT_API_KEY = config.get("agent", "api_key", fallback="").strip()

API_KEYS = {}
if config.has_section("api_keys"):
   for key, host in config.items("api_keys"):
      API_KEYS[key.strip()] = host.strip()
# protects rule editing in the dashboard; empty = anyone who can open the dashboard may edit
ADMIN_TOKEN = config.get("dashboard", "admin_token", fallback="").strip()
