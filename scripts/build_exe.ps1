param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)

Write-Warning "build_exe.ps1 now builds the portable onedir ZIP."
& "$PSScriptRoot\build_portable.ps1" -Version $Version
exit $LASTEXITCODE
