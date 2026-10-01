import json
import tempfile
import unittest
from pathlib import Path

from app import CHANNELS, Database, DomainError, seed_demo


class ChannelDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.db.save_cue(self.version, "bob",
                         {"cue_index": 1, "start_ms": 1000, "end_ms": 3000,
                          "text": "seal 海豹在冰面", "expected_revision": 0})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _lock(self):
        delivery = self.db.lock(self.version, "alice", "owner")
        return delivery

    def _files(self, delivery, channel):
        package = next(c for c in delivery["channels"] if c["channel"] == channel)
        return package["manifest_data"]["files"]

    def _receipt_files(self, delivery, channel):
        return [{"path": f["path"], "sha256": f["sha256"]} for f in self._files(delivery, channel)]

    def test_lock_generates_channel_packages_and_is_idempotent(self):
        delivery = self._lock()
        self.assertEqual({c["channel"] for c in delivery["channels"]}, set(CHANNELS))
        for channel in CHANNELS:
            package = next(c for c in delivery["channels"] if c["channel"] == channel)
            self.assertEqual(package["status"], "packed")
            self.assertEqual(len(package["package_hash"]), 64)
        cinema = {f["path"] for f in self._files(delivery, "cinema")}
        streaming = {f["path"] for f in self._files(delivery, "streaming")}
        tv = {f["path"] for f in self._files(delivery, "tv")}
        self.assertEqual(cinema, {"subtitles.xml"})
        self.assertEqual(streaming, {"subtitles.vtt", "glossary.json"})
        self.assertEqual(tv, {"subtitles.srt", "glossary.json"})
        self.db.unlock(self.version, "alice", "owner")
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "再批"}, "reviewer")
        # 重复锁定/重复请求不生成第二份包。
        relocked = self.db.lock(self.version, "alice", "owner")
        self.db.lock(self.version, "alice", "owner")
        ids = {channel: None for channel in CHANNELS}
        for channel in CHANNELS:
            ids[channel] = next(c["id"] for c in relocked["channels"] if c["channel"] == channel)
        again = self.db.get_delivery(relocked["id"])
        for channel in CHANNELS:
            self.assertEqual(next(c["id"] for c in again["channels"] if c["channel"] == channel), ids[channel])
            self.assertEqual(next(c["status"] for c in again["channels"] if c["channel"] == channel), "packed")

    def test_packaging_failure_keeps_done_channels_and_retries_remaining(self):
        calls = {"n": 0}

        def broken_streaming(cues, glossary):
            calls["n"] += 1
            raise RuntimeError("streaming pipeline down")

        self.db.packers["streaming"] = broken_streaming
        with self.assertRaisesRegex(DomainError, "渠道打包未完成"):
            self.db.lock(self.version, "alice", "owner")
        partial = self.db.get_delivery_for_version(self.version)
        statuses = {c["channel"]: c["status"] for c in partial["channels"]}
        # cinema 排在最前已完成保留；streaming 失败；tv 尚未开始。
        self.assertEqual(statuses["cinema"], "packed")
        self.assertEqual(statuses["streaming"], "failed")
        self.assertEqual(statuses["tv"], "pending")
        cinema_hash = next(c for c in partial["channels"] if c["channel"] == "cinema")["package_hash"]
        streaming_attempts = next(c for c in partial["channels"] if c["channel"] == "streaming")["attempts"]
        self.assertEqual(streaming_attempts, 1)

        self.db.packers["streaming"] = self.db.packers["tv"]
        resumed = self.db.deliver(self.version, "alice", "owner")
        statuses = {c["channel"]: c["status"] for c in resumed["channels"]}
        self.assertEqual(statuses, {"cinema": "packed", "streaming": "packed", "tv": "packed"})
        # 已完成的 cinema 没有被重打。
        self.assertEqual(next(c for c in resumed["channels"] if c["channel"] == "cinema")["package_hash"], cinema_hash)
        self.assertEqual(calls["n"], 1)

    def test_glossary_change_invalidates_only_glossary_channels(self):
        delivery = self._lock()
        hashes = {c["channel"]: c["package_hash"] for c in delivery["channels"]}
        self.db.unlock(self.version, "alice", "owner")
        self.db.set_glossary(self.project, "alice",
                             {"source_term": "seal", "required_translation": "海豹",
                              "forbidden_terms": ["密封", "封条"], "notes": "动物学语境"}, "owner")
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        relocked = self.db.lock(self.version, "alice", "owner")
        new_hashes = {c["channel"]: c["package_hash"] for c in relocked["channels"]}
        self.assertEqual(new_hashes["cinema"], hashes["cinema"], "影院包不含术语表，不应失效")
        self.assertNotEqual(new_hashes["streaming"], hashes["streaming"])
        self.assertNotEqual(new_hashes["tv"], hashes["tv"])

    def test_cue_change_invalidates_all_channels(self):
        delivery = self._lock()
        hashes = {c["channel"]: c["package_hash"] for c in delivery["channels"]}
        self.db.unlock(self.version, "alice", "owner")
        self.db.save_cue(self.version, "bob",
                         {"cue_id": self.db.list_cues(self.version)[0]["id"],
                          "cue_index": 1, "start_ms": 1000, "end_ms": 3200,
                          "text": "seal 海豹在冰面滑行", "expected_revision": 1})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        relocked = self.db.lock(self.version, "alice", "owner")
        for channel in CHANNELS:
            self.assertNotEqual(next(c for c in relocked["channels"] if c["channel"] == channel)["package_hash"],
                                hashes[channel])

    def test_receipts_reconcile_and_version_completes_only_when_all_match(self):
        delivery = self._lock()
        delivery_id = delivery["id"]
        for i, channel in enumerate(CHANNELS, 1):
            result = self.db.record_receipt(delivery_id, channel, f"{channel}-bot",
                                            {"receipt_no": f"R-{channel}-1",
                                             "files": self._receipt_files(delivery, channel)})
            self.assertTrue(result["reconciled"])
            expect_delivered = channel == "tv"
            self.assertEqual(result["version_status"], "delivered" if expect_delivered else "locked")
        final = self.db.get_delivery(delivery_id)
        self.assertEqual(final["version_status"], "delivered")
        self.assertIsNotNone(final["completed_at"])
        self.assertTrue(all(c["status"] == "confirmed" for c in final["channels"]))

    def test_duplicate_receipt_recorded_once(self):
        delivery = self._lock()
        payload = {"receipt_no": "R-streaming-1", "files": self._receipt_files(delivery, "streaming")}
        first = self.db.record_receipt(delivery["id"], "streaming", "streaming-bot", payload)
        second = self.db.record_receipt(delivery["id"], "streaming", "streaming-bot", payload)
        self.assertTrue(second["duplicated"])
        self.assertEqual(second["id"], first["id"])
        package = next(c for c in self.db.get_delivery(delivery["id"])["channels"] if c["channel"] == "streaming")
        self.assertEqual(len(package["receipts"]), 1)

    def test_missing_file_or_bad_checksum_blocks_delivery(self):
        delivery = self._lock()
        files = self._receipt_files(delivery, "cinema")
        missing = self.db.record_receipt(delivery["id"], "cinema", "cinema-bot",
                                         {"receipt_no": "R-cinema-bad", "files": []})
        self.assertFalse(missing["reconciled"])
        self.assertEqual(missing["missing_files"], ["subtitles.xml"])
        package = next(c for c in self.db.get_delivery(delivery["id"])["channels"] if c["channel"] == "cinema")
        self.assertEqual(package["status"], "packed", "对账不符不能确认该渠道")

        tampered = [{"path": files[0]["path"], "sha256": "a" * 64}]
        mismatch = self.db.record_receipt(delivery["id"], "cinema", "cinema-bot",
                                          {"receipt_no": "R-cinema-tamper", "files": tampered})
        self.assertFalse(mismatch["reconciled"])
        self.assertEqual(mismatch["mismatched_files"][0]["path"], "subtitles.xml")

        for channel in ("streaming", "tv"):
            self.db.record_receipt(delivery["id"], channel, f"{channel}-bot",
                                   {"receipt_no": f"R-{channel}-ok",
                                    "files": self._receipt_files(delivery, channel)})
        # 影院未对账成功，版本不能标记为已交付。
        self.assertEqual(self.db.get_delivery(delivery["id"])["version_status"], "locked")

        corrected = self.db.record_receipt(delivery["id"], "cinema", "cinema-bot",
                                           {"receipt_no": "R-cinema-fixed", "files": files})
        self.assertTrue(corrected["reconciled"])
        self.assertEqual(corrected["version_status"], "delivered")

    def test_receipt_before_packaging_rejected(self):
        delivery = self._lock()
        # 解锁一个未确认渠道让其失效后，未重打前不接收回执。
        self.db.unlock(self.version, "alice", "owner")
        self.db.set_glossary(self.project, "alice",
                             {"source_term": "seal", "required_translation": "海豹",
                              "forbidden_terms": ["密封", "封条"], "notes": "动物学语境"}, "owner")
        with self.assertRaisesRegex(DomainError, "不在交付中"):
            self.db.record_receipt(delivery["id"], "streaming", "bot",
                                   {"receipt_no": "R-x",
                                    "files": [{"path": "subtitles.vtt", "sha256": "a" * 64}]})

    def test_confirmed_channel_downgrades_on_later_bad_receipt(self):
        delivery = self._lock()
        good = self._receipt_files(delivery, "cinema")
        self.db.record_receipt(delivery["id"], "cinema", "cinema-bot",
                               {"receipt_no": "RC-1", "files": good})
        package = next(c for c in self.db.get_delivery(delivery["id"])["channels"] if c["channel"] == "cinema")
        self.assertEqual(package["status"], "confirmed")
        # 新回执号但校验值不符：不能继续保持确认状态。
        bad = self.db.record_receipt(delivery["id"], "cinema", "cinema-bot",
                                     {"receipt_no": "RC-2",
                                      "files": [{"path": good[0]["path"], "sha256": "a" * 64}]})
        self.assertFalse(bad["reconciled"])
        package = next(c for c in self.db.get_delivery(delivery["id"])["channels"] if c["channel"] == "cinema")
        self.assertEqual(package["status"], "packed")
        self.assertEqual(len(package["receipts"]), 2, "新回执号应记录，但不覆盖旧回执")

    def test_permissions_for_lock_unlock_and_retry(self):
        with self.assertRaisesRegex(DomainError, "锁定"):
            self.db.lock(self.version, "bob", "translator")
        delivery = self._lock()
        with self.assertRaisesRegex(DomainError, "解锁"):
            self.db.unlock(self.version, "bob", "translator")
        with self.assertRaisesRegex(DomainError, "重试交付"):
            self.db.deliver(self.version, "bob", "translator")
        # 管理员可以代行。
        self.db.unlock(self.version, "root", "admin")

    def test_legacy_delivery_backfilled_from_existing_manifest(self):
        import sqlite3

        db_path = Path(self.tmp.name) / "legacy.db"
        # 旧表结构（无渠道包表）+ 一行旧交付，并把版本置为 delivered。
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE projects (id INTEGER PRIMARY KEY,name TEXT,source_language TEXT,duration_ms INTEGER,
                    owner TEXT,media_name TEXT,media_sha256 TEXT,created_at TEXT);
                CREATE TABLE versions (id INTEGER PRIMARY KEY,project_id INTEGER,language TEXT,version_no INTEGER,
                    parent_id INTEGER,status TEXT,revision INTEGER,created_by TEXT,created_at TEXT,updated_at TEXT);
                CREATE TABLE deliveries (id INTEGER PRIMARY KEY,version_id INTEGER UNIQUE,supersedes_version_id INTEGER,
                    snapshot_hash TEXT,manifest TEXT,delivered_by TEXT,created_at TEXT);
                """
            )
            conn.execute("INSERT INTO projects VALUES(1,'旧片','en',1000,'alice',?,?,?)",
                         ("m.mp4", "c" * 64, "2026-09-01T00:00:00+00:00"))
            manifest = {"project_id": 1, "version_id": 1, "language": "zh-CN", "version_no": 1,
                        "cues": [{"cue_index": 1, "start_ms": 0, "end_ms": 2000, "text": "海豹"}],
                        "glossary": [{"source_term": "seal", "required_translation": "海豹",
                                      "forbidden_terms": '["密封"]'}]}
            conn.execute("INSERT INTO versions VALUES(1,1,'zh-CN',1,NULL,'delivered',0,'alice',?,?)",
                         ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"))
            conn.execute("INSERT INTO deliveries VALUES(1,1,NULL,?,?, 'alice',?)",
                         ("deadbeef", json.dumps(manifest, ensure_ascii=False), "2026-09-01T00:00:00+00:00"))

        legacy_db = Database(db_path)
        # 启动初始化时已自动补建，显式再调用应为 0（幂等）。
        self.assertEqual(legacy_db.backfill_legacy_deliveries(), 0)
        delivery = legacy_db.get_delivery(1)
        self.assertEqual(len(delivery["channels"]), 3)
        for channel in CHANNELS:
            package = next(c for c in delivery["channels"] if c["channel"] == channel)
            self.assertEqual(package["status"], "confirmed")
            self.assertTrue(package["manifest_data"]["files"])
            self.assertEqual(package["receipts"][0]["source"], "legacy")
            self.assertEqual(package["receipts"][0]["status"], "matched")
        # 补建幂等：重新打开数据库不会重复补。
        Database(db_path)
        delivery = legacy_db.get_delivery(1)
        self.assertEqual(len(delivery["channels"]), 3)
        for channel in CHANNELS:
            package = next(c for c in delivery["channels"] if c["channel"] == channel)
            self.assertEqual(len(package["receipts"]), 1)

    def test_deliver_listing_shows_every_channel_status(self):
        delivery = self._lock()
        self.db.record_receipt(delivery["id"], "cinema", "cinema-bot",
                               {"receipt_no": "R1", "files": self._receipt_files(delivery, "cinema")})
        listing = self.db.list_deliveries()
        self.assertEqual(len(listing), 1)
        summary = {c["channel"]: c["status"] for c in listing[0]["channels"]}
        self.assertEqual(summary, {"cinema": "confirmed", "streaming": "packed", "tv": "packed"})


if __name__ == "__main__":
    unittest.main()
