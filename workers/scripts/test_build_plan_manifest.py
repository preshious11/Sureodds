"""Tests build_plan_manifest.py's determinism guarantee: re-running it against
the SAME batch content must produce the SAME plan_id, since that identity is
what makes the SSH relay's apply-plan.ts idempotency (one transaction per plan
id, no-op on a repeat) actually work across a retried delivery.

Run with: python -m unittest workers/scripts/test_build_plan_manifest.py
(from the repo root) or `python workers/scripts/test_build_plan_manifest.py`.
"""
import json
import os
import shutil
import tempfile
import unittest

import build_plan_manifest as bpm


class BuildPlanManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sureodds-bpm-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> None:
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_same_content_same_plan_id(self):
        self._write("0001.sql", "INSERT INTO t VALUES (1);")
        self._write("0002.sql", "INSERT INTO t VALUES (2);")
        self.assertEqual(bpm.main([bpm.__name__, self.tmp]), 0)
        with open(os.path.join(self.tmp, "plan.json"), encoding="utf-8") as fh:
            first = json.load(fh)

        # Re-run against the identical files (simulates a retried workflow
        # step re-building the manifest for the same already-produced batch).
        self.assertEqual(bpm.main([bpm.__name__, self.tmp]), 0)
        with open(os.path.join(self.tmp, "plan.json"), encoding="utf-8") as fh:
            second = json.load(fh)

        self.assertEqual(first["plan_id"], second["plan_id"])

    def test_different_content_different_plan_id(self):
        self._write("0001.sql", "INSERT INTO t VALUES (1);")
        bpm.main([bpm.__name__, self.tmp])
        with open(os.path.join(self.tmp, "plan.json"), encoding="utf-8") as fh:
            first_id = json.load(fh)["plan_id"]

        self._write("0001.sql", "INSERT INTO t VALUES (999);")
        bpm.main([bpm.__name__, self.tmp])
        with open(os.path.join(self.tmp, "plan.json"), encoding="utf-8") as fh:
            second_id = json.load(fh)["plan_id"]

        self.assertNotEqual(first_id, second_id)

    def test_checksums_match_actual_file_contents(self):
        self._write("0001.sql", "SELECT 1;")
        bpm.main([bpm.__name__, self.tmp])
        with open(os.path.join(self.tmp, "plan.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["checksums"]["0001.sql"], bpm.sha256_of_file(os.path.join(self.tmp, "0001.sql")))

    def test_fails_loudly_on_empty_batch_directory(self):
        self.assertEqual(bpm.main([bpm.__name__, self.tmp]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "plan.json")))


if __name__ == "__main__":
    unittest.main()
