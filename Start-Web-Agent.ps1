param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "Starting xai-computer web agent..." -ForegroundColor Cyan

if (-not (Test-Path ".env")) {
    Write-Host "No .env file found. Copy .env.example to .env and set XAI_API_KEY." -ForegroundColor Yellow
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
    Write-Host "Using .venv Python." -ForegroundColor DarkGray
} else {
    $python = "python"
    Write-Host "Using python from PATH. Create .venv for a more predictable launch." -ForegroundColor DarkGray
}

if (-not (Test-Path "web\dist\index.html")) {
    Write-Host "web/dist is missing; building the web app..." -ForegroundColor Yellow
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "npm was not found. Install Node.js 20+ and run: cd web; npm install; npm run build" -ForegroundColor Red
        exit 1
    }
    Push-Location "web"
    try {
        if (-not (Test-Path "node_modules")) {
            npm install
        }
        npm run build
    } finally {
        Pop-Location
    }
}

Write-Host "Opening http://127.0.0.1:$Port" -ForegroundColor Green
& $python web_server.py --host 127.0.0.1 --port $Port --open
