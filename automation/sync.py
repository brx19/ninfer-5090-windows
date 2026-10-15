"""Merge headpiece/dev into a candidate; refresh dependency pins on an independent cadence.

Two logically independent triggers produce candidates, each with its own schedule:

  * ``--mode source``        (every 6 hours) — captures a new headpiece/dev source
    snapshot. Dependency pins stay at the values already locked; a candidate is created
    only when the source (or our own build-affecting automation) actually changed.
  * ``--mode dependencies``  (daily)          — re-resolves the pinned dependencies
    (FFmpeg + vcpkg) and creates a candidate only when a real dependency update is
    available. The source stays at the already-locked headpiece snapshot.

``sync.plan_sync`` (this module) hosts the pure decision logic so the scheduling rules
are unit-testable without git or network. The build-affecting fingerprint intentionally
EXCLUDES ``upstream_master_sha``: that field is provenance only, so upstream master
moving on its own can no longer manufacture a build candidate.

Idempotency: a candidate branch name is deterministic per (source, build fingerprint),
so a repeated no-change run reuses the existing branch and never rewrites it.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

# Lock fields that change the build output. ``upstream_master_sha`` is deliberately
# omitted: it records where upstream master was, but does not affect compilation.
BUILD_FIELDS = ("cuda_version", "vcpkg_sha", "ffmpeg", "automation_sha256", "headpiece_sha")


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


def deps_of(lock):
    """Extract the dependency sub-dict (the fields a dependency refresh may move)."""
    return {k: lock[k] for k in ("cuda_version", "vcpkg_sha", "ffmpeg")}


def build_lock(source_sha, deps, automation_sha256, upstream_master_sha):
    lock = dict(deps)
    lock["automation_sha256"] = automation_sha256
    lock["headpiece_sha"] = source_sha
    lock["upstream_master_sha"] = upstream_master_sha
    return lock


def build_fingerprint(lock):
    """Only the build-affecting fields; used to decide whether a candidate is needed."""
    return {k: lock[k] for k in BUILD_FIELDS}


def candidate_name(source_sha, lock):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Invalid source SHA")
    digest = hashlib.sha256(json.dumps(build_fingerprint(lock), sort_keys=True).encode()).hexdigest()[:12]
    return f"sync/headpiece-{source_sha[:12]}-{digest}"


def plan_sync(current_lock, *, source_sha, source_is_ancestor, resolved_deps,
              automation_sha256, upstream_sha, mode, force):
    """Pure scheduling decision. Returns (new_lock, changed, merge_source_sha).

    ``merge_source_sha`` is the commit to record in the lock and merge into the
    candidate. No git/network here — the caller supplies the facts.
    """
    if current_lock is None:
        # First run: nothing is pinned yet, so capture source and dependencies together.
        return build_lock(source_sha, resolved_deps, automation_sha256, upstream_sha), True, source_sha
    if mode == "source":
        # Source may move; dependencies stay pinned; provenance refreshed for the record.
        src = current_lock["headpiece_sha"] if source_is_ancestor else source_sha
        deps = deps_of(current_lock)
        upstream = upstream_sha
    elif mode == "dependencies":
        # Source stays pinned; dependencies may move to the freshly resolved values.
        src = current_lock["headpiece_sha"]
        deps = resolved_deps
        upstream = current_lock["upstream_master_sha"]
    else:
        raise ValueError("unknown mode: " + mode)
    new_lock = build_lock(src, deps, automation_sha256, upstream)
    changed = (build_fingerprint(new_lock) != build_fingerprint(current_lock)) or force
    return new_lock, changed, src


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
    parser.add_argument("--mode", choices=["source", "dependencies"], default="source",
                        help="which cadence produced this run (default: source)")
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
    resolved_deps = resolve_dependencies(settings)
    automation_tree = git("ls-tree", "-r", "HEAD", "--", "automation", ".github/workflows/brx-sync-build.yml").stdout
    automation_entries = "\n".join(line for line in automation_tree.splitlines()
                                   if not line.endswith("\tautomation/dependencies.lock.json"))
    automation_sha256 = hashlib.sha256(automation_entries.encode()).hexdigest()
    upstream_sha = api(f"repos/{settings['upstream_repository']}/commits/{settings['upstream_branch']}")["sha"]
    ancestor = git("merge-base", "--is-ancestor", source_sha, "HEAD", check=False)
    if ancestor.returncode not in (0, 1):
        raise RuntimeError("Cannot compare source history")
    source_is_ancestor = ancestor.returncode == 0
    path = Path("automation/dependencies.lock.json")
    current = json.loads(path.read_text()) if path.exists() else None
    lock, changed, merge_sha = plan_sync(
        current, source_sha=source_sha, source_is_ancestor=source_is_ancestor,
        resolved_deps=resolved_deps, automation_sha256=automation_sha256,
        upstream_sha=upstream_sha, mode=args.mode, force=args.force_build)
    if not changed:
        output("changed", "false")
        return
    branch = candidate_name(merge_sha, lock)
    # A manual forced retry gets a fresh branch name and package rather than modifying an old tag.
    if args.force_build:
        branch += "-run" + os.environ.get("GITHUB_RUN_ID", "local")
    git("checkout", "-B", branch)
    merged = git("merge", "--no-edit", merge_sha, check=False)
    if merged.returncode:
        conflicts = git("diff", "--name-only", "--diff-filter=U", check=False).stdout
        conflict_paths = conflicts.splitlines()
        if not conflict_paths or any(not name.startswith(".github/workflows/") for name in conflict_paths):
            report_conflict(repo, merge_sha, merged.stdout + merged.stderr + "\n" + conflicts)
            git("merge", "--abort", check=False)
            raise RuntimeError("Merge stopped; no changes pushed to dev")
        git("restore", "--source=" + base_sha, "--staged", "--worktree", "--", ".github/workflows")
        git("commit", "--no-edit")
    # This fork owns its workflows. Preserve them when merging engine changes, so the
    # standard Actions token never needs permission to import upstream workflow edits.
    if git("ls-tree", "-r", "--name-only", base_sha, "--", ".github/workflows").stdout.strip():
        git("restore", "--source=" + base_sha, "--staged", "--worktree", "--", ".github/workflows")
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    git("add", "automation/dependencies.lock.json")
    if git("diff", "--cached", "--quiet", check=False).returncode:
        git("commit", "-m", f"chore(automation): pin candidate source and dependencies ({args.mode})")
    elif args.force_build:
        git("commit", "--allow-empty", "-m", f"chore(automation): rebuild pinned candidate ({args.mode})")
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
    output("changed", "true")
    output("candidate", branch)
    output("candidate_sha", sha)
    output("base_sha", base_sha)
    output("mode", args.mode)


if __name__ == "__main__":
    main()
