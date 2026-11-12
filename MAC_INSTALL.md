# Install the FBF Command Center (Athena) on your Mac

About 20–40 minutes, most of it the one-time download of Athena's model (17 GB).

## 1. Get the code
Open **Terminal** (Spotlight → "Terminal") and paste:

```bash
cd ~ && git clone https://github.com/forgedbyfreedom/jarvis-trading.git && cd jarvis-trading
```

The repo is private: if it asks you to sign in, run `brew install gh && gh auth login`
(choose GitHub.com → HTTPS → "Login with a web browser"), then paste the line above again.
If `git` itself is missing, macOS offers to install the Command Line Tools: click Install, then retry.

## 2. Run the installer
```bash
bash mac/install.sh
```
It installs Homebrew (if needed), Python 3.12, ffmpeg and Ollama, sets up the app, downloads
Athena's model, and puts **Athena - FBF Command Center** on your Desktop.

It asks for one thing: the **FBF AI library key**. Copy the `FBF_AI_API_KEY=` value from
`C:\jarvis-trading\.env.local` on the PC (open it in Notepad) and paste it when asked. It stays only on the Mac.

## 3. Start it
Double-click **Athena - FBF Command Center** on the Desktop. (First time, macOS may say it's from an
unidentified developer: right-click it → Open → Open.)

Then:
- Allow the **microphone** when the browser asks (for "Talk to Athena").
- Open **FBF Clients** and sign in once with your FBF coach login.

## Good to know
- Everything runs locally on the Mac (port 8000, not exposed to the network), just like the PC.
- On the Mac, Athena's hearing runs Whisper on the CPU (a little slower than the PC's RTX 3090).
- God's Eye View is only installed on the PC.
- To update later: `cd ~/jarvis-trading && git pull && bash mac/install.sh`
