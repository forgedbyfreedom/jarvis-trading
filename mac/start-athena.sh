#!/bin/bash
# Start the FBF Command Center on a Mac and open it in its own window.
cd "$(dirname "$0")/.."
mkdir -p data
# Ollama (Athena's brain)
curl -s -o /dev/null http://127.0.0.1:11434/ || open -a Ollama 2>/dev/null || (ollama serve >/dev/null 2>&1 &)
# Server (local only)
if ! curl -s -o /dev/null http://127.0.0.1:8000/; then
  nohup .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 > data/server.log 2> data/server.err.log &
  for i in $(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:8000/ && break; sleep 0.5; done
fi
URL="http://127.0.0.1:8000/"
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL"
elif [ -d "/Applications/Microsoft Edge.app" ]; then
  open -na "Microsoft Edge" --args --app="$URL"
else
  open "$URL"
fi
