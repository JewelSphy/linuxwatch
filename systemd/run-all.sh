#!/bin/bash
# Runs server, agent and monitor together. If any exits, stop the rest and
# exit non-zero so systemd restarts the whole group.
# Set LINUXWATCH_HOST=0.0.0.0 to expose the server beyond localhost.
cd "$(dirname "$0")/.."

./venv/bin/uvicorn server:app --host "${LINUXWATCH_HOST:-127.0.0.1}" --port "${LINUXWATCH_PORT:-8000}" &
sleep 2   # let the server come up before the agent posts
./venv/bin/python agent.py &
./venv/bin/python monitor.py &

trap 'kill $(jobs -p) 2>/dev/null' EXIT TERM INT
wait -n
exit 1
