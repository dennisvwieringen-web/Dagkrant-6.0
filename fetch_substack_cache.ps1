# -----------------------------------------------------------------------------
# Substack-cache voor De Weekkrant.
#
# Substack weigert de servers van GitHub Actions (HTTP 403 op elke feed, gezien
# 9 okt 2026), maar vanaf deze pc werken de feeds wel. Dit script haalt daarom
# alle feeds uit substack_feeds.json op, houdt alleen de posts van de laatste
# $KeepDays dagen over (kleiner bestand) en zet het resultaat als
# substack_cache.json op de branch `substack-cache`. De cloud-run leest dat
# bestand (zie src/substack_feed.py en .github/workflows/dagkrant.yml).
#
# Draait dagelijks via de Taakplanner-taak "Weekkrant-Substack": de cron-run
# van vrijdag 03:00 gebruikt dus de cache van de laatste dag dat de pc aan stond.
# Posts die daarna verschijnen komen de week erna mee (venster 8 dagen).
#
# Authenticatie: hetzelfde token uit Git Credential Manager als run_dagkrant.ps1.
# Log: logs\substack-<datum>.log (laatste 30 bewaard).
#
# Let op: bestand bewust alleen ASCII - Windows PowerShell 5.1 leest scripts
# zonder BOM als ANSI.
# -----------------------------------------------------------------------------

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # maakt Invoke-WebRequest veel sneller

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir   = Join-Path $Root 'logs'
$Owner    = 'dennisvwieringen-web'
$Repo     = 'Dagkrant-6.0'
$Branch   = 'substack-cache'
$CacheFile = 'substack_cache.json'
$KeepDays = 10
$UserAgent = 'Mozilla/5.0 (compatible; Weekkrant RSS reader)'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogFile = Join-Path $LogDir ("substack-" + (Get-Date -Format 'yyyy-MM-dd_HH-mm-ss') + ".log")

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    $line | Tee-Object -FilePath $LogFile -Append | Out-Null
    Write-Output $line
}

# Verwijder <item>s ouder dan $KeepDays. Lukt het parsen niet, dan de hele feed.
function Trim-Feed([string]$xmlText) {
    try {
        $doc = New-Object System.Xml.XmlDocument
        $doc.PreserveWhitespace = $true
        $doc.LoadXml($xmlText)
        $cutoff = (Get-Date).ToUniversalTime().AddDays(-$KeepDays)
        $inv = [Globalization.CultureInfo]::InvariantCulture
        foreach ($item in @($doc.SelectNodes('//channel/item'))) {
            $node = $item.SelectSingleNode('pubDate')
            if (-not $node) { continue }
            $raw = $node.InnerText -replace '\s+(GMT|UT|UTC)$', ' +0000'
            try { $date = [DateTimeOffset]::Parse($raw, $inv).UtcDateTime } catch { continue }
            if ($date -lt $cutoff) { [void]$item.ParentNode.RemoveChild($item) }
        }
        return $doc.OuterXml
    } catch {
        return $xmlText
    }
}

Log "=== Substack-cache verversen ==="
$code = 0
try {
    $feedsPath = Join-Path $Root 'substack_feeds.json'
    $feeds = Get-Content -Path $feedsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cache = @{}
    $ok = 0; $failed = 0
    foreach ($feed in $feeds) {
        if (-not $feed.feed) { continue }
        if ($feed.PSObject.Properties.Name -contains 'enabled' -and -not $feed.enabled) { continue }
        try {
            $resp = Invoke-WebRequest -Uri $feed.feed -UseBasicParsing -UserAgent $UserAgent -TimeoutSec 30
            # Altijd als UTF-8 lezen: PS 5.1 gokt anders soms Latin-1.
            $text = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
            $cache[$feed.feed] = Trim-Feed $text
            $ok++
        } catch {
            $failed++
            Log "  FOUT $($feed.name): $($_.Exception.Message)"
        }
    }
    Log "Feeds opgehaald: $ok gelukt, $failed mislukt."
    if ($ok -eq 0) { throw "Geen enkele feed opgehaald - cache niet bijgewerkt." }

    $payload = @{
        fetched_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss+00:00')
        feeds      = $cache
    } | ConvertTo-Json -Depth 4 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    Log ("Cachegrootte: {0:N0} kB" -f ($bytes.Length / 1KB))

    # Token uit Git Credential Manager (zie run_dagkrant.ps1 voor het waarom van bash).
    $gitDir = Split-Path -Parent (Get-Command git).Source
    $bash   = Join-Path (Split-Path -Parent $gitDir) 'bin\bash.exe'
    if (-not (Test-Path $bash)) { $bash = 'C:\Program Files\Git\bin\bash.exe' }
    $cred  = & $bash -c "printf 'protocol=https\nhost=github.com\n\n' | git credential fill"
    $token = ($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
    if ([string]::IsNullOrWhiteSpace($token)) { throw "Geen GitHub-token gevonden in Git Credential Manager." }

    $headers = @{
        'Authorization'        = "Bearer $token"
        'Accept'               = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2022-11-28'
        'User-Agent'           = 'dagkrant-substack-cache'
    }
    $uri = "https://api.github.com/repos/$Owner/$Repo/contents/$CacheFile"

    # Bestaand bestand? Dan is de sha nodig om het te overschrijven.
    $sha = $null
    try {
        $existing = Invoke-RestMethod -Uri "$uri`?ref=$Branch" -Headers $headers -Method Get
        $sha = $existing.sha
    } catch {
        if (-not ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404)) { throw }
    }

    $body = @{
        message = "Substack-cache $(Get-Date -Format 'yyyy-MM-dd HH:mm') ($ok feeds)"
        content = [Convert]::ToBase64String($bytes)
        branch  = $Branch
    }
    if ($sha) { $body.sha = $sha }
    $json = $body | ConvertTo-Json -Compress
    $null = Invoke-RestMethod -Uri $uri -Headers $headers -Method Put -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($json))
    Log "Cache bijgewerkt op branch '$Branch'."
}
catch {
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    Log "FOUT (HTTP $status): $($_.Exception.Message)"
    Log "Tip: 401/403 = token verlopen; doe 'git push' of 'git fetch' om het te verversen."
    $code = 1
}
finally {
    Log "=== Klaar (exitcode $code) ==="
    Get-ChildItem -Path $LogDir -Filter 'substack-*.log' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

exit $code
