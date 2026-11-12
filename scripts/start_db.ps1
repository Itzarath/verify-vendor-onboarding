# Starts the local PostgreSQL (portable copy in .tools, data in .pgdata) on 127.0.0.1:55432.
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root ".tools\pgsql\bin"
$data = Join-Path $root ".pgdata"
& "$bin\pg_isready.exe" -h 127.0.0.1 -p 55432 | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Output "PostgreSQL already running on 127.0.0.1:55432"; exit 0 }
Start-Process -WindowStyle Hidden -FilePath "$bin\pg_ctl.exe" -ArgumentList @("start", "-D", "`"$data`"", "-l", "`"$data\server.log`"", "-o", "`"-p 55432 -h 127.0.0.1`"")
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    & "$bin\pg_isready.exe" -h 127.0.0.1 -p 55432 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Output "PostgreSQL started on 127.0.0.1:55432"; exit 0 }
}
Write-Output "PostgreSQL did not start; see $data\server.log"; exit 1
