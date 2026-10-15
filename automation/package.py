"""Create and verify the minimal, server-only Windows release archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile


APPLICATION = "ninfer-serve.exe"
FORBIDDEN_APPLICATIONS = {"ninfer.exe", "ninfer-perplexity.exe"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_archive(archive: Path) -> dict:
    """Validate the package contract and return its manifest."""
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("ZIP CRC validation failed")
        names = set(bundle.namelist())
        executables = {Path(name).name.lower() for name in names if name.lower().endswith(".exe")}
        if executables != {APPLICATION}:
            raise ValueError(f"Expected exactly {APPLICATION}; found {sorted(executables)}")
        required = {APPLICATION, "BUILD-MANIFEST.json", "LICENSE", "NOTICE", "SHA256SUMS"}
        missing = required - names
        if missing:
            raise ValueError("Archive is missing: " + ", ".join(sorted(missing)))
        checksums = bundle.read("SHA256SUMS").decode("utf-8").splitlines()
        checked = set()
        for line in checksums:
            digest, name = line.split("  ", 1)
            if name == "SHA256SUMS" or name not in names:
                raise ValueError("Invalid internal checksum entry: " + name)
            if hashlib.sha256(bundle.read(name)).hexdigest() != digest:
                raise ValueError("Internal checksum mismatch: " + name)
            checked.add(name)
        if checked != names - {"SHA256SUMS"}:
            raise ValueError("Internal checksum coverage mismatch")
        return json.loads(bundle.read("BUILD-MANIFEST.json"))


def package(stage: Path, output: Path, source_sha: str, lock: dict, run_id: str) -> Path:
    for name in (APPLICATION, "LICENSE", "NOTICE"):
        if not (stage / name).is_file():
            raise ValueError("Missing package input: " + name)
    applications = {path.name.lower() for path in stage.glob("*.exe")}
    if applications != {APPLICATION}:
        raise ValueError(f"Stage must contain exactly {APPLICATION}; found {sorted(applications)}")
    if applications & FORBIDDEN_APPLICATIONS:
        raise ValueError("Unused application was staged")
    data = (stage / APPLICATION).read_bytes()
    if not data.startswith(b"MZ"):
        raise ValueError("Invalid PE executable: " + APPLICATION)
    for forbidden in (b"MSVCP140D.dll", b"VCRUNTIME140D.dll", b"ucrtbased.dll"):
        if forbidden.lower() in data.lower():
            raise ValueError("Debug CRT import: " + APPLICATION)

    metadata = {
        "schema": 1,
        "repository": "brx19/ninfer-5090-windows",
        "fork_commit": source_sha,
        "upstream": {
            "repository": "headpiece747/ninfer-5090-windows",
            "commit": lock.get("headpiece_sha"),
            "canonical_repository": "Neroued/ninfer",
            "canonical_commit": lock.get("upstream_master_sha"),
        },
        "dependencies": lock,
        "cuda_architecture": "sm_120a",
        "build_type": "Release",
        "cuda_compiler": os.environ.get("BRX_NINFER_NVCC_VERSION"),
        "msvc": os.environ.get("BRX_NINFER_MSVC_VERSION"),
        "build_timestamp_utc": os.environ.get("BRX_NINFER_BUILD_TIMESTAMP"),
        "workflow_run_id": run_id,
        "application": APPLICATION,
        "runtime_dlls": sorted(path.name for path in stage.glob("*.dll")),
        "gpu_qualified": False,
        "loader_smoke": os.environ.get("BRX_NINFER_LOADER_SMOKE", "not_run"),
    }
    (stage / "BUILD-MANIFEST.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (stage / "README-BRX.txt").write_text(
        "NInfer server-only Windows build for RTX 5090 (sm_120a).\n"
        "Requires Windows x64 and a CUDA 13.3-compatible NVIDIA driver.\n"
        "The hosted build is not GPU qualification; see BUILD-MANIFEST.json.\n"
        "Source/dependency provenance and exact pins are recorded in the manifest.\n",
        encoding="utf-8")
    files = sorted(path for path in stage.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (stage / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.relative_to(stage).as_posix()}\n" for path in files),
        encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"ninfer-serve-windows-rtx5090-{source_sha[:12]}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                target.write(file, file.relative_to(stage).as_posix())
    digest = sha256(archive)
    (output / (archive.name + ".sha256")).write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8")
    manifest = verify_archive(archive)
    if manifest["fork_commit"] != source_sha:
        raise ValueError("Packaged manifest commit does not match source")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace").stdout.strip()
    lock = json.loads(Path("automation/dependencies.lock.json").read_text(encoding="utf-8"))
    print(package(args.stage, args.output, source_sha, lock,
                  os.environ.get("GITHUB_RUN_ID", "local")))
