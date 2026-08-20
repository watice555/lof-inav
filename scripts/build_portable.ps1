param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$')]
    [string]$Version,
    [string]$SourceDatabase = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "The Windows portable package must be built on Windows."
}
if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne "X64") {
    throw "This script produces win-x64 packages and must run on Windows x64."
}

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

if (-not $SourceDatabase) {
    $SourceDatabase = Join-Path $ProjectRoot "data\lof_inav.sqlite3"
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    python -m venv .venv
    $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}

& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python scripts\validate_config.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if (-not $SkipTests) {
    & $Python -m unittest discover -s tests -p "test_*.py"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$SeedDatabase = Join-Path $ProjectRoot "build\portable_seed\lof_inav.sqlite3"
& $Python scripts\create_seed_database.py `
    --source $SourceDatabase `
    --output $SeedDatabase `
    --force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Python -m PyInstaller --clean --noconfirm packaging\lof_inav.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$PackageName = "LOF_iNAV-Portable-$Version-win-x64"
$PackageRoot = Join-Path $ProjectRoot "build\portable_package"
$PackageDirectory = Join-Path $PackageRoot $PackageName
$ArchivePath = Join-Path $ProjectRoot "dist\$PackageName.zip"
$PortableReadmeName = ([char[]]@(0x4F7F, 0x7528, 0x8BF4, 0x660E) -join "") + ".txt"
$StopCommandName = ([char[]]@(0x5173, 0x95ED) -join "") + " LOF_iNAV.bat"

if (Test-Path $PackageRoot) {
    Remove-Item -Recurse -Force $PackageRoot
}
New-Item -ItemType Directory -Path $PackageDirectory | Out-Null
Copy-Item -Recurse -Force "dist\LOF_iNAV\*" $PackageDirectory
Copy-Item "packaging\PORTABLE_README.txt" (Join-Path $PackageDirectory $PortableReadmeName)
Copy-Item "packaging\STOP_LOF_iNAV.bat" (Join-Path $PackageDirectory $StopCommandName)
Copy-Item "LICENSE" $PackageDirectory
New-Item -ItemType Directory -Path (Join-Path $PackageDirectory "config") | Out-Null
Copy-Item "config\fund_rules.json" (Join-Path $PackageDirectory "config\fund_rules.json")
New-Item -ItemType Directory -Path (Join-Path $PackageDirectory "scripts") | Out-Null
Copy-Item "scripts\stop_server.ps1" (Join-Path $PackageDirectory "scripts\stop_server.ps1")

if (Test-Path $ArchivePath) {
    Remove-Item -Force $ArchivePath
}
Compress-Archive -Path $PackageDirectory -DestinationPath $ArchivePath -CompressionLevel Optimal

Write-Host ""
Write-Host "Built $ArchivePath"
Write-Host "Test the ZIP after extracting it to a clean, writable directory."
