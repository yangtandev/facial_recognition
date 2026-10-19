"""Offline regression suite: no systemd changes, cloud writes, email or models."""
import ast
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import types
import time
import unittest
import sys
import subprocess
import smtplib
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid
import os

from ota.core import (Store, SafeRedirect, atomic_json, file_lock, merge_defaults,
                      migrate_config, read_json, safe_extract, save_config, select_release, version_key)
from ota.health import Health
from ota.notifications import mail_once, send_mail, validate_smtp
from ota.package_release import package, REQUIRED
from ota.updater import Agent


def release(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft}


class VersionTests(unittest.TestCase):
    def test_numeric_rc_order(self):
        self.assertGreater(version_key("v1.0.1-rc.10"), version_key("v1.0.1-rc.2"))

    def test_stable_promotion(self):
        self.assertEqual("v1.0.1", select_release([release("v1.0.1")], "test", "v1.0.1-rc.2")["tag_name"])

    def test_production_excludes_rc_and_draft(self):
        releases = [release("v1.0.2-rc.1", True), release("v1.0.2", draft=True), release("v1.0.1")]
        self.assertEqual("v1.0.1", select_release(releases, "production", "v1.0.0")["tag_name"])

    def test_no_test_downgrade(self):
        self.assertIsNone(select_release([release("v1.0.1")], "test", "v1.0.2-rc.1"))

    def test_inconsistent_flags(self):
        self.assertIsNone(select_release([release("v1.0.1-rc.1"), release("v1.0.1", True)], "test", "v1.0.0"))

    def test_failed_release_not_retried(self):
        self.assertIsNone(select_release([release("v1.0.1")], "production", "v1.0.0", {"v1.0.1"}))

    def test_development_does_not_follow_ota(self):
        self.assertIsNone(select_release([release("v9.0.0")], "development", "v1.0.0"))

    def test_unknown_role_and_bad_tags(self):
        with self.assertRaises(ValueError):
            select_release([], "unknown", "v1.0.0")
        for tag in ("v1.01.1", "v1.0.1_test", "../1.0.1", "v1.0.1-rc.0"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                version_key(tag)


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_keep_host_and_unknown_fields(self):
        current = {"Server": {"location_ID": 7, "secret": "local"}, "MQTT": {"broker_ip": "private"}}
        merged = merge_defaults({"Server": {"location_ID": 1, "new": True}}, current)
        self.assertEqual(7, merged["Server"]["location_ID"])
        self.assertEqual(current["MQTT"], merged["MQTT"])
        self.assertNotIn("new", current["Server"])

    def test_bad_config_type_rejected(self):
        with self.assertRaises(ValueError):
            merge_defaults({"Server": {}}, {"Server": "broken"})
        with self.assertRaises(ValueError):
            merge_defaults({"enabled": True}, {"enabled": 1})

    def test_explicit_rename_preserves_value_and_is_idempotent(self):
        rules = [{"op": "rename", "from": ["MQTT", "old_host"], "to": ["MQTT", "broker_ip"]}]
        result = migrate_config({"MQTT": {"broker_ip": "default"}}, {"MQTT": {"old_host": "private", "secret": "local"}}, rules)
        self.assertEqual({"MQTT": {"broker_ip": "private", "secret": "local"}}, result)
        self.assertEqual(result, migrate_config({}, result, rules))

    def test_conflicting_rename_and_unknown_operations_rejected(self):
        rules = [{"op": "rename", "from": ["old"], "to": ["new"]}]
        with self.assertRaises(ValueError):
            migrate_config({}, {"old": "a", "new": "b"}, rules)
        for rule in ({"op": "delete", "from": ["old"], "to": ["new"]}, {"op": "rename", "from": ["x"], "to": ["x", "child"]}):
            with self.assertRaises(ValueError):
                migrate_config({}, {}, [rule])

    def test_config_save_preserves_symlink_and_omitted_keys(self):
        target, link = self.root / "config.json", self.root / "link.json"
        atomic_json(target, {"theme": "dark", "MQTT": {"port": 1883}})
        link.symlink_to(target)
        save_config(link, {"theme": "light"})
        self.assertTrue(link.is_symlink())
        self.assertEqual(1883, read_json(target)["MQTT"]["port"])

    def test_explicit_gui_edit_can_correct_legacy_field_type(self):
        target = self.root / "config.json"
        atomic_json(target, {"enabled": 0, "MQTT": {"port": 1883}})
        save_config(target, {"enabled": False})
        self.assertIs(read_json(target)["enabled"], False)
        self.assertEqual(1883, read_json(target)["MQTT"]["port"])

    def test_config_writes_rejected_during_ota(self):
        target = self.root / "config.json"
        atomic_json(target, {"a": 1})
        with file_lock(str(target) + ".lock"):
            with self.assertRaises(BlockingIOError):
                save_config(target, {"a": 2})
        self.assertEqual({"a": 1}, read_json(target))

    def bundle(self, entries):
        archive = self.root / (str(uuid.uuid4()) + ".tar.gz")
        with tarfile.open(archive, "w:gz") as bundle:
            for name, kind in entries:
                member = tarfile.TarInfo(name)
                if kind == "file":
                    member.size = 1
                    bundle.addfile(member, io.BytesIO(b"x"))
                else:
                    member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    bundle.addfile(member)
        return archive

    def test_archive_rejects_traversal_links_duplicates(self):
        for entries in ([('app/../../escape', 'file')], [('app/link', 'link')],
                        [('app/a', 'file'), ('app/a', 'file')], [('/app/a', 'file')], [('other/a', 'file')]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                safe_extract(self.bundle(entries), self.root / str(uuid.uuid4()))

    def test_archive_expansion_limit(self):
        with self.assertRaises(ValueError):
            safe_extract(self.bundle([("app/a", "file")]), self.root / "output", max_bytes=0)

    def test_archive_extracts_regular_files(self):
        safe_extract(self.bundle([("app/a", "file")]), self.root / "output")
        self.assertEqual(b"x", (self.root / "output/app/a").read_bytes())

    def test_redirect_strips_token_and_rejects_http(self):
        req = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer private"})
        redirected = SafeRedirect().redirect_request(req, None, 302, "", {}, "https://objects.githubusercontent.com/x")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(ValueError):
            SafeRedirect().redirect_request(req, None, 302, "", {}, "http://example.com/x")


class StoreTests(FilesystemTests):
    def test_latest_attempt_uses_sequence_not_wall_clock(self):
        store = Store(self.root / "agent.db")
        with patch("ota.core.time.time", return_value=1000):
            store.record("first", "failed", {"target_version": "v1.0.1"}, {"device_id": "host", "role": "test"})
        with patch("ota.core.time.time", return_value=100):
            store.record("second", "failed", {"target_version": "v1.0.2"}, {"device_id": "host", "role": "test"})
        self.assertEqual("v1.0.2", store.latest_summary()["target_version"])

    def test_outbox_survives_reopen_and_only_ack_removes_pending(self):
        path = self.root / "agent.db"
        store = Store(path)
        identity = {"device_id": str(uuid.uuid4()), "role": "test"}
        store.record("attempt", "failed", {"target_version": "v1.0.1", "secret_config": "must-not-leak"}, identity)
        reopened = Store(path)
        event = reopened.pending()[0]
        self.assertNotIn("must-not-leak", event["body"])
        self.assertEqual({"v1.0.1"}, reopened.failed())
        reopened.acknowledge(event["id"])
        self.assertEqual([], Store(path).pending())

    def test_outbox_and_attempt_are_atomic(self):
        store = Store(self.root / "agent.db")
        with patch.object(Store, "_enqueue", side_effect=RuntimeError("Disk failure")):
            with self.assertRaises(RuntimeError):
                store.record("attempt", "preparing", {"target_version": "v1.0.1"}, {"device_id": "host", "role": "test"})
        self.assertEqual([], store.unfinished())

    def test_only_failures_pending_but_success_audit_remains(self):
        store = Store(self.root / "agent.db")
        identity = {"device_id": "host", "role": "test"}
        for phase in ("preparing", "success", "failed"):
            store.record(phase, phase, {"target_version": "v1.0.1"}, identity)
        self.assertEqual(1, store.outstanding())
        with store.connect() as db:
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def test_retry_progress_persists(self):
        store = Store(self.root / "agent.db")
        store.record("a", "failed", {"target_version": "v1.0.1"}, {"device_id": "host", "role": "test"})
        row = store.pending()[0]
        store.defer(row["id"], 3, "503")
        self.assertEqual([], Store(store.path).pending())
        with store.connect() as db:
            saved = db.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual(3, saved["tries"])
            self.assertEqual("503", saved["error"])


class HealthTests(unittest.TestCase):
    def test_camera_frames_optional_but_capture_worker_required(self):
        status = Health()
        status.set(config=True, models=True, index=True, inference=True,
                   mqtt_connected=True, mqtt_subscribed=True, sync=True)
        status.cameras = [0]
        for name in ("gui", "capture:0", "detector:0", "comparison:0"):
            status.pulse(name)
        value = status.snapshot()
        self.assertTrue(value["update_ready"])
        self.assertFalse(value["healthy"])
        self.assertFalse(value["cameras"]["0"])
        self.assertEqual({"gui", "capture:0", "detector:0", "comparison:0"}, set(value["counters"]))
        status.progress["capture:0"] = (1, time.monotonic() - 20)
        self.assertFalse(status.snapshot()["update_ready"])

    def test_progress_not_http_server_determines_health(self):
        status = Health()
        status.set(config=True, models=True, index=True, inference=True,
                   mqtt_connected=True, mqtt_subscribed=True, sync=True)
        status.cameras = [0]
        self.assertFalse(status.snapshot()["healthy"])
        for name in ("gui", "capture:0", "frame:0", "detector:0", "comparison:0"):
            status.pulse(name)
        self.assertTrue(status.snapshot()["healthy"])
        status.set(mqtt_subscribed=False)
        self.assertFalse(status.snapshot()["healthy"])
        self.assertTrue(status.snapshot()["core_ok"])

    def test_stalled_worker_and_removed_camera(self):
        status = Health()
        status.set(config=True, models=True, index=True, inference=True,
                   mqtt_connected=True, mqtt_subscribed=True, sync=True)
        status.cameras = [0]
        for name in ("gui", "capture:0", "frame:0", "detector:0", "comparison:0", "frame:1"):
            status.pulse(name)
        self.assertNotIn("frame:1", status.snapshot()["counters"])
        status.progress["detector:0"] = (1, time.monotonic() - 20)
        self.assertFalse(status.snapshot()["core_ok"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.device, self.token = str(uuid.uuid4()), "a" * 40
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.host_config = {"Server": {"API_url": "https://project.api.example.com/api/v1", "location_ID": 9}, "MQTT": {"port": 1884}}
        atomic_json(self.shared / "config.json", self.host_config)
        self.config = {"device_id": self.device, "role": "test",
                       "repo": "owner/repo", "smtp": {"host": "smtp.example.com", "port": 587, "tls": "starttls",
                       "from": "ota@example.com", "to": ["admin@example.com"]},
                       "state_dir": str(self.root / "agent"), "shared_dir": str(self.shared),
                       "stable_seconds": 1, "startup_timeout": 1}
        atomic_json(self.root / "agent.json", self.config)
        self.agent = Agent(self.root / "agent.json")
        self.identity = {"device_id": self.device, "role": "test", "project": "project", "location": "測試閘道"}
        self.agent.identity = lambda: self.identity
        self.old_info = {"version": "v1.0.0", "build_id": "old"}
        self.new_info = {"version": "v1.0.1", "build_id": "new"}
        self.old, self.new = self.root / "baseline", self.root / "candidate"
        self.old.mkdir()
        self.new.mkdir()
        atomic_json(self.old / "BUILD_INFO.json", self.old_info)
        atomic_json(self.new / "BUILD_INFO.json", self.new_info)
        atomic_json(self.new / "config.defaults.json", {"new_setting": True})
        self.agent.point(self.old)
        self.agent.check = lambda info: {"healthy": True, "core_ok": True, "external_ok": True}
        self.agent.wait_healthy = lambda info: {"healthy": True}
        self.agent.stage = lambda *args: (self.new, self.new_info)

    def tearDown(self):
        self.temp.cleanup()

    def test_update_success_preserves_config(self):
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        self.assertEqual(self.new, self.agent.current()[0])
        saved = read_json(self.shared / "config.json")
        self.assertEqual(self.host_config["MQTT"], saved["MQTT"])
        self.assertTrue(saved["new_setting"])
        self.assertEqual("success", self.agent.store.latest_summary()["phase"])

    def test_failed_candidate_rolls_back_and_keeps_shared_photos(self):
        (self.shared / "media").mkdir()
        (self.shared / "media/new-photo.jpg").write_bytes(b"latest")
        self.agent.wait_healthy = lambda info: (_ for _ in ()).throw(RuntimeError("model failure")) if info == self.new_info else {}
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        self.assertEqual(self.old, self.agent.current()[0])
        self.assertEqual(self.host_config, read_json(self.shared / "config.json"))
        self.assertEqual(b"latest", (self.shared / "media/new-photo.jpg").read_bytes())
        self.assertEqual("success", self.agent.store.latest_summary()["rollback"])

    def test_preparation_failure_never_stops_old_service(self):
        self.agent.stage = lambda *args: (_ for _ in ()).throw(ValueError("checksum mismatch"))
        with patch("ota.updater.systemctl") as control:
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        control.assert_not_called()
        self.assertEqual(self.old, self.agent.current()[0])
        self.assertEqual("failed", self.agent.store.latest_summary()["phase"])

    def test_config_migration_failure_does_not_stop_old(self):
        atomic_json(self.new / "config.defaults.json", {"MQTT": "invalid"})
        with patch("ota.updater.systemctl") as control:
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        control.assert_not_called()
        self.assertEqual(self.host_config, read_json(self.shared / "config.json"))

    def test_baseline_external_failure_postpones_update(self):
        self.agent.check = lambda info: {"healthy": False}
        with patch("ota.updater.systemctl") as control:
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        control.assert_not_called()
        self.assertEqual([], self.agent.store.unfinished())

    def test_rollback_failure_blocks_further_updates(self):
        self.agent.wait_healthy = lambda *args: (_ for _ in ()).throw(RuntimeError("all health checks failed"))
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        self.assertTrue(self.agent.store.get("blocked"))
        row = json.loads(self.agent.store.pending()[-1]["body"])
        self.assertEqual("rollback_failed", row["phase"])
        self.assertFalse(row["service_healthy"])
        self.assertEqual("unknown", row["running_version"])

    def test_recovery_after_switch_restores_baseline(self):
        backup = self.root / "backup.json"
        atomic_json(backup, self.host_config)
        data = {"target_version": "v1.0.1", "old_info": self.old_info, "old_path": str(self.old), "backup": str(backup)}
        self.agent.store.record("interrupted", "switching", data, self.identity)
        self.agent.point(self.new)
        with patch("ota.updater.systemctl"):
            self.agent.recover()
        self.assertEqual(self.old, self.agent.current()[0])
        self.assertEqual([], self.agent.store.unfinished())

    def test_journal_failure_does_not_prevent_rollback(self):
        record = self.agent.store.record
        def fault(attempt, phase, data, identity):
            if phase == "rolling_back":
                raise OSError("journal unavailable")
            return record(attempt, phase, data, identity)
        self.agent.store.record = fault
        self.agent.wait_healthy = lambda info: (_ for _ in ()).throw(RuntimeError("new model failure")) if info == self.new_info else {}
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        self.assertEqual(self.old, self.agent.current()[0])
        self.assertEqual("success", self.agent.store.latest_summary()["rollback"])

    def test_recovery_does_not_restart_already_restored_old_build(self):
        backup = self.root / "backup.json"
        atomic_json(backup, self.host_config)
        data = {"target_version": "v1.0.1", "old_info": self.old_info, "old_path": str(self.old), "backup": str(backup)}
        self.agent.store.record("interrupted", "rolling_back", data, self.identity)
        with patch("ota.updater.systemctl") as control:
            self.agent.recover()
        control.assert_not_called()
        self.assertEqual("failed", self.agent.store.latest_summary()["phase"])

    def test_mail_failure_persists_then_sends(self):
        self.agent.store.record("a", "failed", {"target_version": "v1.0.1"}, self.identity)
        failing = lambda *args: (_ for _ in ()).throw(OSError("SMTP down"))
        mail_once(self.config, self.agent.store, sender=failing)
        reopened = Store(self.agent.store.path)
        self.assertEqual(1, reopened.outstanding())
        with reopened.connect() as db:
            self.assertEqual(1, db.execute("SELECT tries FROM outbox").fetchone()[0])
            db.execute("UPDATE outbox SET due=0")
        sent = []
        mail_once(self.config, reopened, sender=lambda *args: sent.append(args))
        mail_once(self.config, reopened, sender=lambda *args: sent.append(args))
        self.assertEqual(1, len(sent))
        self.assertEqual(0, reopened.outstanding())

    def test_partial_recipients_do_not_resend_to_accepted_recipient(self):
        self.config["smtp"]["to"] = ["one@example.com", "two@example.com"]
        self.agent.store.record("a", "failed", {"target_version": "v1.0.1"}, self.identity)
        calls = []
        def sender(cfg, event, recipient):
            calls.append(recipient)
            if len(calls) == 2:
                raise OSError("second recipient unavailable")
        mail_once(self.config, self.agent.store, sender)
        with self.agent.store.connect() as db:
            db.execute("UPDATE outbox SET due=0")
        mail_once(self.config, Store(self.agent.store.path), sender)
        self.assertEqual(["one@example.com", "two@example.com", "two@example.com"], calls)
        self.assertEqual(0, self.agent.store.outstanding())

    def test_smtp_outage_never_rolls_back_successful_update(self):
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        with patch("ota.notifications.send_mail", side_effect=OSError("SMTP down")) as sender:
            self.agent.notify_once()
        sender.assert_not_called()
        self.assertEqual(self.new, self.agent.current()[0])

    def test_refused_first_recipient_does_not_block_other_administrators(self):
        self.config["smtp"]["to"] = ["bad@example.com", "good@example.com"]
        self.agent.store.record("a", "failed", {"target_version": "v1.0.1"}, self.identity)
        sent = []
        def sender(config, event, recipient):
            if recipient == "bad@example.com":
                raise smtplib.SMTPRecipientsRefused({recipient: (550, b"refused")})
            sent.append(recipient)
        mail_once(self.config, self.agent.store, sender)
        self.assertEqual(["good@example.com"], sent)
        self.assertEqual(1, self.agent.store.outstanding())
        with self.agent.store.connect() as db:
            db.execute("UPDATE outbox SET due=0")
        mail_once(self.config, self.agent.store, sender)
        self.assertEqual(["good@example.com"], sent)

    def test_outage_stops_batch_without_dropping_remaining_events(self):
        for number in range(5):
            self.agent.store.record(str(number), "failed", {"target_version": "v1.0.1"}, self.identity)
        with patch("ota.notifications.send_mail", side_effect=OSError("SMTP down")) as sender:
            self.agent.notify_once()
        self.assertEqual(1, sender.call_count)
        self.assertEqual(5, self.agent.store.outstanding())

    def test_manifest_download_failure_is_a_durable_failure_notification(self):
        self.agent.releases = lambda: [release("v1.0.1")]
        self.agent.manifest = lambda candidate: (_ for _ in ()).throw(OSError("manifest unavailable"))
        with patch("ota.updater.systemctl") as control:
            self.agent.once()
        control.assert_not_called()
        self.assertEqual("failed", self.agent.store.latest_summary()["phase"])
        self.assertEqual(1, self.agent.store.outstanding())

    def test_legacy_queue_migration_keeps_pending_failures_and_audit(self):
        store = self.agent.store
        store.record("ok", "success", {"target_version": "v1.0.1"}, self.identity)
        store.record("bad", "failed", {"target_version": "v1.0.2"}, self.identity)
        with store.connect() as db:
            db.execute("UPDATE outbox SET delivered=0")
            # Simulate a legacy pending heartbeat, not a new update result.
            db.execute("INSERT INTO outbox(id,body) VALUES (?,?)", (str(uuid.uuid4()), json.dumps({"kind": "heartbeat"})))
        store.migrate_notifications()
        self.assertEqual(1, store.outstanding())
        with store.connect() as db:
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def test_camera_offline_allows_update_and_rollback_validation(self):
        self.agent.check = lambda info: {"healthy": False, "update_ready": True}
        self.agent.wait_healthy = lambda info: {"healthy": False, "update_ready": True}
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        self.assertEqual("success", self.agent.store.latest_summary()["phase"])
        self.assertEqual(self.new, self.agent.current()[0])
        self.assertEqual(0, self.agent.store.outstanding())
        with self.agent.store.connect() as db:
            result = json.loads(db.execute("SELECT data FROM attempts").fetchone()[0])
        self.assertFalse(result["service_healthy"])

    def test_offline_camera_does_not_turn_successful_rollback_into_failure(self):
        self.agent.check = lambda info: {"healthy": False, "update_ready": True}
        self.agent.wait_healthy = lambda info: (_ for _ in ()).throw(RuntimeError("bad model")) if info == self.new_info else {"healthy": False, "update_ready": True}
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        self.assertEqual(self.old, self.agent.current()[0])
        self.assertEqual("success", self.agent.store.latest_summary()["rollback"])
        self.assertFalse(self.agent.store.get("blocked", False))

    def test_rollback_failure_timestamps_are_preserved_until_mail(self):
        self.agent.wait_healthy = lambda info: (_ for _ in ()).throw(RuntimeError("failed"))
        with patch("ota.updater.systemctl"):
            self.agent.update(release("v1.0.1"), {"sha256": "0" * 64})
        event = json.loads(self.agent.store.pending()[-1]["body"])
        for key in ("failed_at", "rollback_started_at", "rollback_failed_at"):
            self.assertRegex(event[key], r"T.*[+-][0-9]{2}:[0-9]{2}$")
        sent = []
        mail_once(self.config, Store(self.agent.store.path), sender=lambda cfg, event, recipient: sent.append(event))
        self.assertEqual(event["rollback_failed_at"], sent[0]["rollback_failed_at"])

    def test_monitor_grace_dedup_recovery_and_restart(self):
        self.agent.check = lambda info: {"healthy": False, "core_ok": True, "external_ok": True,
                                         "camera_ok": False, "cameras": {"0": False}}
        with patch("ota.core.time.time", return_value=100):
            self.agent.monitor()
        self.assertEqual(0, self.agent.store.outstanding())
        with patch("ota.core.time.time", return_value=401):
            self.agent.store = Store(self.agent.store.path)
            self.agent.monitor()
            self.agent.monitor()
        self.assertEqual(1, self.agent.store.outstanding())
        event = json.loads(self.agent.store.pending()[0]["body"])
        self.assertEqual("camera", event["alert"])
        self.agent.check = lambda info: {"healthy": True, "core_ok": True, "external_ok": True, "camera_ok": True}
        self.agent.monitor()
        self.assertEqual({}, self.agent.store.get("incidents"))

    def test_monitor_does_not_alert_during_update_or_blocked_rollback(self):
        self.agent.store.set("blocked", True)
        self.agent.check = lambda info: (_ for _ in ()).throw(RuntimeError("offline"))
        self.agent.monitor()
        self.assertEqual({}, self.agent.store.get("incidents", {}))
        self.agent.store.set("blocked", False)
        with file_lock(self.agent.root / "update.lock"), patch("ota.notifications.send_mail"):
            self.agent.notify_once()
        self.assertEqual({}, self.agent.store.get("incidents", {}))

    def test_camera_alert_is_not_duplicated_across_update_window(self):
        self.agent.check = lambda info: {"healthy": False, "core_ok": True, "external_ok": True,
                                         "camera_ok": False, "cameras": {"0": False}}
        with patch("ota.core.time.time", return_value=100):
            self.agent.monitor()
        with patch("ota.core.time.time", return_value=401):
            self.agent.monitor()
        self.assertEqual(1, self.agent.store.outstanding())
        with file_lock(self.agent.root / "update.lock"), patch("ota.notifications.send_mail"):
            self.agent.notify_once()
        self.agent.monitor()
        with self.agent.store.connect() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM outbox WHERE json_extract(body,'$.kind')='alert'").fetchone()[0])

    def test_manual_retry_selects_only_requested_version(self):
        self.agent.store.record("old-failure", "failed", {"target_version": "v1.0.1"}, self.identity)
        self.agent.store.set("retry", "v1.0.1")
        self.agent.releases = lambda: [release("v1.0.1"), release("v1.0.2")]
        self.agent.manifest = lambda value: {"projects": ["project"], "sha256": "0" * 64}
        selected = []
        self.agent.update = lambda value, manifest: selected.append(value["tag_name"])
        self.agent.once()
        self.assertEqual(["v1.0.1"], selected)

    def test_cleanup_keeps_current_previous_and_shared_business_data(self):
        self.shared.joinpath("media").mkdir()
        self.shared.joinpath("media/latest.jpg").write_bytes(b"latest")
        previous = self.old
        slots = []
        for number in range(1, 4):
            attempt = str(uuid.uuid4())
            tag = f"v1.0.{number}"
            app = self.agent.root / "releases" / (tag + "-" + attempt) / "app"
            app.mkdir(parents=True)
            atomic_json(app / "BUILD_INFO.json", {"version": tag, "build_id": tag})
            (app / "media").symlink_to(self.shared / "media")
            self.agent.store.record(attempt, "success", {"target_version": tag, "old_path": str(previous), "new_path": str(app)}, self.identity)
            previous = app
            slots.append(app)
        self.agent.point(slots[-1])
        self.agent.cleanup()
        self.assertFalse(slots[0].exists())
        self.assertTrue(slots[1].exists())
        self.assertTrue(slots[2].exists())
        self.assertEqual(b"latest", self.shared.joinpath("media/latest.jpg").read_bytes())

    def test_pid_and_build_identity_required(self):
        value = {"pid": 100, "version": "v1.0.0", "build_id": "old", "boot_id": "boot", "healthy": True}
        with patch("ota.updater.health", return_value=value), patch("ota.updater.systemctl", return_value="101"):
            with self.assertRaises(RuntimeError):
                Agent.check(self.agent, self.old_info)
        with patch("ota.updater.health", return_value=value), patch("ota.updater.systemctl", return_value="100"):
            self.assertTrue(Agent.check(self.agent, self.old_info)["healthy"])
            with self.assertRaises(RuntimeError):
                Agent.check(self.agent, self.new_info)

    def test_stable_window_and_frozen_counters(self):
        values = [{"healthy": True, "boot_id": "one", "counters": {"gui": 1}},
                  {"healthy": True, "boot_id": "one", "counters": {"gui": 2}}]
        self.agent.check = lambda info: values.pop(0)
        with patch("ota.updater.time.monotonic", side_effect=[0, 0, 0, 1, 1]), patch("ota.updater.time.sleep"):
            self.assertTrue(Agent.wait_healthy(self.agent, self.new_info)["healthy"])
        self.agent.check = lambda info: {"healthy": True, "boot_id": "one", "counters": {"gui": 1}}
        with patch("ota.updater.time.monotonic", side_effect=[0, 0, 0, 1, 1]), patch("ota.updater.time.sleep"):
            with self.assertRaises(RuntimeError):
                Agent.wait_healthy(self.agent, self.new_info)

    def test_camera_offline_stable_window_still_requires_real_worker_progress(self):
        values = [{"healthy": False, "update_ready": True, "boot_id": "one", "counters": {"capture:0": 1}},
                  {"healthy": False, "update_ready": True, "boot_id": "one", "counters": {"capture:0": 2}}]
        self.agent.check = lambda info: values.pop(0)
        with patch("ota.updater.time.monotonic", side_effect=[0, 0, 0, 1, 1]), patch("ota.updater.time.sleep"):
            self.assertFalse(Agent.wait_healthy(self.agent, self.new_info)["healthy"])

    def test_installer_and_upgrade_preserve_baseline_config_history_and_pending_mail(self):
        from ota.install_agent import install
        info = {**self.old_info, "config_schema": 1, "data_schema": 1, "feature_model_sha256": "a" * 64}
        atomic_json(self.old / "BUILD_INFO.json", info)
        (self.old / "venv/bin").mkdir(parents=True)
        (self.old / "venv/bin/python").symlink_to(sys.executable)
        (self.old / "config.json").symlink_to(self.shared / "config.json")
        config_path = self.root / "agent.json"
        config = {**self.config, "python": sys.executable}
        atomic_json(config_path, config)
        def control(*args):
            if args[0] == "is-active":
                return "inactive"
            if "--property=WorkingDirectory" in args:
                return str(self.agent.root / "current")
            if "--property=ExecStart" in args:
                return str(self.agent.root / "current/venv/bin/python")
            return ""
        with patch("ota.install_agent.Path.home", return_value=self.root), \
             patch("ota.install_agent.systemctl", side_effect=control), \
             patch("ota.updater.Agent.check", return_value={"healthy": False, "update_ready": True}):
            destination = install(config_path, self.old)
            directory = self.root / ".config/systemd/user"
            units = [directory / ("facial-ota-" + action + ".service") for action in ("poll", "notify")]
            for unit in units:
                self.assertIn(str(destination), unit.read_text())
            verified = subprocess.run(["systemd-analyze", "--user", "verify", *(str(unit) for unit in units)],
                                      capture_output=True, text=True)
            self.assertEqual(0, verified.returncode, verified.stderr)
            self.agent.store.record("legacy-failure", "failed", {"target_version": "v1.0.1"}, self.identity)
            runtime_file = self.agent.root / "agent/ota/core.py"
            runtime_file.write_text("# old-runtime")
            legacy_unit = directory / "facial-ota-report.service"
            legacy_unit.write_text("# old-report")
            install(destination, upgrade=True)
        self.assertTrue(legacy_unit.with_name(legacy_unit.name + ".before-smtp").exists())
        self.assertNotEqual("# old-runtime", runtime_file.read_text())
        self.assertTrue(list(self.agent.root.glob("agent-backup-*/ota/core.py")))
        self.assertEqual(self.host_config, read_json(self.shared / "config.json"))
        self.assertEqual(self.device, read_json(destination)["device_id"])
        self.assertEqual(1, self.agent.store.outstanding())

    def test_upgrade_refuses_active_updater(self):
        from ota.install_agent import install
        info = {**self.old_info, "config_schema": 1, "data_schema": 1, "feature_model_sha256": "a" * 64}
        atomic_json(self.old / "BUILD_INFO.json", info)
        (self.old / "venv/bin").mkdir(parents=True)
        (self.old / "venv/bin/python").symlink_to(sys.executable)
        (self.old / "config.json").symlink_to(self.shared / "config.json")
        (self.agent.root / "agent").mkdir()
        dropin = self.root / ".config/systemd/user/facial_recognition.service.d/50-ota.conf"
        dropin.parent.mkdir(parents=True)
        dropin.write_text("# existing")
        with patch("ota.install_agent.Path.home", return_value=self.root), \
             patch("ota.install_agent.systemctl", return_value="active"), \
             patch("ota.updater.Agent.check", return_value={"healthy": True}), self.assertRaises(RuntimeError):
            install(self.root / "agent.json", upgrade=True)
        self.assertEqual("# existing", dropin.read_text())

    def test_health_timeout(self):
        self.agent.check = lambda info: {"healthy": False, "boot_id": "one"}
        with patch("ota.updater.time.monotonic", side_effect=[0, 0, 2]), patch("ota.updater.time.sleep"):
            with self.assertRaises(RuntimeError):
                Agent.wait_healthy(self.agent, self.new_info)

    def staged_bundle(self, embedded_config=False):
        weight = b"resolved-test-weight"
        fingerprint = hashlib.sha256(weight).hexdigest()
        self.old_info["feature_model_sha256"] = fingerprint
        atomic_json(self.old / "BUILD_INFO.json", self.old_info)
        info = {**self.new_info, "commit": "c" * 40, "data_schema": 1, "feature_model_sha256": fingerprint}
        payload = io.BytesIO()
        entries = {"app/main.py": b"# app", "app/requirements.txt": b"", "app/ota/core.py": b"# core",
                   "app/ota/health.py": b"# health", "app/config.defaults.json": b"{}",
                   "app/BUILD_INFO.json": json.dumps(info).encode(),
                   "app/models/data/20180402-114759-vggface2.pt": weight}
        if embedded_config:
            entries["app/config.json"] = b"{}"
        with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
            for name, content in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
        content = payload.getvalue()
        manifest = {"version": "v1.0.1", "build_id": "new", "commit": "c" * 40,
                    "archive": "app.tar.gz", "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                    "python": "3.12", "architecture": "x86_64"}
        rel = {**release("v1.0.1"), "assets": [{"name": "app.tar.gz", "state": "uploaded", "size": len(content),
               "url": "https://api.github.com/repos/owner/repo/releases/assets/1"}]}
        return content, manifest, rel

    def test_real_staging_isolated_environment_and_shared_links(self):
        content, manifest, rel = self.staged_bundle()
        with patch("ota.updater.request", return_value=io.BytesIO(content)), patch("ota.updater.subprocess.run") as runner, \
             patch("ota.updater.subprocess.check_output", return_value="3.12\nx86_64\n"):
            app, info = Agent.stage(self.agent, rel, manifest, str(uuid.uuid4()))
        self.assertTrue((app / "config.json").is_symlink())
        self.assertEqual(self.shared / "media", (app / "media").resolve())
        self.assertEqual("v1.0.1", info["version"])
        self.assertIn("--no-index", runner.call_args_list[1].args[0])
        self.assertEqual(self.old, self.agent.current()[0])

    def test_staging_rejects_integrity_wrong_python_and_host_config(self):
        for mode in ("checksum", "python", "host_config"):
            content, manifest, rel = self.staged_bundle(mode == "host_config")
            if mode == "checksum":
                manifest["sha256"] = "0" * 64
            elif mode == "python":
                manifest["python"] = "3.10"
            with self.subTest(mode=mode), patch("ota.updater.request", return_value=io.BytesIO(content)), \
                 patch("ota.updater.subprocess.run") as runner, patch("ota.updater.subprocess.check_output", return_value="3.12\nx86_64\n"):
                with self.assertRaises(ValueError):
                    Agent.stage(self.agent, rel, manifest, str(uuid.uuid4()))
                runner.assert_not_called()

    def test_name_lookup_failure_uses_cached_label(self):
        self.agent.store.set("identity", {"key": "https://project.api.example.com/api/v1/locations/9/", "project": "project", "location": "cached"})
        with patch("ota.updater.request", side_effect=OSError("API down")):
            self.assertEqual("cached", Agent.identity(self.agent)["location"])

    def test_lan_config_save_refuses_during_update(self):
        from web_ui.app import app as local_app
        with patch("web_ui.app.CONFIG_PATH", str(self.shared / "config.json")):
            client = local_app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            with file_lock(str(self.shared / "config.json") + ".lock"):
                response = client.post("/api/config", json=self.host_config)
                self.assertEqual(409, response.status_code)


class PackageTests(FilesystemTests):
    def sources(self):
        root, wheels = self.root / "source", self.root / "wheels"
        root.mkdir()
        wheels.mkdir()
        files = list(REQUIRED) + ["setting/build_config.py", "config.json", "py_ssh/id_rsa"]
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        (root / "setting/build_config.py").write_text("default_config = {'enabled': True}\n")
        (root / "setting/ota_migrations.json").write_text("[]\n")
        (wheels / "fixture-1.0-py3-none-any.whl").write_bytes(b"fixture-wheel")
        def git(args, **kwargs):
            return "a" * 40 + "\n" if "rev-parse" in args else ("\0".join(files) + "\0").encode()
        return root, wheels, git

    def test_package_embeds_identity_and_excludes_secrets(self):
        root, wheels, git = self.sources()
        output = self.root / "output"
        with patch("ota.package_release.subprocess.run", return_value=types.SimpleNamespace(returncode=1)), patch("ota.package_release.subprocess.check_output", side_effect=git):
            manifest = package(root, wheels, output, "v1.0.1-rc.1", ["project"], "3.12", "x86_64")
        with tarfile.open(output / manifest["archive"]) as bundle:
            self.assertNotIn("app/config.json", bundle.getnames())
            self.assertNotIn("app/py_ssh/id_rsa", bundle.getnames())
            info = json.load(bundle.extractfile("app/BUILD_INFO.json"))
            self.assertEqual("v1.0.1-rc.1", info["version"])
        with patch("ota.package_release.subprocess.run", return_value=types.SimpleNamespace(returncode=1)), patch("ota.package_release.subprocess.check_output", side_effect=git):
            with self.assertRaises(FileExistsError):
                package(root, wheels, output, "v1.0.1-rc.1", ["project"], "3.12", "x86_64")

    def test_unresolved_lfs_rejected(self):
        root, wheels, git = self.sources()
        (root / "models/data/20180402-114759-vggface2.pt").write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
        with patch("ota.package_release.subprocess.run", return_value=types.SimpleNamespace(returncode=1)), patch("ota.package_release.subprocess.check_output", side_effect=git):
            with self.assertRaises(ValueError):
                package(root, wheels, self.root / "output", "v1.0.1-rc.1", ["project"], "3.12", "x86_64")

    def test_existing_tag_cannot_be_repackaged_from_different_commit(self):
        root, wheels, git = self.sources()
        with patch("ota.package_release.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout="different")), patch("ota.package_release.subprocess.check_output", side_effect=git):
            with self.assertRaises(ValueError):
                package(root, wheels, self.root / "output", "v1.0.1-rc.1", ["project"], "3.12", "x86_64")


class ConfigurationTests(FilesystemTests):
    def test_install_shell_config_only_preserves_business_config_without_sudo(self):
        import shutil
        project, target, existing = self.configure_fixture(role="development")
        source = Path(__file__).parent
        shutil.copytree(source, project / "ota", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.local.json", "__pycache__", "dist", "wheelhouse"))
        shutil.copyfile(source.parent / "install.sh", project / "install.sh")
        before = (project / "config.json").read_bytes()
        result = subprocess.run(["bash", str(project / "install.sh"), "--ota-config-only"], input="\n",
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(existing["device_id"], read_json(target)["device_id"])
        self.assertEqual(before, (project / "config.json").read_bytes())
        result = subprocess.run(["bash", str(project / "install.sh")], input="",
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--ota-config-only", result.stdout)
        self.assertEqual(before, (project / "config.json").read_bytes())

    def test_new_test_and_production_setups_auto_generate_config(self):
        from ota.configure import configure
        identifiers = []
        for role in ("test", "production"):
            project = self.root / role
            project.mkdir()
            answers = {"主機角色": role, "SMTP 主機": "smtp.example.com", "寄件信箱": "ota@example.com",
                       "管理員收件信箱": "admin@example.com,second@example.com"}
            def answer(label, default="", **kwargs):
                return next((value for key, value in answers.items() if label.startswith(key)), default)
            with patch("ota.configure.prompt", side_effect=answer), patch("ota.configure.Path.home", return_value=project):
                target = configure(project)
            value = read_json(target)
            uuid.UUID(value["device_id"])
            identifiers.append(value["device_id"])
            self.assertEqual(role, value["role"])
            self.assertEqual(2, len(value["smtp"]["to"]))
            self.assertFalse((project / "config.json").exists())
        self.assertNotEqual(*identifiers)

    def test_existing_update_lock_prevents_setup_from_overwriting_config(self):
        from ota.configure import configure
        project, target, existing = self.configure_fixture()
        installed = Path(existing["state_dir"]) / "agent.json"
        atomic_json(installed, existing)
        before = installed.read_bytes()
        with file_lock(installed.parent / "update.lock"), \
             patch("ota.configure.prompt", side_effect=lambda label, default="", **kwargs: default), \
             self.assertRaises(BlockingIOError):
            configure(project)
        self.assertEqual(before, installed.read_bytes())

    def configure_fixture(self, role="test", installed=False):
        project = self.root / "project"
        project.mkdir()
        atomic_json(project / "config.json", {"site": "original", "password": "business-secret"})
        target = self.root / "agent.json" if installed else project / "ota/agent.local.json"
        existing = {"device_id": str(uuid.uuid4()), "role": role, "repo": "owner/repo", "custom": "keep",
                    "state_dir": str(self.root / "state"), "shared_dir": str(project), "github_token": "private-token",
                    "smtp": {"host": "smtp.example.com", "port": 587, "tls": "starttls", "username": "sender",
                             "password": "smtp-secret", "from": "sender@example.com", "to": ["admin@example.com"]},
                    "management_url": "https://legacy.example.com", "device_token": "legacy-token"}
        atomic_json(target, existing)
        return project, target, existing

    def test_setup_preserves_config_uuid_secrets_and_removes_legacy_tokens(self):
        from ota.configure import configure
        project, target, existing = self.configure_fixture()
        before = (project / "config.json").read_bytes()
        with patch("ota.configure.prompt", side_effect=lambda label, default="", **kwargs: default), \
             patch("ota.configure.Path.home", return_value=self.root), patch("builtins.print") as output:
            self.assertEqual(target, configure(project))
        saved = read_json(target)
        self.assertEqual(existing["device_id"], saved["device_id"])
        self.assertEqual(existing["smtp"], saved["smtp"])
        self.assertEqual("keep", saved["custom"])
        self.assertNotIn("management_url", saved)
        self.assertNotIn("device_token", saved)
        self.assertEqual(before, (project / "config.json").read_bytes())
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertNotIn("smtp-secret", str(output.call_args_list))
        self.assertNotIn("private-token", str(output.call_args_list))
        self.assertTrue(target.with_name(target.name + ".before-smtp.local.json").exists())

    def test_setup_prefers_installed_json(self):
        from ota.configure import configure
        project, target, existing = self.configure_fixture(installed=True)
        installed = Path(existing["state_dir"]) / "agent.json"
        atomic_json(installed, existing)
        atomic_json(project / "ota/agent.local.json", {"state_dir": existing["state_dir"], "device_id": str(uuid.uuid4())})
        with patch("ota.configure.prompt", side_effect=lambda label, default="", **kwargs: default):
            self.assertEqual(installed, configure(project))
        self.assertEqual(existing["device_id"], read_json(installed)["device_id"])

    def test_new_development_setup_generates_stable_uuid_without_smtp_prompt(self):
        from ota.configure import configure
        project = self.root / "project"
        project.mkdir()
        questions = []
        def answer(label, default="", **kwargs):
            questions.append(label)
            return "development" if "角色" in label else default
        with patch("ota.configure.prompt", side_effect=answer), patch("ota.configure.Path.home", return_value=self.root):
            target = configure(project)
            first = read_json(target)["device_id"]
            configure(project)
        uuid.UUID(first)
        self.assertEqual(first, read_json(target)["device_id"])
        self.assertEqual(2, len(questions))

    def test_secret_prompt_does_not_display_saved_password(self):
        from ota.configure import prompt
        with patch("ota.configure.getpass.getpass", return_value="") as ask:
            self.assertEqual("secret", prompt("SMTP 密碼", "secret", secret=True))
        self.assertNotIn("secret", ask.call_args.args[0])

    def test_smtp_tls_headers_and_real_event_times(self):
        config = {"smtp": {"host": "smtp.example.com", "port": 587, "tls": "starttls", "username": "user",
                           "password": "secret", "from": "ota@example.com", "to": ["admin@example.com"]}}
        event = {"event_id": str(uuid.uuid4()), "occurred_at": 1000, "phase": "rollback_failed",
                 "project": "demosite", "location": "測試閘道", "rollback_failed_at": "2026-10-18T14:32:10+08:00"}
        with patch("ota.notifications.smtplib.SMTP") as factory:
            client = factory.return_value.__enter__.return_value
            client.send_message.return_value = {}
            send_mail(config, event, "admin@example.com")
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("user", "secret")
        message = client.send_message.call_args.args[0]
        self.assertIn("2026-10-18T14:32:10+08:00", message.get_content())
        self.assertIn("回退失敗當下時間", message.get_content())
        self.assertIn("通知寄送時間", message.get_content())
        self.assertNotIn("secret", message.as_string())
        self.assertEqual(["admin@example.com"], client.send_message.call_args.kwargs["to_addrs"])
        config["smtp"]["tls"] = "ssl"
        config["smtp"]["port"] = 465
        with patch("ota.notifications.smtplib.SMTP_SSL") as factory:
            factory.return_value.__enter__.return_value.send_message.return_value = {}
            send_mail(config, event, "admin@example.com")
        self.assertIn("context", factory.call_args.kwargs)
        factory.return_value.__enter__.return_value.starttls.assert_not_called()

    def test_smtp_refusal_is_not_acknowledged(self):
        config = {"smtp": {"host": "smtp.example.com", "port": 587, "tls": "starttls",
                           "from": "ota@example.com", "to": ["admin@example.com"]}}
        event = {"event_id": str(uuid.uuid4()), "occurred_at": time.time()}
        with patch("ota.notifications.smtplib.SMTP") as factory:
            factory.return_value.__enter__.return_value.send_message.return_value = {"admin@example.com": (550, b"refused")}
            with self.assertRaises(smtplib.SMTPRecipientsRefused):
                send_mail(config, event, "admin@example.com")
        for tls, recipient in (("none", "admin@example.com"), ("ssl", "admin@example.com\nBcc:x@example.com")):
            config["smtp"].update(tls=tls, to=[recipient])
            with self.assertRaises(ValueError):
                validate_smtp(config["smtp"])


class MQTTTests(unittest.TestCase):
    def callbacks(self):
        tree = ast.parse((Path(__file__).parent.parent / "main.py").read_text())
        original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FaceRecognitionSystem")
        names = {"setup_mqtt_client", "on_mqtt_connect", "on_mqtt_subscribe", "on_mqtt_disconnect", "on_mqtt_message"}
        klass = ast.ClassDef(name="Callbacks", bases=[], keywords=[], decorator_list=[],
                             body=[node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names])
        client = types.SimpleNamespace(connect=lambda *args: None, loop_start=lambda: None,
                                       subscribe=lambda topic: None, loop_stop=lambda: None, disconnect=lambda: None)
        namespace = {"CONFIG": {}, "LOGGER": types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None),
                     "mqtt": types.SimpleNamespace(Client=lambda *args: client, CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[])), "callbacks", "exec"), namespace)
        instance = namespace["Callbacks"]()
        instance.health = Health()
        instance._network_tasks_done = {"mqtt": False}
        instance._request_face_sync = lambda: None
        instance._start_network_retry_loop = lambda: None
        instance.setup_mqtt_client()
        return instance, client

    def test_connect_call_is_not_a_broker_ack(self):
        instance, client = self.callbacks()
        self.assertFalse(instance._network_tasks_done["mqtt"])
        instance.on_mqtt_connect(client, None, None, 0)
        self.assertTrue(instance.health.checks["mqtt_connected"])
        self.assertFalse(instance._network_tasks_done["mqtt"])

    def test_suback_rejection_and_disconnect(self):
        instance, client = self.callbacks()
        instance.on_mqtt_subscribe(client, None, 1, [0])
        self.assertTrue(instance._network_tasks_done["mqtt"])
        instance.on_mqtt_subscribe(client, None, 1, [128])
        self.assertFalse(instance._network_tasks_done["mqtt"])
        instance.on_mqtt_disconnect(client, None, None, 0)
        self.assertFalse(instance.health.checks["mqtt_connected"])

    def test_message_callback_does_not_run_sync_inline(self):
        instance, client = self.callbacks()
        calls = []
        instance._request_face_sync = lambda: calls.append(True)
        instance.on_mqtt_message(client, None, None)
        self.assertEqual([True], calls)


class DescriptorTests(unittest.TestCase):
    def run_generator(self, root, fail=False):
        tree = ast.parse((Path(__file__).parent.parent / "main.py").read_text())
        klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FaceRecognitionSystem")
        method = next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == "_generate_new_descriptors")
        vector = types.SimpleNamespace(shape=(512,))
        tensor = types.SimpleNamespace(detach=lambda: types.SimpleNamespace(numpy=lambda: vector))
        def save(stream, value, **kwargs):
            stream.write(b"new-complete-descriptor")
            if fail:
                raise OSError("descriptor write interrupted")
        fake_mp = types.ModuleType("init.mediapipe_handler")
        fake_mp.MediaPipeHandler = lambda **kwargs: types.SimpleNamespace(detect=lambda image: ([[0, 0, 1, 1]], None, [[]]), close=lambda: None)
        namespace = {"os": os, "uuid": uuid,
                     "LOGGER": types.SimpleNamespace(exception=lambda *args: None),
                     "np": types.SimpleNamespace(array=lambda image: image, save=save,
                         isfinite=lambda value: types.SimpleNamespace(all=lambda: True)),
                     "Image": types.SimpleNamespace(open=lambda path: types.SimpleNamespace(convert=lambda mode: None)),
                     "tqdm": lambda items, **kwargs: items,
                     "crop_face_without_forehead": lambda *args: types.SimpleNamespace(unsqueeze=lambda n: None),
                     "sync_directory": lambda path: None}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "descriptor", "exec"), namespace)
        person = root / "person.jpg"
        person.write_bytes(b"photo")
        descriptor = root / "person.npy"
        descriptor.write_bytes(b"old-good-descriptor")
        os.utime(descriptor, (1, 1))
        with patch.dict(sys.modules, {"init.mediapipe_handler": fake_mp}):
            success = namespace["_generate_new_descriptors"](types.SimpleNamespace(resnet=lambda value: [tensor]), ["person.jpg"], str(root), str(root))
        return success, descriptor

    def test_interrupted_descriptor_write_preserves_old_data_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success, descriptor = self.run_generator(root, fail=True)
            self.assertFalse(success)
            self.assertEqual(b"old-good-descriptor", descriptor.read_bytes())
            self.assertEqual([], list(root.glob("*.tmp")))

    def test_complete_descriptor_atomically_replaces_old_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            success, descriptor = self.run_generator(root)
            self.assertTrue(success)
            self.assertEqual(b"new-complete-descriptor", descriptor.read_bytes())
            self.assertEqual([], list(root.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
