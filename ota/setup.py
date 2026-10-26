"""One-command enrollment, persistent recovery and independent background setup."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from ota.configure import configuration_path, configure
from ota.core import (atomic_json, atomic_text, digest, error_summary, file_lock,
                      migrate_config, read_json, select_release, sync_directory, sync_tree, timestamp, version_key)
from ota.install_agent import install, waiting_install
from ota.notifications import mail_once
from ota.updater import Agent, independent_python, systemctl, update_ready

SERVICE = "facial-ota-setup.service"
TERMINAL = ("complete", "waiting_release", "failed", "rollback_failed")
MODEL = "models/data/20180402-114759-vggface2.pt"
IGNORE = shutil.ignore_patterns("__pycache__", "test_*", "*.local.json", "dist", "wheelhouse")


def property_value(service, name):
    return systemctl("show", service, "--property=" + name, "--value")


def active(service):
    try:
        return systemctl("is-active", service) in ("active", "activating", "reloading")
    except subprocess.CalledProcessError:
        return False


def safe_service_paths(*paths):
    for path in paths:
        if any(character.isspace() or character in '%"\\' for character in str(path)):
            raise ValueError("Service paths must not contain whitespace or systemd specifiers")


class Setup:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.agent = Agent(config_path)
        self.journal = self.agent.root / "setup.json"
        self.data = read_json(self.journal)
        self.units = Path.home() / ".config/systemd/user"
        self.dropin = self.units / "facial_recognition.service.d/40-ota-bootstrap.conf"
        self.targets = {"bootstrap": self.dropin, "runtime": self.agent.root / "agent",
                        "current": self.agent.root / "current", "config": self.agent.root / "agent.json",
                        "override": self.units / "facial_recognition.service.d/50-ota.conf",
                        **{action: self.units / ("facial-ota-" + action + ".service") for action in ("poll", "notify")}}

    def phase(self, phase, **values):
        value = {**self.data, **values, "phase": phase}
        atomic_json(self.journal, value)
        self.data = value
        print("OTA setup:", phase, self.data.get("target_version", ""), flush=True)

    def snapshot(self):
        backup = self.agent.root / "enrollment" / self.data["attempt_id"]
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshots = {}
        for name, target in self.targets.items():
            saved = backup / name
            if target.is_symlink():
                snapshots[name] = {"kind": "link", "target": os.readlink(target)}
            elif target.is_dir():
                shutil.copytree(target, saved, symlinks=True)
                snapshots[name] = {"kind": "directory"}
            elif target.is_file():
                atomic_text(saved, target.read_text(encoding="utf-8"))
                snapshots[name] = {"kind": "file"}
            else:
                snapshots[name] = {"kind": "absent"}
        atomic_json(backup / "site-config.json", read_json(self.agent.shared / "config.json"))
        atomic_text(backup / "original-service.txt", systemctl("cat", self.agent.service))
        sync_tree(backup)
        self.data.update(backup_dir=str(backup), snapshots=snapshots)

    def select(self):
        releases = self.agent.releases()
        if self.data.get("automatic"):
            releases = [release for release in releases if release.get("tag_name") == self.data["requested_version"]]
        project = self.agent.identity()["project"]
        if project == "unknown":
            raise ValueError("Cannot determine project; check Server.API_url and location_ID")
        while candidate := select_release(releases, self.agent.config["role"], "v0.0.0"):
            manifest = self.agent.manifest(candidate)
            if "*" in manifest["projects"] or project in manifest["projects"]:
                return candidate, manifest
            releases.remove(candidate)
        raise RuntimeError("No published Release applies to this role/project; complete Release assets are required")

    def original(self):
        agent = self.agent
        if not active(agent.service) or int(property_value(agent.service, "MainPID")) <= 0:
            raise RuntimeError("Original recognition service must be running before enrollment")
        working = Path(property_value(agent.service, "WorkingDirectory")).resolve(strict=True)
        if (working / "config.json").resolve() != (agent.shared / "config.json").resolve():
            raise ValueError("Original service does not use the configured shared config.json")
        safe_service_paths(agent.root, working, self.units)
        old_info = read_json(working / "BUILD_INFO.json") if (working / "BUILD_INFO.json").is_file() else None
        if old_info:
            version_key(old_info["version"])
            self.data.update(old_info=old_info, from_version=old_info["version"])
        return working

    def validate_waiting(self):
        agent = self.agent
        owned = agent.store.get("waiting_enrollment")
        if owned:
            if agent.config["role"] != "production" or not self.targets["runtime"].is_dir() or self.targets["runtime"].is_symlink():
                raise ValueError("Waiting enrollment identity/runtime mismatch")
            for name in ("poll", "notify"):
                content = self.targets[name].read_text()
                if (not content.startswith("# Managed by facial-ota\n") or str(agent.root / "agent.json") not in content
                        or digest(self.targets[name]) != owned.get("unit_sha256", {}).get(name)):
                    raise ValueError("Unowned waiting service; do not overwrite manual settings")
        allowed = {"config", "runtime", "poll", "notify"} if owned else {"config"}
        if any(path.exists() or path.is_symlink() for name, path in self.targets.items() if name not in allowed):
            raise FileExistsError("Incomplete/manual OTA installation detected; preserve existing files and repair it first")
        return owned

    def register_waiting(self):
        agent = self.agent
        self.phase("preparing", mode="waiting_registration", from_version="legacy", target_version="unavailable",
                   waiting_before=agent.store.get("waiting_enrollment"))
        if agent.store.get("blocked"):
            raise RuntimeError("Repair the blocked installation before maintaining waiting tools")
        owned = self.validate_waiting()
        working = self.original()
        with file_lock(str((agent.shared / "config.json").resolve()) + ".lock"):
            self.snapshot()
            self.phase("switching", waiting_descriptor={"working_dir": str(working),
                       "version": self.data.get("old_info", {}).get("version", "legacy")})
            if owned:
                systemctl("stop", "facial-ota-poll.service", "facial-ota-notify.service")
            waiting_install(agent)
            self.data["waiting_descriptor"]["unit_sha256"] = {name: digest(self.targets[name]) for name in ("poll", "notify")}
            self.phase("committed")

    def enroll(self):
        agent = self.agent
        self.phase("preparing", from_version="legacy", target_version=self.data.get("requested_version", "unavailable"),
                   mode="enrollment", waiting_before=agent.store.get("waiting_enrollment"))
        owned = self.validate_waiting()
        working = self.original()
        release, manifest = self.select()
        self.phase("preparing", target_version=release["tag_name"])
        old_info = self.data.get("old_info")
        if old_info and version_key(release["tag_name"]) < version_key(old_info["version"]):
            raise ValueError("Enrollment must not downgrade the running packaged version")
        if not (working / MODEL).is_file() or digest(working / MODEL) != manifest.get("feature_model_sha256"):
            raise ValueError("Feature model change/unknown identity requires an explicit data migration")
        new, info = agent.stage(release, manifest, self.data["attempt_id"], compare_baseline=False)
        self.data.update(new_path=str(new), new_info=info)
        config_path = (agent.shared / "config.json").resolve()
        with file_lock(str(config_path) + ".lock"):
            migrations = new / "config.migrations.json"
            merged = migrate_config(read_json(new / "config.defaults.json"), read_json(config_path),
                                    read_json(migrations) if migrations.exists() else [])
            self.snapshot()
            self.phase("switching")  # Durable before the first stop/config/service mutation.
            systemctl("stop", agent.service)
            atomic_json(config_path, merged)
            atomic_text(self.dropin, "[Service]\nWorkingDirectory=" + str(new) + "\nExecStart=\nExecStart="
                        + str(new / "venv/bin/python") + " -u " + str(new / "main.py")
                        + "\nEnvironment=FACIAL_OTA_HEALTH_PORT=" + str(agent.port) + "\n")
            systemctl("daemon-reload")
            systemctl("start", agent.service)
            agent.wait_healthy(info)
            self.phase("installing")
            if owned:
                systemctl("stop", "facial-ota-poll.service", "facial-ota-notify.service")
            install(self.config_path, new, upgrade=bool(owned), _locked=True)
            self.phase("committed")

    def upgrade_agent(self):
        agent = self.agent
        _, info = agent.current()
        if not update_ready(agent.check(info)):
            raise RuntimeError("Repair the running service before maintaining the updater")
        if not (agent.root / "agent").is_dir() or not all(self.targets[name].is_file() for name in ("override", "poll", "notify")):
            raise FileExistsError("Incomplete agent installation; automatic replacement refused")
        self.phase("preparing", mode="agent_upgrade", old_info=info, from_version=info["version"],
                   target_version=info["version"], new_info=info, new_path=str(agent.current()[0]))
        with file_lock(str((agent.shared / "config.json").resolve()) + ".lock"):
            self.snapshot()
            self.phase("switching")
            systemctl("stop", "facial-ota-poll.service", "facial-ota-notify.service")
            install(self.config_path, upgrade=True, _locked=True)
            self.phase("committed")

    def finish(self, recovering=False):
        agent = self.agent
        if self.data["mode"] == "waiting_registration":
            agent.store.set("waiting_enrollment", self.data["waiting_descriptor"])
            systemctl("enable", "--now", "facial-ota-poll.service", "facial-ota-notify.service")
            self.phase("waiting_release", rollback="not_needed")
            return
        status = agent.wait_healthy(self.data["new_info"]) if recovering else agent.check(self.data["new_info"])
        if not update_ready(status):
            raise RuntimeError("Baseline became unhealthy before enabling OTA")
        if self.data["mode"] == "enrollment" and self.dropin.exists():
            self.dropin.rename(Path(self.data["backup_dir"]) / ("bootstrap-complete-" + str(uuid.uuid4()) + ".conf"))
            sync_directory(self.dropin.parent)
            sync_directory(self.data["backup_dir"])
            systemctl("daemon-reload")
        systemctl("enable", "--now", "facial-ota-poll.service", "facial-ota-notify.service")
        agent.store.set("blocked", False)
        agent.store.set("waiting_enrollment", None)
        agent.store.record(self.data["attempt_id"], "success", {**self.data, "operation": self.data["mode"],
            "running_version": self.data["target_version"], "service_healthy": status.get("healthy") is True,
            "rollback": "not_needed"}, agent.identity())
        self.phase("complete", rollback="not_needed")

    def restore(self):
        agent = self.agent
        self.data.setdefault("rollback_started_at", timestamp())
        try:
            self.phase("restoring")
        except Exception:
            print("Setup recovery journal unavailable; continuing original-service restoration", flush=True)
        backup = Path(self.data["backup_dir"])
        if self.data["mode"] == "enrollment":
            systemctl("stop", agent.service)
        for name in ("poll", "notify"):
            if self.targets[name].exists():
                try:
                    systemctl("stop", self.targets[name].name)
                    if self.data["snapshots"][name]["kind"] == "absent":
                        systemctl("disable", self.targets[name].name)
                except subprocess.CalledProcessError:
                    # A unit may not have loaded yet after an interrupted install.
                    # The setup journal still prevents its poll loop from updating.
                    pass
        for name, target in self.targets.items():
            if self.data["mode"] in ("agent_upgrade", "waiting_registration") and name in ("bootstrap", "current"):
                continue  # Never disturb the running application's current path during agent maintenance.
            snapshot = self.data["snapshots"][name]
            if target.exists() or target.is_symlink():
                target.rename(backup / ("replaced-" + name + "-" + str(uuid.uuid4())))
                sync_directory(target.parent)
            kind = snapshot["kind"]
            if kind == "file":
                atomic_text(target, (backup / name).read_text(encoding="utf-8"))
            elif kind == "directory":
                shutil.copytree(backup / name, target, symlinks=True)
            elif kind == "link":
                target.symlink_to(snapshot["target"])
                sync_directory(target.parent)
        if self.data["mode"] == "enrollment":
            atomic_json((agent.shared / "config.json").resolve(), read_json(backup / "site-config.json"))
        systemctl("daemon-reload")
        if self.data["mode"] in ("enrollment", "waiting_registration"):
            agent.store.set("waiting_enrollment", self.data.get("waiting_before"))
        if self.data["mode"] == "enrollment":
            systemctl("reset-failed", agent.service)
            systemctl("start", agent.service)
        if self.data["mode"] == "agent_upgrade" or self.data.get("waiting_before"):
            systemctl("enable", "--now", "facial-ota-poll.service", "facial-ota-notify.service")
        if self.data.get("old_info"):
            value = agent.wait_healthy(self.data["old_info"])
            self.data.update(rollback="success", service_healthy=value.get("healthy") is True)
        else:
            # Legacy builds have no functional health signal. Verify only process restoration.
            deadline, stable_since, previous = time.monotonic() + 180, None, None
            while time.monotonic() < deadline:
                pid = property_value(agent.service, "MainPID")
                if active(agent.service) and int(pid) > 0:
                    stable_since = time.monotonic() if pid != previous else stable_since
                    if stable_since is not None and time.monotonic() - stable_since >= 10:
                        break
                else:
                    stable_since = None
                previous = pid
                time.sleep(2)
            else:
                raise RuntimeError("Original recognition process did not remain running after restoration")
            self.data.update(rollback="restored_unverified", service_healthy=False)
        self.data["rollback_completed_at"] = timestamp()

    def fail(self, error):
        self.data.setdefault("failed_at", timestamp())
        self.data.setdefault("error", error_summary(error))
        changed = self.data["phase"] in ("switching", "installing", "committed", "restoring")
        phase = "failed"
        if changed:
            try:
                with file_lock(str((self.agent.shared / "config.json").resolve()) + ".lock"):
                    self.restore()
            except Exception as rollback_error:
                self.data.update(rollback="failed", rollback_error=error_summary(rollback_error),
                                 rollback_failed_at=timestamp(), service_healthy=False)
                self.agent.store.set("blocked", True)
                phase = "rollback_failed"
        else:
            self.data.update(rollback="not_needed", service_healthy=False)
        self.agent.store.record(self.data["attempt_id"], phase, {**self.data, "operation": self.data.get("mode", "enrollment"),
            "running_version": self.data.get("from_version", "unknown") if phase == "failed" else "unknown"},
            self.agent.identity())
        self.phase(phase)

    def run(self):
        agent = self.agent
        with file_lock(agent.root / "update.lock", blocking=False), \
             file_lock(agent.root / "notify.lock", blocking=False), \
             file_lock(agent.root / "report.lock", blocking=False):
            phase = self.data["phase"]
            if phase == "requested":
                try:
                    if (agent.root / "current").is_symlink():
                        self.upgrade_agent()
                    elif agent.config["role"] == "production" and not self.data.get("automatic"):
                        self.register_waiting()
                    else:
                        self.enroll()
                    self.finish()
                except Exception as error:
                    self.fail(error)
            elif phase == "committed":
                try:
                    self.finish(recovering=True)
                except Exception as error:
                    self.fail(error)
            elif phase not in TERMINAL:
                self.fail(RuntimeError("Interrupted initial setup; restoring original service"))
        # An enrolled waiting notify worker owns mail retry; do not keep setup active
        # during an SMTP outage and thereby block a later eligible Release.
        if agent.store.get("waiting_enrollment") and active("facial-ota-notify.service"):
            systemctl("disable", SERVICE)
            return self.data["phase"] in ("complete", "waiting_release")
        # Leave recovery enabled until failed mail is durably accepted, even across reboot.
        while agent.store.outstanding():
            live = agent.root / "agent.json"
            source = live if live.is_file() else Path(self.data.get("source_config", str(self.config_path)))
            latest = read_json(source if source.is_file() else self.config_path)
            try:
                with file_lock(agent.root / "notify.lock", blocking=False):
                    mail_once(latest, agent.store)
            except BlockingIOError:
                pass
            if agent.store.outstanding():
                time.sleep(15)
        systemctl("disable", SERVICE)
        return self.data["phase"] == "complete"


def launch(project, config_path=None, profile=None, role=None, automatic=False, version=None):
    os.umask(0o077)
    selected = configuration_path(project, config_path)
    if profile or role:
        selected = configure(project, config_path, profile=profile, role=role, interactive=False)
    elif not selected.is_file():
        selected = configure(project, config_path)
    config_path = selected
    agent = Agent(config_path)
    if agent.config["role"] == "development":
        raise ValueError("Development hosts do not enroll in field OTA")
    unit = Path.home() / ".config/systemd/user" / SERVICE
    safe_service_paths(agent.root, unit.parent, config_path)
    python = independent_python(agent.config.get("python", "python3"), agent.shared, agent.root)
    safe_service_paths(python)
    with file_lock(agent.root / "setup-launch.lock", blocking=False):
        journal = agent.root / "setup.json"
        data = read_json(journal) if journal.is_file() else {}
        if automatic and (active(SERVICE) or agent.store.get("blocked") or (agent.root / "current").is_symlink()):
            return agent.root
        if automatic and (not agent.store.get("waiting_enrollment") or agent.config["role"] != "production" or not version_key(version)[3]):
            raise ValueError("Automatic first enrollment requires an owned production waiting installation and stable tag")
        if active(SERVICE) and data.get("phase") not in TERMINAL:
            print("首次接入正在執行，沿用同一作業。")
            return agent.root
        if active(SERVICE):
            systemctl("stop", SERVICE)
        if data and data.get("phase") not in TERMINAL:
            # Resume the same independent runtime/journal after interruption.
            systemctl("enable", SERVICE)
            systemctl("--no-block", "start", SERVICE)
            return agent.root
        attempt = str(uuid.uuid4())
        tools = agent.root / "setup-tools" / attempt
        shutil.copytree(Path(__file__).parent, tools / "ota", ignore=IGNORE)
        config_copy = tools / "setup.local.json"
        atomic_json(config_copy, agent.config)
        atomic_text(unit, "[Unit]\nDescription=Facial OTA automatic enrollment and recovery\nAfter=network.target\n\n"
                    + "[Service]\nType=simple\nWorkingDirectory=" + str(tools) + "\nExecStart=" + python
                    + " -m ota.setup --worker --config " + str(config_copy)
                    + "\nRestart=on-failure\nRestartSec=30\nTimeoutStartSec=0\nUMask=0077\n\n[Install]\nWantedBy=default.target\n")
        atomic_json(journal, {"attempt_id": attempt, "phase": "requested", "from_version": "unknown",
                              "target_version": "unavailable", "source_config": str(config_path),
                              "automatic": automatic, **({"requested_version": version} if automatic else {})})
        systemctl("daemon-reload")
        systemctl("enable", SERVICE)
        systemctl("--no-block", "start", SERVICE)
    return agent.root


def wait(root):
    previous = None
    while True:
        data = read_json(Path(root) / "setup.json")
        if data["phase"] != previous:
            print("OTA:", data["phase"], data.get("target_version", ""), flush=True)
            previous = data["phase"]
        if data["phase"] in TERMINAL:
            if data["phase"] == "waiting_release":
                print("OTA 輪詢與通知已啟用；等待適用正式版，現有辨識服務不變。")
                return True
            if data["phase"] == "complete":
                print("OTA 已啟用；後續版本由主機自動更新。")
                return True
            print("接入失敗:", data.get("error", "unknown"), "回退:", data.get("rollback", "unknown"))
            print("原始設定與服務備份:", data.get("backup_dir", "尚未切換"))
            return False
        if not active(SERVICE):
            raise RuntimeError("Setup worker is not running; inspect facial-ota-setup.service and rerun --ota-setup")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--config")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--status", action="store_true", help="Show setup/update status without changing services")
    parser.add_argument("--profile", help="Import a reusable private profile without prompts")
    parser.add_argument("--role", choices=("test", "production"))
    args = parser.parse_args()
    if args.status:
        import json
        config = configuration_path(args.project, args.config)
        if not config.is_file():
            raise SystemExit("尚未設定 OTA；請先執行 --ota-config-only 或 --ota-setup。")
        print(json.dumps(Agent(config).status(), ensure_ascii=False, indent=2))
    elif args.worker:
        Setup(args.config).run()
    else:
        raise SystemExit(0 if wait(launch(args.project, args.config, args.profile, args.role)) else 1)


if __name__ == "__main__":
    main()
