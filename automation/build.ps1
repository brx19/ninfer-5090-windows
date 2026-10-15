param([switch]$GpuTests)
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$Settings = Get-Content -LiteralPath "automation/settings.json" -Raw | ConvertFrom-Json
$Lock = Get-Content -LiteralPath "automation/dependencies.lock.json" -Raw | ConvertFrom-Json

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit $LASTEXITCODE" }
}

function Import-VsEnvironment {
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio/Installer/vswhere.exe"
    if (-not (Test-Path -LiteralPath $VsWhere)) { throw "Visual Studio C++ installation not found" }
    $VsRoot = & $VsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $VsRoot) { throw "Visual Studio Desktop C++ workload not found" }
    $DevCmd = Join-Path $VsRoot "Common7/Tools/VsDevCmd.bat"
    $EnvironmentLines = & cmd.exe /d /s /c "`"`"$DevCmd`" -arch=x64 -host_arch=x64 >nul && set`""
    if ($LASTEXITCODE -ne 0) { throw "VsDevCmd failed" }
    foreach ($Line in $EnvironmentLines) {
        if ($Line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
}

Import-VsEnvironment
if ($env:NINFER_CUDA_PATH) { $env:CUDA_PATH = $env:NINFER_CUDA_PATH }
if (-not $env:CUDA_PATH) { throw "CUDA_PATH is missing; install CUDA $($Lock.cuda_version)" }
$Nvcc = Join-Path $env:CUDA_PATH "bin/nvcc.exe"
if (-not (Test-Path -LiteralPath $Nvcc)) { throw "nvcc not found: $Nvcc" }
$NvccVersion = & $Nvcc --version
if ($LASTEXITCODE -ne 0 -or ($NvccVersion -join "`n") -notmatch 'release 13\.3') { throw "This pipeline pins CUDA 13.3; received: $NvccVersion" }
$env:PATH = "$($env:CUDA_PATH)\bin;$env:PATH"
$env:CUDA_HOME = $env:CUDA_PATH

# Both hosted and GPU jobs use exactly the candidate's dependency pins.
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $RepoRoot ".automation-temp" }
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
$VcpkgRoot = Join-Path $TempRoot "ninfer-vcpkg-$($Lock.vcpkg_sha)"
if (-not (Test-Path -LiteralPath (Join-Path $VcpkgRoot ".git"))) {
    Invoke-Checked git @("clone", "--filter=blob:none", "https://github.com/microsoft/vcpkg.git", $VcpkgRoot)
}
Invoke-Checked git @("-C", $VcpkgRoot, "fetch", "origin", $Lock.vcpkg_sha)
Invoke-Checked git @("-C", $VcpkgRoot, "checkout", "--detach", $Lock.vcpkg_sha)
Invoke-Checked (Join-Path $VcpkgRoot "bootstrap-vcpkg.bat") @("-disableMetrics")
Invoke-Checked (Join-Path $VcpkgRoot "vcpkg.exe") @("install", "curl[schannel]:x64-windows", "--disable-metrics")
$CurlPrefix = Join-Path $VcpkgRoot "installed/x64-windows"
$env:PATH = "$CurlPrefix\bin;$env:PATH"

$FfmpegZip = Join-Path $TempRoot "ninfer-ffmpeg.zip"
Invoke-WebRequest -Uri $Lock.ffmpeg.url -OutFile $FfmpegZip
$ActualHash = (Get-FileHash -LiteralPath $FfmpegZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ActualHash -ne $Lock.ffmpeg.sha256) { throw "FFmpeg SHA256 changed after candidate pinning; retry synchronization" }
$FfmpegExtract = Join-Path $TempRoot "ninfer-ffmpeg-extract"
if (Test-Path -LiteralPath $FfmpegExtract) { Remove-Item -LiteralPath $FfmpegExtract -Recurse -Force }
Expand-Archive -LiteralPath $FfmpegZip -DestinationPath $FfmpegExtract
$FfmpegTrees = @(Get-ChildItem -LiteralPath $FfmpegExtract -Directory | Where-Object { Test-Path (Join-Path $_.FullName "include/libavformat") })
if ($FfmpegTrees.Count -ne 1) { throw "Expected one FFmpeg development tree" }
if (Test-Path -LiteralPath "ffmpeg") { Remove-Item -LiteralPath "ffmpeg" -Recurse -Force }
Copy-Item -LiteralPath $FfmpegTrees[0].FullName -Destination "ffmpeg" -Recurse

if ($GpuTests) {
    if (-not $env:NINFER_TEST_ARTIFACT -or -not (Test-Path -LiteralPath $env:NINFER_TEST_ARTIFACT)) { throw "NINFER_TEST_ARTIFACT must name the existing model on this runner" }
    $Busy = @(Get-Process -Name "ninfer-serve", "infernix-serve", "llama-server" -ErrorAction SilentlyContinue)
    if ($Busy.Count) { throw "Inference is active; qualification will not share the GPU. Retry while idle." }
    Invoke-Checked nvidia-smi @("--query-gpu=name,driver_version", "--format=csv,noheader")
    $GpuNames = & nvidia-smi --query-gpu=name --format=csv,noheader
    if (($GpuNames -join "`n") -notmatch 'RTX 5090') { throw "RTX 5090 not detected" }
    Invoke-Checked python @("-m", "pip", "install", "-r", "tests/text/requirements.txt")
    $env:NINFER_PYTHON = (Get-Command python).Source
    # Make the existing recipe's CMake find the same required CURL build.
    $env:CMAKE_PREFIX_PATH = $CurlPrefix
    Invoke-Checked cmd.exe @("/d", "/c", "tools\scripts\test_v3.cmd")
    Invoke-Checked python @("tools/release/check_test_baseline.py", "--no-cache")
    return
}

Invoke-Checked cmake @("-S", ".", "-B", "build", "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CUDA_ARCHITECTURES=120a", "-DCMAKE_CUDA_RUNTIME_LIBRARY=Static", "-DCMAKE_PREFIX_PATH=$CurlPrefix", "-DBUILD_TESTING=OFF", "-DNINFER_BUILD_BENCHMARKS=OFF")
Invoke-Checked cmake @("--build", "build", "--config", "Release", "--parallel", "4")
$Stage = Join-Path $RepoRoot "dist/package"
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
foreach ($Name in @("ninfer-serve.exe", "ninfer.exe", "ninfer-perplexity.exe")) {
    $Image = Join-Path $RepoRoot "build/apps/$Name"
    if (-not (Test-Path -LiteralPath $Image)) { throw "Missing release executable: $Image" }
    Copy-Item -LiteralPath $Image -Destination $Stage
}
Copy-Item "ffmpeg/bin/*.dll" $Stage
Copy-Item "$CurlPrefix/bin/*.dll" $Stage
Copy-Item "$($env:CUDA_PATH)/bin/cudart64*.dll" $Stage -ErrorAction SilentlyContinue
foreach ($Name in @("LICENSE", "NOTICE")) { Copy-Item -LiteralPath $Name -Destination $Stage }
$Licenses = Join-Path $Stage "licenses"
New-Item -ItemType Directory -Force -Path $Licenses | Out-Null
Copy-Item "ffmpeg/LICENSE*" $Licenses
Copy-Item "ffmpeg/README*" $Licenses -ErrorAction SilentlyContinue
foreach ($Port in Get-ChildItem "$CurlPrefix/share" -Directory) {
    $CopyrightFile = Join-Path $Port.FullName "copyright"
    if (Test-Path -LiteralPath $CopyrightFile) { Copy-Item -LiteralPath $CopyrightFile -Destination (Join-Path $Licenses "$($Port.Name)-copyright.txt") }
}
foreach ($LicenseFile in Get-ChildItem "third_party" -Recurse -File | Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE|COPYRIGHT)(\.|$)' }) {
    $RelativeLicense = [IO.Path]::GetRelativePath((Join-Path $RepoRoot "third_party"), $LicenseFile.FullName)
    $DestinationLicense = Join-Path $Licenses ("vendored/" + $RelativeLicense)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DestinationLicense) | Out-Null
    Copy-Item -LiteralPath $LicenseFile.FullName -Destination $DestinationLicense
}
$env:BRX_NINFER_NVCC_VERSION = $NvccVersion -join "`n"
$env:BRX_NINFER_MSVC_VERSION = $env:VCToolsVersion
Invoke-Checked python @("automation/package.py", "--stage", $Stage, "--output", "dist")
$OriginalPath = $env:PATH
try {
    $env:PATH = "$Stage;$env:SystemRoot\System32;$env:SystemRoot"
    Invoke-Checked (Join-Path $Stage "ninfer-serve.exe") @("--help")
} finally { $env:PATH = $OriginalPath }
