import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})


if __name__ == "__main__":
    unittest.main()
