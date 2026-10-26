"""Automatic setup regressions; all services, downloads and email are isolated."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from ota.configure import configure, export_profile, repository
from ota.core import atomic_json, digest, read_json
from ota.setup import Setup, SERVICE, launch, wait
from ota.updater import Agent, independent_python


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.shared = self.root / "project"
        self.shared.mkdir()
        self.state = self.root / "state"
        self.site = {"Server": {"API_url": "https://project.api.example.com/api/v1", "location_ID": 7},
                     "local_field": "keep"}
        atomic_json(self.shared / "config.json", self.site)
        weight = self.shared / "models/data/20180402-114759-vggface2.pt"
        weight.parent.mkdir(parents=True)
        weight.write_bytes(b"tested-feature-model")
        self.info = {"version": "v1.4.0-rc.2", "build_id": "test-build", "config_schema": 1,
                     "data_schema": 1, "feature_model_sha256": digest(weight)}
        self.config = {"device_id": str(uuid.uuid4()), "role": "test", "repo": "owner/repo",
                       "state_dir": str(self.state), "shared_dir": str(self.shared), "python": sys.executable,
                       "smtp": {"host": "smtp.example.com", "port": 587, "tls": "starttls",
                                "from": "ota@example.com", "to": ["admin@example.com"]}}
        self.source = self.shared / "ota/agent.local.json"
        atomic_json(self.source, self.config)
        self.new = self.state / "releases/test/app"
        self.new.mkdir(parents=True)
        atomic_json(self.new / "BUILD_INFO.json", self.info)
        atomic_json(self.new / "config.defaults.json", {"new_field": True})
        (self.new / "venv/bin").mkdir(parents=True)
        (self.new / "venv/bin/python").symlink_to(sys.executable)
        (self.new / "config.json").symlink_to(self.shared / "config.json")
        self.calls, self.sent = [], []
        self.running = {"facial_recognition.service": True}
        self.new_failure, self.restore_failure = False, False
        self.clock = 0
        self.stack = ExitStack()
        self.stack.enter_context(patch("ota.setup.Path.home", return_value=self.root))
        self.stack.enter_context(patch("ota.setup.systemctl", side_effect=self.control))
        self.stack.enter_context(patch("ota.install_agent.systemctl", side_effect=self.control))
        self.stack.enter_context(patch("ota.updater.systemctl", side_effect=self.control))
        self.stack.enter_context(patch("ota.updater.health", return_value={"pid": 123, "healthy": True,
            "core_ok": True, "external_ok": True, "camera_ok": True, "update_ready": True}))
        self.stack.enter_context(patch("ota.updater.Agent.identity", return_value={
            "device_id": self.config["device_id"], "role": "test", "project": "project", "location": "測試閘道"}))
        self.stack.enter_context(patch("ota.updater.Agent.releases", return_value=[
            {"tag_name": self.info["version"], "prerelease": True, "draft": False}]))
        self.stack.enter_context(patch("ota.updater.Agent.manifest", return_value={**self.info, "projects": ["project"]}))
        self.stack.enter_context(patch("ota.updater.Agent.stage", return_value=(self.new, self.info)))
        self.stack.enter_context(patch("ota.updater.Agent.wait_healthy", side_effect=self.check_health))
        self.stack.enter_context(patch("ota.updater.Agent.check", side_effect=self.check_health))
        self.stack.enter_context(patch("ota.notifications.send_mail", side_effect=lambda *args: self.sent.append(args)))
        self.stack.enter_context(patch("ota.setup.time.monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch("ota.setup.time.sleep", side_effect=self.advance))
        self.stack.enter_context(patch("builtins.print"))

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def advance(self, seconds):
        self.clock += seconds

    def working(self):
        directory = self.root / ".config/systemd/user/facial_recognition.service.d"
        value = str(self.shared)
        for path in sorted(directory.glob("*.conf")):
            for line in path.read_text().splitlines():
                if line.startswith("WorkingDirectory="):
                    value = line.split("=", 1)[1]
        return Path(value).resolve()

    def control(self, *args):
        self.calls.append(args)
        if args[0] == "is-active":
            return "active" if self.running.get(args[1]) else "inactive"
        if args[0] == "cat":
            return "[Service]\nWorkingDirectory=" + str(self.shared) + "\n"
        if args[0] == "show":
            if "--property=MainPID" in args:
                return "123" if self.running.get(args[1]) else "0"
            if "--property=WorkingDirectory" in args:
                # Return the lexical path, as systemd does, for the enrolled override check.
                override = self.root / ".config/systemd/user/facial_recognition.service.d/50-ota.conf"
                return str(self.state / "current") if override.is_file() else str(self.working())
            if "--property=ExecStart" in args:
                return str(self.state / "current/venv/bin/python")
        if args[0] in ("stop", "start"):
            for service in args[1:]:
                if self.restore_failure and args[0] == "start" and service == "facial_recognition.service" and self.working() == self.shared:
                    raise subprocess.CalledProcessError(1, ["systemctl"])
                self.running[service] = args[0] == "start"
        if args[0] == "--no-block" and args[1] == "start":
            self.running[args[2]] = True
        if args[0] == "enable" and "--now" in args:
            for service in args[2:]:
                self.running[service] = True
        return ""

    def check_health(self, info):
        if self.new_failure and info["build_id"] == "test-build":
            raise RuntimeError("injected startup failure")
        return {"healthy": True, "update_ready": True, "core_ok": True, "external_ok": True}

    def worker(self):
        attempt = read_json(self.state / "setup.json")["attempt_id"]
        path = self.state / "setup-tools" / attempt / "setup.local.json"
        return Setup(path)

    def start(self):
        self.assertEqual(self.state, launch(self.shared))
        return self.worker()

    def test_first_enrollment_preserves_site_and_automatically_enables_services(self):
        setup = self.start()
        self.assertTrue(setup.run())
        self.assertEqual("complete", read_json(self.state / "setup.json")["phase"])
        self.assertEqual(self.new, (self.state / "current").resolve())
        self.assertEqual(self.config["device_id"], read_json(self.state / "agent.json")["device_id"])
        self.assertEqual(self.site["Server"], read_json(self.shared / "config.json")["Server"])
        self.assertTrue(read_json(self.shared / "config.json")["new_field"])
        self.assertTrue(self.running["facial-ota-poll.service"])
        self.assertTrue(self.running["facial-ota-notify.service"])
        self.assertEqual([], self.sent)
        backup = Path(setup.data["backup_dir"])
        self.assertEqual(self.site, read_json(backup / "site-config.json"))
        self.assertTrue((backup / "original-service.txt").is_file())
        self.assertEqual(0o600, (backup / "site-config.json").stat().st_mode & 0o777)

    def test_production_never_bootstraps_rc_and_leaves_original_service_untouched(self):
        self.config["role"] = "production"
        atomic_json(self.source, self.config)
        setup = self.start()
        self.assertTrue(setup.run())
        self.assertEqual(self.site, read_json(self.shared / "config.json"))
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertFalse((self.state / "current").exists())
        self.assertEqual([], self.sent)
        self.assertEqual("waiting_release", setup.data["phase"])
        self.assertTrue(setup.agent.store.get("waiting_enrollment"))
        self.assertTrue(self.running["facial-ota-poll.service"])
        self.assertTrue(self.running["facial-ota-notify.service"])

    def waiting(self):
        self.config["role"] = "production"
        atomic_json(self.source, self.config)
        setup = self.start()
        self.assertTrue(setup.run())
        self.running[SERVICE] = False  # The independent worker returned/exited.
        return setup

    def stable(self):
        self.info["version"] = "v1.4.0"
        atomic_json(self.new / "BUILD_INFO.json", self.info)
        self.stack.enter_context(patch("ota.updater.Agent.releases", return_value=[
            {"tag_name": "v1.4.0", "prerelease": False, "draft": False}]))
        self.stack.enter_context(patch("ota.updater.Agent.manifest", return_value={**self.info, "projects": ["project"]}))

    def test_waiting_registration_does_not_need_github_or_create_fake_release_identity(self):
        with patch("ota.updater.Agent.releases", side_effect=OSError("offline")) as releases:
            setup = self.waiting()
        releases.assert_not_called()
        self.assertFalse((self.shared / "BUILD_INFO.json").exists())
        self.assertFalse(setup.targets["override"].exists())
        self.assertEqual({}, setup.agent.store.latest_summary())
        self.assertEqual(0, setup.agent.store.outstanding())
        self.assertEqual(0o600, (self.state / "agent.json").stat().st_mode & 0o777)

    def test_waiting_poll_records_rc_skip_without_manifest_download_or_service_switch(self):
        setup = self.waiting()
        self.calls.clear()
        with patch("ota.updater.Agent.manifest") as manifest, patch("ota.updater.Agent.stage") as stage:
            setup.agent.once()
        manifest.assert_not_called()
        stage.assert_not_called()
        result = setup.agent.status()
        self.assertEqual("waiting_release", result["enrollment_state"])
        self.assertTrue(result["last_poll"]["query_ok"])
        self.assertEqual(["v1.4.0-rc.2"], result["last_poll"]["rc_skipped"])
        self.assertEqual("waiting_release", result["last_poll"]["action"])
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertEqual(self.site, read_json(self.shared / "config.json"))
        self.assertEqual(0, setup.agent.store.outstanding())

    def test_waiting_ignores_drafts_rc_flag_mismatches_and_wrong_projects(self):
        setup = self.waiting()
        releases = [{"tag_name": tag, "prerelease": pre, "draft": draft} for tag, pre, draft in (
            ("v1.4.0-rc.9", False, False), ("v1.4.0", True, False), ("v1.4.1", False, True), ("v1.3.9", False, False))]
        with patch("ota.updater.Agent.releases", return_value=releases), \
             patch("ota.updater.Agent.manifest", return_value={"projects": ["another-project"]}) as manifest:
            setup.agent.once()
        self.assertEqual(1, manifest.call_count)
        self.assertEqual("waiting_release", setup.agent.store.get("last_poll")["action"])
        self.assertEqual(0, setup.agent.store.outstanding())

    def test_waiting_query_failure_is_not_an_update_failure_and_leaves_service_running(self):
        setup = self.waiting()
        with patch("ota.updater.Agent.releases", side_effect=OSError("do-not-print-secret-url")), self.assertRaises(OSError):
            setup.agent.once()
        result = setup.agent.store.get("last_poll")
        self.assertFalse(result["query_ok"])
        self.assertEqual("query_failed", result["action"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(0, setup.agent.store.outstanding())
        self.assertTrue(self.running["facial_recognition.service"])

    def test_waiting_monitor_does_not_report_absent_baseline_as_core_failure(self):
        setup = self.waiting()
        setup.agent.notify_once()
        self.assertEqual({}, setup.agent.store.get("incidents", {}))
        with patch("ota.updater.health", side_effect=OSError("legacy endpoint absent")):
            setup.agent.notify_once()
        self.assertTrue(setup.agent.store.get("last_health")["legacy_process_only"])
        self.assertEqual({}, setup.agent.store.get("incidents", {}))
        self.assertEqual([], self.sent)

    def test_waiting_camera_disconnect_is_not_an_update_failure(self):
        setup = self.waiting()
        with patch("ota.updater.health", return_value={"pid": 123, "core_ok": True,
                "external_ok": True, "camera_ok": False, "healthy": False, "update_ready": True,
                "cameras": {"0": False}}):
            setup.agent.notify_once()
            setup.agent.once()
        self.assertEqual({"camera"}, set(setup.agent.store.get("incidents")))
        self.assertEqual(0, setup.agent.store.outstanding())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        setup.agent.notify_once()
        self.assertEqual({}, setup.agent.store.get("incidents"))

    def test_first_formal_release_is_dispatched_to_independent_worker_and_enrolled(self):
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        data = read_json(self.state / "setup.json")
        self.assertEqual("requested", data["phase"])
        self.assertTrue(data["automatic"])
        self.assertEqual("v1.4.0", data["requested_version"])
        self.assertFalse((self.state / "current").exists())
        self.assertTrue(self.worker().run())
        self.assertEqual(self.new, (self.state / "current").resolve())
        self.assertIsNone(setup.agent.store.get("waiting_enrollment"))
        self.assertEqual("success", setup.agent.store.latest_summary()["phase"])
        self.assertEqual(self.site["Server"], read_json(self.shared / "config.json")["Server"])
        self.assertTrue(self.running["facial-ota-poll.service"])
        self.assertTrue(self.running["facial-ota-notify.service"])
        self.assertEqual([], self.sent)

    def test_first_formal_failure_restores_original_and_waiting_workers_without_retry_loop(self):
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        self.new_failure = True
        self.assertFalse(self.worker().run())
        self.running[SERVICE] = False
        self.assertEqual(self.shared, self.working())
        self.assertFalse((self.state / "current").exists())
        self.assertTrue(setup.agent.store.get("waiting_enrollment"))
        self.assertTrue(self.running["facial-ota-poll.service"])
        self.assertTrue(self.running["facial-ota-notify.service"])
        before = read_json(self.state / "setup.json")
        setup.agent.once()
        self.assertEqual(before, read_json(self.state / "setup.json"))
        self.assertEqual(1, setup.agent.store.outstanding())
        setup.agent.notify_once()
        self.assertEqual(1, len(self.sent))
        self.assertEqual("restored_unverified", self.sent[0][1]["rollback"])
        self.assertIn("failed_at", self.sent[0][1])

    def test_failed_manifest_is_reported_once_without_stopping_original_service(self):
        setup = self.waiting()
        self.stable()
        with patch("ota.updater.Agent.manifest", side_effect=ValueError("bad manifest")) as manifest:
            setup.agent.once()
            setup.agent.once()
        self.assertEqual(1, manifest.call_count)
        self.assertEqual(1, setup.agent.store.outstanding())
        self.assertEqual(self.shared, self.working())

    def test_interrupted_first_formal_install_restores_waiting_registration(self):
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        with patch("ota.setup.install", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.worker().run()
        self.assertFalse(self.worker().run())
        self.assertEqual(self.shared, self.working())
        self.assertTrue(setup.agent.waiting())
        self.assertTrue(self.running["facial-ota-poll.service"])
        self.assertEqual(self.site, read_json(self.shared / "config.json"))

    def test_waiting_rerun_updates_tools_without_reinstalling_or_stopping_app(self):
        self.waiting()
        self.calls.clear()
        self.assertTrue(self.start().run())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertEqual(self.config["device_id"], read_json(self.state / "agent.json")["device_id"])
        self.assertEqual(self.site, read_json(self.shared / "config.json"))

    def test_waiting_unit_files_are_valid_and_private(self):
        setup = self.waiting()
        units = [setup.targets[name] for name in ("poll", "notify")]
        result = subprocess.run(["systemd-analyze", "--user", "verify", *(str(unit) for unit in units)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        for unit in units:
            self.assertEqual(0o600, unit.stat().st_mode & 0o777)

    def test_manual_waiting_unit_changes_are_not_overwritten(self):
        setup = self.waiting()
        path = setup.targets["poll"]
        path.write_text(path.read_text() + "\n# manual settings\n")
        before = path.read_bytes()
        self.assertFalse(self.start().run())
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(self.shared, self.working())

    def test_first_same_version_may_be_adopted_but_older_releases_are_not_downloaded(self):
        self.stable()
        atomic_json(self.shared / "BUILD_INFO.json", self.info)
        setup = self.waiting()
        with patch("ota.updater.Agent.releases", return_value=[{"tag_name": "v1.3.9", "draft": False, "prerelease": False}]), \
             patch("ota.updater.Agent.manifest") as manifest:
            setup.agent.once()
        manifest.assert_not_called()
        setup.agent.once()
        self.assertEqual("requested", read_json(self.state / "setup.json")["phase"])
        self.assertTrue(self.worker().run())

    def test_waiting_explicit_retry_can_recover_after_host_problem_is_repaired(self):
        from ota.updater import main
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        self.new_failure = True
        self.assertFalse(self.worker().run())
        self.running[SERVICE] = False
        self.new_failure = False
        with patch("sys.argv", ["updater", "--config", str(self.state / "agent.json"), "retry", "--version", "v1.4.0"]):
            main()
        setup.agent.once()
        self.assertTrue(self.worker().run())
        self.assertEqual("", setup.agent.store.get("retry"))

    def test_waiting_role_change_does_not_bypass_stable_only_policy(self):
        setup = self.waiting()
        setup.agent.config["role"] = "test"
        with self.assertRaises(ValueError):
            setup.agent.once()
        self.assertFalse((self.state / "current").exists())

    def test_smtp_outage_does_not_keep_setup_active_or_block_later_stable_release(self):
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        self.new_failure = True
        self.assertFalse(self.worker().run())
        self.running[SERVICE] = False
        with patch("ota.notifications.send_mail", side_effect=OSError("SMTP unavailable")):
            setup.agent.notify_once()
        self.assertEqual(1, setup.agent.store.outstanding())
        with patch("ota.updater.Agent.releases", return_value=[{"tag_name": "v1.4.1", "draft": False, "prerelease": False}]), \
             patch("ota.updater.Agent.manifest", return_value={**self.info, "version": "v1.4.1", "projects": ["project"]}):
            setup.agent.once()
        self.assertEqual("v1.4.1", read_json(self.state / "setup.json")["requested_version"])

    def test_after_first_stable_enrollment_newer_rc_is_still_ignored(self):
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        self.assertTrue(self.worker().run())
        self.running[SERVICE] = False
        with patch("ota.updater.Agent.releases", return_value=[{"tag_name": "v1.4.1-rc.1", "draft": False, "prerelease": True}]), \
             patch("ota.updater.Agent.manifest") as manifest:
            setup.agent.once()
        manifest.assert_not_called()
        self.assertEqual("no_update", setup.agent.store.get("last_poll")["action"])
        self.assertEqual(["v1.4.1-rc.1"], setup.agent.store.get("last_poll")["rc_skipped"])
        self.assertEqual("v1.4.0", setup.agent.current()[1]["version"])

    def test_waiting_registration_interruption_restores_files_without_stopping_app(self):
        self.config["role"] = "production"
        atomic_json(self.source, self.config)
        setup = self.start()
        from ota.install_agent import waiting_install
        def interrupted(agent):
            waiting_install(agent)
            raise KeyboardInterrupt
        with patch("ota.setup.waiting_install", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            setup.run()
        self.assertFalse(self.worker().run())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertFalse(setup.targets["runtime"].exists())
        self.assertIsNone(setup.agent.store.get("waiting_enrollment"))

    def test_waiting_committed_checkpoint_finishes_after_restart_without_reinstall(self):
        self.config["role"] = "production"
        atomic_json(self.source, self.config)
        setup = self.start()
        with patch.object(setup, "finish", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            setup.run()
        self.assertEqual("committed", read_json(setup.journal)["phase"])
        with patch("ota.setup.waiting_install") as installer:
            self.assertTrue(self.worker().run())
        installer.assert_not_called()
        self.assertTrue(setup.agent.waiting())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)

    def test_waiting_partial_registration_is_recovered_without_stopping_original(self):
        self.config["role"] = "production"
        atomic_json(self.source, self.config)
        setup = self.start()
        from ota.install_agent import waiting_install
        def partial(agent):
            waiting_install(agent)
            raise RuntimeError("injected registration failure")
        with patch("ota.setup.waiting_install", side_effect=partial):
            self.assertFalse(setup.run())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertFalse(setup.targets["runtime"].exists())
        self.assertFalse(setup.targets["poll"].exists())
        self.assertIsNone(setup.agent.store.get("waiting_enrollment"))

    def test_waiting_rollback_failure_is_blocked_and_timestamped(self):
        setup = self.waiting()
        self.stable()
        setup.agent.once()
        self.new_failure = self.restore_failure = True
        self.assertFalse(self.worker().run())
        self.assertTrue(setup.agent.store.get("blocked"))
        self.assertIn("rollback_failed_at", setup.agent.store.latest_summary())
        self.running[SERVICE] = False
        before = read_json(self.state / "setup.json")
        setup.agent.once()
        self.assertEqual(before, read_json(self.state / "setup.json"))

    def test_early_preparation_failure_does_not_stop_original_service(self):
        setup = self.start()
        with patch("ota.updater.Agent.stage", side_effect=RuntimeError("invalid archive")):
            self.assertFalse(setup.run())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertEqual(self.site, read_json(self.shared / "config.json"))
        self.assertEqual("not_needed", setup.data["rollback"])

    def test_new_build_failure_restores_legacy_without_claiming_functional_health(self):
        setup = self.start()
        self.new_failure = True
        self.assertFalse(setup.run())
        self.assertEqual(self.shared, self.working())
        self.assertEqual(self.site, read_json(self.shared / "config.json"))
        self.assertFalse(setup.dropin.exists())
        self.assertEqual("restored_unverified", setup.data["rollback"])
        self.assertFalse(self.sent[0][1]["service_healthy"])

    def test_rollback_failure_records_time_and_blocks_updates(self):
        setup = self.start()
        self.new_failure = self.restore_failure = True
        self.assertFalse(setup.run())
        self.assertEqual("rollback_failed", setup.data["phase"])
        self.assertTrue(setup.agent.store.get("blocked"))
        self.assertIn("rollback_failed_at", self.sent[0][1])

    def test_model_change_is_rejected_before_stopping_original(self):
        setup = self.start()
        with patch("ota.updater.Agent.manifest", return_value={**self.info, "projects": ["project"], "feature_model_sha256": "0" * 64}):
            self.assertFalse(setup.run())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)

    def test_interrupted_switch_restores_original_on_worker_restart(self):
        setup = self.start()
        with patch("ota.setup.install", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                setup.run()
        self.assertEqual("installing", read_json(setup.journal)["phase"])
        restarted = self.worker()
        self.assertFalse(restarted.run())
        self.assertEqual(self.shared, self.working())
        self.assertEqual(self.site, read_json(self.shared / "config.json"))

    def test_interrupted_commit_completes_without_reinstalling(self):
        setup = self.start()
        with patch.object(setup, "finish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                setup.run()
        self.assertEqual("committed", read_json(setup.journal)["phase"])
        with patch("ota.setup.install") as installer:
            self.assertTrue(self.worker().run())
        installer.assert_not_called()
        self.assertEqual(self.new, (self.state / "current").resolve())

    def test_concurrent_launch_reuses_existing_job(self):
        self.start()
        before = read_json(self.state / "setup.json")
        launch(self.shared)
        self.assertEqual(before, read_json(self.state / "setup.json"))
        self.assertEqual(1, len(list(self.state.glob("setup-tools/*/setup.local.json"))))

    def test_rerun_refreshes_agent_without_stopping_app_or_losing_id(self):
        setup = self.start()
        self.assertTrue(setup.run())
        self.running[SERVICE] = False
        self.calls.clear()
        self.start()
        # Select the new independent runtime rather than the prior successful request.
        latest = read_json(self.state / "setup.json")["attempt_id"]
        setup = Setup(self.state / "setup-tools" / latest / "setup.local.json")
        self.assertTrue(setup.run())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertEqual(self.config["device_id"], read_json(self.state / "agent.json")["device_id"])

    def test_pending_setup_prevents_normal_polling_or_duplicate_incident(self):
        setup = self.start()
        setup.agent.once()
        setup.agent.monitor()
        self.assertEqual({}, setup.agent.store.latest_summary())
        self.assertEqual({}, setup.agent.store.get("incidents", {}))

    def test_setup_unit_syntax_and_private_independent_runtime(self):
        self.start()
        unit = self.root / ".config/systemd/user" / SERVICE
        result = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Restart=on-failure", unit.read_text())
        self.assertEqual(0o600, next(self.state.glob("setup-tools/*/setup.local.json")).stat().st_mode & 0o777)

    def test_partial_install_failure_restores_all_owned_files_and_original_service(self):
        setup = self.start()
        def partial(*args, **kwargs):
            setup.agent.point(self.new)
            (self.state / "agent").mkdir()
            atomic_json(self.state / "agent.json", self.config)
            for name in ("override", "poll", "notify"):
                setup.targets[name].parent.mkdir(parents=True, exist_ok=True)
                setup.targets[name].write_text("# partially installed")
            raise RuntimeError("partial service installation failed")
        with patch("ota.setup.install", side_effect=partial):
            self.assertFalse(setup.run())
        for name in ("runtime", "current", "override", "poll", "notify"):
            self.assertFalse(setup.targets[name].exists())
        self.assertEqual(self.site, read_json(self.shared / "config.json"))
        self.assertEqual(self.shared, self.working())
        self.assertIn(("reset-failed", "facial_recognition.service"), self.calls)

    def test_agent_refresh_failure_restores_old_runtime_without_stopping_app(self):
        self.assertTrue(self.start().run())
        self.running[SERVICE] = False
        self.calls.clear()
        setup = self.start()
        old_code = (self.state / "agent/ota/updater.py").read_bytes()
        def partial(*args, **kwargs):
            (self.state / "agent/ota/updater.py").write_text("# broken replacement")
            raise RuntimeError("agent replacement failed")
        with patch("ota.setup.install", side_effect=partial):
            self.assertFalse(setup.run())
        self.assertEqual(old_code, (self.state / "agent/ota/updater.py").read_bytes())
        self.assertEqual(self.new, (self.state / "current").resolve())
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)
        self.assertEqual("success", setup.data["rollback"])
        self.assertEqual("agent_upgrade", self.sent[-1][1]["operation"])

    def test_recovery_journal_failure_does_not_prevent_original_service_restoration(self):
        setup = self.start()
        self.new_failure = True
        original = setup.phase
        def journal(phase, **kwargs):
            if phase == "restoring":
                raise OSError("journal temporarily unavailable")
            return original(phase, **kwargs)
        with patch.object(setup, "phase", side_effect=journal):
            self.assertFalse(setup.run())
        self.assertEqual(self.shared, self.working())
        self.assertEqual(self.site, read_json(self.shared / "config.json"))

    def test_failed_setup_mail_survives_worker_restart_without_repeating_setup(self):
        setup = self.start()
        with patch("ota.updater.Agent.stage", side_effect=RuntimeError("download failed")), \
             patch("ota.notifications.send_mail", side_effect=OSError("SMTP offline")), \
             patch("ota.setup.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                setup.run()
        self.assertEqual("failed", read_json(self.state / "setup.json")["phase"])
        with setup.agent.store.connect() as db:
            db.execute("UPDATE outbox SET due=0")
        self.calls.clear()
        self.assertFalse(self.worker().run())
        self.assertEqual(1, len(self.sent))
        self.assertNotIn(("stop", "facial_recognition.service"), self.calls)

    def test_status_before_enrollment_is_readable_and_excludes_secrets(self):
        self.config["github_token"] = "do-not-print-this-token"
        atomic_json(self.source, self.config)
        result = subprocess.run(["bash", str(Path(__file__).parent.parent / "install.sh"), "--ota-status", "--config", str(self.source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        status = json.loads(result.stdout)
        self.assertIsNone(status["selected_build"])
        self.assertNotIn("do-not-print-this-token", result.stdout)

    def test_background_poll_reloads_atomic_configuration_on_next_iteration(self):
        from ota.updater import main
        observed = []
        def once(agent):
            observed.append(agent.config["smtp"]["host"])
            if len(observed) == 1:
                value = read_json(self.source)
                value["smtp"]["host"] = "new-smtp.example.com"
                atomic_json(self.source, value)
            else:
                raise KeyboardInterrupt
        with patch("sys.argv", ["updater", "--config", str(self.source), "poll"]), \
             patch("ota.updater.Agent.once", new=once), self.assertRaises(KeyboardInterrupt):
            main()
        self.assertEqual(["smtp.example.com", "new-smtp.example.com"], observed)

    def test_setup_mail_uses_live_installed_configuration_after_settings_change(self):
        setup = self.start()
        self.assertTrue(setup.run())
        live = self.state / "agent.json"
        value = read_json(live)
        value["smtp"]["host"] = "new-smtp.example.com"
        atomic_json(live, value)
        setup.agent.store.record("deferred", "failed", {"target_version": "v1.4.0-rc.3"}, setup.agent.identity())
        self.worker().run()
        self.assertEqual("new-smtp.example.com", self.sent[-1][0]["smtp"]["host"])

    def test_target_python_uses_matching_installed_minor(self):
        agent = Agent(self.source)
        with patch("ota.updater.shutil.which", side_effect=lambda name: "/usr/bin/" + name), \
             patch("ota.updater.subprocess.check_output", side_effect=["3.10\nx86_64\n", "3.12\nx86_64\n"]):
            self.assertEqual("/usr/bin/python3.12", agent.target_python({"python": "3.12", "architecture": "x86_64"}))

    def test_symlinked_app_venv_must_not_be_used_as_independent_agent_python(self):
        app_python = self.new / "venv/bin/python"
        with self.assertRaises(ValueError):
            independent_python(str(app_python), self.shared, self.state)


class ProvisioningTests(unittest.TestCase):
    def test_settings_in_another_project_are_never_selected_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / ".local/share/facial-ota/agent.json"
            atomic_json(installed, {"shared_dir": str(root / "other-project"), "device_id": str(uuid.uuid4())})
            before = installed.read_bytes()
            with patch("ota.configure.Path.home", return_value=root), self.assertRaises(ValueError):
                configure(root / "new-project")
            self.assertEqual(before, installed.read_bytes())

    def test_export_profile_excludes_host_identity_and_cannot_overwrite_device_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ota/agent.local.json"
            atomic_json(source, {"device_id": str(uuid.uuid4()), "state_dir": str(root / "state"),
                "shared_dir": str(root), "role": "test", "repo": "owner/repo", "github_token": "private",
                "smtp": {"host": "smtp.example.com"}})
            profile = root / "fleet.local.json"
            export_profile(root, profile)
            self.assertEqual({"role", "repo", "github_token", "smtp"}, set(read_json(profile)))
            self.assertEqual(0o600, profile.stat().st_mode & 0o777)
            before = source.read_bytes()
            with self.assertRaises(ValueError):
                export_profile(root, source)
            self.assertEqual(before, source.read_bytes())

    def test_repository_detection_without_exposing_remote_credentials(self):
        for remote in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git",
                       "ssh://git@github.com/owner/repo.git", "https://private-token@github.com/owner/repo.git"):
            with patch("ota.configure.subprocess.check_output", return_value=remote):
                self.assertEqual("owner/repo", repository("."))

    def test_profile_creates_distinct_ids_without_site_config_or_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "fleet.local.json"
            atomic_json(profile, {"role": "production", "repo": "owner/repo", "github_token": "private",
                "smtp": {"host": "smtp.example.com", "tls": "ssl", "from": "ota@example.com", "to": ["admin@example.com"]}})
            ids = []
            for name in ("a", "b"):
                project = root / name
                project.mkdir()
                with patch("ota.configure.Path.home", return_value=project), patch("ota.configure.prompt") as ask:
                    target = configure(project, profile=profile, interactive=False)
                ask.assert_not_called()
                value = read_json(target)
                ids.append(value["device_id"])
                self.assertEqual(465, value["smtp"]["port"])
                self.assertEqual("production", value["role"])
                self.assertFalse((project / "config.json").exists())
            self.assertNotEqual(*ids)

    def test_profile_cannot_clone_device_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "bad.local.json"
            atomic_json(profile, {"device_id": str(uuid.uuid4())})
            with self.assertRaises(ValueError):
                configure(root, profile=profile, interactive=False)


if __name__ == "__main__":
    unittest.main()
