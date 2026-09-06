<#
.SYNOPSIS
    Build a standalone Windows release of discdig — no Python needed to run it.

.DESCRIPTION
    Packs the app with PyInstaller in one-folder mode and zips the result. The
    reader extracts the zip anywhere and runs discdig.exe.

    One-folder rather than one-file: a single .exe has to unpack ~40 MB to a
    temp directory on every launch, which is a visible pause on a tool you open
    to type one search into. The folder form starts immediately.

.PARAMETER Clean
    Remove build/, dist/ and the .spec file first.

.PARAMETER SkipNetworkCheck
    Skip the parts of the smoke test that reach discmaster and archive.org.
    Useful in CI, where a third-party outage should not fail a build.

.EXAMPLE
    .\build_windows.ps1
    .\build_windows.ps1 -Clean
    .\build_windows.ps1 -Clean -SkipNetworkCheck
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipNetworkCheck
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$name    = 'discdig'
$version = (Select-String -Path 'discdig\__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$outName = "$name-$version-windows-x64"
$python  = '.\.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "No virtualenv found at $python. Run:  uv venv --python 3.12 .venv;  uv pip install -e ."
}

# Prefer uv when it is on PATH, but do not require it -- CI runners and anyone
# using plain `python -m venv` should be able to build too.
$useUv = $null -ne (Get-Command uv -ErrorAction SilentlyContinue)
if (-not $useUv) {
    & $python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '==> installing pyinstaller into .venv' -ForegroundColor Cyan
        # A uv-created virtualenv ships without pip, so bootstrap one first.
        & $python -m pip --version 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '    bootstrapping pip' -ForegroundColor DarkGray
            & $python -m ensurepip --upgrade 2>&1 | Out-Null
        }
        & $python -m pip install --quiet --disable-pip-version-check pyinstaller
        if ($LASTEXITCODE -ne 0) {
            throw "Could not install PyInstaller. Install uv, or run: $python -m pip install pyinstaller"
        }
    }
}

if ($Clean) {
    Write-Host '==> cleaning' -ForegroundColor Cyan
    Remove-Item -Recurse -Force build, dist, "$name.spec" -ErrorAction SilentlyContinue
}

Write-Host "==> building $outName" -ForegroundColor Cyan

# --collect-all textual: Textual ships its own .tcss and widget data files that
#   a bare import scan will not see.
# --add-data: our stylesheet, placed where discdig.app.asset() looks for it.
# --console: this is a terminal application; never build it windowed.
$pyiArgs = @(
    '--noconfirm', '--clean',
    '--name', $name,
    '--onedir', '--console',
    '--collect-all', 'textual',
    '--collect-all', 'textual_dev',
    '--add-data', 'discdig\discdig.tcss;discdig',
    '--hidden-import', 'discdig.app',
    '--hidden-import', 'discdig.cli',
    'packaging\entry.py'
)

if ($useUv) {
    & uv run --with pyinstaller pyinstaller @pyiArgs
} else {
    & $python -m PyInstaller @pyiArgs
}

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$dist = Join-Path 'dist' $name
if (-not (Test-Path (Join-Path $dist "$name.exe"))) { throw "no exe produced in $dist" }

Write-Host '==> adding readme + licence' -ForegroundColor Cyan
Copy-Item 'LICENSE' $dist -Force
@"
discdig $version - browse and download discmaster.textfiles.com

Run discdig.exe in this folder. No installation, and no Python required.

  discdig.exe              open the app
  discdig.exe check        verify this build works on your machine
  discdig.exe --help       the command line options

Settings and the download queue live in %USERPROFILE%\.discdig
Downloads go to %USERPROFILE%\Downloads\discmaster unless you change it
with the ',' key inside the app.

Keep this whole folder together - discdig.exe needs the files beside it.

Project:  https://github.com/starrlord/discdig
Licence:  MIT (see LICENSE)

discdig is an independent client, not affiliated with DiscMaster or the
Internet Archive. Please use it considerately.
"@ | Set-Content -Path (Join-Path $dist 'README.txt') -Encoding UTF8

Write-Host '==> smoke test' -ForegroundColor Cyan
$exe = Join-Path $dist "$name.exe"
& $exe --version
if ($LASTEXITCODE -ne 0) { throw '--version failed' }
if ($SkipNetworkCheck) {
    & $exe check --offline
} else {
    & $exe check
}
if ($LASTEXITCODE -ne 0) { throw 'check failed - the build is not sound' }

Write-Host '==> zipping' -ForegroundColor Cyan
$zip = "dist\$outName.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $dist -DestinationPath $zip -CompressionLevel Optimal

$mb = [Math]::Round((Get-Item $zip).Length / 1MB, 1)
$folderMb = [Math]::Round(((Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host ''
Write-Host "Built $zip  ($mb MB zipped, $folderMb MB extracted)" -ForegroundColor Green
Write-Host "Extract anywhere and run $name.exe"
