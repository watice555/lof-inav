param(
    [string]$ProjectRoot,
    [int]$Port = 8001
)

$ErrorActionPreference = "SilentlyContinue"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

$projectRootPath = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd("\")
$servePath = Join-Path $projectRootPath "serve.py"
$venvPythonPath = Join-Path $projectRootPath ".venv\Scripts\python.exe"
$expectedExePaths = @(
    (Join-Path $projectRootPath "LOF_iNAV.exe"),
    (Join-Path $projectRootPath "dist\LOF_iNAV.exe")
) | ForEach-Object {
    if (Test-Path -LiteralPath $_) { (Resolve-Path -LiteralPath $_).Path }
}
$pidPaths = @(
    (Join-Path $projectRootPath "data\lof_inav.pid"),
    (Join-Path $projectRootPath "dist\data\lof_inav.pid")
) | Select-Object -Unique
$servePattern = [regex]::Escape($servePath)
$checkedPorts = @($Port)

function Get-ListenerPids {
    param([int]$CandidatePort)

    return @(
        Get-NetTCPConnection -LocalPort $CandidatePort -State Listen |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Test-ProjectServerProcess {
    param($Process, [int]$CandidatePort)

    $listenerPids = @(Get-ListenerPids $CandidatePort)
    if (-not $Process -or $Process.ProcessId -notin $listenerPids) {
        return $false
    }
    $usesExactServePath = $Process.CommandLine -and $Process.CommandLine -match $servePattern
    $usesProjectVenv = $Process.ExecutablePath -and
        $Process.ExecutablePath.Equals($venvPythonPath, [System.StringComparison]::OrdinalIgnoreCase) -and
        $Process.CommandLine -match '(^|[\s"''])serve\.py([\s"'']|$)'
    $usesProjectExe = $Process.ExecutablePath -and ($expectedExePaths -contains $Process.ExecutablePath)
    return $usesExactServePath -or $usesProjectVenv -or $usesProjectExe
}

$targets = @()
foreach ($pidPath in $pidPaths) {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        continue
    }
    try {
        $record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
        $candidatePid = [int]$record.pid
        $candidatePort = if ($record.port) { [int]$record.port } else { $Port }
        $checkedPorts += $candidatePort
    } catch {
        Write-Host ("Ignoring invalid PID file: {0}" -f $pidPath)
        continue
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$candidatePid"
    $candidateListenerPids = @(Get-ListenerPids $candidatePort)
    if (Test-ProjectServerProcess $process $candidatePort) {
        $targets += [pscustomobject]@{
            Process = $process
            PidPath = $pidPath
            Port = $candidatePort
        }
    } elseif (-not $process -or $candidatePid -notin $candidateListenerPids) {
        Remove-Item -LiteralPath $pidPath -Force
    } else {
        Write-Host ("PID file was not trusted; leaving process {0} untouched." -f $candidatePid)
    }
}

# Backward-compatible fallback for a server started before PID-file support.
if (-not $targets) {
    $listenerPids = @(Get-ListenerPids $Port)
    foreach ($listenerPid in $listenerPids) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
        if (Test-ProjectServerProcess $process $Port) {
            $targets += [pscustomobject]@{
                Process = $process
                PidPath = $null
                Port = $Port
            }
        }
    }
}

$targets = @($targets | Sort-Object { $_.Process.ProcessId } -Unique)
if (-not $targets) {
    Write-Host "No verified LOF iNAV server process found."
} else {
    foreach ($target in $targets) {
        $process = $target.Process
        Write-Host ("Stopping LOF iNAV server PID {0}: {1}" -f $process.ProcessId, $process.CommandLine)
        Stop-Process -Id $process.ProcessId -Force
        if ($target.PidPath -and (Test-Path -LiteralPath $target.PidPath)) {
            Remove-Item -LiteralPath $target.PidPath -Force
        }
    }
    Start-Sleep -Milliseconds 500
    Write-Host "LOF iNAV server stopped."
}

$checkedPorts = @($checkedPorts | Sort-Object -Unique)
foreach ($checkedPort in $checkedPorts) {
    $remainingListeners = @(Get-ListenerPids $checkedPort)
    if (-not $remainingListeners) {
        Write-Host ("Port {0} is free." -f $checkedPort)
    } else {
        Write-Host ("Port {0} is still used by unverified process(es):" -f $checkedPort)
        foreach ($listenerPid in $remainingListeners) {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
            Write-Host ("  PID {0}: {1}" -f $listenerPid, $process.CommandLine)
        }
    }
}
