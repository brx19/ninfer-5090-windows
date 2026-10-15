# BRX automation

The default branch is dev. Scheduled synchronization merges headpiece/dev into
a candidate while preserving local commits; it never force-pushes dev.
Source and mutable dependency assets are resolved into dependencies.lock.json
before a build. Neroued/master is recorded, not independently merged.

Cloud builds publish runtime-only prereleases. They are not equivalent to the
maintainer's original release gate or a GPU performance qualification.
With ENABLE_GPU_QUALIFICATION=true and an online Windows/rtx5090 runner,
the existing model-required randomized suite and the exact published binary
smoke test must pass before promotion/integration. This functional GPU test
does not run the historical Hermes workload. Evidence is qualification.json;
the original archive manifest intentionally remains a record of its hosted build.

CUDA13.3 and Actions are pinned. FFmpeg LGPL shared9 and vcpkg/curl refresh
automatically. A dependency/API/merge failure stops the candidate; previous dev
and previous releases remain available. Runtime installation into JIT-Gateway
is a separate operation and does not occur through this pipeline.

After a successful build, integration fast-forwards dev with force=false. It
requires no pull-request creation setting or personal token. A concurrent local
commit makes integration fail safely and is included in the next candidate.
This fork owns .github/workflows; upstream workflow edits are not imported.
Engine code and vendored dependencies are merged normally. Automation changes
also trigger a build; generated lock files do not cause endless rebuilds.

GitHub-hosted builds use a persistent cache under D:\ninfer-cache with four
independent actions/cache layers: the sccache C/C++ cache, the sccache CUDA
cache, the pinned vcpkg checkout plus its filesystem binary cache, and the
FFmpeg archive keyed by its locked SHA256. Cache keys are OS/arch, MSVC
toolset, CUDA version, sm_120a, a hash of the build-flag files, and the
dependency fingerprint plus the cache schema; none are keyed to the candidate
commit, so incremental changes restore the previous cache. sccache is pinned
with a verified SHA256 in settings.json; C/C++ caching is always on, while
NVCC routing through sccache stays off by default (cuda_cache dispatch input,
NINFER_CUDA_COMPILER_LAUNCHER CMake gate) until a parity run proves
identical outputs. Each run reports cache hits, misses, non-cacheable
commands, per-language statistics, and compilation duration to build/
cache-metrics.json and the step summary. A cache save is only attempted after
a successful build and is skipped when the cache root exceeds 7 GiB.
brx-cache-verify.yml is a scratch workflow that exercises the same cache on
a branch without publishing a candidate; its corrupt_cache input proves that
corrupted entries fall back to a recompile instead of a bad binary.
