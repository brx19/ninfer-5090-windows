"""Package a runtime-only prerelease; GPU status is explicit and never inferred from build success."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def package(stage, output, source_sha, lock, run_id):
    for name in ("ninfer-serve.exe", "ninfer.exe", "ninfer-perplexity.exe", "LICENSE", "NOTICE"):
        if not (stage / name).is_file():
            raise ValueError("Missing package input: " + name)
    for executable in stage.glob("*.exe"):
        data = executable.read_bytes()
        if not data.startswith(b"MZ"):
            raise ValueError("Invalid PE executable: " + executable.name)
        for forbidden in (b"MSVCP140D.dll", b"VCRUNTIME140D.dll", b"ucrtbased.dll"):
            if forbidden.lower() in data.lower():
                raise ValueError("Debug CRT import: " + executable.name)
    metadata = {"repository": "brx19/ninfer-5090-windows", "commit": source_sha,
                "dependencies": lock, "cuda_architecture": "120a", "build_type": "Release",
                "cuda_compiler": os.environ.get("BRX_NINFER_NVCC_VERSION"),
                "msvc": os.environ.get("BRX_NINFER_MSVC_VERSION"), "gpu_qualified": False,
                "workflow_run_id": run_id, "loader_smoke": os.environ.get("BRX_NINFER_LOADER_SMOKE", "not_run")}
    (stage / "BUILD-MANIFEST.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (stage / "README-BRX.txt").write_text(
        "NInfer Windows RTX 5090 runtime candidate.\n"
        "Requires Windows x64, a CUDA 13.3-compatible NVIDIA driver. The MSVC runtime is statically linked; no separate Visual C++ redistributable is needed.\n"
        "GPU qualification is separate; this manifest records build status only.\n"
        "No model or maintainer launchers are bundled. Use JIT-Gateway's explicit model profile.\n"
        "FFmpeg uses the LGPL shared 9.0 build; corresponding source is available from the pinned BtbN release.\n"
        "Dependencies and source identities are recorded in BUILD-MANIFEST.json.\n", encoding="utf-8")
    files = sorted(p for p in stage.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (stage / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.relative_to(stage).as_posix()}\n" for p in files), encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"ninfer-windows-brx-{source_sha[:12]}-{run_id}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                target.write(file, file.relative_to(stage).as_posix())
    (output / (archive.name + ".sha256")).write_text(f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    lock = json.loads(Path("automation/dependencies.lock.json").read_text())
    archive = package(args.stage, args.output, sha, lock, os.environ.get("GITHUB_RUN_ID", "local"))
    print(archive)
