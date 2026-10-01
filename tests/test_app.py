import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import CHANNELS, Database, DomainError, Handler, seed_demo


class SubtitleQCTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def add_cue(self, index, start, end, text, sdh=False, revision=None):
        payload = {"cue_index": index, "start_ms": start, "end_ms": end, "text": text}
        if sdh:
            payload["sdh"] = True
        if revision is not None:
            payload["expected_revision"] = revision
        return self.db.save_cue(self.version, "bob", payload)

    def approve_and_lock(self):
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        return self.db.lock(self.version, "alice")

    def channel(self, status, name):
        return next(c for c in status["channels"] if c["channel"] == name)

    def receipt_files(self, name, *, drop=(), tamper=None):
        status = self.db.delivery_status(self.version)
        files = []
        for f in self.channel(status, name)["files"]:
            if f["path"] in drop:
                continue
            digest = "0" * 64 if tamper == f["path"] else f["sha256"]
            files.append({"path": f["path"], "sha256": digest})
        return files

    def reconcile(self, name, receipt_id, **kwargs):
        files = self.receipt_files(name, **kwargs)
        return self.db.add_receipt(
            self.version, "alice",
            {"channel": name, "receipt_id": receipt_id, "files": files}, "owner")

    def reconcile_all(self, suffix=""):
        for name in CHANNELS:
            status = self.db.delivery_status(self.version)
            ch = self.channel(status, name)
            if ch["status"] == "packaged" and not ch["reconciled"]:
                self.reconcile(name, f"r-{name}{suffix}")


class ReviewFlowTest(SubtitleQCTestCase):
    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.add_cue(1, 1000, 3000, "seal 海豹在冰面", revision=0)
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        status = self.approve_and_lock()
        # Locking creates one independently packaged task per channel.
        self.assertEqual([c["channel"] for c in status["channels"]], list(CHANNELS))
        self.assertTrue(all(c["status"] == "packaged" for c in status["channels"]))
        self.assertTrue(all(c["package_hash"] for c in status["channels"]))
        # Delivery must not be marked until every channel receipt reconciles.
        with self.assertRaises(DomainError):
            self.db.deliver(self.version, "alice")
        self.reconcile_all()
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.add_cue(1, 1000, 3000, "海豹", revision=0)
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.add_cue(2, 2500, 4000, "另一句", revision=1)
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.add_cue(2, 3500, 4000, "密封装置", revision=1)
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})


class ChannelRulesTest(SubtitleQCTestCase):
    def test_channels_follow_their_own_packaging_rules(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        self.add_cue(2, 4000, 6000, "（音乐）海浪声", sdh=True, revision=1)
        status = self.approve_and_lock()
        cinema, streaming, tv = (self.channel(status, n) for n in CHANNELS)
        # Cinema DCP excludes SDH cues; streaming/TV carry them.
        self.assertEqual(cinema["cue_count"], 1)
        self.assertEqual(streaming["cue_count"], 2)
        self.assertEqual(tv["cue_count"], 2)
        paths = {f["path"] for f in cinema["files"]}
        self.assertEqual(paths, {"subtitle.xml", "glossary.json"})
        self.assertIn("subtitle.vtt", {f["path"] for f in streaming["files"]})
        self.assertIn("subtitle.stl", {f["path"] for f in tv["files"]})
        # Each channel derives its own content hash.
        hashes = {c["package_hash"] for c in status["channels"]}
        self.assertEqual(len(hashes), 3)

    def test_sdh_cue_change_only_invalidates_streaming_and_tv(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        sdh = self.add_cue(2, 4000, 6000, "（音乐）海浪声", sdh=True, revision=1)
        status = self.approve_and_lock()
        cinema_hash = self.channel(status, "cinema")["package_hash"]
        self.db.unlock(self.version, "alice")
        self.db.save_cue(self.version, "bob", {"cue_id": sdh["id"], "cue_index": 2,
                                               "start_ms": 4000, "end_ms": 6000,
                                               "text": "（音乐）风声", "sdh": True,
                                               "expected_revision": 2})
        status = self.db.delivery_status(self.version)
        self.assertEqual(self.channel(status, "cinema")["status"], "packaged")
        self.assertEqual(self.channel(status, "streaming")["status"], "stale")
        self.assertEqual(self.channel(status, "tv")["status"], "stale")
        # Re-approve and re-lock: stale channels rebuild, cinema is left as-is.
        status = self.approve_and_lock()
        cinema, streaming, tv = (self.channel(status, n) for n in CHANNELS)
        self.assertEqual(cinema["package_hash"], cinema_hash)
        # Cinema was never rebuilt: exactly one package.built audit entry.
        cinema_builds = [e for e in self.db.audit()
                         if e["action"] == "package.built"
                         and json.loads(e["details"]).get("channel") == "cinema"]
        self.assertEqual(len(cinema_builds), 1)
        self.assertGreaterEqual(streaming["revision"], 2)
        self.assertEqual(streaming["status"], "packaged")
        # A normal cue change does reach the cinema package.
        self.db.unlock(self.version, "alice")
        self.db.save_cue(self.version, "bob", {"cue_id": 1, "cue_index": 1,
                                               "start_ms": 1000, "end_ms": 3000,
                                               "text": "海豹趴在冰面", "expected_revision": 3})
        status = self.db.delivery_status(self.version)
        self.assertTrue(all(self.channel(status, n)["status"] == "stale" for n in CHANNELS))

    def test_glossary_scope_only_invalidates_affected_channels(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        status = self.approve_and_lock()
        before = {n: self.channel(status, n)["package_hash"] for n in CHANNELS}
        # A new cinema-only term changes only the cinema render.
        self.db.set_glossary(self.project, "alice", {
            "source_term": "iceberg", "required_translation": "冰山",
            "forbidden_terms": [], "channels": ["cinema"], "notes": ""}, "owner")
        status = self.db.delivery_status(self.version)
        self.assertEqual(self.channel(status, "cinema")["status"], "stale")
        self.assertEqual(self.channel(status, "streaming")["status"], "packaged")
        self.assertEqual(self.channel(status, "tv")["status"], "packaged")
        status = self.db.build_packages(self.version, "alice", {"channels": ["cinema"]}, "owner")
        after = {n: self.channel(status, n)["package_hash"] for n in CHANNELS}
        self.assertNotEqual(before["cinema"], after["cinema"])
        self.assertEqual(before["streaming"], after["streaming"])
        self.assertEqual(before["tv"], after["tv"])
        # Moving the term cinema -> tv invalidates both old and new carrier.
        self.db.set_glossary(self.project, "alice", {
            "source_term": "iceberg", "required_translation": "冰山",
            "forbidden_terms": [], "channels": ["tv"], "notes": ""}, "owner")
        status = self.db.delivery_status(self.version)
        self.assertEqual(self.channel(status, "cinema")["status"], "stale")
        self.assertEqual(self.channel(status, "streaming")["status"], "packaged")
        self.assertEqual(self.channel(status, "tv")["status"], "stale")


class PackagingRetryTest(SubtitleQCTestCase):
    def test_failed_channel_is_retried_without_rebuilding_finished_ones(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        self.db.build_failures.add("cinema")
        status = self.approve_and_lock()
        # Lock still succeeds: the failed channel is recorded, siblings finish.
        self.assertEqual(self.channel(status, "cinema")["status"], "failed")
        self.assertTrue(self.channel(status, "cinema")["error"])
        self.assertEqual(self.channel(status, "streaming")["status"], "packaged")
        self.assertEqual(self.channel(status, "tv")["status"], "packaged")
        self.assertFalse(status["all_packaged"])
        ids_before = {n: self.channel(status, n).get("revision") for n in CHANNELS}
        with self.assertRaisesRegex(DomainError, "暂不能对账"):
            self.reconcile("cinema", "r-cinema")
        with self.assertRaises(DomainError):
            self.db.deliver(self.version, "alice")
        # Retrying while the pipeline is still broken keeps the failed state.
        status = self.db.build_packages(self.version, "alice", {"channels": ["cinema"]}, "owner")
        self.assertEqual(self.channel(status, "cinema")["status"], "failed")
        self.db.build_failures.clear()
        status = self.db.build_packages(self.version, "alice", {"channels": ["cinema"]}, "owner")
        self.assertEqual(self.channel(status, "cinema")["status"], "packaged")
        # Repeated requests never produce a second package: one row per channel
        # and packaged rows keep their hashes across retries.
        rows = self.db.delivery_status(self.version)["channels"]
        self.assertEqual(len(rows), 3)
        status = self.db.build_packages(self.version, "alice", {}, "owner")
        hashes = {n: self.channel(status, n)["package_hash"] for n in CHANNELS}
        status = self.db.build_packages(self.version, "alice", {"channels": list(CHANNELS)}, "owner")
        self.assertEqual({n: self.channel(status, n)["package_hash"] for n in CHANNELS}, hashes)
        self.assertEqual(len(self.db.delivery_status(self.version)["channels"]), 3)
        self.assertEqual(ids_before["streaming"], self.channel(status, "streaming")["revision"])


class ReceiptReconciliationTest(SubtitleQCTestCase):
    def test_missing_and_mismatched_receipts_block_delivery(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        self.approve_and_lock()
        missing = self.reconcile("cinema", "r-cinema", drop=("glossary.json",))
        self.assertEqual(missing["status"], "mismatched")
        self.assertEqual(missing["missing"], ["glossary.json"])
        bad = self.reconcile("streaming", "r-streaming", tamper="subtitle.vtt")
        self.assertEqual(bad["status"], "mismatched")
        self.assertEqual(bad["mismatches"], ["subtitle.vtt"])
        with self.assertRaises(DomainError) as ctx:
            self.db.deliver(self.version, "alice")
        blockers = json.loads(str(ctx.exception))["blockers"]
        blocked_channels = {b["channel"] for b in blockers}
        self.assertEqual(blocked_channels, {"cinema", "streaming", "tv"})
        # A corrected receipt later reconciles the channel.
        fixed_cinema = self.reconcile("cinema", "r-cinema-2")
        fixed_stream = self.reconcile("streaming", "r-streaming-2")
        self.assertEqual(fixed_cinema["status"], "matched")
        self.assertEqual(fixed_stream["status"], "matched")
        self.reconcile("tv", "r-tv")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        self.assertEqual(self.db.delivery_status(self.version)["status"], "delivered")
        with self.assertRaisesRegex(DomainError, "已经交付"):
            self.db.deliver(self.version, "alice")

    def test_duplicate_receipt_is_recorded_once(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        self.approve_and_lock()
        first = self.reconcile("streaming", "RCP-1001")
        again = self.reconcile("streaming", "RCP-1001")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.channel(self.db.delivery_status(self.version), "streaming")["receipt_count"], 1)

    def test_stale_rebuild_retires_old_receipt(self):
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        self.approve_and_lock()
        self.reconcile("cinema", "r-cinema-1")
        self.db.set_glossary(self.project, "alice", {
            "source_term": "iceberg", "required_translation": "冰山",
            "forbidden_terms": [], "channels": ["cinema"]}, "owner")
        status = self.db.delivery_status(self.version)
        ch = self.channel(status, "cinema")
        self.assertFalse(ch["reconciled"])
        self.assertEqual(ch["latest_receipt"]["active"], False)
        self.db.build_packages(self.version, "alice", {"channels": ["cinema"]}, "owner")
        # The old receipt number must not silently reconcile the new bytes.
        old = self.reconcile("cinema", "r-cinema-1")
        # Same receipt_id against the same package still deduplicates to itself,
        # but it was retired; a fresh receipt reconciles the rebuild.
        self.assertFalse(old["active"])
        fresh = self.reconcile("cinema", "r-cinema-2")
        self.assertEqual(fresh["status"], "matched")
        self.assertTrue(fresh["active"])


class LegacyBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"
        cues = [{"cue_index": 1, "start_ms": 0, "end_ms": 2000, "text": "旧版字幕"}]
        glossary = [{"source_term": "seal", "required_translation": "海豹",
                     "forbidden_terms": ["密封"]}]
        old_manifest = {"project_id": 1, "version_id": 1, "language": "zh-CN",
                        "version_no": 1, "cues": cues, "glossary": glossary}
        snapshot = __import__("hashlib").sha256(
            json.dumps(old_manifest, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        first = Database(self.db_path)  # creates current schema
        with first.connect() as conn:
            conn.execute(
                "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                ("老项目", "en", 120000, "alice", "old.mp4", "a" * 64, "2026-01-01T00:00:00+00:00"))
            conn.execute(
                "INSERT INTO versions(project_id,language,version_no,status,revision,created_by,created_at,updated_at) VALUES(?,?,?,'delivered',0,'alice',?,?)",
                (1, "zh-CN", 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
            conn.execute(
                "INSERT INTO deliveries(version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?)",
                (1, snapshot, json.dumps(old_manifest, ensure_ascii=False, sort_keys=True),
                 "alice", "2026-01-02T00:00:00+00:00"))
        self.snapshot = snapshot

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_manifest_rebuilds_channel_records_once(self):
        db = Database(self.db_path)
        status = db.delivery_status(1)
        for name in CHANNELS:
            ch = next(c for c in status["channels"] if c["channel"] == name)
            self.assertEqual(ch["status"], "packaged")
            self.assertTrue(ch["legacy"])
            self.assertTrue(ch["reconciled"])
            self.assertTrue(ch["latest_receipt"]["legacy"])
            self.assertTrue(ch["latest_receipt"]["active"])
            self.assertEqual(ch["latest_receipt"]["status"], "matched")
        self.assertTrue(status["delivered"])
        # Backfill is idempotent across restarts.
        Database(self.db_path)
        rows = Database(self.db_path).delivery_status(1)["channels"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(c["receipt_count"] == 1 for c in rows))
        delivery = db.list_deliveries()[0]
        self.assertEqual(delivery["snapshot_hash"], self.snapshot)


class HttpApiTest(SubtitleQCTestCase):
    def setUp(self):
        super().setUp()
        self.add_cue(1, 1000, 3000, "海豹在冰面", revision=0)
        Handler.db = self.db
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method, path, body=None, user="alice", role="owner"):
        data = json.dumps(body).encode() if body is not None else b"{}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data if method == "POST" else None,
            method=method, headers={"Content-Type": "application/json", "X-User": user, "X-Role": role})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_lock_packages_and_receipt_dedup_over_http(self):
        status, _ = self.request("POST", f"/api/versions/{self.version}/submit", {}, user="bob", role="translator")
        self.assertEqual(status, 200)
        status, _ = self.request("POST", f"/api/versions/{self.version}/review",
                                 {"decision": "approve", "comment": "ok"}, user="carol", role="reviewer")
        self.assertEqual(status, 200)
        status, body = self.request("POST", f"/api/versions/{self.version}/lock")
        self.assertEqual(status, 200)
        self.assertTrue(all(c["status"] == "packaged" for c in body["channels"]))
        status, body = self.request("GET", f"/api/versions/{self.version}/packages")
        self.assertEqual(status, 200)
        streaming = next(c for c in body["channels"] if c["channel"] == "streaming")
        receipt = {"channel": "streaming", "receipt_id": "HTTP-1",
                   "files": [{"path": f["path"], "sha256": f["sha256"]} for f in streaming["files"]]}
        status, first = self.request("POST", f"/api/versions/{self.version}/receipts", receipt)
        self.assertEqual(status, 201)
        self.assertFalse(first["deduplicated"])
        status, second = self.request("POST", f"/api/versions/{self.version}/receipts", receipt)
        self.assertEqual(status, 201)
        self.assertTrue(second["deduplicated"])
        status, body = self.request("GET", f"/api/versions/{self.version}/packages")
        streaming = next(c for c in body["channels"] if c["channel"] == "streaming")
        self.assertEqual(streaming["receipt_count"], 1)


if __name__ == "__main__":
    unittest.main()
