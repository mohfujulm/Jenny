param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtual environment not found at .venv. Create it first with: python -m venv .venv"
}

& $python -m app.ocr
if ($LASTEXITCODE -ne 0) {
    Write-Warning "PDF OCR is not ready. The app will still start, but scanned PDFs will fail until the configured OCR engine is installed."
}

$args = @(
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    $HostAddress,
    "--port",
    $Port
)

$enableReload = $Reload -and (-not $NoReload)

if ($enableReload) {
    $args += @(
        "--reload",
        "--reload-dir",
        "app",
        "--reload-exclude",
        "app/data/*",
        "--reload-exclude",
        ".venv/*",
        "--reload-exclude",
        "outputs/*",
        "--reload-exclude",
        "work/*"
    )
}

if ($enableReload) {
    Write-Host "Starting Team Knowledge Agent on http://$HostAddress`:$Port with code reload enabled (watching app/ only)"
} else {
    Write-Host "Starting Team Knowledge Agent on http://$HostAddress`:$Port"
    Write-Host "Tip: use -Reload only when editing code. It is slower on Windows."
}

& $python @args
