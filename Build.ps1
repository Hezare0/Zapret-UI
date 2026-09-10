param([string]$OutputDirectory = (Join-Path $PSScriptRoot 'dist'), [switch]$OneDirectory)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $OutputDirectory 'ZapretClient')) {
    throw 'The output directory already exists. Choose a new -OutputDirectory; existing files are preserved.'
}
$taskBuildArguments = @('.\build_support\build.py', '--output', $OutputDirectory)
if ($OneDirectory) { $taskBuildArguments += '--onedir' }
& '.\.venv\Scripts\python.exe' @taskBuildArguments
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Host "Build completed in $OutputDirectory (single EXE by default)."
