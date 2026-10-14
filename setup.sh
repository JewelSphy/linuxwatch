#!/bin/bash
# One-time setup for the LinuxWatch server and agent.
set -e
cd "$(dirname "$0")"

sudo apt install -y python3-venv
python3 -m venv venv
./venv/bin/pip install fastapi uvicorn psutil twisted

echo ""
echo "Setup done. Two API keys for config.ini:"
echo "  key 1: $(python3 -c 'import secrets; print(secrets.token_hex(16))')"
echo "  key 2: $(python3 -c 'import secrets; print(secrets.token_hex(16))')"
