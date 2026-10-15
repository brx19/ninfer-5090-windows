from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync import api, git, run


def integrate_candidate(repo, branch, sha):
    candidate = api(f"repos/{repo}/git/ref/heads/{branch}")
    if candidate["object"]["sha"] != sha:
        raise ValueError("Candidate branch changed; integration refused")
    # GitHub enforces ancestry atomically: a concurrent dev commit cannot be overwritten.
    result = api(f"repos/{repo}/git/refs/heads/dev", "PATCH", {"sha": sha, "force": False})
    if result.get("object", {}).get("sha") != sha:
        raise ValueError("Candidate was not integrated")
    print("Integrated candidate: " + sha)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--qualified", action="store_true")
    parser.add_argument("--integrate", action="store_true")
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    if repo != "brx19/ninfer-5090-windows":
        raise ValueError("Unexpected target repository")
    run_id = os.environ["GITHUB_RUN_ID"]
    tag = f"brx-candidate-{args.sha[:12]}-{run_id}"
    archives = list(Path("dist").glob("*.zip"))
    if len(archives) != 1:
        raise ValueError("Expected exactly one built runtime archive")
    notes = Path("dist/RELEASE-NOTES-BRX.md")
    if args.qualified:
        report = json.loads(Path("dist/qualification.json").read_text(encoding="utf-8"))
        actual_digest = hashlib.sha256(archives[0].read_bytes()).hexdigest()
        if report.get("gpu_qualified") is not True or report.get("source_commit") != args.sha or report.get("archive_sha256") != actual_digest:
            raise ValueError("GPU qualification does not describe this exact source/archive")
        notes.write_text(f"Source commit: `{args.sha}`.\n\n"
                         "GPU qualification: RTX5090 source suite + model coverage gate (two randomized runs) "
                         "+ exact published runtime smoke test.\n"
                         "This is functional qualification; historical Hermes performance replay was not run.\n"
                         "The archive BUILD-MANIFEST records the original hosted build, so its gpu_qualified field "
                         "remains false. Separate qualification.json is authoritative for GPU testing.\n", encoding="utf-8")
        run(["gh", "release", "edit", tag, "--repo", repo, "--prerelease=false", "--latest", "--notes-file", str(notes)])
        run(["gh", "release", "upload", tag, "dist/qualification.json", "--repo", repo, "--clobber"])
    else:
        notes.write_text(f"Source commit: `{args.sha}`.\n\n"
                         "Automatically compiled Windows sm_120a Release candidate. GPU tests not yet completed.\n"
                         "Includes explicit source/dependency pins, runtime DLLs, licenses and SHA256SUMS.\n"
                         "Microsoft Visual C++ x64 Redistributable is required.\n", encoding="utf-8")
        existing = run(["gh", "release", "view", tag, "--repo", repo], check=False)
        if existing.returncode:
            run(["gh", "release", "create", tag, "--repo", repo, "--target", args.sha,
                 "--prerelease", "--title", tag, "--notes-file", str(notes)])
        run(["gh", "release", "upload", tag, str(archives[0]), str(archives[0]) + ".sha256", "--repo", repo, "--clobber"])
    if args.integrate:
        integrate_candidate(repo, args.candidate, args.sha)



if __name__ == "__main__":
    main()
