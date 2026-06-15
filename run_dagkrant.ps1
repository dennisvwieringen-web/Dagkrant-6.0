# ─────────────────────────────────────────────────────────────────────────────
# Lokale launcher voor De Dagkrant.
#
# Wordt aangeroepen door de Windows Taakplanner-taak "De Dagkrant 16:00"
# (ma/wo/do/vr om 16:00). Draait de pijplijn rechtstreeks op deze pc — stipt,
# zonder GitHub-wachtrij of externe cron-dienst. Voorwaarde: pc aan + ingelogd.
#
# Alles wordt gelogd naar logs\dagkrant-<datum>.log. De laatste 30 logs blijven
# bewaard.
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Continue'

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = 'C:\Users\Dennis\AppData\Local\Programs\Python\Python313\python.exe'
$SrcDir = Join-Path $Root 'src'
$LogDir = Join-Path $Root 'logs'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$Stamp   = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$LogFile = Join-Path $LogDir ("dagkrant-$Stamp.log")

"=== De Dagkrant lokale run gestart: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $LogFile -Encoding utf8

Push-Location $SrcDir
try {
    & $Python 'main.py' *>&1 | Tee-Object -FilePath $LogFile -Append
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

"=== Klaar: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (exitcode $code) ===" | Out-File -FilePath $LogFile -Append -Encoding utf8

# Oude logs opruimen: bewaar de 30 nieuwste.
Get-ChildItem -Path $LogDir -Filter 'dagkrant-*.log' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
