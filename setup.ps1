<#
.SYNOPSIS
    One-command setup of a PlaySmart capture PC (Windows 10/11).

.DESCRIPTION
    Installs what the capture rig needs and wires it up, idempotently - run it again
    after a partial failure and it skips what is already done.

      1. winget: Git, Python 3.10, OBS Studio, VC++ 2017 x64 (OpenFace), GitHub CLI
      2. pipx + Poetry, Poetry environment on Python 3.10, `poetry install`
      3. OpenFace 2.2.0 (binary zip from the OpenFace GitHub release) + its CEN models
      4. .env from .env.example, asking for the OBS and SFTP passwords
      5. Points you at the two things it cannot do: Tobii Eye Tracker Manager
         (calibration) and League of Legends (Vanguard, account login)

    What it deliberately does NOT do: touch OBS's own settings (see obs settings\readme.md
    and TESTING.md section 3), or install the 3 GB of Torch/Whisper/YOLO the capture does
    not use (pass -WithTranscription / -WithVision if you want them).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithTranscription

.NOTES
    Added 2026-09 (iteration 5). Run from the repo root; winget prompts for admin
    (UAC) for the installers that need it.
#>
[CmdletBinding()]
param(
    [switch]$SkipInstalls,       # skip winget entirely (offline, or already done)
    [switch]$SkipOpenFace,       # you will set PLAYSMART_OPENFACE=0
    [switch]$WithTranscription,  # poetry --with transcription (+ ffmpeg)
    [switch]$WithVision,         # poetry --with vision
    [switch]$NoPrompt            # do not ask for passwords (CI / unattended)
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Repo

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   ok   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   warn $msg" -ForegroundColor Yellow }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
}

function Winget-Install($id, $label) {
    $ErrorActionPreference = "Continue"   # winget writes progress to stderr; do not let Stop turn that into a failure
    $listed = winget list --id $id -e --accept-source-agreements 2>$null | Select-String -SimpleMatch $id
    if ($listed) { Ok "$label already installed"; return }
    Write-Host "   installing $label ..."
    winget install --id $id -e --accept-package-agreements --accept-source-agreements --silent | Out-Null
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # -1978335189 = already installed / no upgrade
        Warn "${label}: winget exit $LASTEXITCODE - install it by hand and re-run"
    } else { Ok "$label installed" }
}

# ---------------------------------------------------------------- 1. installs
Step "System packages (winget)"
if ($SkipInstalls) {
    Warn "-SkipInstalls: not touching winget"
} elseif (-not (Have winget)) {
    Warn "winget not found. Install 'App Installer' from the Microsoft Store, or install Git, Python 3.10, OBS Studio and the VC++ 2017 x64 redistributable by hand, then re-run with -SkipInstalls."
} else {
    Winget-Install "Git.Git"                          "Git"
    Winget-Install "Python.Python.3.10"               "Python 3.10"
    Winget-Install "OBSProject.OBSStudio"             "OBS Studio"
    Winget-Install "Microsoft.VCRedist.2015+.x64"     "VC++ 2015-2022 x64 (OpenFace)"
    Winget-Install "GitHub.cli"                       "GitHub CLI"
    if ($WithTranscription) { Winget-Install "Gyan.FFmpeg" "ffmpeg (Whisper)" }
    Refresh-Path
}

# ------------------------------------------------------------- 2. python env
Step "Python 3.10 and Poetry"
$py310 = $null
try { $py310 = (& py -3.10 -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $py310) {
    throw "Python 3.10 not found (py -3.10). tobii-research ships cp310 wheels only. Install it from python.org and re-run."
}
Ok "Python 3.10 at $py310"

if (-not (Have poetry)) {
    Write-Host "   installing pipx + Poetry ..."
    & $py310 -m pip install --user --quiet pipx
    & $py310 -m pipx ensurepath | Out-Null
    Refresh-Path
    & $py310 -m pipx install poetry | Out-Null
    Refresh-Path
}
if (-not (Have poetry)) { throw "Poetry still not on PATH - open a new terminal and re-run." }
Ok "Poetry $(poetry --version)"

Write-Host "   creating the environment on Python 3.10 and installing the capture dependencies ..."
poetry env use $py310 | Out-Null
$with = @()
if ($WithTranscription) { $with += "transcription" }
if ($WithVision)        { $with += "vision" }
if ($with.Count) { poetry install --with ($with -join ",") } else { poetry install }
if ($LASTEXITCODE -ne 0) { throw "poetry install failed - see the output above" }
$ver = poetry run python -c "import sys; print(sys.version.split()[0])"
if (-not $ver.StartsWith("3.10")) { throw "Poetry environment is on $ver, not 3.10" }
Ok "poetry environment ready on $ver"

# ----------------------------------------------------------------- 3. OpenFace
Step "OpenFace 2.2.0"
$ofDir = Join-Path $Repo "OpenFace_2.2.0_win_x64"
$ofExe = Join-Path $ofDir "FeatureExtraction.exe"
if ($SkipOpenFace) {
    Warn "-SkipOpenFace: set PLAYSMART_OPENFACE=0 in .env (no emotion stream)"
} else {
    if (-not (Test-Path $ofExe)) {
        $zip = Join-Path $env:TEMP "OpenFace_2.2.0_win_x64.zip"
        $url = "https://github.com/TadasBaltrusaitis/OpenFace/releases/download/OpenFace_2.2.0/OpenFace_2.2.0_win_x64.zip"
        Write-Host "   downloading $url ..."
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath $Repo -Force
        } catch {
            Warn "download failed ($($_.Exception.Message)). Get OpenFace_2.2.0_win_x64.zip from the OpenFace GitHub releases page or the PlaySmart Google Drive, unzip it into the repo root, and re-run."
        }
    }
    if (Test-Path $ofExe) {
        Ok "FeatureExtraction.exe present"
        $models = Get-ChildItem (Join-Path $ofDir "model\patch_experts") -Filter "cen_patches_*.dat" -ErrorAction SilentlyContinue
        if ($models.Count -lt 4) {
            Write-Host "   downloading the CEN patch-expert models (not in the zip) ..."
            Push-Location $ofDir
            try { & powershell -ExecutionPolicy Bypass -File .\download_models.ps1 } catch { Warn "download_models.ps1 failed: $($_.Exception.Message)" }
            Pop-Location
            $models = Get-ChildItem (Join-Path $ofDir "model\patch_experts") -Filter "cen_patches_*.dat" -ErrorAction SilentlyContinue
        }
        if ($models.Count -ge 4) { Ok "$($models.Count) CEN models present" } else { Warn "CEN models missing - OpenFace will exit at once; run download_models.ps1 inside $ofDir" }
    }
}

# --------------------------------------------------------------------- 4. .env
Step "Secrets (.env)"
$envFile = Join-Path $Repo ".env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $Repo ".env.example") $envFile; Ok "created .env from .env.example" }
else {
    # .env is untracked, so git pull never updates it: append keys that are new in
    # .env.example (values already there are never changed) and fix trailing comments.
    poetry run python src\preflight.py --env-only --fix-env
    Ok ".env exists (synced with .env.example)"
}
if (-not $NoPrompt) {
    $content = Get-Content $envFile -Raw
    if ($content -match "PLAYSMART_OBS_PASSWORD=\s*(\r?\n|$)") {
        $p = Read-Host "   OBS websocket password (OBS -> Tools -> WebSocket Server Settings -> Show Connect Info; Enter to skip)"
        if ($p) { $content = $content -replace "PLAYSMART_OBS_PASSWORD=\s*(?=\r?\n|$)", ("PLAYSMART_OBS_PASSWORD=" + $p.Replace('$', '$$')) }
    }
    if ($content -match "PLAYSMART_SFTP_PASSWORD=\s*(\r?\n|$)") {
        $p = Read-Host "   SFTP upload password for queenbee (Enter to skip; files then stay local)"
        if ($p) { $content = $content -replace "PLAYSMART_SFTP_PASSWORD=\s*(?=\r?\n|$)", ("PLAYSMART_SFTP_PASSWORD=" + $p.Replace('$', '$$')) }
    }
    Set-Content -Path $envFile -Value $content -NoNewline
}
if (Select-String -Path $envFile -Pattern "PLAYSMART_OBS_PASSWORD=\S" -Quiet) { Ok "OBS password set" } else { Warn "OBS password empty" }
if (Select-String -Path $envFile -Pattern "PLAYSMART_SFTP_PASSWORD=\S" -Quiet) { Ok "SFTP password set" } else { Warn "SFTP password empty - no upload until it is" }

# ------------------------------------------------------ 5. the human-only parts
Step "Things this script cannot do"
$tobii = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*Tobii*" -or $_.DisplayName -like "*Tobii*" }
if ($tobii) { Ok "Tobii service present ($($tobii[0].DisplayName))" }
else { Warn "no Tobii service found - install Tobii Pro Eye Tracker Manager: https://www.tobii.com/products/software/applications-and-developer-kits/tobii-pro-eye-tracker-manager" }
Write-Host "   - Calibrate the player in Tobii Pro Eye Tracker Manager before every session."
Write-Host "   - Install League of Legends (Riot Vanguard needs TPM 2.0 + Secure Boot on Windows 11), log in, open Practice Tool once."
Write-Host "   - OBS: enable the WebSocket server, set 1920x1080, MKV, no auto-remux, Game Capture of the League window."
Write-Host "     Details: obs settings\readme.md and TESTING.md section 3."
Write-Host "   - Windows Settings -> Privacy: allow desktop apps to use the Camera and Microphone. Display scaling 100 %."

Step "Done. Next:"
Write-Host "   poetry run python src\preflight.py      # every item should be ok/warn, nothing FAIL"
Write-Host "   main.bat                                # pre-flight, then F7 to arm a capture"
