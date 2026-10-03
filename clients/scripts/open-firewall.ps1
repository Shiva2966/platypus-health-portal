# Run as Administrator. Opens inbound TCP 8000 for the demo backend.
$name = "Health Portal 8000"
if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private,Public | Out-Null
  "Firewall rule '$name' created."
} else { "Rule '$name' already exists." }
