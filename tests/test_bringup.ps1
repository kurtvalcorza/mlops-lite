<#
.SYNOPSIS
  One-command bring-up/teardown smoke test (002 US3, T056 / SC-010).

.DESCRIPTION
  From a running platform: assert the gateway is healthy AND resolves every daemon
  (`/platform/health`) AND the supervisor reports all daemons healthy (:8099/status).
  Then `down_all` and assert the gateway is gone (compose down) and the GPU has no leftover
  compute processes (no orphans). Finally `up_all` again to restore the platform.

  Run AFTER `up_all` (or it will fail the "resolves all" assertion). Exits non-zero on failure.
  Pass -FullCycle to also exercise a fresh up_all at the end (default does the restore).
#>
param([string]$Distro = "Ubuntu")

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$gwPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }
$gw = "http://localhost:$gwPort"
$fail = 0

function Check($name, $cond) {
    if ($cond) { Write-Host "[OK] $name" -ForegroundColor Green }
    else { Write-Host "[FAIL] $name" -ForegroundColor Red; $script:fail++ }
}

# 1. Platform is up: gateway healthy + resolves all daemons.
try { $hz = Invoke-RestMethod "$gw/healthz" -TimeoutSec 5 } catch { $hz = $null }
Check "gateway /healthz ok" ($hz.status -eq "ok")

try { $ph = Invoke-RestMethod "$gw/platform/health" -TimeoutSec 8 } catch { $ph = $null }
Check "gateway resolves all daemons (/platform/health all_healthy)" ($ph.all_healthy -eq $true)

$sup = (wsl.exe -d $Distro bash -c "curl -s http://localhost:8099/status") | ConvertFrom-Json
$states = @($sup.daemons | ForEach-Object { $_.state })
Check "supervisor reports all daemons healthy" ($states.Count -ge 1 -and ($states | Where-Object { $_ -ne 'healthy' }).Count -eq 0)

# 2. Teardown: down_all, then assert gateway gone + no GPU orphans.
Write-Host "`n--- down_all ---" -ForegroundColor Cyan
& "$repo/scripts/down_all.ps1" -Distro $Distro | Write-Host

Start-Sleep 3
$gwGone = $false
try { Invoke-RestMethod "$gw/healthz" -TimeoutSec 4 | Out-Null } catch { $gwGone = $true }
Check "gateway is down after down_all" $gwGone

$apps = (wsl.exe -d $Distro bash -c "nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null").Trim()
Check "no leftover GPU compute processes" ([string]::IsNullOrWhiteSpace($apps))
if (-not [string]::IsNullOrWhiteSpace($apps)) { Write-Host "  leftover: $apps" -ForegroundColor Red }

# 3. Restore the platform.
Write-Host "`n--- up_all (restore) ---" -ForegroundColor Cyan
& "$repo/scripts/up_all.ps1" -Distro $Distro | Write-Host

if ($fail -eq 0) { Write-Host "`nT056 PASS — one-command up/down verified (healthy bring-up, clean teardown, no GPU orphans)" -ForegroundColor Green; exit 0 }
else { Write-Host "`n$fail check(s) failed." -ForegroundColor Red; exit 1 }
