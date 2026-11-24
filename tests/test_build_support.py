from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from build_support import cleanup_build_artifacts, create_build_version


class BuildSupportTests(unittest.TestCase):
    def test_create_build_version_updates_version_and_avoids_same_timestamp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "version.txt").write_text("1.1\n", encoding="utf-8")
            now = datetime(2026, 11, 24, 10, 44, 10)

            first = create_build_version(base_dir, now)
            second = create_build_version(base_dir, now)

            self.assertEqual(first, "2026.11.24_104410")
            self.assertEqual(second, "2026.11.24_104410_01")
            self.assertEqual((base_dir / "version.txt").read_text().strip(), second)

    def test_cleanup_removes_only_generated_intermediates_and_stale_specs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            work_dir = base_dir / "Click2Connect_v2026.11.24_104410_work"
            unrelated_dir = base_dir / "cache"
            dist_dir = base_dir / "dist_portable"
            current_spec = base_dir / "Click2Connect_v2026.11.24_104410_portable.spec"
            stale_spec = base_dir / "Click2Connect_v2026.11.23_104410_portable.spec"

            work_dir.mkdir()
            unrelated_dir.mkdir()
            dist_dir.mkdir()
            current_spec.touch()
            stale_spec.touch()
            exe = dist_dir / "Click2Connect_v2.exe"
            archive = dist_dir / "Click2Connect_v1.zip"
            exe.touch()
            archive.touch()

            cleanup_build_artifacts(base_dir, current_spec, work_dir)

            self.assertFalse(work_dir.exists())
            self.assertTrue(unrelated_dir.exists())
            self.assertTrue(current_spec.exists())
            self.assertFalse(stale_spec.exists())
            self.assertTrue(exe.exists())
            self.assertTrue(archive.exists())

    def test_cleanup_skips_reparse_point_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            work_dir = base_dir / "build_portable" / "Click2Connect_v1_work"
            work_dir.mkdir(parents=True)

            with patch("build_support._is_reparse_point", return_value=True):
                errors = cleanup_build_artifacts(base_dir, current_work_dir=work_dir)

            self.assertTrue(work_dir.exists())
            self.assertEqual(len(errors), 1)
            self.assertIn("skipped reparse-point", errors[0])


if __name__ == "__main__":
    unittest.main()
