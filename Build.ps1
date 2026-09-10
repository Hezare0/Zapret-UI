param([string]$OutputDirectory = (Join-Path $PSScriptRoot 'dist'))
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $OutputDirectory 'ZapretClient')) {
    throw 'The output directory already exists. Choose a new -OutputDirectory; existing files are preserved.'
}
& '.\.venv\Scripts\python.exe' '.\build_support\build.py' --output $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Host "Build: $OutputDirectory\ZapretClient\ZapretClient.exe (distribute the whole directory)"
