# Bootstrap Nginx Proxy Manager via its REST API (idempotent).
# Usage: .\scripts\configure_npm.ps1 [-RepoRoot <path>] [-BaseUrl http://127.0.0.1:81]
param(
    [string]$RepoRoot = "",
    [string]$BaseUrl = "http://127.0.0.1:81",
    [int]$WaitSeconds = 120
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$envPath = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Host "skip npm configure (no .env)"
    exit 0
}

function Get-EnvValue([string]$Key) {
    foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
        if ($line -match ("^" + [regex]::Escape($Key) + "=(.*)$")) {
            return $Matches[1].Trim()
        }
    }
    return ""
}

function Set-EnvKey([string]$Key, [string]$Value) {
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in [System.IO.File]::ReadAllLines($envPath)) { [void]$lines.Add($line) }
    $found = $false
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match ("^" + [regex]::Escape($Key) + "=")) {
            $found = $true
            [void]$out.Add("$Key=$Value")
        }
        else { [void]$out.Add($line) }
    }
    if (-not $found) { [void]$out.Add("$Key=$Value") }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($envPath, $out.ToArray(), $utf8)
}

function Invoke-NpmApi {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [string]$Token = ""
    )
    $headers = @{ Accept = "application/json" }
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }
    $uri = ($BaseUrl.TrimEnd("/") + $Path)
    try {
        if ($null -ne $Body) {
            $json = $Body | ConvertTo-Json -Depth 8 -Compress
            return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -ContentType "application/json" -Body $json -TimeoutSec 180
        }
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 60
    }
    catch {
        return $null
    }
}

$adminEmail = Get-EnvValue "NPM_ADMIN_EMAIL"
if (-not $adminEmail) { $adminEmail = "admin@example.com" }
$adminPass = Get-EnvValue "NPM_ADMIN_PASSWORD"
if (-not $adminPass) { $adminPass = "changeme" }
$domain = Get-EnvValue "NPM_DOMAIN"
$absDomain = Get-EnvValue "NPM_ABS_DOMAIN"
$kavitaDomain = Get-EnvValue "NPM_KAVITA_DOMAIN"
$leEmail = Get-EnvValue "NPM_LETSENCRYPT_EMAIL"

Write-Host "Waiting for Nginx Proxy Manager API at $BaseUrl ..."
$ready = $false
$attempts = [math]::Max(1, [int]($WaitSeconds / 2))
for ($i = 0; $i -lt $attempts; $i++) {
    try {
        $null = Invoke-WebRequest -Uri ($BaseUrl.TrimEnd("/") + "/api/tokens") -Method POST -ContentType "application/json" -Body '{}' -UseBasicParsing -TimeoutSec 3
        $ready = $true
        break
    }
    catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -in 400, 401, 403, 404, 405) {
            $ready = $true
            break
        }
    }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "skip npm configure (API timeout)"
    exit 0
}

function Get-NpmToken([string]$Identity, [string]$Secret) {
    $resp = Invoke-NpmApi -Method POST -Path "/api/tokens" -Body @{ identity = $Identity; secret = $Secret }
    if ($resp -and $resp.token) { return [string]$resp.token }
    return ""
}

$token = Get-NpmToken $adminEmail $adminPass
$usedDefault = $false
if (-not $token -and ($adminEmail -ne "admin@example.com" -or $adminPass -ne "changeme")) {
    $token = Get-NpmToken "admin@example.com" "changeme"
    $usedDefault = [bool]$token
}
if (-not $token) {
    Write-Host "skip npm configure (login failed)"
    exit 0
}

if ($usedDefault) {
    $me = Invoke-NpmApi -Method GET -Path "/api/users/me" -Token $token
    if ($me -and $me.id) {
        $update = @{
            email       = $adminEmail
            name        = $(if ($me.name) { $me.name } else { "Admin" })
            nickname    = $(if ($me.nickname) { $me.nickname } else { "Admin" })
            is_disabled = $false
            password    = $adminPass
        }
        $null = Invoke-NpmApi -Method PUT -Path "/api/users/$($me.id)" -Body $update -Token $token
        Write-Host "NPM admin credentials set ($adminEmail)"
        $newTok = Get-NpmToken $adminEmail $adminPass
        if ($newTok) { $token = $newTok }
    }
}

Write-Host "NPM API authenticated"

$libraryAdvanced = @"
# Library Site — long timeouts + websockets
location /api/search/live-stream {
    proxy_set_header Host `$host;
    proxy_set_header X-Real-IP `$remote_addr;
    proxy_set_header X-Forwarded-For `$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto `$scheme;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    proxy_pass `$forward_scheme://`$server:`$port;
}
location /api/stream/ {
    proxy_set_header Host `$host;
    proxy_set_header X-Real-IP `$remote_addr;
    proxy_set_header X-Forwarded-For `$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto `$scheme;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_pass `$forward_scheme://`$server:`$port;
}
location /api/requests/ws {
    proxy_set_header Host `$host;
    proxy_set_header X-Real-IP `$remote_addr;
    proxy_set_header X-Forwarded-For `$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto `$scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade `$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400s;
    proxy_pass `$forward_scheme://`$server:`$port;
}
"@

function Get-ProxyHosts {
    $resp = Invoke-NpmApi -Method GET -Path "/api/nginx/proxy-hosts" -Token $token
    if ($resp -is [System.Array]) { return $resp }
    return @()
}

function Find-Host($Hosts, [string]$Name) {
    foreach ($h in $Hosts) {
        foreach ($d in @($h.domain_names)) {
            if ([string]$d -eq $Name) { return $h }
        }
    }
    return $null
}

function Ensure-Cert([string[]]$DomainNames) {
    if (-not $leEmail) { return 0 }
    $certs = Invoke-NpmApi -Method GET -Path "/api/nginx/certificates" -Token $token
    $items = @()
    if ($certs -is [System.Array]) { $items = $certs }
    $want = $DomainNames | ForEach-Object { $_.ToLowerInvariant() }
    foreach ($c in $items) {
        $have = @($c.domain_names) | ForEach-Object { ([string]$_).ToLowerInvariant() }
        $all = $true
        foreach ($w in $want) { if ($have -notcontains $w) { $all = $false } }
        if ($all -and $c.id) { return [int]$c.id }
    }
    $body = @{
        provider     = "letsencrypt"
        domain_names = $DomainNames
        meta         = @{
            letsencrypt_email = $leEmail
            letsencrypt_agree = $true
            dns_challenge     = $false
        }
    }
    $resp = Invoke-NpmApi -Method POST -Path "/api/nginx/certificates" -Body $body -Token $token
    if ($resp -and $resp.id) {
        Write-Host "Let's Encrypt cert issued for $($DomainNames -join ', ') (id=$($resp.id))"
        return [int]$resp.id
    }
    Write-Host "warn: Let's Encrypt failed for $($DomainNames -join ', ') — check DNS / ports 80+443, then re-run configure_npm.ps1"
    return 0
}

function Upsert-Host([string]$DomainName, [string]$ForwardHost, [int]$ForwardPort, [string]$Advanced = "") {
    if ([string]::IsNullOrWhiteSpace($DomainName)) { return }
    $hosts = Get-ProxyHosts
    $existing = Find-Host $hosts $DomainName
    $certId = 0
    if ($leEmail) { $certId = Ensure-Cert @($DomainName) }
    $body = @{
        domain_names            = @($DomainName)
        forward_scheme          = "http"
        forward_host            = $ForwardHost
        forward_port            = $ForwardPort
        certificate_id          = $certId
        ssl_forced              = [bool]$certId
        http2_support           = [bool]$certId
        hsts_enabled            = $false
        hsts_subdomains         = $false
        block_exploits          = $true
        caching_enabled         = $false
        allow_websocket_upgrade = $true
        access_list_id          = 0
        advanced_config         = $Advanced
        enabled                 = $true
        meta                    = @{ letsencrypt_agree = $false; dns_challenge = $false }
        locations               = @()
    }
    if ($existing -and $existing.id) {
        if (-not $certId -and $existing.certificate_id) {
            $body.certificate_id = $existing.certificate_id
            $body.ssl_forced = [bool]$existing.ssl_forced
            $body.http2_support = [bool]$existing.http2_support
        }
        $null = Invoke-NpmApi -Method PUT -Path "/api/nginx/proxy-hosts/$($existing.id)" -Body $body -Token $token
        $action = "updated"
    }
    else {
        $null = Invoke-NpmApi -Method POST -Path "/api/nginx/proxy-hosts" -Body $body -Token $token
        $action = "created"
    }
    $scheme = if ($body.ssl_forced) { "https" } else { "http" }
    Write-Host "Proxy host ${action}: ${scheme}://${DomainName} -> ${ForwardHost}:${ForwardPort}"
}

if (-not $domain -and -not $absDomain -and -not $kavitaDomain) {
    # LAN-only: create HTTP proxy host(s) for hostname / primary IP (no GUI).
    $lanNames = New-Object System.Collections.Generic.List[string]
    try {
        $hn = [System.Net.Dns]::GetHostName()
        if ($hn) { [void]$lanNames.Add($hn) }
    } catch {}
    try {
        $ip = (
            Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
            Select-Object -First 1 -ExpandProperty IPAddress
        )
        if (-not $ip) {
            $ip = (
                Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike '127.*' } |
                Select-Object -First 1 -ExpandProperty IPAddress
            )
        }
        if ($ip) { [void]$lanNames.Add([string]$ip) }
    } catch {}
    $unique = @()
    foreach ($n in $lanNames) {
        if ($n -and ($unique -notcontains $n)) { $unique += $n }
    }
    if ($unique.Count -gt 0) {
        Write-Host ("No NPM_DOMAIN - creating LAN HTTP proxy host(s): " + ($unique -join ', '))
        foreach ($name in $unique) {
            Upsert-Host $name "app" 8080 $libraryAdvanced
        }
        Write-Host ("LAN Library URL via NPM: http://" + $unique[0] + "/  (also http://127.0.0.1:8085)")
        Write-Host "  Admin UI: $BaseUrl"
        exit 0
    }
    Write-Host "NPM ready (no domains set - LAN / HTTP-only)."
    Write-Host "  Admin UI: $BaseUrl"
    Write-Host "  Set NPM_DOMAIN (+ optional NPM_LETSENCRYPT_EMAIL) and re-run this script."
    exit 0
}

Upsert-Host $domain "app" 8080 $libraryAdvanced
Upsert-Host $absDomain "audiobookshelf" 80
Upsert-Host $kavitaDomain "kavita" 5000

if ($domain) {
    if ($leEmail) { Set-EnvKey "APP_URL" "https://$domain" }
    else { Set-EnvKey "APP_URL" "http://$domain" }
    Write-Host "APP_URL updated for NPM domain"
}

Write-Host "NPM configure complete"
