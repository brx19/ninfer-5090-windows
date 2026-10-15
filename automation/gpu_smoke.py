"""Verify and run the exact cloud-built archive on the local RTX5090, without changing Gateway."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    archives = list(Path("dist").glob("*.zip"))
    if len(archives) != 1:
        raise ValueError("Expected one cloud-built runtime archive")
    archive = archives[0]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected = Path(str(archive) + ".sha256").read_text().split()[0]
    if digest != expected:
        raise ValueError("Cloud archive SHA mismatch")
    stage = Path("dist/smoke-runtime")
    stage.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or ":" in name:
                raise ValueError("Unsafe archive path")
        z.extractall(stage)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log = Path("dist/gpu-smoke-server.log")
    command = [str((stage / "ninfer-serve.exe").resolve()), str(args.artifact.resolve()),
               "--host", "127.0.0.1", "--port", str(port), "--model-id", "Qwen3.8-27B-nvfp4-NInfer-v3",
               "--max-context", "196608", "--kv-capacity", "196608", "--kv-dtype", "fp8",
               "--host-context-mib", "8192", "--max-concurrency", "1", "--prefill-chunk", "1024",
               "--spec", "dflash2", "--draft-tokens", "5", "--lm-head-draft"]
    with log.open("wb") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Published binary failed startup; see gpu-smoke-server.log")
                try:
                    with urllib.request.urlopen(base + "/health", timeout=3) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Published binary readiness timeout")
            results = []
            for number in range(3):
                body = {"model": "Qwen3.8-27B-nvfp4-NInfer-v3", "messages": [
                    {"role": "user", "content": "Reply briefly: what is a prefix cache?"}],
                    "max_tokens": 256, "stream": False, "enable_thinking": False}
                request = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    result = json.load(response)
                if result.get("error") or not result.get("choices") or not result["choices"][0].get("message", {}).get("content"):
                    raise RuntimeError("Smoke request failed")
                results.append({"request": number, "finish_reason": result["choices"][0].get("finish_reason"), "usage": result.get("usage")})
            report = {"archive_sha256": digest, "source_commit": json.loads((stage / "BUILD-MANIFEST.json").read_text())["commit"],
                      "gpu_qualified": True, "scope": "source unit/model suite twice plus exact hosted runtime smoke",
                      "historical_hermes_replay": "not_run", "smoke_requests": results}
            Path("dist/qualification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
