<# .SYNOPSIS
    One-click launcher for the OpenMontage + OmniVoice + Fableframe video studio.

.DESCRIPTION
    Checks all prerequisites (Python venv, Node.js, CUDA, OmniVoice, Fableframe),
    then runs the automate_video.py script.

    Supports two modes:
    - Topic to Video: generates script, narration, and video from a text prompt
    - Audio to Video: generates video from an existing audio recording

.EXAMPLE
    .\start_studio.ps1 "How AI works"
    .\start_studio.ps1 -Topic "Chand ki Kahani" -Language Hindi -Duration 30
    .\start_studio.ps1 -Audio "C:\Users\MSII\OmniVoice\hindi_30s.wav"
    .\start_studio.ps1 -Audio "song.mp3" -Mode song
    .\start_studio.ps1 -CheckOnly
#>

param(
    [Parameter(Position=0)]
    [string]$Topic,

    [string]$Audio,

    [string]$Language = "Hindi",

    [string]$Voice = "Male, Middle-aged, Indian Accent",

    [int]$Duration = 30,

    [ValidateSet("story", "song", "auto")]
    [string]$Mode,

    [ValidateSet("native", "roman", "none")]
    [string]$CaptionScript,

    [string]$Platform = "youtube",

    [string]$ProjectId,

    [switch]$Force,

    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$REPO_ROOT    = "C:\Users\MSII\OpenMontage"
$OMNIVOICE    = "C:\Users\MSII\OmniVoice"
$FABLEFRAME   = "C:\Users\MSII\FABLEFRAME"
$PYTHON       = "$REPO_ROOT\.venv\Scripts\python.exe"
$OV_PYTHON    = "$OMNIVOICE\.venv\Scripts\python.exe"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  OpenMontage Video Studio - Preflight Check" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$ok = $true

# 1. Python environments
if (Test-Path $PYTHON) {
    $pyVer = & $PYTHON --version 2>&1
    Write-Host "  [OK] OpenMontage Python: $pyVer" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] OpenMontage .venv not found at $PYTHON" -ForegroundColor Red
    $ok = $false
}

if (Test-Path $OV_PYTHON) {
    $ovVer = & $OV_PYTHON --version 2>&1
    Write-Host "  [OK] OmniVoice Python:   $ovVer" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] OmniVoice .venv not found at $OV_PYTHON" -ForegroundColor Red
    $ok = $false
}

# 2. Node.js
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCmd) {
    $nodeVer = & node --version 2>&1
    Write-Host "  [OK] Node.js:            $nodeVer" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Node.js not found on PATH" -ForegroundColor Red
    $ok = $false
}

# 3. FFmpeg
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpegCmd) {
    Write-Host "  [OK] FFmpeg:             available" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] FFmpeg not found on PATH" -ForegroundColor Red
    $ok = $false
}

# 4. GPU / CUDA
try {
    $cudaCheck = & $OV_PYTHON -c "import torch; ok=torch.cuda.is_available(); print('CUDA_OK' if ok else 'CUDA_NO', torch.cuda.get_device_name(0) if ok else '')" 2>&1
    if ($cudaCheck -match "CUDA_OK") {
        $gpuName = ($cudaCheck -replace "CUDA_OK", "").Trim()
        Write-Host "  [OK] CUDA GPU:           $gpuName" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] CUDA not available (will use CPU)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  [WARN] Could not detect CUDA status" -ForegroundColor Yellow
}

# 5. Fableframe node_modules
if (Test-Path "$FABLEFRAME\node_modules") {
    Write-Host "  [OK] Fableframe:         dependencies installed" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Fableframe node_modules missing. Run: cd $FABLEFRAME && npm install" -ForegroundColor Red
    $ok = $false
}

# 6. OmniVoice weights
$modelPath = "$OMNIVOICE\models\OmniVoice\model.safetensors"
if (Test-Path $modelPath) {
    $modelSize = [math]::Round((Get-Item $modelPath).Length / 1MB, 0)
    Write-Host "  [OK] OmniVoice model:    ${modelSize} MB" -ForegroundColor Green
} else {
    Write-Host "  [WARN] OmniVoice model not cached (will download on first run)" -ForegroundColor Yellow
}

# 7. Groq API
$envContent = ""
if (Test-Path "$REPO_ROOT\.env") { $envContent = Get-Content "$REPO_ROOT\.env" -Raw }
if (Test-Path "$FABLEFRAME\.env") { $envContent += Get-Content "$FABLEFRAME\.env" -Raw }

if ($envContent -match 'GROQ_API\s*=\s*"?gsk_') {
    Write-Host "  [OK] Groq API key:       configured" -ForegroundColor Green
} else {
    Write-Host "  [WARN] Groq API key:     not found (will use templates)" -ForegroundColor Yellow
}

Write-Host ""

if (-not $ok) {
    Write-Host "  PREFLIGHT FAILED - fix issues above." -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host "  All preflight checks passed!" -ForegroundColor Green
Write-Host ""

if ($CheckOnly) {
    exit 0
}

# Run the pipeline
if (-not $Topic -and -not $Audio) {
    Write-Host "  No topic or audio provided. Usage:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Topic to Video:" -ForegroundColor Cyan
    Write-Host '    .\start_studio.ps1 "Your video topic here"' -ForegroundColor White
    Write-Host '    .\start_studio.ps1 -Topic "AI explained" -Language English -Duration 60' -ForegroundColor White
    Write-Host ""
    Write-Host "  Audio to Video:" -ForegroundColor Cyan
    Write-Host '    .\start_studio.ps1 -Audio "C:\path\to\narration.wav"' -ForegroundColor White
    Write-Host '    .\start_studio.ps1 -Audio "song.mp3" -Mode song' -ForegroundColor White
    Write-Host ""
    Write-Host '    .\start_studio.ps1 -CheckOnly' -ForegroundColor White
    Write-Host ""
    exit 0
}

$cmd = @($PYTHON, "$REPO_ROOT\scripts\automate_video.py")

if ($Audio) {
    $cmd += @("--audio", $Audio)
    if ($Mode) { $cmd += @("--mode", $Mode) }
    if ($CaptionScript) { $cmd += @("--caption-script", $CaptionScript) }
} else {
    $cmd += @("--topic", $Topic, "--language", $Language, "--voice", $Voice, "--duration", $Duration)
}

$cmd += @("--platform", $Platform)

if ($ProjectId) {
    $cmd += @("--project-id", $ProjectId)
}
if ($Force) {
    $cmd += "--force"
}

$pipelineMode = if ($Audio) { "Audio -> Video" } else { "Topic -> Video" }

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Starting video production ($pipelineMode)..." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

& $cmd[0] $cmd[1..($cmd.Count-1)]
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host ""
    Write-Host "  Done! Your video has been rendered successfully." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  Pipeline exited with code $exitCode" -ForegroundColor Red
}

exit $exitCode
