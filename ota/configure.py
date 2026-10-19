"""Interactive OTA/SMTP configuration; safe to run on an existing installation."""
import argparse
from contextlib import ExitStack
import getpass
from pathlib import Path
import shutil
import re
import subprocess
import sys
import time
import uuid

from ota.core import atomic_json, file_lock, overlay_config, read_json
from ota.notifications import send_mail, validate_smtp


def prompt(label, default="", secret=False):
    if secret:
        return getpass.getpass(label + (" [Enter 保留既有值]" if default else "") + ": ") or default
    return input(label + (" [" + str(default) + "]" if default != "" else "") + ": ").strip() or default


def configuration_path(project, config_path=None):
    local = Path(config_path).expanduser().resolve() if config_path else Path(project).resolve() / "ota/agent.local.json"
    if not config_path:
        previous = read_json(local) if local.exists() else {}
        installed = Path(previous.get("state_dir", Path.home() / ".local/share/facial-ota")).expanduser() / "agent.json"
        if installed.is_file():
            value = read_json(installed)
            if Path(value["shared_dir"]).expanduser().resolve() != Path(project).resolve():
                raise ValueError("Existing OTA state belongs to another project; use the original project/configuration")
            return installed.resolve()
    return local


def repository(project):
    try:
        remote = subprocess.check_output(["git", "config", "--get", "remote.origin.url"], cwd=project,
                                         text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        match = re.fullmatch(r"(?:https://(?:[^/@]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", remote)
        return match[1] if match else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def export_profile(project, destination, config_path=None):
    source = configuration_path(project, config_path)
    if Path(destination).expanduser().resolve() == source.resolve():
        raise ValueError("Export profile must not overwrite the device configuration")
    value = read_json(source)
    atomic_json(Path(destination).expanduser(), {key: value[key] for key in ("role", "repo", "github_token", "smtp") if key in value})


def configure(project, config_path=None, profile=None, role=None, interactive=True):
    project = Path(project).resolve()
    local = configuration_path(project, config_path)
    previous = read_json(local) if local.exists() else {}
    defaults = read_json(profile) if profile else {}
    if not isinstance(defaults, dict) or set(defaults) - {"role", "repo", "github_token", "smtp"}:
        raise ValueError("Provisioning profile may contain only role, repo, github_token and smtp")
    config = overlay_config(overlay_config(read_json(Path(__file__).with_name("agent.example.json")), defaults), previous)
    config["shared_dir"] = str(project) if "shared_dir" not in previous else previous["shared_dir"]
    config["state_dir"] = previous.get("state_dir", str(Path.home() / ".local/share/facial-ota"))
    base_python = Path(sys.base_prefix) / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    default_python = str(base_python) if sys.version_info >= (3, 10) and base_python.is_file() else "/usr/bin/python3.10"
    config["python"] = previous.get("python", default_python)
    if "repo" not in previous and "repo" not in defaults:
        config["repo"] = repository(project) or config["repo"]
    ask = prompt if interactive else lambda label, default="", **kwargs: default
    role = role or ask("主機角色 development / test / production", previous.get("role", defaults.get("role", "production")))
    if role not in ("development", "test", "production"):
        raise ValueError("角色需為 development、test 或 production")
    config["role"] = role
    device = previous.get("device_id", "")
    config["device_id"] = str(uuid.uuid4()) if not device or device.startswith("CHANGE_ME") else device
    uuid.UUID(config["device_id"])
    if role != "development":
        config["repo"] = ask("GitHub owner/repo", config["repo"])
        config["github_token"] = ask("GitHub 唯讀 token（公開 repo 可留空）", config.get("github_token", ""), secret=True)
        smtp = dict(config.get("smtp", {}))
        provided_smtp = overlay_config(defaults.get("smtp", {}), previous.get("smtp", {}))
        if not interactive and any(not provided_smtp.get(key) for key in ("host", "from", "to")):
            raise ValueError("Non-interactive setup requires explicit SMTP host, sender and recipients")
        smtp["host"] = ask("SMTP 主機", "" if "CHANGE_ME" in smtp.get("host", "") else smtp.get("host", ""))
        smtp["tls"] = ask("SMTP TLS 模式 starttls / ssl", smtp.get("tls", "starttls"))
        explicit_port = "port" in previous.get("smtp", {}) or "port" in defaults.get("smtp", {})
        smtp["port"] = int(ask("SMTP 連接埠", smtp.get("port", 587) if explicit_port else (465 if smtp["tls"] == "ssl" else 587)))
        smtp["username"] = ask("SMTP 帳號（無需認證可留空）", smtp.get("username", ""))
        smtp["password"] = ask("SMTP 密碼／應用程式密碼", smtp.get("password", ""), secret=True)
        smtp["from"] = ask("寄件信箱", smtp.get("from", ""))
        smtp["to"] = list(dict.fromkeys(address.strip() for address in
                          ask("管理員收件信箱（多位以逗號分隔）", ",".join(smtp.get("to", []))).split(",") if address.strip()))
        validate_smtp(smtp)
        config["smtp"] = smtp
    for key in ("management_url", "device_token"):
        config.pop(key, None)
    state = Path(config["state_dir"]).expanduser().resolve()
    # Do not accidentally edit active settings while an update/mail transaction is running.
    with ExitStack() as locks:
        if (state / "agent.json").exists() or (state / "setup.json").exists():
            print("等待目前 OTA 作業完成後保存設定；不停止辨識服務。")
            for name in ("update.lock", "notify.lock", "report.lock"):
                locks.enter_context(file_lock(state / name, blocking=True))
        local = configuration_path(project, config_path)
        if local.exists():
            atomic_json(local.with_name(local.name + ".before-smtp.local.json"), read_json(local))
        atomic_json(local, config)
    print("已寫入 OTA 設定（權限 600）:", local)
    print("主機 ID:", config["device_id"], "角色:", role)
    if role != "development" and interactive and ask("現在寄送一封測試信？ y/N", "N").lower() == "y":
        event = {"event_id": str(uuid.uuid4()), "alert": "test", "device_id": config["device_id"],
                 "role": role, "project": "SMTP 設定測試", "location": "本機", "occurred_at": time.time()}
        try:
            for recipient in config["smtp"]["to"]:
                send_mail(config, event, recipient)
            print("SMTP 已接受測試信；請確認管理員信箱收到。")
        except Exception as error:
            print("測試信失敗:", type(error).__name__, "；設定已保存，請修正後重試。")
    return local


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--config")
    parser.add_argument("--profile", help="Private provisioning profile; excludes device ID and site config")
    parser.add_argument("--role", choices=("development", "test", "production"))
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--export-profile", help="Export reusable private settings, never device identity/site paths")
    args = parser.parse_args()
    try:
        if args.export_profile:
            export_profile(args.project, args.export_profile, args.config)
            print("已匯出私有部署設定（含帳密、權限 600），請安全傳送，不要加入 Git。")
        else:
            configure(args.project, args.config, args.profile, args.role, not args.non_interactive)
    except (ValueError, OSError) as error:
        raise SystemExit("設定未完成: " + type(error).__name__ + "；請檢查輸入與檔案權限。") from None


if __name__ == "__main__":
    main()
