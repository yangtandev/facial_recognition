"""Enroll a verified baseline; install independent user services without sudo."""
import argparse
from contextlib import nullcontext
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from ota.core import atomic_json, atomic_text, file_lock, read_json, sync_directory, sync_tree, version_key
from ota.updater import Agent, independent_python, systemctl, update_ready


def publish_runtime(agent, replace=False):
    """Publish independent tools/config/units; never change the app service."""
    directory = Path.home() / ".config/systemd/user"
    destination = agent.root / "agent.json"
    target = agent.root / "agent"
    python = independent_python(agent.config.get("python", "python3"), agent.shared, agent.root)
    for path in (agent.root, directory, python):
        if any(character.isspace() or character in '%"\\' for character in str(path)):
            raise ValueError("Service paths must not contain whitespace or systemd specifiers")
    if target.exists() and (not replace or target.is_symlink()):
        raise FileExistsError("Do not overwrite an unowned OTA runtime")
    if Path(__file__).resolve().is_relative_to(target):
        raise ValueError("Run maintenance from new tools, not the installed runtime")
    staging = agent.root / ("agent-next-" + str(uuid.uuid4()))
    shutil.copytree(Path(__file__).parent, staging / "ota", ignore=shutil.ignore_patterns("__pycache__", "test_*", "*.local.json", "dist", "wheelhouse"))
    subprocess.run([python, "-c", "import ota.updater, ota.setup, ota.configure"], cwd=staging,
                   check=True, timeout=30, capture_output=True)
    sync_tree(staging)
    if destination.exists():
        atomic_json(agent.root / ("agent-before-upgrade-" + str(uuid.uuid4()) + ".local.json"), read_json(destination))
    if replace:
        target.rename(agent.root / ("agent-backup-" + str(uuid.uuid4())))
    staging.rename(target)
    atomic_json(destination, agent.config)
    agent.store.set("device_id", agent.config["device_id"])
    directory.mkdir(parents=True, exist_ok=True)
    for action in ("poll", "notify"):
        atomic_text(directory / ("facial-ota-" + action + ".service"),
                    "# Managed by facial-ota\n[Unit]\nDescription=Facial OTA " + action + "\nAfter=network.target\n\n[Service]\nType=simple\n"
                    + "WorkingDirectory=" + str(target) + "\nExecStart=" + python + " -m ota.updater --config "
                    + str(destination) + " " + action + "\nRestart=on-failure\nRestartSec=15\nUMask=0077\n\n[Install]\nWantedBy=default.target\n")
    sync_directory(directory)
    return destination


def waiting_install(agent):
    """Called under setup recovery locks, after snapshotting all owned files."""
    if agent.config["role"] != "production":
        raise ValueError("Waiting enrollment requires production role")
    directory = Path.home() / ".config/systemd/user"
    if any((directory / "facial_recognition.service.d" / name).exists()
           for name in ("40-ota-bootstrap.conf", "50-ota.conf")) or (agent.root / "current").is_symlink():
        raise FileExistsError("Waiting enrollment must not replace an app override/baseline")
    destination = publish_runtime(agent, replace=bool(agent.store.get("waiting_enrollment")))
    systemctl("daemon-reload")
    return destination


def install(config_path, baseline=None, enable=False, upgrade=False, _locked=False):
    os.umask(0o077)
    agent = Agent(config_path)
    with (nullcontext() if _locked else file_lock(agent.root / "update.lock", blocking=False)), \
         (nullcontext() if _locked else file_lock(agent.root / "notify.lock", blocking=False)), \
         (nullcontext() if _locked else file_lock(agent.root / "report.lock", blocking=False)):
        if agent.config["role"] == "development":
            raise ValueError("Development hosts do not install automatic OTA services")
        baseline = agent.current()[0] if upgrade and baseline is None else Path(baseline).resolve(strict=True)
        info = read_json(baseline / "BUILD_INFO.json")
        version_key(info["version"])
        if info.get("config_schema") != 1 or info.get("data_schema") != 1 or not info.get("feature_model_sha256"):
            raise ValueError("A packaged health-enabled baseline is required")
        if not (baseline / "venv/bin/python").is_file() or not update_ready(agent.check(info)):
            raise ValueError("Baseline must already be running and healthy")
        if (baseline / "config.json").resolve() != (agent.shared / "config.json").resolve():
            raise ValueError("Baseline must use the configured shared config.json")
        systemctl("cat", agent.service)
        directory = Path.home() / ".config/systemd/user"
        dropin = directory / "facial_recognition.service.d/50-ota.conf"
        units = [directory / ("facial-ota-" + action + ".service") for action in ("poll", "notify")]
        runtime = agent.root / "agent"
        if not upgrade and (runtime.exists() or dropin.exists() or any(path.exists() for path in units)):
            raise FileExistsError("OTA installation already exists; do not overwrite running agent/services")
        if upgrade:
            waiting = agent.store.get("waiting_enrollment") and not (agent.root / "current").is_symlink()
            if not runtime.is_dir() or runtime.is_symlink() or (not dropin.is_file() and not waiting):
                raise ValueError("A previous independent OTA installation is required")
            for action in ("poll", "report", "notify"):
                try:
                    active = systemctl("is-active", "facial-ota-" + action + ".service")
                except subprocess.CalledProcessError:
                    active = "inactive"
                if active in ("active", "activating", "deactivating", "reloading"):
                    raise RuntimeError("Stop poll/report/notify services before upgrading the agent")
        for path in (agent.root, baseline, directory):
            if any(character in str(path) for character in ('%', '\n', '\r', '"', '\\')) or " " in str(path):
                raise ValueError("Service paths must not contain whitespace or systemd specifiers")
        destination = publish_runtime(agent, replace=upgrade)
        agent.point(baseline)
        dropin.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(dropin, "[Service]\nWorkingDirectory=" + str(agent.root / "current") + "\nExecStart=\nExecStart="
                          + str(agent.root / "current/venv/bin/python") + " -u " + str(agent.root / "current/main.py")
                          + "\nEnvironment=FACIAL_OTA_HEALTH_PORT=" + str(agent.port) + "\n")
        if upgrade:
            legacy = directory / "facial-ota-report.service"
            if legacy.is_file():
                systemctl("disable", legacy.name)
                legacy.rename(legacy.with_name(legacy.name + ".before-smtp"))
            agent.store.migrate_notifications()
        systemctl("daemon-reload")
        if (systemctl("show", agent.service, "--property=WorkingDirectory", "--value") != str(agent.root / "current")
                or str(agent.root / "current/venv/bin/python") not in systemctl("show", agent.service, "--property=ExecStart", "--value")):
            raise RuntimeError("Another service override conflicts with OTA; polling was not enabled")
        if enable:
            systemctl("enable", "--now", *(path.name for path in units))
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade a stopped existing agent; preserve data/history")
    parser.add_argument("--enable", action="store_true", help="Explicitly start automatic polling/notifications")
    args = parser.parse_args()
    if not args.upgrade and not args.baseline:
        parser.error("--baseline is required for first installation")
    print("Installed agent configuration:", install(args.config, args.baseline, args.enable, args.upgrade))


if __name__ == "__main__":
    main()
