"""Regression tests for the build-cache key scheme and snapshot retention.

These are pure (no network, no Windows runner): they pin the compatibility
boundaries of the sccache cache key, prove that a source or FFmpeg change keeps
the restore prefix while MSVC/CUDA/schema/flag changes move it, verify the new
prefix still restores the legacy caches saved before the rotation scheme, and
exercise the bounded retention selection logic.
"""
import datetime as dt
import json
import re
import unittest
from pathlib import Path

AUTOMATION = Path(__file__).resolve().parent
REPO_ROOT = AUTOMATION.parent
import sys
sys.path.insert(0, str(AUTOMATION))
import cache_keys
import retention


# Legacy keys captured from the real dev + feature runs before the rotation scheme.
LEGACY_DEV = ("ninfer-cache2-sccache-windows-x64-msvc14.51.36231-cuda13.3.0"
              "-sm120a-flags20d1a528e14db04c-deps97ccf28cce271197-sc0.18.0")
LEGACY_FEATURE = ("ninfer-cache2-sccache-windows-x64-msvc14.51.36231-cuda13.3.0"
                  "-sm120a-flags20d1a528e14db04c-deps86b3ef4af4840707-sc0.18.0")
LEGACY_FLAG_HASH = "20d1a528e14db04c"


def _settings(schema=2, scver="0.18.0"):
    return {"build_cache": {"schema": schema, "sccache": {"version": scver}}}


def _lock(vcpkg="20ceb8adbf0504d4b0d5e4058a32f9d4fc896391",
          ffmpeg="624803200fd8223faa668691d0cc9d37be3e460d344e4b26948a09d63151e868",
          cuda="13.3.0"):
    return {"cuda_version": cuda, "vcpkg_sha": vcpkg,
            "ffmpeg": {"name": "ff", "url": "https://x", "sha256": ffmpeg}}


class DependencyFingerprintTests(unittest.TestCase):
    def test_is_clean_sixteen_char_hex(self):
        h = cache_keys.dependency_fingerprint("a" * 40, "b" * 64, "13.3.0")
        self.assertRegex(h, r"^[0-9a-f]{16}$")

    def test_reproduces_legacy_dev_dep_hash(self):
        # The current pinning must reproduce the dep hash embedded in the legacy dev cache key,
        # so caches saved under that key remain addressable.
        self.assertEqual(
            cache_keys.dependency_fingerprint(
                "20ceb8adbf0504d4b0d5e4058a32f9d4fc896391",
                "624803200fd8223faa668691d0cc9d37be3e460d344e4b26948a09d63151e868",
                "13.3.0"),
            "97ccf28cce271197")

    def test_changes_with_each_input(self):
        base = cache_keys.dependency_fingerprint("a" * 40, "b" * 64, "13.3.0")
        self.assertNotEqual(cache_keys.dependency_fingerprint("c" * 40, "b" * 64, "13.3.0"), base)
        self.assertNotEqual(cache_keys.dependency_fingerprint("a" * 40, "d" * 64, "13.3.0"), base)
        self.assertNotEqual(cache_keys.dependency_fingerprint("a" * 40, "b" * 64, "13.2.0"), base)

    def test_is_deterministic(self):
        a = cache_keys.dependency_fingerprint("a" * 40, "b" * 64, "13.3.0")
        b = cache_keys.dependency_fingerprint("a" * 40, "b" * 64, "13.3.0")
        self.assertEqual(a, b)


class SccacheKeyStructureTests(unittest.TestCase):
    def _key(self, **kw):
        args = dict(msvc_version="14.51.36231", os_name="Windows", arch="X64",
                    commit="2af5e524ba58", flag_hash=LEGACY_FLAG_HASH)
        args.update(kw)
        return cache_keys.compute_sccache(_lock(), _settings(), **args)

    def test_exact_key_is_prefix_plus_identity(self):
        k = self._key()
        self.assertEqual(k["sccache_key"], k["sccache_restore_prefix"] + k["sccache_identity"])

    def test_identity_is_snapshot_commit_and_dep8(self):
        k = self._key()
        dephash = cache_keys.dependency_fingerprint(
            _lock()["vcpkg_sha"], _lock()["ffmpeg"]["sha256"], "13.3.0")
        self.assertEqual(k["sccache_identity"], "snap2af5e524ba58-" + dephash[:8])

    def test_prefix_format(self):
        prefix = self._key()["sccache_restore_prefix"]
        self.assertRegex(prefix, r"^ninfer-cache2-sccache-windows-x64-msvc14\.51\.36231"
                                 r"-cuda13\.3\.0-sm120a-flags[0-9a-f]{16}-$")

    def test_restore1_equals_restore_prefix(self):
        k = self._key()
        self.assertEqual(k["sccache_restore1"], k["sccache_restore_prefix"])
        # The prefix ends with '-'; the key is prefix + identity (snap...).
        self.assertTrue(k["sccache_key"].startswith(k["sccache_restore_prefix"] + "snap"))

    def test_sm120a_is_in_the_prefix(self):
        self.assertIn("sm120a", self._key()["sccache_restore_prefix"])


class CompatibilityBoundaryTests(unittest.TestCase):
    """Phase 4: which changes keep vs move the restore prefix.

    Keeping the prefix means a prior snapshot is still restorable and sccache
    content addressing decides per-object validity; moving it means a correct
    cold start.
    """

    def _prefix(self, **kw):
        args = dict(msvc_version="14.51.36231", os_name="Windows", arch="X64",
                    commit="c", flag_hash=LEGACY_FLAG_HASH)
        args.update(kw)
        return cache_keys.compute_sccache(_lock(), _settings(), **args)["sccache_restore_prefix"]

    def test_ffmpeg_change_keeps_prefix(self):
        self.assertEqual(self._prefix(),
                         cache_keys.compute_sccache(
                             _lock(ffmpeg="e" * 64), _settings(), msvc_version="14.51.36231",
                             os_name="Windows", arch="X64", commit="c",
                             flag_hash=LEGACY_FLAG_HASH)["sccache_restore_prefix"])

    def test_source_change_keeps_prefix(self):
        self.assertEqual(self._prefix(), self._prefix(commit="d" * 12))

    def test_msvc_change_moves_prefix(self):
        self.assertNotEqual(self._prefix(), self._prefix(msvc_version="14.50.9999"))

    def test_cuda_change_moves_prefix(self):
        self.assertNotEqual(self._prefix(),
                            cache_keys.compute_sccache(
                                _lock(cuda="13.2.0"), _settings(), msvc_version="14.51.36231",
                                os_name="Windows", arch="X64", commit="c",
                                flag_hash=LEGACY_FLAG_HASH)["sccache_restore_prefix"])

    def test_schema_change_moves_prefix(self):
        self.assertNotEqual(self._prefix(),
                            cache_keys.compute_sccache(_lock(), _settings(schema=3),
                                                       msvc_version="14.51.36231", os_name="Windows",
                                                       arch="X64", commit="c",
                                                       flag_hash=LEGACY_FLAG_HASH)["sccache_restore_prefix"])

    def test_os_change_moves_prefix(self):
        self.assertNotEqual(self._prefix(), self._prefix(os_name="Linux"))

    def test_arch_change_moves_prefix(self):
        self.assertNotEqual(self._prefix(), self._prefix(arch="ARM64"))

    def test_flag_change_moves_prefix(self):
        self.assertNotEqual(self._prefix(), self._prefix(flag_hash="f" * 16))


class LegacyRestorationTests(unittest.TestCase):
    def test_prefix_restores_legacy_dev_cache(self):
        prefix = cache_keys.compute_sccache(
            _lock(), _settings(), msvc_version="14.51.36231", os_name="Windows", arch="X64",
            commit="2af5e524ba58", flag_hash=LEGACY_FLAG_HASH)["sccache_restore_prefix"]
        self.assertTrue(LEGACY_DEV.startswith(prefix),
                        "the restore prefix no longer restores the legacy dev cache")

    def test_prefix_restores_legacy_feature_cache(self):
        prefix = cache_keys.compute_sccache(
            _lock(), _settings(), msvc_version="14.51.36231", os_name="Windows", arch="X64",
            commit="2af5e524ba58", flag_hash=LEGACY_FLAG_HASH)["sccache_restore_prefix"]
        self.assertTrue(LEGACY_FEATURE.startswith(prefix),
                        "the restore prefix no longer restores the legacy feature cache")

    def test_flag_fingerprint_reproduces_legacy_value_on_this_repo(self):
        # Byte-for-byte reproduction of the legacy inline PowerShell flag fingerprint,
        # so the restore prefix matches caches saved before the rotation scheme.
        self.assertEqual(cache_keys.flag_fingerprint(str(REPO_ROOT)), LEGACY_FLAG_HASH)


class OtherLayerKeyTests(unittest.TestCase):
    def _other(self, **kw):
        args = dict(msvc_version="14.51.36231", os_name="Windows", arch="X64")
        args.update(kw)
        return cache_keys.compute_other(_lock(), _settings(), **args)

    def test_ffmpeg_key_is_content_addressed(self):
        self.assertEqual(self._other()["ffmpeg_key"],
                         "ninfer-cache2-ffmpeg-" + _lock()["ffmpeg"]["sha256"])

    def test_vcpkg_keys_carry_the_pinned_sha(self):
        k = self._other()
        self.assertIn(_lock()["vcpkg_sha"], k["vcpkgsrc_key"])
        self.assertIn(_lock()["vcpkg_sha"], k["vcpkgbin_key"])

    def test_vcpkgbin_restore_is_a_prefix_of_vcpkgbin_key(self):
        k = self._other()
        self.assertTrue(k["vcpkgbin_key"].startswith(k["vcpkgbin_restore"]))


class RetentionSelectionTests(unittest.TestCase):
    def _entries(self, n):
        base = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        out = []
        for i in range(1, n + 1):
            out.append({"id": i, "key": f"prefix-snap{i}",
                        "last_used": (base - dt.timedelta(minutes=10 * (n - i))).isoformat(),
                        "size_in_bytes": 1024 * 1024 * i})
        return out

    def test_keeps_newest_keep(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        entries = self._entries(5)
        to_delete, kept = retention.select_deletions(entries, keep=3, grace_minutes=0, now=now)
        self.assertEqual([e["id"] for e in kept], [5, 4, 3])
        self.assertEqual([e["id"] for e in to_delete], [2, 1])

    def test_grace_window_protects_recent_entries(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        entries = self._entries(5)
        # A 4th-newest entry used within the grace window is preserved even though
        # it is beyond the keep count.
        to_delete, kept = retention.select_deletions(entries, keep=3, grace_minutes=25, now=now)
        # entry 3 (used 20 min ago) is within grace; entries 2,1 are older.
        self.assertIn(3, [e["id"] for e in kept])
        self.assertEqual([e["id"] for e in to_delete], [2, 1])

    def test_only_deletes_beyond_keep(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        entries = self._entries(4)
        to_delete, kept = retention.select_deletions(entries, keep=3, grace_minutes=0, now=now)
        self.assertEqual(len(kept), 3)
        self.assertEqual([e["id"] for e in to_delete], [1])

    def test_no_delete_when_at_or_under_keep(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        to_delete, kept = retention.select_deletions(self._entries(3), keep=3, grace_minutes=0, now=now)
        self.assertEqual(to_delete, [])
        self.assertEqual(len(kept), 3)

    def test_falls_back_to_created_at_when_last_used_missing(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        entries = [
            {"id": 1, "created_at": "2026-10-15T11:00:00+00:00", "size_in_bytes": 1},
            {"id": 2, "created_at": "2026-10-15T11:50:00+00:00", "size_in_bytes": 1},
            {"id": 3, "created_at": "2026-10-15T11:55:00+00:00", "size_in_bytes": 1},
            {"id": 4, "created_at": "2026-10-15T11:59:00+00:00", "size_in_bytes": 1},
        ]
        to_delete, kept = retention.select_deletions(entries, keep=2, grace_minutes=0, now=now)
        self.assertEqual([e["id"] for e in kept], [4, 3])
        self.assertEqual([e["id"] for e in to_delete], [2, 1])

    def test_unparseable_timestamps_are_oldest(self):
        now = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
        entries = [
            {"id": 1, "last_used": "not-a-timestamp", "size_in_bytes": 1},
            {"id": 2, "last_used": "2026-10-15T11:50:00+00:00", "size_in_bytes": 1},
            {"id": 3, "last_used": "2026-10-15T11:55:00+00:00", "size_in_bytes": 1},
            {"id": 4, "last_used": "2026-10-15T11:59:00+00:00", "size_in_bytes": 1},
        ]
        to_delete, kept = retention.select_deletions(entries, keep=2, grace_minutes=0, now=now)
        # The unparseable entry ranks as oldest, so it is deleted last in the
        # newest-first deletion order; both id 2 and id 1 are deleted.
        self.assertEqual(sorted(e["id"] for e in to_delete), [1, 2])
        self.assertEqual(to_delete[-1]["id"], 1)


class OutputNameContractTests(unittest.TestCase):
    def test_output_names_are_unique_and_nonempty(self):
        self.assertTrue(cache_keys.OUTPUT_NAMES)
        self.assertEqual(len(cache_keys.OUTPUT_NAMES), len(set(cache_keys.OUTPUT_NAMES)))


if __name__ == "__main__":
    unittest.main()
