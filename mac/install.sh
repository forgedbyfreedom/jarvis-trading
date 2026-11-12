#!/bin/bash
# FBF Command Center (Athena) — Mac installer.
# Run from the repo folder:   bash mac/install.sh
# Safe to run again: it skips anything already installed.
set -e
cd "$(dirname "$0")/.."
REPO="$(pwd)"
say() { printf "\n\033[1;33m> %s\033[0m\n" "$1"; }

say "Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi

say "Installing Python 3.12, ffmpeg and Ollama (skips what's already there)"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list ffmpeg      >/dev/null 2>&1 || brew install ffmpeg
command -v ollama >/dev/null 2>&1 || brew install --cask ollama
PY="$(brew --prefix python@3.12)/bin/python3.12"

say "Creating the Python environment"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "AI library key"
if [ -f .env.local ] && grep -q '^FBF_AI_API_KEY=' .env.local; then
  echo "Already set in .env.local"
else
  echo "Paste the FBF AI library key (the value after FBF_AI_API_KEY= in C:\\jarvis-trading\\.env.local on the PC)."
  echo "It won't show on screen as you paste. Press Enter when done."
  read -rs KEY; echo
  echo "FBF_AI_API_KEY=$KEY" >> .env.local
  chmod 600 .env.local
fi

say "Athena's model"
open -a Ollama 2>/dev/null || (ollama serve >/dev/null 2>&1 &)
sleep 5
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
MODEL="huihui_ai/Qwen3.8-abliterated:latest"
if [ "$RAM_GB" -lt 32 ]; then
  echo "This Mac has ${RAM_GB} GB of memory; Athena's 17 GB model needs about 32 GB to run well."
  echo "Installing it anyway - answers will be slower. (Set COACH_MODEL in .env.local to use a smaller model.)"
fi
ollama pull "$MODEL"

say "Desktop launcher"
LAUNCH="$HOME/Desktop/Athena - FBF Command Center.command"
printf '#!/bin/bash\nexec "%s/mac/start-athena.sh"\n' "$REPO" > "$LAUNCH"
chmod +x "$LAUNCH" mac/start-athena.sh

say "Done. Double-click 'Athena - FBF Command Center' on your Desktop to start."
echo "First time: allow the microphone when the browser asks, then sign in under FBF Clients."
