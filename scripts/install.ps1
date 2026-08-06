param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

& $Python -m venv .venv
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e ".[dev]"
& $VenvPython -m playwright install chromium

if (-not (Test-Path -LiteralPath "config.yaml")) {
    Copy-Item -LiteralPath "config.example.yaml" -Destination "config.yaml"
}

Write-Host "安装完成。下一步：编辑 config.yaml，然后设置系统凭据。"
