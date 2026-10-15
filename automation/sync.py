"""Merge headpiece/dev into a candidate; update dependency pins without rewriting local work."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def run(args, *, check=True, input=None):
    return subprocess.run(args, input=input, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def git(*args, check=True):
    return run(["git", *args], check=check)


def api(endpoint, method="GET", body=None):
    args = ["gh", "api", endpoint, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    result = run(args, input=json.dumps(body) if body is not None else None)
    return json.loads(result.stdout) if result.stdout.strip() else None


def output(name, value):
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{name}={value}\n")
    print(f"{name}={value}")


def candidate_name(source_sha, lock):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Invalid source SHA")
    digest = hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()[:12]
    return f"sync/headpiece-{source_sha[:12]}-{digest}"


def resolve_dependencies(settings):
    release = api(f"repos/{settings['ffmpeg_repository']}/releases/latest")
    assets = [a for a in release["assets"] if a["name"] == settings["ffmpeg_asset"]]
    if len(assets) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", assets[0].get("digest", "")):
        raise ValueError("Exactly one FFmpeg asset with SHA256 is required")
    asset = assets[0]
    expected = f"https://github.com/{settings['ffmpeg_repository']}/releases/download/"
    if not asset["browser_download_url"].startswith(expected):
        raise ValueError("Unexpected FFmpeg download URL")
    vcpkg = api(f"repos/{settings['vcpkg_repository']}/commits/master")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", vcpkg):
        raise ValueError("Invalid vcpkg commit")
    return {"cuda_version": settings["cuda_version"], "vcpkg_sha": vcpkg,
            "ffmpeg": {"name": asset["name"], "url": asset["browser_download_url"],
                       "sha256": asset["digest"][7:]}}


def report_conflict(repo, source_sha, message):
    # A pull request cannot represent an uncommitted merge conflict; put the evidence in the run.
    path = Path("automation-conflict.log")
    path.write_text(message, encoding="utf-8")
    output("conflict", "true")
    print(f"Merge conflict for {source_sha}; see workflow artifact, repository {repo}.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-build", action="store_true")
    args = parser.parse_args()
    settings = json.loads(Path("automation/settings.json").read_text(encoding="utf-8"))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo != settings["repository"]:
        raise ValueError("This automation is scoped to " + settings["repository"])
    base = settings["integration_branch"]
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("config", "core.hooksPath", ".git/disabled-hooks")
    git("fetch", "origin", base)
    git("checkout", "-B", base, "origin/" + base)
    base_sha = git("rev-parse", "HEAD").stdout.strip()
    git("fetch", f"https://github.com/{settings['source_repository']}.git", settings["source_branch"])
    source_sha = git("rev-parse", "FETCH_HEAD").stdout.strip()
    lock = resolve_dependencies(settings)
    lock["headpiece_sha"] = source_sha
    lock["upstream_master_sha"] = api(f"repos/{settings['upstream_repository']}/commits/{settings['upstream_branch']}")["sha"]
    ancestor = git("merge-base", "--is-ancestor", source_sha, "HEAD", check=False)
    if ancestor.returncode not in (0, 1):
        raise RuntimeError("Cannot compare source history")
    path = Path("automation/dependencies.lock.json")
    current = json.loads(path.read_text()) if path.exists() else None
    if ancestor.returncode == 0 and current == lock and not args.force_build:
        output("changed", "false")
        return
    branch = candidate_name(source_sha, lock)
    # A manual forced retry gets a fresh branch name and package rather than modifying an old tag.
    if args.force_build:
        branch += "-run" + os.environ.get("GITHUB_RUN_ID", "local")
    git("checkout", "-B", branch)
    merged = git("merge", "--no-edit", source_sha, check=False)
    if merged.returncode:
        conflicts = git("diff", "--name-only", "--diff-filter=U", check=False).stdout
        report_conflict(repo, source_sha, merged.stdout + merged.stderr + "\n" + conflicts)
        git("merge", "--abort", check=False)
        raise RuntimeError("Merge stopped; no changes pushed to dev")
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    git("add", "automation/dependencies.lock.json")
    if git("diff", "--cached", "--quiet", check=False).returncode:
        git("commit", "-m", "chore(automation): pin candidate source and dependencies")
    elif args.force_build:
        git("commit", "--allow-empty", "-m", "chore(automation): rebuild pinned candidate")
    # Retry an existing identical candidate safely; never force-rewrite a branch.
    pushed = git("push", "origin", "HEAD:refs/heads/" + branch, check=False)
    if pushed.returncode:
        git("fetch", "origin", branch)
        previous = git("rev-parse", "FETCH_HEAD").stdout.strip()
        # Same source/dependencies plus same tree is reusable even if the commit timestamp differs.
        if git("rev-parse", "HEAD^{tree}").stdout != git("rev-parse", previous + "^{tree}").stdout:
            raise RuntimeError("Existing candidate differs; branch was not overwritten")
        git("checkout", "--detach", previous)
    sha = git("rev-parse", "HEAD").stdout.strip()
    prs = api(f"repos/{repo}/pulls?state=open&head=brx19:{branch}&base={base}")
    if not prs:
        api(f"repos/{repo}/pulls", "POST", {
            "title": f"chore(sync): headpiece {source_sha[:12]} and dependency refresh",
            "head": branch, "base": base,
            "body": "Automated candidate. Built and published as prerelease before integration. "
                    "GPU qualification is recorded separately. Source: " + source_sha,
        })
    output("changed", "true")
    output("candidate", branch)
    output("candidate_sha", sha)
    output("base_sha", base_sha)


if __name__ == "__main__":
    main()
