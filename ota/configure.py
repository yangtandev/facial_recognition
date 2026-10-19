"""Interactive OTA/SMTP configuration; safe to run on an existing installation."""
import argparse
from contextlib import ExitStack
import getpass
from pathlib import Path
import shutil
import sys
import time
import uuid

from ota.core import atomic_json, file_lock, read_json
from ota.notifications import send_mail, validate_smtp


def prompt(label, default="", secret=False):
    if secret:
        return getpass.getpass(label + (" [Enter 保留既有值]" if default else "") + ": ") or default
    return input(label + (" [" + str(default) + "]" if default != "" else "") + ": ").strip() or default


def configure(project, config_path=None):
    project = Path(project).resolve()
    local = Path(config_path).expanduser().resolve() if config_path else project / "ota/agent.local.json"
    previous = read_json(local) if local.exists() else {}
    if not config_path:
        installed = Path(previous.get("state_dir", Path.home() / ".local/share/facial-ota")).expanduser() / "agent.json"
        if installed.is_file():
            local, previous = installed.resolve(), read_json(installed)
    config = {**read_json(Path(__file__).with_name("agent.example.json")), **previous}
    config["shared_dir"] = str(project) if "shared_dir" not in previous else previous["shared_dir"]
    config["state_dir"] = previous.get("state_dir", str(Path.home() / ".local/share/facial-ota"))
    default_python = shutil.which("python3") if sys.version_info >= (3, 10) else "/usr/bin/python3.10"
    config["python"] = previous.get("python", default_python)
    role = prompt("主機角色 development / test / production", previous.get("role", "production"))
    if role not in ("development", "test", "production"):
        raise ValueError("角色需為 development、test 或 production")
    config["role"] = role
    device = previous.get("device_id", "")
    config["device_id"] = str(uuid.uuid4()) if not device or device.startswith("CHANGE_ME") else device
    uuid.UUID(config["device_id"])
    if role != "development":
        config["repo"] = prompt("GitHub owner/repo", config["repo"])
        config["github_token"] = prompt("GitHub 唯讀 token（公開 repo 可留空）", config.get("github_token", ""), secret=True)
        smtp = dict(config.get("smtp", {}))
        smtp["host"] = prompt("SMTP 主機", "" if "CHANGE_ME" in smtp.get("host", "") else smtp.get("host", ""))
        smtp["tls"] = prompt("SMTP TLS 模式 starttls / ssl", smtp.get("tls", "starttls"))
        smtp["port"] = int(prompt("SMTP 連接埠", smtp.get("port", 587) if previous.get("smtp") else (465 if smtp["tls"] == "ssl" else 587)))
        smtp["username"] = prompt("SMTP 帳號（無需認證可留空）", smtp.get("username", ""))
        smtp["password"] = prompt("SMTP 密碼／應用程式密碼", smtp.get("password", ""), secret=True)
        smtp["from"] = prompt("寄件信箱", smtp.get("from", ""))
        smtp["to"] = list(dict.fromkeys(address.strip() for address in
                          prompt("管理員收件信箱（多位以逗號分隔）", ",".join(smtp.get("to", []))).split(",") if address.strip()))
        validate_smtp(smtp)
        config["smtp"] = smtp
    for key in ("management_url", "device_token"):
        config.pop(key, None)
    state = Path(config["state_dir"]).expanduser().resolve()
    # Do not accidentally edit active settings while an update/mail transaction is running.
    with ExitStack() as locks:
        if (state / "agent.json").exists():
            for name in ("update.lock", "notify.lock", "report.lock"):
                locks.enter_context(file_lock(state / name, blocking=False))
        if local.exists():
            atomic_json(local.with_name(local.name + ".before-smtp.local.json"), previous)
        atomic_json(local, config)
    print("已寫入 OTA 設定（權限 600）:", local)
    print("主機 ID:", config["device_id"], "角色:", role)
    if role != "development" and prompt("現在寄送一封測試信？ y/N", "N").lower() == "y":
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
    args = parser.parse_args()
    try:
        configure(args.project, args.config)
    except (ValueError, OSError) as error:
        raise SystemExit("設定未完成: " + type(error).__name__ + "；請檢查輸入與檔案權限。") from None


if __name__ == "__main__":
    main()
