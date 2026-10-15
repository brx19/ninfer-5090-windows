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

    def test_divergent_candidate_is_never_overwritten(self):
        first = self.invoke()
        # Someone else advanced the candidate branch on the remote with a different tree.
        probe = self.root / "probe"
        subprocess.run(["git", "clone", str(self.origin), str(probe)], check=True, capture_output=True)
        configure(probe)
        git_at(probe, "checkout", first["candidate"])
        (probe / "divergence.txt").write_text("someone else's candidate work\n")
        git_at(probe, "add", ".")
        git_at(probe, "commit", "-m", "foreign candidate update")
        git_at(probe, "push", "origin", first["candidate"])
        with self.assertRaisesRegex(RuntimeError, "Existing candidate differs"):
            self.invoke()
        # The foreign tree is still intact on the remote: refusal means no rewrite.
        git_at(self.origin, "cat-file", "-e", first["candidate"] + ":divergence.txt")

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


class WorkflowGatingTests(unittest.TestCase):
    """The release workflow must not publish, qualify or integrate anything it did not build.

    The gates are structural: a candidate-release that runs after a skipped build has no
    artifacts to download and turns an idle synchronization into a red run, and a publish that
    pins the triggering push rather than the candidate commit silently detaches release
    provenance from the bytes that were built.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.build_wf = (root / ".github" / "workflows" / "brx-sync-build.yml").read_text(encoding="utf-8")
        cls.gpu_wf = (root / ".github" / "workflows" / "gpu.yml").read_text(encoding="utf-8")

    @staticmethod
    def job_section(text: str, job: str) -> str:
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(f"  {job}:"))
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("  ") and not lines[j].startswith("    "):
                end = j
                break
        return "\n".join(lines[start:end])

    def test_candidate_release_requires_a_new_candidate(self):
        section = self.job_section(self.build_wf, "candidate-release")
        self.assertIn("if: needs.sync.outputs.changed == 'true'", section)

    def test_gpu_qualification_requires_a_new_candidate_and_the_flag(self):
        section = self.job_section(self.build_wf, "gpu")
        self.assertIn("needs.sync.outputs.changed == 'true'", section)
        self.assertIn("vars.ENABLE_GPU_QUALIFICATION == 'true'", section)

    def test_gpu_runner_labels_match_the_suite_workflow(self):
        # Two workflows sharing one card must name it the same way, or one of them queues
        # forever on a label no registered runner carries.
        build_gpu = self.job_section(self.build_wf, "gpu")
        suite = self.job_section(self.gpu_wf, "suite")
        self.assertIn("runs-on: [self-hosted, windows, gpu-5090]", build_gpu)
        self.assertIn("runs-on: [self-hosted, windows, gpu-5090]", suite)

    def test_integration_requires_successful_build_and_publish(self):
        section = self.job_section(self.build_wf, "integrate")
        self.assertIn("needs.build.result == 'success'", section)
        self.assertIn("needs.candidate-release.result == 'success'", section)

    def test_publish_provenance_uses_the_candidate_sha(self):
        # Provenance is the candidate commit, not the triggering push: a push to dev can
        # predate the candidate that merge produced.
        for line in self.build_wf.splitlines():
            if "publish.py" in line:
                self.assertNotIn("github.sha", line)
        self.assertIn("CANDIDATE_SHA: ${{ needs.sync.outputs.sha }}", self.build_wf)


class BuildCacheContractTests(unittest.TestCase):
    """Regression tests for the cross-run build-cache contract.

    The first cached runs (PR #2) failed on five confirmed defects: a generated
    cache-key output that no consuming expression referenced, PowerShell that indexed
    a non-existent ``$steps`` variable, restore/save steps that pointed at different
    directories, a bare ``actions/cache`` step paired with an explicit
    ``actions/cache/save`` (which cannot disable the implicit post-save), and a
    fingerprint formatted with ``[BitConverter]::ToString`` that produced a
    hyphen-separated 16-character string. These tests read the committed workflow
    files -- the source of truth for what the Windows runner executes -- and fail if
    any of those defects is reintroduced.
    """

    WORKFLOWS = ("brx-sync-build.yml", "brx-cache-verify.yml")

    @classmethod
    def setUpClass(cls):
        import yaml
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        cls.raw = {}
        cls.doc = {}
        for name in cls.WORKFLOWS:
            text = (root / name).read_text(encoding="utf-8")
            cls.raw[name] = text
            cls.doc[name] = yaml.safe_load(text)

    def _steps(self, name):
        return self.doc[name]["jobs"]["build"]["steps"]

    def _step(self, name, step_name):
        for step in self._steps(name):
            if step.get("name") == step_name:
                return step
        self.fail(f"{name}: step {step_name!r} not found")

    @staticmethod
    def _key_outputs(run_block):
        # Every entry in the "Compute cache keys" $Out array is a quoted
        # "name=value" line; those names are the outputs downstream expressions may
        # reference as steps.cache-keys.outputs.<name>.
        return set(re.findall(r'(?m)^\s+"([a-z0-9_]+)=[^"]*"', run_block))

    def test_every_consumed_cache_key_is_generated(self):
        # The "Input required and not supplied: key" failure: a consuming expression
        # named an output the compute step never emits (the vpkgsrc_key typo).
        for name in self.WORKFLOWS:
            generated = self._key_outputs(self._step(name, "Compute cache keys")["run"])
            self.assertGreater(len(generated), 0,
                               f"{name}: no cache-key output entries in the compute step")
            consumed = set(re.findall(r"steps\.cache-keys\.outputs\.([A-Za-z0-9_]+)", self.raw[name]))
            self.assertEqual(consumed - generated, set(),
                             f"{name}: consumed cache-key outputs the compute step never "
                             f"generates: {sorted(consumed - generated)}")

    def test_no_broken_vpkg_token_remains(self):
        # "vcpkg" does not contain "vpkg" as a substring, so any "vpkg" hit can only be
        # a broken name (vpkgsrc_key, vpkgbin_key, vpkg-bin, vpkg).
        for name in self.WORKFLOWS:
            self.assertNotIn("vpkg", self.raw[name], f"{name}: broken 'vpkg' token present")

    def test_restore_and_save_paths_match_per_layer(self):
        # A restore that reads one directory and a save that writes another means the
        # save republishes a directory the restore never validated.
        layers = ("compiler caches", "vcpkg checkout", "vcpkg binary cache", "FFmpeg archive")
        for name in self.WORKFLOWS:
            for layer in layers:
                rpath = self._step(name, "Restore " + layer)["with"]["path"]
                spath = self._step(name, "Save " + layer)["with"]["path"]
                self.assertEqual(rpath, spath,
                                 f"{name}: '{layer}' restore path {rpath!r} != save path {spath!r}")

    def test_vcpkg_binary_path_is_vcpkg_bin(self):
        # build.ps1 populates D:\ninfer-cache\vcpkg-bin; a save/restore aimed at a
        # different directory caches an empty (or wrong) tree.
        for name in self.WORKFLOWS:
            for step in (self._step(name, "Restore vcpkg binary cache"),
                         self._step(name, "Save vcpkg binary cache")):
                self.assertEqual(step["with"]["path"], "D:\\ninfer-cache\\vcpkg-bin",
                                 f"{name}: {step['name']} uses {step['with']['path']!r}")

    def test_restore_steps_use_the_restore_action(self):
        # A bare actions/cache step carries an implicit post-job save that explicit save
        # steps cannot disable. Restore must use actions/cache/restore (no post hook) and
        # save must use actions/cache/save; the bare action must not appear at all.
        for name in self.WORKFLOWS:
            for step in self._steps(name):
                uses = step.get("uses", "")
                if not uses.startswith("actions/cache"):
                    continue
                if step.get("name", "").startswith("Restore "):
                    self.assertTrue(uses.startswith("actions/cache/restore@"),
                                    f"{name}: {step['name']!r} uses {uses!r}, not "
                                    f"actions/cache/restore (no implicit post-save)")
                elif step.get("name", "").startswith("Save "):
                    self.assertTrue(uses.startswith("actions/cache/save@"),
                                    f"{name}: {step['name']!r} uses {uses!r}, not actions/cache/save")
            self.assertNotRegex(self.raw[name], r"actions/cache@(?!/)",
                                f"{name}: bare actions/cache (implicit post-save) is present")

    def test_fingerprints_use_clean_hex_and_a_sorted_file_list(self):
        # [BitConverter]::ToString yields a hyphen-separated string, so its 16-char
        # prefix was not a clean hex fingerprint; the file list must also be sorted so
        # the hash is stable across runners with different git index ordering.
        for name in self.WORKFLOWS:
            self.assertNotIn("[BitConverter]::ToString", self.raw[name],
                             f"{name}: hyphen-separated fingerprint (BitConverter::ToString)")
            self.assertIn("[Convert]::ToHexString", self.raw[name],
                          f"{name}: clean hex fingerprint (Convert::ToHexString) missing")
            self.assertIn("| Sort-Object)", self.raw[name],
                          f"{name}: fingerprint file list is not sorted deterministically")

    def test_schema_version_does_not_reuse_broken_entries(self):
        settings = json.loads((Path(__file__).resolve().parents[1] / "automation" /
                               "settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(settings["build_cache"]["schema"], 2,
                                "cache schema must be >= 2 so corrected key generation "
                                "cannot reuse entries made under the broken key scheme")

    def test_metrics_step_reads_mapped_env_not_the_steps_context(self):
        # The "$steps.PSObject.Properties" access raised "Cannot index into a null
        # array" because the GitHub steps context is not a PowerShell variable. The
        # cache-hit outputs must instead be mapped into step environment variables.
        for name in self.WORKFLOWS:
            self.assertNotIn("$steps.PSObject.Properties", self.raw[name],
                             f"{name}: PowerShell cannot index the GitHub steps context")
            metrics = self._step(name, "Report cache metrics")
            self.assertIn('[System.Environment]::GetEnvironmentVariable($Name + "_CACHE_HIT")',
                          metrics["run"],
                          f"{name}: metrics step does not read cache-hit via the "
                          f"mapped environment variables")
            env = json.dumps(metrics.get("env", {}))
            for step_id in ("compiler-caches", "vcpkg-checkout", "vcpkg-binary", "ffmpeg"):
                self.assertIn(f"steps['{step_id}'].outputs['cache-hit']", env,
                              f"{name}: metrics step does not map the {step_id} cache-hit "
                              f"into an environment variable")

    def test_save_steps_skip_on_exact_hit_and_failed_builds(self):
        # A save that runs after an exact-hit restore republishes bytes already in the
        # cache; a save on a failed build publishes unverified contents.
        for name in self.WORKFLOWS:
            for step in self._steps(name):
                if not step.get("name", "").startswith("Save "):
                    continue
                if "actions/cache" not in step.get("uses", ""):
                    continue
                cond = step.get("if", "")
                self.assertIn("success()", cond,
                              f"{name}: {step['name']} is not guarded by success()")
                self.assertIn("cache-hit'] != 'true'", cond,
                              f"{name}: {step['name']} does not skip on an exact cache hit")

    def test_corruption_run_never_saves(self):
        # The corruption scenario (R6) must not republish the cache it just corrupted.
        name = "brx-cache-verify.yml"
        found = 0
        for step in self._steps(name):
            if not step.get("name", "").startswith("Save "):
                continue
            if "actions/cache" not in step.get("uses", ""):
                continue
            found += 1
            self.assertIn("inputs.corrupt_cache != 'true'", step.get("if", ""),
                          f"{name}: {step['name']} can save during the corruption run and "
                          f"poison the production cache")
        self.assertGreater(found, 0, f"{name}: no cache save steps found to check")

    def test_metrics_rendering_empty_missed_hit_partial(self):
        # Runs the committed metrics PowerShell under mocked empty / missed / exact-hit
        # / partially-completed steps and checks the rendered report. This is the direct
        # regression for the "$steps" exception: a missing metric must render as
        # "unavailable", never raise.
        import os
        import shutil
        import subprocess
        import tempfile
        pwsh = os.environ.get("NINFER_TEST_PWSH") or shutil.which("pwsh")
        if not pwsh or not os.path.exists(pwsh):
            self.skipTest("pwsh is not available; the metrics step runs on the Windows "
                          "runner where PowerShell Core is preinstalled")
        scenarios = (
            ("empty", ("", "", "", ""),
             {"COMPILER": "unavailable", "VCPKG_SOURCE": "unavailable",
              "VCPKG_BINARY": "unavailable", "FFMPEG": "unavailable"}),
            ("missed", ("false", "false", "false", "false"),
             {"COMPILER": "false", "VCPKG_SOURCE": "false",
              "VCPKG_BINARY": "false", "FFMPEG": "false"}),
            ("exact_hit", ("true", "true", "true", "true"),
             {"COMPILER": "true", "VCPKG_SOURCE": "true",
              "VCPKG_BINARY": "true", "FFMPEG": "true"}),
            ("partial", ("true", "false", "", ""),
             {"COMPILER": "true", "VCPKG_SOURCE": "false",
              "VCPKG_BINARY": "unavailable", "FFMPEG": "unavailable"}),
        )
        env_names = ("COMPILER_CACHE_HIT", "VCPKG_SOURCE_CACHE_HIT",
                     "VCPKG_BINARY_CACHE_HIT", "FFMPEG_CACHE_HIT")
        for name in self.WORKFLOWS:
            run_block = self._step(name, "Report cache metrics")["run"]
            prefix = "restore " if "restore cache-hit=" in run_block else ""
            for label, values, expected in scenarios:
                with tempfile.TemporaryDirectory() as tmp:
                    summary = os.path.join(tmp, "summary.md")
                    full = dict(os.environ)
                    for var, val in zip(env_names, values):
                        full[var] = val
                    full["GITHUB_STEP_SUMMARY"] = summary
                    full["GITHUB_OUTPUT"] = os.path.join(tmp, "github_output")
                    proc = subprocess.run(
                        [pwsh, "-NoProfile", "-NonInteractive", "-Command", run_block],
                        cwd=tmp, env=full, capture_output=True, text=True,
                        encoding="utf-8", timeout=180, check=False)
                    self.assertEqual(proc.returncode, 0,
                                     f"{name} [{label}]: metrics step raised an "
                                     f"exception (regression of the $steps bug):\n"
                                     f"{proc.stderr}")
                    rendered = Path(summary).read_text(encoding="utf-8")
                    for cache_name, value in expected.items():
                        self.assertIn(f"{cache_name} {prefix}cache-hit={value}", rendered,
                                      f"{name} [{label}]: expected "
                                      f"'{cache_name} {prefix}cache-hit={value}' but the "
                                      f"rendered summary was:\n{rendered}")


if __name__ == "__main__":
    unittest.main()
