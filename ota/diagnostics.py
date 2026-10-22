"""Read-only host inventory and running OTA acceptance; no service/state changes."""
import argparse
from contextlib import closing
import getpass
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time

from ota.configure import configuration_path
from ota.core import read_json, version_key
from ota.updater import health

APP = "facial_recognition.service"
SERVICES = (APP, "facial-ota-poll.service", "facial-ota-notify.service")


class CheckFailed(ValueError):
    """A fixed, credential-free explanation safe to display."""


def preflight(project):
    project = Path(project).resolve()
    try:
        operating_system = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except OSError:
        operating_system = platform.system()
    gpu = "未取得；可安裝 pciutils 後重查，不代表沒有 GPU"
    if shutil.which("lspci"):
        try:
            lines = subprocess.run(["lspci", "-nn"], capture_output=True, text=True,
                                   check=True, timeout=5).stdout.splitlines()
            gpu = [line for line in lines if any(word in line.lower() for word in ("vga", "3d controller", "display controller"))]
        except (OSError, subprocess.SubprocessError):
            pass
    errors = []
    if os.geteuid() == 0:
        errors.append("請使用桌面服務帳號，不要用 root／sudo")
    if platform.system() != "Linux" or sys.version_info < (3, 10):
        errors.append("需要 Linux 與 Python 3.10+；應用程式另須符合 Release 的 Python minor／CPU")
    return {"result": "fail" if errors else "info", "errors": errors,
            "user": getpass.getuser(), "home": str(Path.home()), "os": operating_system,
            "architecture": platform.machine(), "python": platform.python_version(),
            "interpreter": sys.executable, "free_gib": round(shutil.disk_usage(project).free / 1024**3, 1),
            "gpu": gpu, "project": str(project), "config_exists": (project / "config.json").exists(),
            "next": "已有 config：勿重跑完整安裝" if (project / "config.json").exists() else "全新主機：先確認適用 Release 及現場參數",
            "note": "僅環境資訊；未測 GitHub／SMTP／相機連線、venv 及 Release 相容性"}


def services():
    result = {}
    for name in SERVICES:
        output = subprocess.run(["systemctl", "--user", "show", name,
            "--property=ActiveState,UnitFileState,MainPID,NRestarts"],
            check=True, capture_output=True, text=True, timeout=10).stdout
        value = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if value.get("ActiveState") != "active" or value.get("UnitFileState") != "enabled":
            raise CheckFailed(name + " 必須 active 且 enabled；請查服務日誌")
        result[name] = value
    if int(result[APP].get("MainPID", "0")) <= 0:
        raise CheckFailed("辨識服務沒有 MainPID")
    return result


def inspect_health(value, info, units):
    if (value.get("version") != info["version"] or value.get("build_id") != info["build_id"]
            or value.get("pid") != int(units[APP]["MainPID"])
            or not isinstance(value.get("boot_id"), str) or not value["boot_id"]):
        raise CheckFailed("即時健康版本／build ID／PID 不符，或缺少 boot ID")
    observed = value.get("observed_at")
    if (type(observed) not in (int, float) or not math.isfinite(observed)
            or abs(time.time() - observed) > 30):
        raise CheckFailed("健康回應缺少有效時間或已過期")
    if any(value.get(key) is not True for key in ("update_ready", "core_ok", "external_ok")):
        raise CheckFailed("核心或 MQTT／同步尚未通過健康檢查")
    required = {"config", "models", "index", "inference", "mqtt_connected", "mqtt_subscribed", "sync"}
    if any(value.get("checks", {}).get(key) is not True for key in required):
        raise CheckFailed("必要健康檢查未全部通過")
    live, counters = value.get("live", {}), value.get("counters", {})
    if "gui" not in live or not all(ok is True for ok in live.values()):
        raise CheckFailed("GUI／必要執行緒未正常運作")
    for name in live:
        counter = counters.get(name)
        if type(counter) is not int or counter < 0:
            raise CheckFailed("缺少有效的執行緒進度計數")


def check(project, config_path=None, expect_version=None, expect_role=None, seconds=5):
    if not math.isfinite(seconds) or not 1 <= seconds <= 30:
        raise CheckFailed("觀察秒數須介於 1～30")
    config_path = configuration_path(project, config_path)
    config = read_json(config_path)
    role = config.get("role")
    if role not in ("test", "production") or (expect_role and role != expect_role):
        raise CheckFailed("主機角色不符，或是沒有現場 OTA 的開發機")
    root = Path(config["state_dir"]).expanduser().resolve()
    journal = read_json(root / "setup.json")
    if journal.get("phase") != "complete":
        raise CheckFailed("首次接入／更新器維護尚未完成；請查 --ota-status")
    current = root / "current"
    if not current.is_symlink():
        raise CheckFailed("尚無已接入 OTA 的 current 版本")
    info = read_json(current.resolve(strict=True) / "BUILD_INFO.json")
    version = version_key(info["version"])
    if not isinstance(info.get("build_id"), str) or not info["build_id"]:
        raise CheckFailed("套件缺少 build ID")
    if (expect_version and info["version"] != expect_version) or (role == "production" and not version[3]):
        raise CheckFailed("目前版本不符預期，或正式機正在使用 RC")
    # Never instantiate Agent/Store: those constructors initialize/migrate local state.
    with closing(sqlite3.connect((root / "agent.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        row = db.execute("SELECT value FROM state WHERE key='blocked'").fetchone()
        if row and json.loads(row[0]):
            raise CheckFailed("OTA blocked；請先修復並查回退紀錄")
        latest = db.execute("SELECT phase,tag FROM attempts ORDER BY updated DESC LIMIT 1").fetchone()
        if not latest or latest != ("success", info["version"]):
            raise CheckFailed("最近作業未成功，或成功紀錄與選中版本不同；請查 --ota-status")
        pending = db.execute("SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0]
    before_units = services()
    before = health(int(config.get("health_port", 8765)))
    inspect_health(before, info, before_units)
    time.sleep(seconds)
    after = health(int(config.get("health_port", 8765)))
    after_units = services()
    inspect_health(after, info, after_units)
    if read_json(current.resolve(strict=True) / "BUILD_INFO.json") != info:
        raise CheckFailed("觀察期間版本切換，請待更新完成後重查")
    if before_units != after_units or before["boot_id"] != after["boot_id"]:
        raise CheckFailed("觀察期間服務重啟／狀態改變，請待穩定後重查")
    if set(before["live"]) != set(after["live"]) or any(
            after["counters"].get(name, -1) <= before["counters"][name] for name in before["live"]):
        raise CheckFailed("觀察期間必要計數未增加；請查日誌，或用 --seconds 15 再查")
    warnings = []
    if before.get("camera_ok") is not True or after.get("camera_ok") is not True:
        warnings.append("攝影機暫無新影像；OTA 核心驗收通過，影像／業務待確認，不因此回退")
    if pending:
        warnings.append("本機有待寄失敗／異常通知；請查 --ota-status 及通知日誌")
    return {"result": "warning" if warnings else "pass", "version": info["version"],
            "build_id": info["build_id"], "role": role, "pid": after["pid"],
            "services": {name: "active / enabled" for name in SERVICES},
            "observed_seconds": seconds, "update_ready": True, "camera_ok": after.get("camera_ok"),
            "pending": pending, "warnings": warnings,
            "note": "短時唯讀檢查通過，不取代首次 60 秒穩定驗收、真人辨識、語音及實際建檔照同步測試"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "check"))
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--config")
    parser.add_argument("--expect-version")
    parser.add_argument("--expect-role", choices=("test", "production"))
    parser.add_argument("--seconds", type=float, default=5)
    args = parser.parse_args()
    try:
        result = preflight(args.project) if args.action == "preflight" else check(
            args.project, args.config, args.expect_version, args.expect_role, args.seconds)
    except CheckFailed as error:
        result = {"result": "fail", "reason": str(error), "next": "詳見 README 第 7.1 節"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as error:
        # Raw exceptions/JSON/service output may contain credentials; never print them.
        result = {"result": "fail", "error_type": type(error).__name__,
                  "next": "檢查是否接入完成、角色／版本、服務及健康；詳見 README 第 7.1 節"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["result"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
