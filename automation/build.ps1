param([switch]$GpuTests)
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$Settings = Get-Content -LiteralPath "automation/settings.json" -Raw | ConvertFrom-Json
$Lock = Get-Content -LiteralPath "automation/dependencies.lock.json" -Raw | ConvertFrom-Json
$TotalStopwatch = [Diagnostics.Stopwatch]::StartNew()

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
    $EnvironmentScript = Join-Path ([IO.Path]::GetTempPath()) ("brx-vs-" + [guid]::NewGuid().ToString("N") + ".cmd")
    $EnvironmentCommands = @(
        '@echo off',
        ('call "' + $DevCmd + '" -arch=x64 -host_arch=x64 >nul'),
        'if errorlevel 1 exit /b %errorlevel%',
        'set'
    )
    [IO.File]::WriteAllLines($EnvironmentScript, $EnvironmentCommands, [Text.UTF8Encoding]::new($false))
    try {
        $EnvironmentLines = & cmd.exe /d /c $EnvironmentScript
        if ($LASTEXITCODE -ne 0) { throw "VsDevCmd failed" }
    } finally { Remove-Item -LiteralPath $EnvironmentScript -Force }
    foreach ($Line in $EnvironmentLines) {
        if ($Line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
}

# --- Build cache (hosted CI) --------------------------------------------------
# NINFER_CACHE_ROOT names a persistent directory that the workflow restores and
# saves through actions/cache. When unset (self-hosted runner, local machine) the
# script behaves exactly as before: everything lives in the per-run temp root.
$CacheRoot = $env:NINFER_CACHE_ROOT
$SccacheExe = $env:NINFER_SCCACHE
$SccacheCudaOn = ($env:NINFER_SCCACHE_CUDA -eq 'on')

Import-VsEnvironment
if ($env:NINFER_CUDA_PATH) { $env:CUDA_PATH = $env:NINFER_CUDA_PATH }
if (-not $env:CUDA_PATH) { throw "CUDA_PATH is missing; install CUDA $($Lock.cuda_version)" }
$Nvcc = Join-Path $env:CUDA_PATH "bin/nvcc.exe"
if (-not (Test-Path -LiteralPath $Nvcc)) { throw "nvcc not found: $Nvcc" }
$NvccVersion = & $Nvcc --version
if ($LASTEXITCODE -ne 0 -or ($NvccVersion -join "`n") -notmatch 'release 13\.3') { throw "This pipeline pins CUDA 13.3; received: $NvccVersion" }
$env:PATH = "$($env:CUDA_PATH)\bin;$env:PATH"
$env:CUDA_HOME = $env:CUDA_PATH

# Exercise the real device compiler, not just nvcc's version-printing frontend.
$CompilerProbe = Join-Path ([IO.Path]::GetTempPath()) ("brx-cuda-" + [guid]::NewGuid().ToString("N"))
try {
    [IO.File]::WriteAllText(($CompilerProbe + ".cu"), 'extern "C" __global__ void brx_probe() {}', [Text.UTF8Encoding]::new($false))
    Invoke-Checked $Nvcc @("-cubin", "-arch=sm_120a", ($CompilerProbe + ".cu"), "-o", ($CompilerProbe + ".cubin"))
} finally {
    Remove-Item -LiteralPath ($CompilerProbe + ".cu"), ($CompilerProbe + ".cubin") -Force -ErrorAction SilentlyContinue
}

# Both hosted and GPU jobs use exactly the candidate's dependency pins.
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { Join-Path $RepoRoot ".automation-temp" }
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null

# --- vcpkg checkout -------------------------------------------------------------
# With a cache root the checkout is a shared partial clone keyed to nothing: the
# pinned SHA from the candidate lock is verified and fetched into it, so an
# incremental upstream change costs one fetch, not one clone. A checkout that is
# missing or unusable is discarded and re-cloned; that is the corruption fallback.
$VcpkgMetrics = "temp"
$VcpkgBinCache = $null
if ($CacheRoot) {
    New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null
    $VcpkgRoot = Join-Path $CacheRoot "vcpkg"
    $PinnedSha = $Lock.vcpkg_sha
    $HasGit = Test-Path (Join-Path $VcpkgRoot ".git")
    $HeadMatches = $false
    if ($HasGit) {
        try {
            $Head = (& git -C $VcpkgRoot rev-parse HEAD 2>$null)
            $HeadMatches = ($LASTEXITCODE -eq 0 -and "$Head" -eq $PinnedSha)
        } catch { $HeadMatches = $false }
    }
    if ($HeadMatches) {
        $VcpkgMetrics = "reused"
    } elseif ($HasGit) {
        # Existing partial clone: fetch the pinned SHA into it. If git cannot work
        # with the checkout (corruption), discard it and clone fresh below.
        try {
            Invoke-Checked git @("-C", $VcpkgRoot, "fetch", "origin", $PinnedSha)
            Invoke-Checked git @("-C", $VcpkgRoot, "checkout", "--detach", $PinnedSha)
            $VcpkgMetrics = "reused-after-fetch"
        } catch {
            Write-Host "Cached vcpkg checkout unusable; re-cloning: $($_.Exception.Message)"
            Remove-Item -LiteralPath $VcpkgRoot -Recurse -Force -ErrorAction SilentlyContinue
            $HasGit = $false
        }
    }
    if (-not $HasGit) {
        if (Test-Path $VcpkgRoot) { Remove-Item -LiteralPath $VcpkgRoot -Recurse -Force }
        Invoke-Checked git @("clone", "--filter=blob:none", "https://github.com/microsoft/vcpkg.git", $VcpkgRoot)
        Invoke-Checked git @("-C", $VcpkgRoot, "checkout", "--detach", $PinnedSha)
        if ($VcpkgMetrics -eq "reused-after-fetch") { $VcpkgMetrics = "cloned-after-corruption" } else { $VcpkgMetrics = "cloned" }
    }
    # Bootstrap only when the tool could have come from a different checkout: a
    # reused checkout carries the vcpkg.exe built from the same pinned SHA.
    if ($VcpkgMetrics -ne "reused" -or -not (Test-Path (Join-Path $VcpkgRoot "vcpkg.exe"))) {
        Invoke-Checked (Join-Path $VcpkgRoot "bootstrap-vcpkg.bat") @("-disableMetrics")
    }
    # Filesystem binary provider (the supported successor of the removed x-gha
    # provider). vcpkg verifies each binary entry against its package hash, so a
    # corrupted entry is rebuilt rather than installed.
    $VcpkgBinCache = Join-Path $CacheRoot "vcpkg-bin"
    New-Item -ItemType Directory -Force -Path $VcpkgBinCache | Out-Null
    $env:VCPKG_BINARY_SOURCES = "clear;files,$VcpkgBinCache,readwrite"
} else {
    $VcpkgRoot = Join-Path $TempRoot "ninfer-vcpkg-$($Lock.vcpkg_sha)"
    if (-not (Test-Path -LiteralPath (Join-Path $VcpkgRoot ".git"))) {
        Invoke-Checked git @("clone", "--filter=blob:none", "https://github.com/microsoft/vcpkg.git", $VcpkgRoot)
    }
    Invoke-Checked git @("-C", $VcpkgRoot, "fetch", "origin", $Lock.vcpkg_sha)
    Invoke-Checked git @("-C", $VcpkgRoot, "checkout", "--detach", $Lock.vcpkg_sha)
    Invoke-Checked (Join-Path $VcpkgRoot "bootstrap-vcpkg.bat") @("-disableMetrics")
}
$VcpkgInstallOut = & (Join-Path $VcpkgRoot "vcpkg.exe") @("install", "curl[ssl]:x64-windows", "--disable-metrics") 2>&1
if ($LASTEXITCODE -ne 0) { throw "vcpkg install failed with exit $LASTEXITCODE" }
$VcpkgRestoredPorts = @($VcpkgInstallOut | Select-String -Pattern '^\s*Restored ').Count
$VcpkgBuiltPorts = @($VcpkgInstallOut | Select-String -Pattern '^\s*Building ').Count
$CurlPrefix = Join-Path $VcpkgRoot "installed/x64-windows"
$env:PATH = "$CurlPrefix\bin;$env:PATH"

# --- FFmpeg ---------------------------------------------------------------------
# The archive is content-addressed by the locked SHA256, so a mutable upstream URL
# can only change what gets downloaded; it cannot change what gets used, because a
# mismatched hash is rejected. Only the archive is cached, not the unpacked tree.
$FfmpegSource = "downloaded"
$LockHash = $Lock.ffmpeg.sha256
$FfmpegZip = Join-Path $TempRoot "ninfer-ffmpeg.zip"
if ($CacheRoot) {
    $FfmpegCacheDir = Join-Path $CacheRoot "ffmpeg"
    New-Item -ItemType Directory -Force -Path $FfmpegCacheDir | Out-Null
    $FfmpegCachedZip = Join-Path $FfmpegCacheDir ($LockHash + ".zip")
    if (Test-Path -LiteralPath $FfmpegCachedZip) {
        $CachedHash = (Get-FileHash -LiteralPath $FfmpegCachedZip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($CachedHash -eq $LockHash) {
            $FfmpegZip = $FfmpegCachedZip
            $FfmpegSource = "cache"
        } else {
            Write-Host "Cached FFmpeg archive fails its pinned hash (corruption); re-downloading."
            Remove-Item -LiteralPath $FfmpegCachedZip -Force
        }
    }
    if ($FfmpegSource -ne "cache") {
        Invoke-WebRequest -Uri $Lock.ffmpeg.url -OutFile $FfmpegZip
        $DownloadedHash = (Get-FileHash -LiteralPath $FfmpegZip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($DownloadedHash -ne $LockHash) { throw "FFmpeg SHA256 changed after candidate pinning; retry synchronization" }
        Copy-Item -LiteralPath $FfmpegZip -Destination $FfmpegCachedZip -Force
    }
} else {
    Invoke-WebRequest -Uri $Lock.ffmpeg.url -OutFile $FfmpegZip
    $DownloadedHash = (Get-FileHash -LiteralPath $FfmpegZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($DownloadedHash -ne $LockHash) { throw "FFmpeg SHA256 changed after candidate pinning; retry synchronization" }
}
$FfmpegExtract = Join-Path $TempRoot "ninfer-ffmpeg-extract"
if (Test-Path -LiteralPath $FfmpegExtract) { Remove-Item -LiteralPath $FfmpegExtract -Recurse -Force }
Expand-Archive -LiteralPath $FfmpegZip -DestinationPath $FfmpegExtract
$FfmpegTrees = @(Get-ChildItem -LiteralPath $FfmpegExtract -Directory | Where-Object { Test-Path (Join-Path $_.FullName "include/libavformat") })
if ($FfmpegTrees.Count -ne 1) { throw "Expected one FFmpeg development tree" }
if (Test-Path -LiteralPath "ffmpeg") { Remove-Item -LiteralPath "ffmpeg" -Recurse -Force }
Copy-Item -LiteralPath $FfmpegTrees[0].FullName -Destination "ffmpeg" -Recurse

# --- sccache -----------------------------------------------------------------------
# One launcher wrapper per cache layer; each pins SCCACHE_DIR for its own server,
# so C/C++ and CUDA statistics stay separate and CUDA caching can be turned off
# without touching the C/C++ layer. Ninja on Windows runs commands through cmd, so
# a .bat launcher is a valid CMake compiler launcher.
$SccacheCppLauncher = $null
$SccacheCudaLauncher = $null
$SccacheVersion = $null
function New-SccacheLauncher {
    param([string]$Name, [string]$CacheDir)
    $Path = Join-Path $LauncherDir $Name
    $Lines = @(
        '@echo off',
        ('set "SCCACHE_DIR=' + $CacheDir + '"'),
        ('call "' + $SccacheExe + '" %*'),
        'exit /b %errorlevel%'
    )
    [IO.File]::WriteAllLines($Path, $Lines, [Text.UTF8Encoding]::new($false))
    return $Path
}
function Get-SccacheStats {
    param([string]$CacheDir)
    $Old = $env:SCCACHE_DIR
    try {
        $env:SCCACHE_DIR = $CacheDir
        $Output = & $SccacheExe --show-stats 2>&1
        return ($Output -join "`n")
    } finally {
        if ($null -eq $Old) { Remove-Item Env:SCCACHE_DIR -ErrorAction SilentlyContinue } else { $env:SCCACHE_DIR = $Old }
    }
}
if ($SccacheExe) {
    if (-not $CacheRoot) { throw "NINFER_SCCACHE is set but NINFER_CACHE_ROOT is not; refusing to guess a cache location" }
    if (-not (Test-Path -LiteralPath $SccacheExe)) { throw "sccache executable not found: $SccacheExe" }
    $SccacheVersion = (& $SccacheExe --version | Select-Object -First 1)
    # Keep the servers alive for the whole job so --show-stats can reach them at the end.
    $env:SCCACHE_IDLE_TIMEOUT = '0'
    if (-not $env:SCCACHE_ERROR_LOG) { $env:SCCACHE_ERROR_LOG = Join-Path $TempRoot "sccache-error.log" }
    $SccacheCppDir = Join-Path $CacheRoot "sccache-cpp"
    New-Item -ItemType Directory -Force -Path $SccacheCppDir | Out-Null
    $SccacheCudaDir = Join-Path $CacheRoot "sccache-cuda"
    if ($SccacheCudaOn) { New-Item -ItemType Directory -Force -Path $SccacheCudaDir | Out-Null }
    $LauncherDir = Join-Path $CacheRoot "launchers"
    New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null
    $SccacheCppLauncher = New-SccacheLauncher "sccache-cpp.bat" $SccacheCppDir
    if ($SccacheCudaOn) { $SccacheCudaLauncher = New-SccacheLauncher "sccache-cuda.bat" $SccacheCudaDir }
}

function Write-CacheMetrics {
    param($Metrics)
    $MetricsPath = Join-Path $RepoRoot "build/cache-metrics.json"
    New-Item -ItemType Directory -Force -Path (Split-Path $MetricsPath) | Out-Null
    ($Metrics | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $MetricsPath -Encoding UTF8
    Write-Host "=== NINFER CACHE METRICS ==="
    foreach ($Key in $Metrics.Keys) {
        $Value = [string]$Metrics[$Key]
        if ($Value.Length -gt 400) { $Value = $Value.Substring(0, 400) + "..." }
        Write-Host ("  {0} = {1}" -f $Key, ($Value -replace "`r?`n", "; "))
    }
}

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
    Write-CacheMetrics ([ordered]@{
        cache_root = $(if ($CacheRoot) { $CacheRoot } else { "disabled" })
        sccache = "not applied to the gpu recipe (test_v3.cmd configures its own build)"
        vcpkg_checkout = $VcpkgMetrics
        vcpkg_restored_ports = $VcpkgRestoredPorts
        vcpkg_built_ports = $VcpkgBuiltPorts
        ffmpeg_source = $FfmpegSource
        total_seconds = [math]::Round($TotalStopwatch.Elapsed.TotalSeconds, 1)
    })
    return
}

$ConfigureArgs = @("-S", ".", "-B", "build", "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CUDA_ARCHITECTURES=120a", "-DCMAKE_CUDA_RUNTIME_LIBRARY=Static", "-DCMAKE_PREFIX_PATH=$CurlPrefix", "-DBUILD_TESTING=OFF", "-DNINFER_BUILD_BENCHMARKS=OFF")
if ($SccacheCppLauncher) {
    $ConfigureArgs += "-DCMAKE_C_COMPILER_LAUNCHER=$SccacheCppLauncher"
    $ConfigureArgs += "-DCMAKE_CXX_COMPILER_LAUNCHER=$SccacheCppLauncher"
}
if ($SccacheCudaOn -and $SccacheCudaLauncher) {
    # Gated in CMakeLists.txt (NINFER_CUDA_COMPILER_LAUNCHER, default OFF): the recipe
    # opts in only after object and binary parity has been demonstrated for this toolchain.
    $ConfigureArgs += "-DCMAKE_CUDA_COMPILER_LAUNCHER=$SccacheCudaLauncher"
    $ConfigureArgs += "-DNINFER_CUDA_COMPILER_LAUNCHER=ON"
}
$PhaseStopwatch = [Diagnostics.Stopwatch]::StartNew()
Invoke-Checked cmake $ConfigureArgs
$ConfigureSeconds = [math]::Round($PhaseStopwatch.Elapsed.TotalSeconds, 1)
$PhaseStopwatch.Restart()
Invoke-Checked cmake @("--build", "build", "--target", "ninfer-serve", "--config", "Release", "--parallel", "4")
$BuildSeconds = [math]::Round($PhaseStopwatch.Elapsed.TotalSeconds, 1)
$Stage = Join-Path $RepoRoot "dist/package"
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
$Image = Join-Path $RepoRoot "build/apps/ninfer-serve.exe"
if (-not (Test-Path -LiteralPath $Image)) { throw "Missing release executable: $Image" }
Copy-Item -LiteralPath $Image -Destination $Stage

# Stage only non-system DLLs reachable from the server's PE import graph. dumpbin
# /dependents includes normal and delay-loaded dependencies; every bundled DLL is
# then scanned recursively. NVIDIA's driver DLL and Windows system DLLs are supplied
# by the target machine and must not be redistributed.
$RuntimeRoots = @((Resolve-Path "ffmpeg/bin").Path, (Resolve-Path "$CurlPrefix/bin").Path, (Resolve-Path "$($env:CUDA_PATH)/bin").Path)
$SystemDlls = @(
    "advapi32.dll", "bcrypt.dll", "cfgmgr32.dll", "crypt32.dll", "d3d12.dll", "dbghelp.dll",
    "gdi32.dll", "iphlpapi.dll", "kernel32.dll", "ncrypt.dll", "nvcuda.dll", "ole32.dll",
    "oleaut32.dll", "rpcrt4.dll", "secur32.dll", "shell32.dll", "shlwapi.dll", "user32.dll",
    "userenv.dll", "version.dll", "winhttp.dll", "winmm.dll", "ws2_32.dll", "wtsapi32.dll"
)
function Get-PeDependencies {
    param([string]$Binary)
    $Lines = & dumpbin /nologo /dependents $Binary 2>&1
    if ($LASTEXITCODE -ne 0) { throw "dumpbin failed for $Binary" }
    return @($Lines | ForEach-Object {
        if ($_ -match '^\s+([A-Za-z0-9_.+-]+\.dll)\s*$') { $Matches[1] }
    } | Sort-Object -Unique)
}
$Pending = [Collections.Generic.Queue[string]]::new()
$Pending.Enqueue((Join-Path $Stage "ninfer-serve.exe"))
$Scanned = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$Bundled = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
while ($Pending.Count -gt 0) {
    $Binary = $Pending.Dequeue()
    if (-not $Scanned.Add($Binary)) { continue }
    foreach ($Dependency in Get-PeDependencies $Binary) {
        $Lower = $Dependency.ToLowerInvariant()
        $SystemPath = Join-Path $env:SystemRoot "System32/$Dependency"
        if ($Lower.StartsWith("api-ms-win-") -or
            $Lower.StartsWith("ext-ms-win-") -or
            ($SystemDlls -contains $Lower) -or
            (Test-Path -LiteralPath $SystemPath)) { continue }
        $Matches = @($RuntimeRoots | ForEach-Object { Join-Path $_ $Dependency } | Where-Object { Test-Path -LiteralPath $_ })
        if ($Matches.Count -eq 0) { throw "Required distributable DLL was not found: $Dependency (imported by $Binary)" }
        $SourceDll = $Matches[0]
        $DestinationDll = Join-Path $Stage $Dependency
        if ($Bundled.Add($Dependency)) {
            Copy-Item -LiteralPath $SourceDll -Destination $DestinationDll
            $Pending.Enqueue($DestinationDll)
        }
    }
}
Write-Host "Bundled runtime DLLs: $((@($Bundled) | Sort-Object) -join ', ')"
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
$env:BRX_NINFER_BUILD_TIMESTAMP = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
# The Windows hosted runner may have no NVIDIA driver, even for --help: this
# executable imports nvcuda.dll. Record that gap instead of treating a build as a GPU test.
$env:BRX_NINFER_LOADER_SMOKE = "skipped_no_nvidia_driver"
if (Test-Path -LiteralPath (Join-Path $env:SystemRoot "System32/nvcuda.dll")) {
    $OriginalPath = $env:PATH
    try {
        $env:PATH = "$Stage;$env:SystemRoot\System32;$env:SystemRoot"
        Invoke-Checked (Join-Path $Stage "ninfer-serve.exe") @("--help")
        $env:BRX_NINFER_LOADER_SMOKE = "passed_help"
    } finally { $env:PATH = $OriginalPath }
} else {
    Write-Host "No NVIDIA driver DLL on hosted runner; loader smoke deferred to GPU qualification."
}

# Parity evidence for the CUDA compiler cache: identical hashes and import tables
# for a cached and an uncached build mean the launcher changed no bits.
if ($env:NINFER_EMIT_BINARY_FINGERPRINT -eq '1') {
    $FingerprintLines = New-Object System.Collections.Generic.List[string]
    $Hash = (Get-FileHash -LiteralPath $Image -Algorithm SHA256).Hash
    $FingerprintLines.Add("== ninfer-serve.exe")
    $FingerprintLines.Add("sha256: $Hash")
    $FingerprintLines.Add("--- exports ---")
    $FingerprintLines.AddRange(((& dumpbin /exports $Image 2>&1) | Out-String).Trim() -split "`r?`n")
    $FingerprintLines.Add("--- dependencies ---")
    $FingerprintLines.AddRange(((& dumpbin /dependencies $Image 2>&1) | Out-String).Trim() -split "`r?`n")
    [IO.File]::WriteAllLines((Join-Path $RepoRoot "build/binary-fingerprint.txt"), $FingerprintLines, [Text.UTF8Encoding]::new($false))
    Write-Host "Binary fingerprint written to build/binary-fingerprint.txt"
}

$SccacheCppStats = $null
$SccacheCudaStats = $null
if ($SccacheExe) {
    try { $SccacheCppStats = Get-SccacheStats -CacheDir (Join-Path $CacheRoot "sccache-cpp") }
    catch { $SccacheCppStats = "stats unavailable: $($_.Exception.Message)" }
    if ($SccacheCudaOn) {
        try { $SccacheCudaStats = Get-SccacheStats -CacheDir (Join-Path $CacheRoot "sccache-cuda") }
        catch { $SccacheCudaStats = "stats unavailable: $($_.Exception.Message)" }
    }
}
$Metrics = [ordered]@{
    cache_root = $(if ($CacheRoot) { $CacheRoot } else { "disabled" })
    cache_schema = $Settings.build_cache.schema
    sccache_version = $SccacheVersion
    sccache_cuda = $(if ($SccacheCudaOn) { "on" } else { "off" })
    vcpkg_checkout = $VcpkgMetrics
    vcpkg_restored_ports = $VcpkgRestoredPorts
    vcpkg_built_ports = $VcpkgBuiltPorts
    ffmpeg_source = $FfmpegSource
    configure_seconds = $ConfigureSeconds
    build_seconds = $BuildSeconds
    total_seconds = [math]::Round($TotalStopwatch.Elapsed.TotalSeconds, 1)
}
if ($null -ne $SccacheCppStats) { $Metrics.sccache_cpp_stats = $SccacheCppStats }
if ($null -ne $SccacheCudaStats) { $Metrics.sccache_cuda_stats = $SccacheCudaStats }
Write-CacheMetrics $Metrics
Invoke-Checked python @("automation/package.py", "--stage", $Stage, "--output", "dist")
