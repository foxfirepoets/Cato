# Cato Desktop Build Script
# Discovers Visual Studio Build Tools via vswhere/VsDevCmd and runs `npx tauri build`.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$Message) {
    Write-Host "`n$Message" -ForegroundColor Yellow
}

function Find-VsWhere {
    $fromPath = Get-Command vswhere.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    $candidates = @(
        $fromPath,
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

    if (-not $candidates) {
        throw "vswhere.exe not found. Install Visual Studio 2022 Build Tools or Visual Studio 2022."
    }

    return @($candidates)[0]
}

function Import-VsDevEnvironment {
    $vswhere = Find-VsWhere
    $installPath = & $vswhere `
        -latest `
        -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath

    if (-not $installPath) {
        throw "No Visual Studio installation with C++ Build Tools was found."
    }

    $vsDevCmd = Join-Path $installPath "Common7\Tools\VsDevCmd.bat"
    if (-not (Test-Path $vsDevCmd)) {
        throw "VsDevCmd.bat not found at $vsDevCmd"
    }

    $envDump = & cmd.exe /s /c "`"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul && set"
    if ($LASTEXITCODE -ne 0) {
        throw "VsDevCmd.bat failed to initialize the MSVC environment."
    }

    foreach ($line in $envDump) {
        if ($line -match "^(.*?)=(.*)$") {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }

    return $installPath
}

function Assert-Command([string]$Name, [string]$Hint) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "$Name not found. $Hint"
    }
    return $cmd
}

function Assert-TauriCli {
    & npx tauri --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri CLI is unavailable. Run `npm ci` in desktop/ and ensure @tauri-apps/cli is installed."
    }
}

function Install-DesktopShortcut {
    param(
        [string]$RepoRoot,
        [string]$ShortcutPath
    )

    $launcher = Join-Path $RepoRoot "Launch-CatoDesktop.ps1"
    if (-not (Test-Path -LiteralPath $launcher)) {
        throw "desktop launcher not found at $launcher"
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    $shortcut.WorkingDirectory = $RepoRoot
    $shortcut.IconLocation = Join-Path $RepoRoot "desktop\src-tauri\target\release\cato-desktop.exe"
    $shortcut.Description = "Launch Cato Desktop with daemon health checks"
    $shortcut.Save()

    Write-Host "Shortcut: $ShortcutPath" -ForegroundColor Green
}

$desktopDir = $PSScriptRoot
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $desktopDir "..")).Path
Set-Location $desktopDir

Write-Host "=== Cato Desktop Build ===" -ForegroundColor Cyan

Write-Step "Syncing desktop manifests to the canonical Cato version..."
python ..\scripts\sync_version.py --write
if ($LASTEXITCODE -ne 0) {
    throw "version sync failed"
}

$vsInstallPath = Import-VsDevEnvironment
Write-Host "Visual Studio: $vsInstallPath" -ForegroundColor Green

$nodeCmd = Assert-Command "node.exe" "Install Node.js 20+."
$cargoCmd = Assert-Command "cargo.exe" "Install Rust with the stable MSVC toolchain."
$npmCmd = Assert-Command "npm.cmd" "Install Node.js 20+."
$clCmd = Assert-Command "cl.exe" "Install Visual Studio 2022 Build Tools with Desktop development for C++."

Write-Host "Node:  $($nodeCmd.Source)" -ForegroundColor Green
Write-Host "Cargo: $($cargoCmd.Source)" -ForegroundColor Green
Write-Host "npm:   $($npmCmd.Source)" -ForegroundColor Green
Write-Host "cl:    $($clCmd.Source)" -ForegroundColor Green

Write-Step "Installing npm dependencies with npm ci..."
npm ci
if ($LASTEXITCODE -ne 0) {
    throw "npm ci failed. Stop any running Vite/Node processes that are locking desktop\\node_modules and retry."
}

Assert-TauriCli

Write-Step "Building Tauri app..."
npx tauri build
if ($LASTEXITCODE -ne 0) {
    throw "tauri build failed"
}

Write-Host "`n=== Build Complete ===" -ForegroundColor Green
Write-Host "EXE:  src-tauri\target\release\cato-desktop.exe"
Write-Host "MSI:  src-tauri\target\release\bundle\msi\"
Write-Host "NSIS: src-tauri\target\release\bundle\nsis\"

Write-Step "Updating desktop shortcut to use Launch-CatoDesktop.ps1..."
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Cato.lnk"
Install-DesktopShortcut -RepoRoot $repoRoot -ShortcutPath $desktopShortcut
