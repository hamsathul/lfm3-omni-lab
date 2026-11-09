# Start LFM3-Omni Lab on Windows. On first run it gets everything it needs:
#   - Python 3.12 (with winget) if there's no Python 3.10 or newer, then the lab's .venv;
#   - llama.cpp's llama-server, the build for this computer's GPU (CUDA for NVIDIA, else Vulkan);
#   - ffmpeg (with winget), for audio and video;
#   - the model (LFM3-Omni-2B GGUF, 2.4 GB) from Hugging Face.
# Then it opens the page. The model itself is started from the page (Start model button).
#
#   powershell -ExecutionPolicy Bypass -File start.ps1      (or double-click start.bat)
#
# Settings: $env:PORT, $env:LFM3_MODEL, $env:LFM3_AUTOSTART=1, $env:NO_BROWSER=1,
# $env:LFM3_LLAMA_BUILD (cuda-13.5, cuda-12.4, vulkan or cpu), $env:LFM3_LLAMA_SERVER (your own llama-server.exe).
# Errors are checked by hand: with 'Stop', Windows PowerShell 5.1 aborts when a program writes
# to stderr while this script's output goes to a file (as when the native app starts it).
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot

$Port = if ($env:PORT) { [int]$env:PORT } else { 8765 }
$LlamaTag = 'b12223'  # the llama.cpp release this lab is tested with
$Venv = Join-Path $PSScriptRoot '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'

function Say($text) { Write-Host $text }

function Quote-Arg([string]$a) {
    if ($a -eq '') { return '""' }
    if ($a -notmatch '[\s"]') { return $a }
    return '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

# Run a program with this script's own stdout and stderr, so its progress bars and messages
# reach the console or log as they are (`& program` would turn its stderr into error records).
# -Capture returns its stdout instead. Returns @{ Code; Out }.
function Invoke-Program([string]$exe, [string[]]$arguments, [switch]$Capture) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = (@($arguments) | ForEach-Object { Quote-Arg $_ }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = [bool]$Capture
    $proc = [System.Diagnostics.Process]::Start($psi)
    $out = if ($Capture) { $proc.StandardOutput.ReadToEnd() } else { $null }
    $proc.WaitForExit()
    return @{ Code = $proc.ExitCode; Out = $out }
}

function Update-Path {
    $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + $env:PATH
}

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '3.13', '3.12', '3.11', '3.10') { $candidates += , @('py', "-$v") }
    }
    foreach ($name in 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        # The Microsoft Store stub in WindowsApps opens the Store instead of running Python.
        if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { $candidates += , @($cmd.Source) }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
        try {
            & $exe @rest -c "import sys, struct; sys.exit(not (sys.version_info >= (3, 10) and struct.calcsize('P') == 8))" 2>$null
            if ($LASTEXITCODE -eq 0) { return , $c }
        } catch {}
    }
    return $null
}

function Install-WithWinget($id, $what) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Say "Installing $what with winget..."
    $common = @('install', '-e', '--id', $id, '--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    $r = Invoke-Program 'winget' ($common + @('--scope', 'user')) -Capture
    if ($r.Code -ne 0) {
        # Some packages don't offer a per-user install.
        $r = Invoke-Program 'winget' $common -Capture
    }
    Update-Path
    return $true
}

# ---- Python environment --------------------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    $python = Find-Python
    if (-not $python) {
        if (Install-WithWinget 'Python.Python.3.12' 'Python 3.12') { $python = Find-Python }
    }
    if (-not $python) {
        Say 'No Python 3.10 or newer was found. Install Python 3.12 from https://www.python.org/downloads/ and run this again.'
        exit 1
    }
    $exe = $python[0]; $rest = @($python | Select-Object -Skip 1)
    Say "First run: creating .venv with $($python -join ' ') and installing the lab's packages (a minute or two)..."
    if ((Invoke-Program $exe ($rest + @('-m', 'venv', $Venv))).Code -ne 0) { Say "Couldn't create .venv."; exit 1 }
    Invoke-Program $VenvPython @('-m', 'pip', 'install', '-q', '--upgrade', 'pip') | Out-Null
    if ((Invoke-Program $VenvPython @('-m', 'pip', 'install', '-q', '-r', 'requirements.txt')).Code -ne 0) {
        Remove-Item -Recurse -Force $Venv
        Say "Installing the lab's packages failed; run this again to retry."
        exit 1
    }
}

# ---- llama.cpp ---------------------------------------------------------------------------
$LlamaDir = Join-Path $PSScriptRoot 'llama.cpp'
$LlamaServer = Join-Path $LlamaDir 'llama-server.exe'
if (-not $env:LFM3_LLAMA_SERVER -and -not (Test-Path $LlamaServer)) {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
    $build = $env:LFM3_LLAMA_BUILD
    if (-not $build) {
        $build = 'vulkan'
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
            $driver = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader | Select-Object -First 1)
            $major = 0
            [void][int]::TryParse(($driver -split '\.')[0].Trim(), [ref]$major)
            # CUDA 13 needs driver 580 or newer (and is the one that knows RTX 50-series cards).
            $build = if ($major -ge 580) { 'cuda-13.5' } else { 'cuda-12.4' }
        }
    }
    if ($build -like 'cuda-12*' -and $arch -eq 'arm64') { $build = 'cpu' }
    $zips = @("llama-$LlamaTag-bin-win-$build-$arch.zip")
    if ($build -like 'cuda-*') { $zips += "cudart-llama-bin-win-$build-$arch.zip" }
    Say "Downloading llama.cpp $LlamaTag ($build build) to run the model..."
    New-Item -ItemType Directory -Force -Path $LlamaDir | Out-Null
    foreach ($zip in $zips) {
        $url = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaTag/$zip"
        $dest = Join-Path $LlamaDir $zip
        if ((Invoke-Program 'curl.exe' @('-fL', '--retry', '3', '--progress-bar', '-o', $dest, $url)).Code -ne 0) {
            Say "Couldn't download $url"; exit 1
        }
        try { Expand-Archive -LiteralPath $dest -DestinationPath $LlamaDir -Force -ErrorAction Stop }
        catch { Say "Couldn't unpack ${zip}: $_"; exit 1 }
        Remove-Item -LiteralPath $dest
    }
    if (-not (Test-Path $LlamaServer)) { Say "The llama.cpp download didn't hold llama-server.exe."; exit 1 }
}

# ---- ffmpeg and the web browser -----------------------------------------------------------
$WingetLinks = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
if (Test-Path $WingetLinks) { $env:PATH = "$env:PATH;$WingetLinks" }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Install-WithWinget 'Gyan.FFmpeg' 'ffmpeg (for audio, recording and video)' | Out-Null
    if (Test-Path $WingetLinks) { $env:PATH = "$env:PATH;$WingetLinks" }
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Say 'Note: ffmpeg not found. Audio, recording and video need it: winget install Gyan.FFmpeg'
    }
}

# The Web tools drive a private Chrome through agent-browser (github.com/vercel-labs/agent-browser).
if (-not (Get-Command agent-browser -ErrorAction SilentlyContinue)) {
    Say 'Note: Web needs agent-browser: npm install -g agent-browser, then agent-browser install'
} elseif (-not (Test-Path (Join-Path $env:USERPROFILE '.agent-browser\browsers'))) {
    Say 'Downloading the browser agent-browser drives (once)...'
    $ab = (Get-Command agent-browser).Source
    $r = if ($ab -like '*.ps1') { Invoke-Program 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ab, 'install') }
         else { Invoke-Program $ab @('install') }
    if ($r.Code -ne 0) { Say "Note: agent-browser install failed; Web won't work until it succeeds." }
}

# ---- the server ----------------------------------------------------------------------------
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Say "Port $Port is already in use. If the lab is already running, open http://localhost:$Port"
    Say 'Otherwise pick another port: $env:PORT=8766; .\start.ps1'
    exit 1
}

# The model: the GGUF build that suits this GPU, downloaded if it isn't here. See app/pick_model.py.
$picked = Invoke-Program $VenvPython @('-m', 'app.pick_model') -Capture
$model = ($picked.Out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
if ($picked.Code -ne 0 -or -not $model) { exit 1 }
$env:LFM3_MODEL = $model.Trim()

$url = "http://localhost:$Port"
Say "LFM3-Omni Lab: $url  (Ctrl+C to quit; this also stops the model)"
if ($env:NO_BROWSER -ne '1') {
    Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 2; Start-Process $u } -ArgumentList $url | Out-Null
}
exit (Invoke-Program $VenvPython @('-m', 'uvicorn', 'app.server:app', '--host', '127.0.0.1', '--port', "$Port", '--log-level', 'warning')).Code
