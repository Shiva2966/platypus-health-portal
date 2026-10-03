# Lists candidate LAN IPv4 addresses and the URL to use.
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
  ForEach-Object { "{0,-20} http://{1}:8000" -f $_.InterfaceAlias, $_.IPAddress }
