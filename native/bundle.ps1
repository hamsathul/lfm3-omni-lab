# Build the native window for Windows.
#   .\bundle.ps1            -> dist\LFM3-Omni.exe
#   .\bundle.ps1 -Install   -> also installs it for this user, with a Start menu shortcut
#   .\bundle.ps1 -Dist      -> a build for the GitHub release, zipped as
#                              dist\LFM3-Omni-<version>-windows-x64.zip
# It needs Rust (rustup, with the MSVC toolchain) and Visual Studio's C++ build tools.
# A normal build finds the lab in the folder it was built from; a -Dist build looks in the
# usual places or offers to download the lab (see src\lab.rs and src\setup.rs), and keeps
# this computer's folder names out of the program.
param([switch]$Install, [switch]$Dist)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$version = (Select-String -Path Cargo.toml -Pattern '^version = "(.*)"' | Select-Object -First 1).Matches[0].Groups[1].Value
$cargo = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $cargo) { $cargo = Get-Command (Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe') -ErrorAction SilentlyContinue }
if (-not $cargo) { Write-Host 'Rust is needed: https://rustup.rs'; exit 1 }

if ($Dist) {
    # Its own target folder, since the changed flags rebuild everything.
    $env:CARGO_TARGET_DIR = 'target\dist'
    $env:CARGO_ENCODED_RUSTFLAGS = "--remap-path-prefix=$env:USERPROFILE=~"
    & $cargo.Source build --release --features dist
    $bin = 'target\dist\release\lfm3-omni.exe'
} else {
    & $cargo.Source build --release
    $bin = 'target\release\lfm3-omni.exe'
}
if ($LASTEXITCODE -ne 0) { exit 1 }

New-Item -ItemType Directory -Force -Path dist | Out-Null
$exe = Join-Path (Resolve-Path dist) 'LFM3-Omni.exe'
Copy-Item -LiteralPath $bin -Destination $exe -Force
Write-Host "Built dist\LFM3-Omni.exe ($version)"

if ($Dist) {
    $bytes = [IO.File]::ReadAllBytes($exe)
    $text = [Text.Encoding]::ASCII.GetString($bytes)
    if ($text.Contains($env:USERPROFILE) -or $text.Contains($env:USERPROFILE.Replace('\', '/'))) {
        Write-Host "The release build still names $env:USERPROFILE; not zipping it."
        exit 1
    }
    $zip = "dist\LFM3-Omni-$version-windows-x64.zip"
    Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue
    Compress-Archive -LiteralPath $exe -DestinationPath $zip
    Write-Host "Release zip: native\$zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
}

if ($Install) {
    $home_ = Join-Path $env:LOCALAPPDATA 'Programs\LFM3-Omni'
    New-Item -ItemType Directory -Force -Path $home_ | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $home_ 'LFM3-Omni.exe') -Force
    $shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'LFM3-Omni.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = Join-Path $home_ 'LFM3-Omni.exe'
    $link.WorkingDirectory = $home_
    $link.Save()
    Write-Host "Installed $home_\LFM3-Omni.exe, with LFM3-Omni in the Start menu"
}
