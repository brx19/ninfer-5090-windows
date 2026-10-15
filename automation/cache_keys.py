"""Compute the NInfer build-cache keys.

Single source of truth for cache-key derivation so the compatibility boundaries are
unit-testable on any OS (the workflow just shells out to this module on the Windows
runner). The flag/dependency fingerprints reproduce, byte-for-byte, the values the
inline PowerShell used to emit, so caches saved under the legacy key layout remain
restorable through the compatibility restore prefix.

Cache layout (one actions/cache entry per layer under D:\\ninfer-cache):
  sccache-cpp / sccache-cuda  compiler output (separate servers, separate stats)
  vcpkg                     shared partial clone at the pinned SHA
  vcpkg-bin                 filesystem binary provider
  ffmpeg                    the archive, content-addressed by its locked SHA256

The compiler (sccache) key is the one with a rotation policy. Its structure is:

  <prefix>-snap<commit12>-<dephash8>

where <prefix> (the compatibility boundary) is:

  ninfer-cache<schema>-sccache-<os>-<arch>-msvc<msvc>-cuda<cuda>-sm120a-flags<flaghash>-

Everything inside the prefix is a hard compatibility boundary: a change to the OS,
arch, MSVC toolset, CUDA toolkit, the sm_120a target, the cache schema, or the
compile-flag fingerprint moves the prefix, so no prior snapshot matches and the run
correctly starts cold. The volatile parts (source commit and dependency fingerprint)
live AFTER the prefix as the snapshot identity, so:

  * a source-only change keeps the prefix and moves the identity -> the prior
    snapshot is restored through the prefix restore-key and sccache content
    addressing decides which individual objects still hit;
  * an FFmpeg (dependency) change keeps the prefix and moves the identity -> same
    behaviour; the old snapshot is reusable because per-entry correctness is enforced
    by sccache, never by the GitHub key;
  * a repeated run of the same source+deps is an exact hit -> fast restore and no
    redundant snapshot is saved.

The primary restore-key is the current prefix. One explicitly allow-listed prior flag
fingerprint may be emitted as a second, fully bounded migration prefix. This permits a
known compile-compatible automation-only change to reuse the verified legacy snapshot
without introducing a broad cross-toolchain fallback.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import subprocess

# Every file that defines compile flags, link behaviour, or build arguments. Any change
# to one of them moves the flag fingerprint and therefore the compatibility prefix.
FLAG_GLOBS = (
    "CMakeLists.txt", "cmake/*", "apps/CMakeLists.txt", "src/CMakeLists.txt",
    "src/*/CMakeLists.txt", "src/*/*/CMakeLists.txt", "tests/CMakeLists.txt",
    "bench/CMakeLists.txt", "third_party/*/CMakeLists.txt", "automation/build.ps1",
)


def _run(args, *, check=True, cwd=None):
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd,
                          encoding="utf-8", errors="replace", check=check)


def flag_fingerprint(repo_root: str) -> str:
    """SHA256[:16] of the sorted per-file SHA256 hashes (uppercase) of FLAG_GLOBS.

    Reproduces the legacy PowerShell:
      ($FlagFiles | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash }) -join "|"
    then SHA256 of that joined string, lowercased, first 16 hex chars.
    """
    files = _run(["git", "ls-files", "--", *FLAG_GLOBS], cwd=repo_root).stdout.split()
    if not files:
        raise ValueError("Flag fingerprint found no CMake files; refusing to compute a cache key")
    # PowerShell Sort-Object is case-insensitive; match it so the fingerprint is
    # byte-identical to the legacy inline computation on the Windows runner. Hash the raw
    # on-disk bytes exactly as Get-FileHash does (no line-ending normalization).
    digests = []
    for name in sorted(files, key=lambda s: s.lower()):
        with open(os.path.join(repo_root, name), "rb") as stream:
            digests.append(hashlib.sha256(stream.read()).hexdigest().upper())
    joined = "|".join(digests)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest().lower()[:16]


def dependency_fingerprint(vcpkg_sha: str, ffmpeg_sha256: str, cuda_version: str) -> str:
    """SHA256[:16] of "vcpkg_sha + ffmpeg_sha256 + cuda_version" (legacy exact form)."""
    payload = f"{vcpkg_sha}{ffmpeg_sha256}{cuda_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest().lower()[:16]


def _commit_short(repo_root: str, length: int = 12) -> str:
    out = _run(["git", "rev-parse", "HEAD"], cwd=repo_root)
    full = out.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", full):
        raise ValueError("Cannot resolve HEAD commit for the snapshot identity")
    return full[:length]


def compute_sccache(lock, settings, *, msvc_version, os_name, arch, commit=None,
                    flag_hash=None, repo_root=None):
    """Return the sccache key, restore prefix, and identity.

    ``commit`` / ``flag_hash`` are injectable for tests; when omitted they are derived
    from ``repo_root`` (the checked-out tree).
    """
    schema = settings["build_cache"]["schema"]
    scver = settings["build_cache"]["sccache"]["version"]
    cuda_version = lock["cuda_version"]
    flaghash = flag_hash if flag_hash is not None else flag_fingerprint(repo_root)
    dephash = dependency_fingerprint(lock["vcpkg_sha"], lock["ffmpeg"]["sha256"], cuda_version)
    commit12 = commit if commit is not None else _commit_short(repo_root)
    prefix = (f"ninfer-cache{schema}-sccache-{os_name.lower()}-{arch.lower()}"
              f"-msvc{msvc_version}-cuda{cuda_version}-sm120a-flags{flaghash}-")
    compatible_hashes = settings["build_cache"].get("compatible_flag_hashes", [])
    if len(compatible_hashes) > 1 or any(not re.fullmatch(r"[0-9a-f]{16}", value)
                                         for value in compatible_hashes):
        raise ValueError("compatible_flag_hashes must contain at most one 16-character hex hash")
    legacy_prefix = ""
    if compatible_hashes and compatible_hashes[0] != flaghash:
        legacy_prefix = (f"ninfer-cache{schema}-sccache-{os_name.lower()}-{arch.lower()}"
                         f"-msvc{msvc_version}-cuda{cuda_version}-sm120a-"
                         f"flags{compatible_hashes[0]}-")
    identity = f"snap{commit12}-{dephash[:8]}"
    return {
        "sccache_key": f"{prefix}{identity}",
        "sccache_restore_prefix": prefix,
        "sccache_restore1": prefix,
        "sccache_restore2": legacy_prefix,
        "sccache_identity": identity,
        "schema": schema,
        "msvc_version": msvc_version,
        "cuda_version": cuda_version,
        "sccache_version": scver,
        "flag_hash": flaghash,
        "dep_hash": dephash,
        "ffmpeg_sha256": lock["ffmpeg"]["sha256"],
        "vcpkg_sha": lock["vcpkg_sha"],
        "snapshot_commit": commit12,
    }


def compute_other(lock, settings, *, msvc_version, os_name, arch):
    """Keys for the vcpkg and ffmpeg layers (unchanged content-addressing)."""
    schema = settings["build_cache"]["schema"]
    return {
        "vcpkgsrc_key": f"ninfer-cache{schema}-vcpkgsrc-{os_name.lower()}-{arch.lower()}-{lock['vcpkg_sha']}",
        "vcpkgbin_key": (f"ninfer-cache{schema}-vcpkgbin-{os_name.lower()}-{arch.lower()}"
                         f"-msvc{msvc_version}-triplet-x64-windows-vcpkg{lock['vcpkg_sha']}"),
        "vcpkgbin_restore": (f"ninfer-cache{schema}-vcpkgbin-{os_name.lower()}-{arch.lower()}"
                             f"-msvc{msvc_version}-triplet-x64-windows-"),
        "ffmpeg_key": f"ninfer-cache{schema}-ffmpeg-{lock['ffmpeg']['sha256']}",
    }


def compute_all(lock, settings, *, msvc_version, os_name, arch, commit=None,
                flag_hash=None, repo_root=None):
    out = {}
    out.update(compute_sccache(lock, settings, msvc_version=msvc_version, os_name=os_name,
                               arch=arch, commit=commit, flag_hash=flag_hash, repo_root=repo_root))
    out.update(compute_other(lock, settings, msvc_version=msvc_version, os_name=os_name, arch=arch))
    return out


# The exact output names this CLI prints as name=value lines; the workflow maps them to
# step outputs and consumes them as steps.cache-keys.outputs.<name>. The contract test
# (test_automation.BuildCacheContractTests) asserts every consumed name is in this set.
OUTPUT_NAMES = (
    "sccache_key", "sccache_restore1", "sccache_restore2", "sccache_restore_prefix", "sccache_identity",
    "vcpkgsrc_key", "vcpkgbin_key", "vcpkgbin_restore", "ffmpeg_key",
    "schema", "msvc_version", "cuda_version", "sccache_version",
    "flag_hash", "dep_hash", "ffmpeg_sha256", "vcpkg_sha", "snapshot_commit",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--msvc", required=True, help="MSVC toolset version, e.g. 14.51.36231")
    parser.add_argument("--os", required=True, help="RUNNER_OS, e.g. Windows")
    parser.add_argument("--arch", required=True, help="RUNNER_ARCH, e.g. X64")
    parser.add_argument("--commit", default=None, help="Override snapshot commit (tests)")
    args = parser.parse_args()
    repo_root = os.path.abspath(args.repo_root)
    lock = json.loads(open(os.path.join(repo_root, "automation/dependencies.lock.json"),
                           encoding="utf-8").read())
    settings = json.loads(open(os.path.join(repo_root, "automation/settings.json"),
                               encoding="utf-8").read())
    keys = compute_all(lock, settings, msvc_version=args.msvc, os_name=args.os,
                       arch=args.arch, commit=args.commit, repo_root=repo_root)
    # Emit as GITHUB_OUTPUT-friendly lines; the workflow maps them to step outputs.
    for name in OUTPUT_NAMES:
        print(f"{name}={keys[name]}")


if __name__ == "__main__":
    main()
