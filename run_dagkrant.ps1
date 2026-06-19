# ─────────────────────────────────────────────────────────────────────────────
# Lokale trigger voor De Dagkrant.
#
# Wordt aangeroepen door de Windows Taakplanner-taak "Dagkrant-1500"
# (ma/wo/do/vr om 14:30; richttijd krant klaar 15:00). Start de GitHub Actions-
# workflow stipt via de API (`workflow_dispatch`) — de krant wordt vervolgens
# IN DE CLOUD opgehaald, vertaald, gerenderd en gemaild.
#
# Waarom niet lokaal draaien? Dit (werk)netwerk blokkeert de mailpoorten
# (IMAP 993 en SMTP 587), dus de pijplijn kan hier geen Gmail benaderen. Alleen
# HTTPS (443) werkt — precies genoeg om de cloud-run aan te sturen.
#
# Authenticatie: hergebruikt het GitHub-token dat Git Credential Manager al
# bewaart voor `git push` (geen aparte PAT nodig). Het token wordt niet gelogd.
#
# Alles wordt gelogd naar logs\dagkrant-<datum>.log (laatste 30 bewaard).
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'logs'
$Owner  = 'dennisvwieringen-web'
$Repo   = 'Dagkrant-6.0'
$Workflow = 'dagkrant.yml'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("dagkrant-" + (Get-Date -Format 'yyyy-MM-dd_HH-mm-ss') + ".log")

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    $line | Tee-Object -FilePath $LogFile -Append | Out-Null
    Write-Output $line
}

Log "=== De Dagkrant trigger gestart ==="

try {
    # 1) Token ophalen uit Git Credential Manager (zelfde credential als git push).
    #    Let op: `git credential fill` via PowerShell-piping faalt (stdin krijgt
    #    een BOM → "missing protocol field"). We voeren het daarom via Git's eigen
    #    bash uit, waar printf | git credential fill betrouwbaar werkt.
    $gitDir = Split-Path -Parent (Get-Command git).Source        # ...\Git\cmd
    $bash   = Join-Path (Split-Path -Parent $gitDir) 'bin\bash.exe'
    if (-not (Test-Path $bash)) { $bash = 'C:\Program Files\Git\bin\bash.exe' }
    $cred  = & $bash -c "printf 'protocol=https\nhost=github.com\n\n' | git credential fill"
    $token = ($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "Geen GitHub-token gevonden in Git Credential Manager."
    }
    Log "Token opgehaald uit Credential Manager (prefix $($token.Substring(0,4)))."

    # 2) Cloud-run starten via workflow_dispatch.
    $uri = "https://api.github.com/repos/$Owner/$Repo/actions/workflows/$Workflow/dispatches"
    $headers = @{
        'Authorization'        = "Bearer $token"
        'Accept'               = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent'           = 'dagkrant-local-trigger'
    }
    $resp = Invoke-WebRequest -Uri $uri -Method Post -Headers $headers `
        -Body '{"ref":"main"}' -UseBasicParsing
    Log "workflow_dispatch verstuurd. HTTP $($resp.StatusCode) (204 = OK)."
    Log "De cloud-run haalt nu de nieuwsbrieven op en mailt de PDF."
    $code = 0
}
catch {
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    Log "FOUT bij starten cloud-run (HTTP $status): $($_.Exception.Message)"
    Log "Tip: 401/403 = token verlopen of mist rechten; doe 'git push' om het te verversen. 404 = repo/workflow-naam controleren."
    $code = 1
}
finally {
    Log "=== Klaar (exitcode $code) ==="
    Get-ChildItem -Path $LogDir -Filter 'dagkrant-*.log' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

exit $code
