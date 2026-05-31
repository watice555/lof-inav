param(
    [string]$ProjectRoot,
    [int]$Port = 8000
)

$ErrorActionPreference = "SilentlyContinue"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$projectRootPath = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd("\")
$servePath = Join-Path $projectRootPath "serve.py"
$startPath = Join-Path $projectRootPath "start.bat"

$servePattern = [regex]::Escape($servePath)
$legacyServePattern = '(^|[\s"''])serve\.py([\s"'']|$)'
$startPattern = [regex]::Escape($startPath)

$listenerPids = @(
    Get-NetTCPConnection -LocalPort $Port -State Listen |
        Select-Object -ExpandProperty OwningProcess -Unique
)

$targets = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            if ($_.CommandLine) {
                $usesCurrentServePath = $_.CommandLine -match $servePattern
                $isLegacyProjectServer = ($_.ProcessId -in $listenerPids) -and ($_.CommandLine -match $legacyServePattern)

                $usesCurrentServePath -or $isLegacyProjectServer
            } else {
                $false
            }
        }
)

if (-not $targets) {
    Write-Host "No LOF iNAV server process found."
} else {
    foreach ($target in $targets) {
        Write-Host ("Stopping LOF iNAV server PID {0}: {1}" -f $target.ProcessId, $target.CommandLine)
        Stop-Process -Id $target.ProcessId -Force
    }

    Start-Sleep -Milliseconds 500

    foreach ($parentId in ($targets | Select-Object -ExpandProperty ParentProcessId -Unique)) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId"
        if ($parent -and $parent.CommandLine -match $startPattern) {
            Stop-Process -Id $parent.ProcessId -Force
        }
    }

    Write-Host "LOF iNAV server stopped."
}

$remainingListeners = @(
    Get-NetTCPConnection -LocalPort $Port -State Listen |
        Select-Object -ExpandProperty OwningProcess -Unique
)

if (-not $remainingListeners) {
    Write-Host ("Port {0} is free." -f $Port)
} else {
    Write-Host ("Port {0} is still used by non-project process(es):" -f $Port)
    foreach ($listenerPid in $remainingListeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
        if ($process) {
            Write-Host ("  PID {0}: {1}" -f $listenerPid, $process.CommandLine)
        } else {
            Write-Host ("  PID {0}: <process details unavailable>" -f $listenerPid)
        }
    }
}
