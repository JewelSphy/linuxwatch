import os
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


def get_ssh_failed_total():
   path = find_auth_log()
   if path is None:
      return None
   count = 0
   try:
      with open(path, errors="ignore") as f:
         for line in f:
            if "sshd" in line and ("Failed password" in line or "Invalid user" in line):
               count = count + 1
   except PermissionError:
      return None
   return count


last_ssh_failed = None


def get_ssh_failed_interval():
   global last_ssh_failed
   current = get_ssh_failed_total()
   if current is None:
      return None
   if last_ssh_failed is None or current < last_ssh_failed:
      last_ssh_failed = current
      return 0
   interval = current - last_ssh_failed
   last_ssh_failed = current
   return interval