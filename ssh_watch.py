import os
import re
import sys
import getpass
import subprocess


AUTH_LOGS = ["/var/log/auth.log", "/var/log/secure"]


def find_auth_log():
   for path in AUTH_LOGS:
      if os.path.exists(path):
         return path
   return None


def ask_ssh_permission():
   path = find_auth_log()
   if path is None:
      print("SSH logins: no auth log found on this system (skipped)")
      return
   if os.access(path, os.R_OK):
      return
   if path != "/var/log/auth.log" or not sys.stdin.isatty():
      print("SSH logins: no permission to read", path, "(skipped)")
      return

   print("LinuxWatch needs permission to read login logs (for failed SSH tracking).")
   answer = input("Allow? [y/N]: ")
   if answer.strip().lower() != "y":
      print("SSH logins: skipped")
      return

   user = getpass.getuser()
   result = subprocess.run(["sudo", "usermod", "-aG", "adm", user])
   if result.returncode == 0:
      print("Done. Log out and back in, then restart LinuxWatch to start counting failed logins.")
   else:
      print("Could not change permission. SSH logins: skipped")


def is_failed_login(line):
   return "sshd" in line and ("Failed password" in line or "Invalid user" in line)


# "Failed password for [invalid user] NAME from IP" or "Invalid user NAME from IP"
FAILED_RE = re.compile(r"(?:Failed password for (?:invalid user )?(\S*)|Invalid user (\S*)) from ([0-9A-Fa-f:.]+)")


def parse_failure(line):
   """Return (user, ip) for a failed SSH login line."""
   match = FAILED_RE.search(line)
   if not match:
      return None, "unknown"
   return match.group(1) or match.group(2) or None, match.group(3)


# (path, inode, byte offset) of the last read, so each call only reads new lines
position = None


def get_ssh_failed_interval():
   """Failed SSH logins since the previous call (monitor.py); None when the log can't be read."""
   failures = get_ssh_failures_interval()
   return None if failures is None else len(failures)


def get_ssh_failures_interval():
   """(user, ip) for each failed SSH login since the previous call, reading only the bytes appended since then."""
   global position
   path = find_auth_log()
   if path is None:
      return None
   try:
      stat = os.stat(path)
      if position is None:
         position = (path, stat.st_ino, stat.st_size)
         return []
      last_path, last_inode, offset = position
      if last_path != path or last_inode != stat.st_ino or stat.st_size < offset:
         offset = 0   # rotated or truncated: count the new file from its start
      with open(path, "rb") as f:
         f.seek(offset)
         chunk = f.read()
   except PermissionError:
      return None

   end = chunk.rfind(b"\n") + 1   # leave a half-written last line for the next call
   lines = chunk[:end].decode(errors="ignore").splitlines()
   position = (path, stat.st_ino, offset + end)
   return [parse_failure(line) for line in lines if is_failed_login(line)]
