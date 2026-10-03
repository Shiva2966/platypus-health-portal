param([switch]$Reseed)
# Restart QA instance on port 8001 only (never touches port 8000).
Set-Location C:\Users\sav11\Projects\health-portal
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match "--port 8001" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 1
$env:DATABASE_URL = "sqlite:///data/qa.db"; $env:DEV_SHOW_OTP = "1"; $env:HP_DISABLE_BACKGROUND = "1"
if ($Reseed) { Remove-Item data\qa.db* -ErrorAction SilentlyContinue; .\.venv\Scripts\python.exe seed.py *> data\qa_seed.log }
Start-Process -FilePath .\.venv\Scripts\python.exe -ArgumentList "-m uvicorn app.main:app --host 127.0.0.1 --port 8001" -RedirectStandardOutput data\qa_server.out.log -RedirectStandardError data\qa_server.log -WindowStyle Hidden
Start-Sleep 6
try { (Invoke-RestMethod http://127.0.0.1:8001/api/health).ok } catch { "server not up" }
