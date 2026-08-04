<#
Compiles the small WinForms bootstrapper into AskJenny.exe using the .NET
Framework compiler already included with Windows. The executable only locates
and launches tray.ps1; the Python application remains the actual server.
#>
param(
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $root "AskJenny.exe"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $root $OutputPath
}

$sourcePath = Join-Path $root "launcher\AskJenny.cs"
$manifestPath = Join-Path $root "launcher\AskJenny.manifest"
$iconPath = Join-Path $root "app\static\jenny.ico"
$outputDirectory = Split-Path -Parent $OutputPath

# Prefer the 64-bit compiler but retain compatibility with 32-bit installations.
$compilerCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$compiler = $compilerCandidates |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1

if (-not $compiler) {
    throw "The .NET Framework C# compiler was not found."
}

foreach ($requiredPath in @($sourcePath, $manifestPath, $iconPath)) {
    if (-not (Test-Path $requiredPath)) {
        throw "Required launcher asset not found: $requiredPath"
    }
}

if (-not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

# winexe suppresses a console window; the manifest and icon provide desktop metadata.
$compilerArguments = @(
    "/nologo",
    "/target:winexe",
    "/optimize+",
    "/platform:anycpu",
    "/reference:System.dll",
    "/reference:System.Windows.Forms.dll",
    "/win32icon:$iconPath",
    "/win32manifest:$manifestPath",
    "/out:$OutputPath",
    $sourcePath
)

& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "AskJenny launcher compilation failed with exit code $LASTEXITCODE."
}

$builtFile = Get-Item $OutputPath
Write-Host "Built AskJenny launcher:"
Write-Host "  $($builtFile.FullName)"
Write-Host "  Version $($builtFile.VersionInfo.FileVersion)"
Write-Host "  $($builtFile.Length) bytes"
