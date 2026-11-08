# Start the Jarvis Command Center (dry run unless JARVIS_TRADING_MODE=live)
Set-Location C:\jarvis-trading
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -match 'uvicorn app:app|spawn_main' } | % { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList '-m','uvicorn','app:app','--port','8000' -WorkingDirectory $PSScriptRoot -WindowStyle Minimized -RedirectStandardOutput "$PSScriptRoot\data\server.log" -RedirectStandardError "$PSScriptRoot\data\server.err.log"
Start-Sleep 8
Start-Process http://127.0.0.1:8000/
