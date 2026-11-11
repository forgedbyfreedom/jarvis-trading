# Athena - FBF Command Center launcher (desktop shortcut).
# Starts the server only if it isn't already running, then opens it in its own app window.
$root = 'C:\jarvis-trading'
function Up { try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/hub/links -TimeoutSec 2).StatusCode -eq 200 } catch { $false } }
if (-not (Up)) {
  Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList '-m','uvicorn','app:app','--port','8000' `
    -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\data\server.log" -RedirectStandardError "$root\data\server.err.log"
  for ($i = 0; $i -lt 40 -and -not (Up); $i++) { Start-Sleep -Milliseconds 500 }
}
$browsers = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
              "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
              "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")
$b = $browsers | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($b) { Start-Process $b -ArgumentList '--app=http://127.0.0.1:8000/', '--window-size=1600,1000' } else { Start-Process 'http://127.0.0.1:8000/' }
