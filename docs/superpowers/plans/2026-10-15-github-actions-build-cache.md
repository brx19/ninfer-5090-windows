# GitHub Actions Build Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persistent, safe cross-run build caching for the `brx-sync-build.yml` Windows pipeline (sccache compiler cache, vcpkg binary cache, vcpkg checkout cache, FFmpeg archive cache) so repeat builds are substantially faster without compromising binary correctness or release integrity.

**Architecture:** All cache state lives under a single cache root (`D:\ninfer-cache` on hosted Windows runners) that `actions/cache@v5` (pinned `caa296126883cff596d87d8935842f9db880ef25`) restores/saves as separate blobs per cache layer. `automation/build.ps1` becomes cache-aware via env vars (`NINFER_CACHE_ROOT`, `NINFER_SCCACHE`, `NINFER_SCCACHE_CUDA`); when `NINFER_CACHE_ROOT` is unset it behaves exactly as before (self-hosted GPU runner path untouched). Cache keys are composed from OS/runner-arch, MSVC toolset version, CUDA version from the candidate lock, sm_120a, a flag fingerprint (SHA-256 of every CMake file + `automation/build.ps1`), a dependency fingerprint (vcpkg SHA + FFmpeg SHA-256 + CUDA version), and a schema version. Fallback `restore-keys` drop volatile components so stale-but-safe cache blobs are reused; per-entry correctness is enforced by the cache systems themselves (sccache content-addressing, vcpkg binary-hash verification, FFmpeg SHA-256 check).

**Tech Stack:** GitHub Actions (windows-2025, pwsh), actions/cache v5.1.0, Mozilla sccache 0.18.0 (verified for this stack: CUDA 13.3 nvcc dry-run fixes in v0.18.0 release notes), vcpkg `files` binary provider, CMake `CMAKE_<LANG>_COMPILER_LAUNCHER`.

**Spec:** the follow-up task message (2026-10-15) — sections 1-7; reference run `38716755754` (49 min total, ~36 min compile).

## Global Constraints

- Windows x64, CUDA 13.3, sm_120a, static MSVC CRT, Winsock — all preserved; no global disable of dependency tracking.
- Candidate provenance immutable; no publication after failed build; integration fast-forward only on build+release success.
- sccache C/C++ ON by default in the pipeline; sccache CUDA OFF by default, independently configurable, gated on a parity proof.
- Cache key never includes candidate commit SHA / upstream HEAD / run ID (would defeat reuse across incremental updates).
- No secrets or credentials in any cache.
- Cold build, warm build, incremental change, dependency change, toolchain change, corrupted/absent cache must all produce a correct build (fallback to clean compilation).
- Repo cache quota: 10 GB total; budget guard blocks saves above 7 GB of cache root.
- `automation/README.md` is the pipeline authority — document the caching behavior there.
- `CMakeLists.txt` is a recorded port divergence (`tools/release/port_delta_baseline.json`); editing it requires updating that entry's reason in the same commit.

## Design Decisions (from investigation)

1. **sccache 0.18.0** (latest stable, 2026-09-14) pinned by URL + SHA-256 `8965c74d5e8a225244f741e18ad2f3f504f48228dc1bac948fc22761a348363d` in `automation/settings.json`. Its release notes explicitly fix "nvcc dryrun parsing for CUDA 13.3" and Windows nvcc escaped-quote dry-run handling — the exact stack here.
2. **Local-directory caches + actions/cache**, not sccache's native GHA backend: explicit key composition per the spec, per-layer separation, auditable save/restore. GHA backend remains a documented alternative.
3. **Separate sccache directories** for C/C++ (`sccache-cpp`) and CUDA (`sccache-cuda`) via small wrapper `.bat` launchers (each sets its own `SCCACHE_DIR`; Ninja on Windows runs commands through cmd so `.bat` launchers work; sccache runs one server per directory). Gives independent CUDA enablement and separate hit/miss statistics. Fallback if wrappers misbehave on the first hosted run: shared directory, CUDA still independently enabled by omitting `CMAKE_CUDA_COMPILER_LAUNCHER`.
4. **vcpkg binary cache** via `VCPKG_BINARY_SOURCES="clear;files,<root>\vcpkg-bin,readwrite"` (the `x-gha` provider is removed from vcpkg; `files` is the supported filesystem provider). vcpkg verifies binary entries by package hash, so corrupted entries rebuild rather than corrupt.
5. **vcpkg checkout cache**: shared partial clone at `<root>\vcpkg`; HEAD checked against the pinned SHA; mismatch → fetch + checkout into the cached clone; missing/corrupt → delete + re-clone. Re-bootstrap only when HEAD changed or `vcpkg.exe` absent.
6. **FFmpeg archive cache**: content-addressed by the locked SHA-256 (`<root>\ffmpeg\<sha256>.zip`); verified before use, deleted and re-downloaded on mismatch. Only the archive is cached (per spec: no unpacked files without demonstrated benefit). The URL stays candidate-pinned; the hash is the trust boundary.
7. **CUDA toolkit install**: not directory-cached in v1. `Jimver/cuda-toolkit` already caches the installer download (`use-github-cache` default). The ~4 min install remains; revisit under quota headroom after measuring.
8. **Explicit `actions/cache/save` steps** after a metrics step with `continue-on-error: true` and a 7 GB budget guard: a save failure (e.g. quota) never fails the build job, so a cache problem can never block publication; the next run simply misses.
9. **Save only on `success()`** — never persist caches from a failed build.
10. **Metrics**: `build.ps1` writes `build/cache-metrics.json` (sccache stats per layer, vcpkg restored/built ports, FFmpeg source, configure/build/total seconds); a workflow step renders `GITHUB_STEP_SUMMARY` and records actions/cache hit flags. `NINFER_EMIT_BINARY_FINGERPRINT=1` makes `build.ps1` emit `build/binary-fingerprint.txt` (SHA-256 + `dumpbin /exports` + `dumpbin /dependencies` per executable) for CUDA-cache parity comparison.
11. **Verification workflow** `brx-build-cache-verify.yml`: manual dispatch only, checks out the triggering ref, same toolchain + cache plumbing, inputs `cuda_cache`, `skip_restore`, `corrupt_cache`, `save_cache`. This is the only way to exercise caching on hosted runners for a feature branch (the production build job always builds the dev candidate).

## File Map

| File | Responsibility |
|---|---|
| `automation/settings.json` (modify) | sccache pin (version/URL/SHA-256) + cache schema version |
| `automation/build.ps1` (modify) | cache-aware vcpkg checkout/binary cache/FFmpeg; sccache launcher wiring; metrics + binary fingerprint |
| `CMakeLists.txt` (modify) | `NINFER_CUDA_COMPILER_LAUNCHER` gate (default OFF, fatal if ON without launcher) |
| `tools/release/port_delta_baseline.json` (modify) | updated reason for `CMakeLists.txt` entry |
| `.github/workflows/brx-sync-build.yml` (modify) | sccache install, key computation, restore/save steps, metrics, `cuda_cache` dispatch input |
| `.github/workflows/brx-build-cache-verify.yml` (create) | manual hosted verification harness for cache scenarios |
| `automation/README.md` (modify) | document caching behavior |

---

### Task 1: Settings pin + CMake gate + baseline reason

**Files:**
- Modify: `automation/settings.json`, `CMakeLists.txt`, `tools/release/port_delta_baseline.json`

- [ ] **Step 1:** Add `build_cache` section to `settings.json` (schema 1; sccache 0.18.0 URL + asset + sha256).
- [ ] **Step 2:** Add `NINFER_CUDA_COMPILER_LAUNCHER` option after the MSVC block in `CMakeLists.txt` (default OFF; FATAL_ERROR if ON without `CMAKE_CUDA_COMPILER_LAUNCHER`; STATUS message naming the experimental status).
- [ ] **Step 3:** Update the `CMakeLists.txt` reason in `port_delta_baseline.json` to mention the sccache CUDA-launcher gate.
- [ ] **Step 4:** Verify: `python -m json.tool` on both JSON files; `python tools/release/check_port_delta.py` behavior unchanged (entry still present).

### Task 2: Cache-aware `automation/build.ps1`

**Files:**
- Modify: `automation/build.ps1`

- [ ] **Step 1:** Read `NINFER_CACHE_ROOT` / `NINFER_SCCACHE` / `NINFER_SCCACHE_CUDA`; start a total stopwatch.
- [ ] **Step 2:** vcpkg: reuse shared checkout (HEAD check → fetch → checkout; delete+re-clone on missing/corrupt; re-bootstrap on HEAD change or missing `vcpkg.exe`); set `VCPKG_BINARY_SOURCES` to the cache root; capture restored/built port counts from install output.
- [ ] **Step 3:** FFmpeg: content-addressed archive in cache root; verify-or-delete; download + verify + stage on miss.
- [ ] **Step 4:** sccache: validate exe, create `sccache-cpp.bat` / `sccache-cuda.bat` wrappers (`SCCACHE_DIR` per layer), pass `CMAKE_C/CXX/CUDA_COMPILER_LAUNCHER` + `NINFER_CUDA_COMPILER_LAUNCHER=ON` when enabled; wrap configure/build in stopwatches.
- [ ] **Step 5:** After build: sccache `--show-stats` per layer, binary fingerprint when requested, write `build/cache-metrics.json` (also on the GpuTests path, minimal).
- [ ] **Step 6:** Verify: `pwsh` parse check of the script; `NINFER_CACHE_ROOT` unset → byte-equivalent legacy behavior (code review + logic trace).

### Task 3: Production workflow cache steps

**Files:**
- Modify: `.github/workflows/brx-sync-build.yml`

- [ ] **Step 1:** `workflow_dispatch` input `cuda_cache` (bool, default false).
- [ ] **Step 2:** build job: install pinned sccache (hash-verified) → compute cache keys (vswhere MSVC version, flag fingerprint, dep fingerprint, schema) → restore 4 layers (sccache dirs, vcpkg checkout, vcpkg binaries, ffmpeg archive).
- [ ] **Step 3:** build step env (`NINFER_CACHE_ROOT`, `NINFER_SCCACHE`, `NINFER_SCCACHE_CUDA` from input, `SCCACHE_ERROR_LOG`).
- [ ] **Step 4:** metrics step (`if: always()`): render summary, record cache-hit flags, 7 GB budget guard → `save_blocked`.
- [ ] **Step 5:** explicit save steps (`success() && save_blocked != 'true'`, `continue-on-error: true`) after the release archive step; add `build/cache-metrics.json` + `D:\ninfer-cache\sccache-error.log` to the failure diagnostics artifact.
- [ ] **Step 6:** Verify: YAML parse, actionlint if available, action versions pinned by SHA per repo convention.

### Task 4: Verification workflow

**Files:**
- Create: `.github/workflows/brx-build-cache-verify.yml`

- [ ] **Step 1:** Manual-dispatch harness: checkout trigger ref → setup-python → cuda-toolkit (identical pin + sub-packages) → sccache install → key computation → conditional restores → optional corruption step → build with `NINFER_EMIT_BINARY_FINGERPRINT=1` → metrics → artifact upload (fingerprint + metrics) → conditional save.
- [ ] **Step 2:** Verify: YAML parse; step ordering; no trigger on push (dispatch only).

### Task 5: Documentation

**Files:**
- Modify: `automation/README.md`

- [ ] **Step 1:** Document cache layers, keys, invalidation, budget guard, CUDA-cache default-off status.
- [ ] **Step 2:** Verify against the actual implementation (no drift).

### Task 6: Local static checks

- [ ] **Step 1:** `pwsh` parse of `build.ps1` (install pwsh in WSL if needed); `python -m json.tool` on JSON files; YAML parse of both workflows.
- [ ] **Step 2:** Run `.githooks/pre-commit` gates that can run in WSL (ruff, mypy, converter tests, link/profile gates); report which gates are Windows-only and rely on `ci.yml` for them.
- [ ] **Step 3:** Commit on branch `perf/github-actions-build-cache`.

### Task 7: Hosted verification runs

- [ ] **Step 1 (R1 cold):** dispatch verify workflow → full clean build, caches filled. Record timings + metrics.
- [ ] **Step 2 (R2 warm):** re-dispatch same commit → expect large compile reduction from hits.
- [ ] **Step 3 (R3 incremental):** commit a trivial 1-file C++ + 1-file CUDA change → dispatch → expect near-full hit rate.
- [ ] **Step 4 (R4 dependency change):** commit moving `vcpkg_sha` to a different vcpkg commit → dispatch → expect vcpkg-bin selective miss + sccache mostly-hit.
- [ ] **Step 5 (R5 corruption):** dispatch with `corrupt_cache=true` → build must succeed via fallback.
- [ ] **Step 6 (R6 absent cache):** dispatch with `skip_restore=true` → clean build succeeds.
- [ ] **Step 7 (R7 CUDA parity):** run with `cuda_cache=false` (reference exes) and `cuda_cache=true`; compare `binary-fingerprint.txt` (SHA-256 + exports + dependencies of all three executables). Verdict: byte-identical → CUDA caching proven safe on this stack; any difference → keep CUDA caching OFF and document.
- [ ] **Step 8:** Confirm production pipeline still works (push `automation/**` change triggers `brx-sync-build.yml` once merged to dev — verify post-merge).

### Task 8: PR + report

- [ ] **Step 1:** Open PR to `dev` with full design + key policy + all run links.
- [ ] **Step 2:** Final report: files changed, architecture, key/invalidation policy, cold/warm timings, hit/miss stats, storage, CUDA verdict, run/PR links, limitations.

## Self-Review Notes

- Spec §1 (compiler caching): Tasks 2/3 — sccache 0.18.0, C/C++ on via launcher, CUDA off-by-default + gate + parity proof. ✔
- Spec §2 (persistence): Task 3 — actions/cache, key components per spec, no candidate-SHA keying. ✔
- Spec §3 (vcpkg): Task 2 — `files` provider, not `x-gha`, ABI/triplet/static-CRT untouched. ✔
- Spec §4 (FFmpeg): Task 2 — archive cached by locked SHA-256, verified, no unpacked cache. ✔
- Spec §5 (safeguards): Global constraints + save-on-success + per-layer corruption fallback. ✔
- Spec §6 (metrics + scenarios): Task 2/3/7 — all listed metrics; all six scenarios (R2=warm, R3=incremental, R4=dep change, R7/toolchain-change covered by key design + documented; R5/R6=corruption/absence). Toolchain version change is covered structurally (MSVC version in key) — noted as such, not run on a second MSVC (hosted runners are uniform). ✔
- Spec §7 (delivery): Tasks 6-8. ✔
