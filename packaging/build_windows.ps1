param(
    [switch]$BundleChromium,
    [switch]$SkipWebBuild,
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $Version) {
    $Version = python -c "import sys; sys.path.insert(0, 'src'); import cuaflow; print(cuaflow.__version__)"
}
Write-Host "==> Building cuaflow v$Version Windows installer..." -ForegroundColor Cyan

if (-not $SkipWebBuild) {
    Write-Host "==> [1/5] Building Web Console..." -ForegroundColor Cyan
    Push-Location "$Root\web"
    try {
        npm ci
        npm run build
    } finally {
        Pop-Location
    }
}

Write-Host "==> [2/5] Preparing Python packaging dependencies & icon..." -ForegroundColor Cyan
python -m pip install -e ".[win]" pyinstaller
python "$PSScriptRoot\generate_icon.py"

if ($BundleChromium) {
    Write-Host "==> Downloading standalone Chromium into local bundle directory..." -ForegroundColor Yellow
    $env:PLAYWRIGHT_BROWSERS_PATH = "$Root\build\ms-playwright"
    $env:CUAFLOW_BUNDLE_CHROMIUM = "1"
    playwright install chromium
} else {
    $env:CUAFLOW_BUNDLE_CHROMIUM = "0"
}

Write-Host "==> [3/5] Freezing standalone executables with PyInstaller..." -ForegroundColor Cyan
if (Test-Path "$Root\dist\cuaflow") { Remove-Item -Recurse -Force "$Root\dist\cuaflow" }
pyinstaller "$PSScriptRoot\cuaflow.spec" --noconfirm --clean

Write-Host "==> [4/5] Creating portable archive..." -ForegroundColor Cyan
$ZipPath = "$Root\dist\cuaflow-portable-$Version-win64.zip"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path "$Root\dist\cuaflow\*" -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "    Portable ZIP: $ZipPath" -ForegroundColor Green

Write-Host "==> [5/5] Compiling Windows Setup .exe with Inno Setup 6..." -ForegroundColor Cyan
# Ensure installer.iss has UTF-8 BOM so Inno Setup parses Chinese characters cleanly
$IssSource = "$PSScriptRoot\installer.iss"
$IssBuild = "$Root\build\installer_utf8bom.iss"
New-Item -ItemType Directory -Force -Path "$Root\build" | Out-Null
$Content = [System.IO.File]::ReadAllText($IssSource, [System.Text.Encoding]::UTF8)
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($IssBuild, $Content, $Utf8Bom)
Copy-Item -Force "$PSScriptRoot\cuaflow.ico" "$Root\build\cuaflow.ico"

$IsccCandidates = @(
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

if ($IsccCandidates.Count -eq 0) {
    Write-Warning "Inno Setup 6 (ISCC.exe) not found; skipping .exe installer compilation. Portable ZIP is ready at $ZipPath"
    exit 0
}

$Iscc = $IsccCandidates[0]
& $Iscc "/DMyAppVersion=$Version" "$IssBuild"
$SetupExe = "$Root\dist\cuaflow-setup-$Version-win64.exe"
if (-not (Test-Path $SetupExe)) {
    throw "Expected installer not found at $SetupExe"
}
Write-Host "    Installer EXE: $SetupExe" -ForegroundColor Green

# Generate SHA256 checksums
$HashLines = @()
foreach ($Artifact in @($SetupExe, $ZipPath)) {
    if (Test-Path $Artifact) {
        $H = (Get-FileHash -Algorithm SHA256 $Artifact).Hash.ToLower()
        $Name = Split-Path -Leaf $Artifact
        $HashLines += "$H  $Name"
    }
}
[System.IO.File]::WriteAllLines("$Root\dist\SHA256SUMS.txt", $HashLines)
Write-Host "==> Done! Artifacts written to $Root\dist" -ForegroundColor Cyan
