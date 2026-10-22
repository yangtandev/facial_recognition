"""Read-only acceptance tests; no live services, downloads, SMTP or cameras."""
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from ota.core import atomic_json, Store
from ota.diagnostics import APP, SERVICES, CheckFailed, check, inspect_health, main, preflight, services


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.project, self.state = self.root / "project", self.root / "state"
        self.project.mkdir()
        self.release = self.state / "releases/one/app"
        self.release.mkdir(parents=True)
        self.config = self.project / "ota/agent.local.json"
        self.settings = {"state_dir": str(self.state), "shared_dir": str(self.project), "role": "test",
                         "github_token": "SECRET-PAT", "smtp": {"password": "SECRET-SMTP"}, "health_port": 18765}
        atomic_json(self.config, self.settings)
        atomic_json(self.state / "setup.json", {"phase": "complete"})
        self.info = {"version": "v1.4.0-rc.2", "build_id": "rc2:commit"}
        atomic_json(self.release / "BUILD_INFO.json", self.info)
        (self.state / "current").symlink_to(self.release)
        self.store = Store(self.state / "agent.sqlite3")
        self.store.record("one", "success", {"target_version": self.info["version"]}, {"device_id": "test", "role": "test"})
        self.units = {name: {"ActiveState": "active", "UnitFileState": "enabled", "MainPID": "321", "NRestarts": "0"}
                      for name in SERVICES}
        self.first = {**self.info, "pid": 321, "boot_id": "one-boot", "observed_at": time.time(),
                      "update_ready": True, "core_ok": True, "external_ok": True, "camera_ok": True,
                      "checks": {name: True for name in ("config", "models", "index", "inference", "mqtt_connected", "mqtt_subscribed", "sync")},
                      "live": {name: True for name in ("gui", "capture:0", "detector:0", "comparison:0")},
                      "counters": {name: 10 for name in ("gui", "capture:0", "detector:0", "comparison:0")}}
        self.second = {**copy.deepcopy(self.first), "counters": {name: 20 for name in self.first["counters"]}}
        self.stack.enter_context(patch("ota.diagnostics.services", return_value=self.units))
        self.fetch = self.stack.enter_context(patch("ota.diagnostics.health", side_effect=lambda port: self.samples.pop(0)))
        self.stack.enter_context(patch("ota.diagnostics.time.sleep"))
        self.samples = [self.first, self.second]

    def run_check(self, **kwargs):
        return check(self.project, self.config, **kwargs)

    def test_success_reads_custom_port_without_state_or_config_changes(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = self.run_check(expect_version="v1.4.0-rc.2", expect_role="test")
        self.assertEqual("pass", result["result"])
        self.assertEqual(321, result["pid"])
        self.assertEqual([(18765,), (18765,)], [call.args for call in self.fetch.call_args_list])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertNotIn("SECRET", json.dumps(result))

    def test_camera_offline_is_warning_not_ota_failure(self):
        self.second["camera_ok"] = False
        result = self.run_check()
        self.assertEqual("warning", result["result"])
        self.assertTrue(result["update_ready"])

    def test_automatically_resolves_enrolled_settings(self):
        atomic_json(self.state / "agent.json", self.settings)
        self.assertEqual("pass", check(self.project)["result"])

    def test_rejects_service_restart_even_when_main_pid_is_reused(self):
        later = copy.deepcopy(self.units)
        later[APP]["NRestarts"] = "1"
        with patch("ota.diagnostics.services", side_effect=[self.units, later]), self.assertRaises(CheckFailed):
            self.run_check()

    def test_rejects_build_change_during_observation(self):
        with patch("ota.diagnostics.time.sleep", side_effect=lambda seconds:
                   atomic_json(self.release / "BUILD_INFO.json", {**self.info, "build_id": "different"})):
            with self.assertRaises(CheckFailed):
                self.run_check()

    def test_pending_failure_mail_is_visible_and_not_acknowledged(self):
        self.store.record("old-fail", "failed", {"target_version": "v1.3.9"}, {"device_id": "test", "role": "test"})
        self.store.record("one", "success", {"target_version": self.info["version"]}, {"device_id": "test", "role": "test"})
        self.assertEqual("warning", self.run_check()["result"])
        self.assertEqual(1, self.store.outstanding())

    def test_missing_database_is_not_created(self):
        self.settings["state_dir"] = str(self.root / "missing-state")
        atomic_json(self.config, self.settings)
        with self.assertRaises(OSError):
            self.run_check()
        self.assertFalse((self.root / "missing-state").exists())

    def test_missing_database_after_journal_and_current_is_not_created(self):
        other = self.root / "no-database"
        other.mkdir()
        atomic_json(other / "setup.json", {"phase": "complete"})
        (other / "current").symlink_to(self.release)
        self.settings["state_dir"] = str(other)
        atomic_json(self.config, self.settings)
        with self.assertRaises(sqlite3.OperationalError):
            self.run_check()
        self.assertFalse((other / "agent.sqlite3").exists())

    def test_rejects_unfinished_setup_blocked_failed_attempt_and_wrong_target(self):
        atomic_json(self.state / "setup.json", {"phase": "switching"})
        with self.assertRaises(CheckFailed):
            self.run_check()
        atomic_json(self.state / "setup.json", {"phase": "complete"})
        self.store.set("blocked", True)
        with self.assertRaises(CheckFailed):
            self.run_check()
        self.store.set("blocked", False)
        self.store.record("failed", "failed", {"target_version": "v1.4.0-rc.3"}, {"device_id": "test", "role": "test"})
        with self.assertRaises(CheckFailed):
            self.run_check()
        with self.assertRaises(CheckFailed):
            self.run_check(expect_version="v1.4.0")

    def test_production_cannot_accept_rc_and_role_must_match(self):
        with self.assertRaises(CheckFailed):
            self.run_check(expect_role="production")
        self.settings["role"] = "production"
        atomic_json(self.config, self.settings)
        with self.assertRaises(CheckFailed):
            self.run_check()

    def test_approved_stable_identity_accepted_on_production(self):
        self.settings["role"] = "production"
        atomic_json(self.config, self.settings)
        self.info.update(version="v1.4.0", build_id="stable:commit")
        atomic_json(self.release / "BUILD_INFO.json", self.info)
        for value in self.samples:
            value.update(self.info)
        self.store.record("stable", "success", {"target_version": "v1.4.0"}, {"device_id": "test", "role": "production"})
        self.assertEqual("pass", self.run_check(expect_role="production", expect_version="v1.4.0")["result"])

    def test_rejects_wrong_pid_identity_stale_health_failed_mqtt_and_bad_counters(self):
        for update in ({"pid": 999}, {"version": "unknown"}, {"build_id": "wrong"}, {"boot_id": ""},
                       {"observed_at": 0}, {"observed_at": float("nan")}, {"update_ready": False},
                       {"external_ok": False}, {"checks": {}}, {"live": {}}, {"counters": {"gui": True}}):
            with self.subTest(update=update), self.assertRaises(CheckFailed):
                inspect_health({**self.first, **update}, self.info, self.units)

    def test_rejects_restart_stuck_workers_or_changed_worker_set(self):
        for update in ({"boot_id": "new"}, {"counters": self.first["counters"]}, {"live": {"gui": True}}):
            self.samples = [self.first, {**self.second, **update}]
            with self.subTest(update=update), self.assertRaises(CheckFailed):
                self.run_check()

    def test_limits_observation_delay(self):
        for delay in (0, 31, float("nan"), float("inf")):
            with self.subTest(delay=delay), self.assertRaises(CheckFailed):
                self.run_check(seconds=delay)

    def test_service_properties_all_must_be_active_and_persistently_enabled(self):
        output = "ActiveState=active\nUnitFileState=enabled\nMainPID=321\nNRestarts=0\n"
        for bad in ("ActiveState=inactive", "UnitFileState=disabled", "UnitFileState=enabled-runtime", "MainPID=0"):
            lines = [line for line in output.splitlines() if not line.startswith(bad.split("=")[0] + "=")]
            with self.subTest(bad=bad), patch("ota.diagnostics.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "\n".join(lines + [bad]))):
                with self.assertRaises(CheckFailed):
                    services()

    def test_cli_redacts_untrusted_exceptions_and_returns_failure(self):
        for error in (ValueError("SECRET-PAT"), subprocess.CalledProcessError(1, "SECRET-SMTP")):
            output = io.StringIO()
            with patch("sys.argv", ["diagnostics", "check"]), patch("ota.diagnostics.check", side_effect=error), redirect_stdout(output):
                self.assertEqual(1, main())
            self.assertNotIn("SECRET", output.getvalue())

    def test_preflight_reports_existing_config_without_installing_or_leaking_it(self):
        atomic_json(self.project / "config.json", {"password": "SECRET"})
        with patch("ota.diagnostics.os.geteuid", return_value=1000), patch("ota.diagnostics.shutil.which", return_value=None):
            value = preflight(self.project)
        self.assertEqual("info", value["result"])
        self.assertTrue(value["config_exists"])
        self.assertNotIn("SECRET", json.dumps(value))

    def test_shell_entrypoints_are_read_only_and_forward_arguments(self):
        installer = Path(__file__).resolve().parent.parent / "install.sh"
        before = self.config.read_bytes()
        for arguments, code in ((["--preflight", "--project", str(self.project)], int(os.geteuid() == 0)),
                                (["--ota-check", "--config", str(self.root / "not-created.json")], 1)):
            result = subprocess.run(["bash", str(installer), *arguments], capture_output=True, text=True, timeout=10)
            self.assertEqual(code, result.returncode, result.stderr)
            output = json.loads(result.stdout)
            self.assertNotIn("SECRET", result.stdout)
            if arguments[0] == "--preflight":
                self.assertEqual(str(self.project), output["project"])
            self.assertEqual(before, self.config.read_bytes())
        self.assertFalse((self.root / "not-created.json").exists())


if __name__ == "__main__":
    unittest.main()
