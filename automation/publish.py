"""Safely integrate, publish, verify, and clean up a server build candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from package import verify_archive
from sync import api, run


REPOSITORY = "brx19/ninfer-5090-windows"
CANDIDATE_RE = re.compile(r"sync/headpiece-[0-9a-f]{12}-[0-9a-f]{12}(?:-run[0-9]+)?$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ref_sha(repo: str, ref: str) -> str:
    return api(f"repos/{repo}/git/ref/{ref}")["object"]["sha"]


def validate_candidate_name(branch: str) -> None:
    if not CANDIDATE_RE.fullmatch(branch):
        raise ValueError("Refusing non-automation candidate branch: " + branch)


def integrate_candidate(repo: str, branch: str, sha: str, expected_base: str | None = None) -> str:
    validate_candidate_name(branch)
    if ref_sha(repo, "heads/" + branch) != sha:
        raise ValueError("Candidate branch changed; integration refused")
    current = ref_sha(repo, "heads/dev")
    if current == sha:
        print("Candidate is already integrated: " + sha)
        return "already-integrated"
    if expected_base and current != expected_base:
        raise ValueError(f"dev advanced from {expected_base} to {current}; integration refused")
    result = api(f"repos/{repo}/git/refs/heads/dev", "PATCH", {"sha": sha, "force": False})
    if result.get("object", {}).get("sha") != sha:
        raise ValueError("Candidate was not integrated")
    print("Integrated candidate: " + sha)
    return "integrated"


def release_inputs(dist: Path, sha: str) -> tuple[Path, Path]:
    archives = list(dist.glob("ninfer-serve-windows-rtx5090-*.zip"))
    if len(archives) != 1:
        raise ValueError("Expected exactly one server release archive")
    archive = archives[0]
    checksum = Path(str(archive) + ".sha256")
    if not checksum.is_file():
        raise ValueError("Missing external archive checksum")
    fields = checksum.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != archive.name or fields[0] != file_sha256(archive):
        raise ValueError("External archive checksum mismatch")
    manifest = verify_archive(archive)
    if manifest.get("fork_commit") != sha:
        raise ValueError("Archive provenance does not match candidate SHA")
    return archive, checksum


def _release(repo: str, tag: str) -> dict | None:
    result = run(["gh", "release", "view", tag, "--repo", repo,
                  "--json", "tagName,isDraft,isPrerelease,targetCommitish,assets,url"], check=False)
    if result.returncode:
        return None
    return json.loads(result.stdout)


def _verify_release(repo: str, tag: str, sha: str, expected: list[Path]) -> dict:
    release = _release(repo, tag)
    if release is None or release.get("isDraft") or release.get("isPrerelease"):
        raise ValueError("Final release is missing, draft, or prerelease")
    if ref_sha(repo, "tags/" + tag) != sha:
        raise ValueError("Release tag does not reference the integrated commit")
    remote = {asset["name"]: asset for asset in release.get("assets", [])}
    for path in expected:
        asset = remote.get(path.name)
        if not asset or int(asset.get("size", -1)) != path.stat().st_size:
            raise ValueError("Release asset is missing or has a different size: " + path.name)
    with tempfile.TemporaryDirectory() as temporary:
        command = ["gh", "release", "download", tag, "--repo", repo, "--dir", temporary]
        for path in expected:
            command += ["--pattern", path.name]
        run(command)
        for path in expected:
            downloaded = Path(temporary) / path.name
            if file_sha256(downloaded) != file_sha256(path):
                raise ValueError("Published release asset differs from verified build: " + path.name)
    return release


def publish_release(repo: str, sha: str, dist: Path, qualified: bool = False) -> tuple[str, dict]:
    archive, checksum = release_inputs(dist, sha)
    tag = "brx-serve-" + sha[:12]
    notes = dist / "RELEASE-NOTES-BRX.md"
    notes.write_text(
        f"Server-only Windows RTX 5090 build from `{sha}`.\n\n"
        "Contains one application (`ninfer-serve.exe`), its required distributable DLLs, "
        "licenses, provenance manifest, and checksums.\n\n"
        f"GPU qualification: {'passed separately; see qualification.json' if qualified else 'not executed'}.\n",
        encoding="utf-8")
    existing = _release(repo, tag)
    if existing is None:
        run(["gh", "release", "create", tag, "--repo", repo, "--target", sha,
             "--title", tag, "--notes-file", str(notes), str(archive), str(checksum)])
    else:
        if existing.get("isDraft") or existing.get("isPrerelease") or ref_sha(repo, "tags/" + tag) != sha:
            raise ValueError("Existing release identity does not match the validated commit")
        existing_assets = {asset["name"] for asset in existing.get("assets", [])}
        for path in (archive, checksum):
            if path.name not in existing_assets:
                run(["gh", "release", "upload", tag, str(path), "--repo", repo])
    expected_assets = [archive, checksum]
    if qualified:
        qualification = dist / "qualification.json"
        if not qualification.is_file():
            raise ValueError("GPU qualification was requested but qualification.json is absent")
        report = json.loads(qualification.read_text(encoding="utf-8"))
        if (report.get("gpu_qualified") is not True or report.get("source_commit") != sha or
                report.get("archive_sha256") != file_sha256(archive)):
            raise ValueError("GPU qualification does not describe this exact source/archive")
        current = _release(repo, tag)
        assets = {asset["name"] for asset in (current or {}).get("assets", [])}
        if qualification.name not in assets:
            run(["gh", "release", "upload", tag, str(qualification), "--repo", repo])
        expected_assets.append(qualification)
    verified = _verify_release(repo, tag, sha, expected_assets)
    print("Verified final release: " + verified["url"])
    return tag, verified


def cleanup_candidate(repo: str, branch: str, sha: str, tag: str) -> str:
    validate_candidate_name(branch)
    if ref_sha(repo, "heads/dev") != sha:
        raise ValueError("dev no longer identifies the released candidate; cleanup refused")
    if ref_sha(repo, "heads/" + branch) != sha:
        raise ValueError("Candidate branch changed; cleanup refused")
    if ref_sha(repo, "tags/" + tag) != sha:
        raise ValueError("Release tag mismatch; cleanup refused")
    api(f"repos/{repo}/git/refs/heads/{branch}", "DELETE")
    print("Deleted verified automation branch: " + branch)
    return "deleted"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--qualified", action="store_true")
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo != REPOSITORY:
        raise ValueError("Unexpected target repository")
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise ValueError("Invalid candidate SHA")
    if args.base_sha and not re.fullmatch(r"[0-9a-f]{40}", args.base_sha):
        raise ValueError("Invalid base SHA")
    # Validate bytes before moving dev. A failure before this point is side-effect free.
    release_inputs(Path("dist"), args.sha)
    integrate_candidate(repo, args.candidate, args.sha, args.base_sha)
    tag, _ = publish_release(repo, args.sha, Path("dist"), args.qualified)
    if not args.no_cleanup:
        cleanup_candidate(repo, args.candidate, args.sha, tag)


if __name__ == "__main__":
    main()
