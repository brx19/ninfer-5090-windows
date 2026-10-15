from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

AUTOMATION = Path(__file__).resolve().parent
sys.path.insert(0, str(AUTOMATION))
import sync
import package
import publish


def git_at(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def configure(path):
    git_at(path, "config", "user.name", "test")
    git_at(path, "config", "user.email", "test@example.invalid")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "headpiece"
        self.source.mkdir()
        git_at(self.source, "init", "-b", "dev")
        configure(self.source)
        (self.source / "README.md").write_text("base\n")
        git_at(self.source, "add", ".")
        git_at(self.source, "commit", "-m", "base")
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "clone", "--bare", str(self.source), str(self.origin)], check=True, capture_output=True)
        self.work = self.root / "work"
        subprocess.run(["git", "clone", str(self.origin), str(self.work)], check=True, capture_output=True)
        configure(self.work)
        (self.work / "automation").mkdir()
        self.settings = json.loads((AUTOMATION / "settings.json").read_text())
        (self.work / "automation/settings.json").write_text(json.dumps(self.settings))
        (self.work / "automation/local.txt").write_text("keep local changes\n")
        git_at(self.work, "add", ".")
        git_at(self.work, "commit", "-m", "local automation")
        git_at(self.work, "push", "origin", "dev")
        self.previous_cwd = Path.cwd()
        os.chdir(self.work)
        self.created_prs = []
        self.dependency_revision = "a" * 40

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def mock_api(self, endpoint, method="GET", body=None):
        if endpoint.endswith("releases/latest"):
            return {"assets": [{"name": self.settings["ffmpeg_asset"],
                                "digest": "sha256:" + "b" * 64,
                                "browser_download_url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg.zip"}]}
        if endpoint == "repos/microsoft/vcpkg/commits/master":
            return {"sha": self.dependency_revision}
        if endpoint.endswith("/commits/master"):
            return {"sha": "c" * 40}
        if "/pulls?" in endpoint:
            return []
        if endpoint.endswith("/pulls") and method == "POST":
            self.created_prs.append(body)
            return {"number": len(self.created_prs)}
        raise AssertionError(endpoint)

    def invoke(self, *arguments):
        actual_git = sync.git
        def local_git(*args, **kwargs):
            args = list(args)
            if len(args) > 1 and args[0] == "fetch" and args[1] == "https://github.com/headpiece747/ninfer-5090-windows.git":
                args[1] = str(self.source)
            return actual_git(*args, **kwargs)
        output = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "brx19/ninfer-5090-windows", "GITHUB_RUN_ID": "123"}), \
             patch.object(sys, "argv", ["sync.py", *arguments]), patch.object(sync, "api", self.mock_api), \
             patch.object(sync, "git", local_git), redirect_stdout(output):
            sync.main()
        return dict(line.split("=", 1) for line in output.getvalue().splitlines() if "=" in line)

    def advance_source(self, content):
        (self.source / "README.md").write_text(content)
        git_at(self.source, "add", ".")
        git_at(self.source, "commit", "-m", "source update")

    def integrate(self, result):
        git_at(self.work, "checkout", "dev")
        git_at(self.work, "merge", "--no-edit", result["candidate_sha"])
        git_at(self.work, "push", "origin", "dev")

    def test_candidate_preserves_history_and_local_files(self):
        self.advance_source("updated upstream\n")
        before = git_at(self.origin, "rev-parse", "dev")
        result = self.invoke()
        self.assertEqual(result["changed"], "true")
        self.assertEqual(git_at(self.origin, "rev-parse", "dev"), before)
        source_sha = git_at(self.source, "rev-parse", "dev")
        git_at(self.work, "merge-base", "--is-ancestor", source_sha, result["candidate_sha"])
        self.assertEqual((self.work / "automation/local.txt").read_text(), "keep local changes\n")
        self.assertEqual(len(self.created_prs), 0)

    def test_automation_change_triggers_new_candidate(self):
        first = self.invoke()
        self.integrate(first)
        (self.work / "automation/local.txt").write_text("updated automation\n")
        git_at(self.work, "add", ".")
        git_at(self.work, "commit", "-m", "update automation")
        git_at(self.work, "push", "origin", "dev")
        second = self.invoke()
        self.assertNotEqual(second["candidate"], first["candidate"])

    def test_upstream_workflows_preserve_fork_owned_workflows(self):
        for root in (self.source, self.work):
            folder = root / ".github/workflows"
            folder.mkdir(parents=True)
            (folder / "ci.yml").write_text("local workflow\n")
            git_at(root, "add", ".")
            git_at(root, "commit", "-m", "workflow")
        git_at(self.work, "push", "origin", "dev")
        # Identical independent commits merge cleanly; source then changes its own CI.
        (self.source / ".github/workflows/ci.yml").write_text("upstream workflow\n")
        git_at(self.source, "add", ".")
        git_at(self.source, "commit", "-m", "update source workflow")
        result = self.invoke()
        self.assertEqual(result["changed"], "true")
        self.assertEqual((self.work / ".github/workflows/ci.yml").read_text(), "local workflow\n")

    def test_no_changes_after_integration(self):
        first = self.invoke()
        self.integrate(first)
        result = self.invoke()
        self.assertEqual(result, {"changed": "false"})

    def test_dependency_change_triggers_new_candidate(self):
        first = self.invoke()
        self.integrate(first)
        self.dependency_revision = "d" * 40
        result = self.invoke()
        self.assertEqual(result["changed"], "true")
        self.assertNotEqual(result["candidate"], first["candidate"])

    def test_conflict_does_not_modify_dev(self):
        (self.work / "README.md").write_text("local modification\n")
        git_at(self.work, "add", ".")
        git_at(self.work, "commit", "-m", "local engine change")
        git_at(self.work, "push", "origin", "dev")
        before = git_at(self.origin, "rev-parse", "dev")
        self.advance_source("conflicting upstream\n")
        with self.assertRaisesRegex(RuntimeError, "Merge stopped"):
            self.invoke()
        self.assertEqual(git_at(self.origin, "rev-parse", "dev"), before)
        self.assertIn("README.md", Path("automation-conflict.log").read_text())

    def test_retry_reuses_identical_candidate(self):
        first = self.invoke()
        second = self.invoke()
        self.assertEqual(first["candidate_sha"], second["candidate_sha"])

    def test_forced_rebuild_has_a_new_commit(self):
        first = self.invoke()
        self.integrate(first)
        second = self.invoke("--force-build")
        self.assertNotEqual(first["candidate_sha"], second["candidate_sha"])
        self.assertIn("-run123", second["candidate"])


class IntegrationTests(unittest.TestCase):
    def test_changed_candidate_is_refused(self):
        with patch.object(publish, "api", return_value={"object": {"sha": "b" * 40}}) as call:
            with self.assertRaisesRegex(ValueError, "Candidate branch changed"):
                publish.integrate_candidate("brx19/ninfer-5090-windows", "sync/test", "a" * 40)
            self.assertEqual(call.call_count, 1)

    def test_integration_never_forces_branch_update(self):
        sha = "a" * 40
        with patch.object(publish, "api", return_value={"object": {"sha": sha}}) as call:
            publish.integrate_candidate("brx19/ninfer-5090-windows", "sync/test", sha)
            self.assertEqual(call.call_args.args, ("repos/brx19/ninfer-5090-windows/git/refs/heads/dev", "PATCH", {"sha": sha, "force": False}))


class PackageTests(unittest.TestCase):
    def stage(self, path, debug=False):
        path.mkdir()
        for name in ("ninfer-serve.exe", "ninfer.exe", "ninfer-perplexity.exe"):
            (path / name).write_bytes(b"MZfake-test-image" + (b"MSVCP140D.dll" if debug else b""))
        (path / "LICENSE").write_text("test license")
        (path / "NOTICE").write_text("test notice")
        (path / "runtime.dll").write_bytes(b"test runtime")

    def test_archive_checksums_and_explicit_qualification(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stage = root / "stage"
            self.stage(stage)
            archive = package.package(stage, root / "output", "f" * 40, {"cuda_version": "13.3.0"}, "123")
            with zipfile.ZipFile(archive) as z:
                self.assertIsNone(z.testzip())
                self.assertFalse(json.loads(z.read("BUILD-MANIFEST.json"))["gpu_qualified"])
                for line in z.read("SHA256SUMS").decode().splitlines():
                    digest, name = line.split("  ", 1)
                    self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
            self.assertEqual(Path(str(archive) + ".sha256").read_text().split()[0], package.sha256(archive))

    def test_debug_crt_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            stage = Path(d) / "stage"
            self.stage(stage, debug=True)
            with self.assertRaisesRegex(ValueError, "Debug CRT"):
                package.package(stage, Path(d) / "output", "f" * 40, {}, "123")

    def test_missing_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "Missing package"):
                package.package(Path(d), Path(d) / "output", "f" * 40, {}, "123")

    def test_branch_identity_rejects_invalid_sha(self):
        with self.assertRaises(ValueError):
            sync.candidate_name("dev;rm", {})


if __name__ == "__main__":
    unittest.main()
