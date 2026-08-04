<#
Starts the local FastAPI server from the project's virtual environment.
Before launch it reports optional OCR readiness and distinguishes network
reachability from API authentication (HTTP 401 still proves the host is reachable).
#>
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

Write-Host "Checking OpenAI API network access..."
$openAiNetworkAvailable = $false
$openAiNetworkDetail = "Unable to connect to the OpenAI API."
try {
    $networkResponse = Invoke-WebRequest `
        -UseBasicParsing `
        -Uri "https://api.openai.com/v1/models" `
        -Method Get `
        -TimeoutSec 8
    $openAiNetworkAvailable = $networkResponse.StatusCode -ge 200 -and $networkResponse.StatusCode -lt 500
    $openAiNetworkDetail = "HTTP $($networkResponse.StatusCode)"
} catch {
    $networkStatusCode = 0
    if ($null -ne $_.Exception.Response) {
        try {
            $networkStatusCode = [int]$_.Exception.Response.StatusCode
        } catch {
            $networkStatusCode = 0
        }
    }
    if ($networkStatusCode -eq 401) {
        $openAiNetworkAvailable = $true
        $openAiNetworkDetail = "HTTP 401 (expected without API authentication)"
    } else {
        $openAiNetworkDetail = $_.Exception.Message
    }
}

if ($openAiNetworkAvailable) {
    Write-Host "OpenAI API network access is available. $openAiNetworkDetail"
} else {
    Write-Warning "OpenAI API network access is blocked. The app will start in degraded mode, but new document embeddings and AI responses may fail. $openAiNetworkDetail"
}

# Build arguments as an array so host/path values are passed without shell re-parsing.
$args = @(
    "-m",
    "uvicorn",
    "app.main:app",
    "--host",
    $HostAddress,
    "--port",
    $Port
)

# Reload is opt-in because broad Windows filesystem watching is comparatively slow.
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
