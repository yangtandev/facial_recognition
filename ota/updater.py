"""Independent user-service agent. No git pull, inbound SSH or application deps."""
import argparse
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ota.core import (Store, atomic_json, digest, file_lock, migrate_config,
                      read_json, request, safe_extract, select_release,
                      sync_directory, sync_tree, version_key, error_summary, timestamp)
from ota.notifications import mail_once, validate_smtp

SHARED = ("media", "voice", "data", "log", "reports", "img_log")


def update_ready(value):
    # Legacy baselines have only healthy; new builds exclude missing camera frames.
    return value.get("update_ready", value.get("healthy")) is True


def health(port=8765):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{int(port)}/health", timeout=3) as response:
        return json.loads(response.read(128 * 1024))


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], check=True,
                          capture_output=True, text=True, timeout=90).stdout.strip()


def independent_python(command, shared, state):
    executable = shutil.which(command)
    if not executable or any(path.is_relative_to(root) for path in
            (Path(executable).absolute(), Path(executable).resolve()) for root in (shared, state)):
        raise ValueError("Interpreter must be installed outside app/release/state directories")
    return executable


class Agent:
    def __init__(self, config_path):
        self.config = read_json(config_path)
        self.root = Path(self.config["state_dir"]).expanduser().resolve()
        self.shared = Path(self.config["shared_dir"]).expanduser().resolve()
        if self.root in (Path("/"), Path.home()) or self.root == self.shared or self.root.is_relative_to(self.shared):
            raise ValueError("OTA state must be outside the existing project/shared directory")
        if self.shared.is_relative_to(self.root / "releases"):
            raise ValueError("Shared business data cannot live inside disposable releases")
        if int(self.config.get("keep_releases", 2)) < 2:
            raise ValueError("Always retain at least current and previous known-good releases")
        if self.config["role"] not in ("development", "test", "production"):
            raise ValueError("Unknown role")
        uuid.UUID(self.config["device_id"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.config["repo"]):
            raise ValueError("Invalid GitHub repository")
        if self.config["role"] != "development":
            validate_smtp(self.config.get("smtp"))
        self.service = "facial_recognition.service"
        self.port = int(self.config.get("health_port", 8765))
        for name in ("startup_timeout", "stable_seconds", "poll_seconds", "alert_grace_seconds"):
            if name in self.config and (not math.isfinite(float(self.config[name])) or float(self.config[name]) <= 0):
                raise ValueError("Timeouts/intervals must be positive")
        if not (self.shared / "config.json").is_file():
            raise ValueError("Shared config.json must already exist")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.root / "agent.sqlite3")
        enrolled = self.store.get("device_id")
        if enrolled and enrolled != self.config["device_id"]:
            raise ValueError("Device identity cannot change while retaining an event database")

    def identity(self):
        cached = self.store.get("identity", {})
        try:
            server = read_json(self.shared / "config.json")["Server"]
            api = server["API_url"].rstrip("/")
            location = str(server["location_ID"])
            project = urllib.parse.urlparse(api).hostname.split(".")[0]
            key = api + "/locations/" + urllib.parse.quote(location, safe="") + "/"
            if cached.get("key") != key:
                cached = {"key": key, "project": project, "location": "location_ID=" + location}
            if cached.get("refresh_at", 0) < time.time():
                # Name lookup is optional and bounded; never contains host credentials.
                cached["refresh_at"] = time.time() + 3600
                try:
                    with request(key, timeout=3) as response:
                        name = json.loads(response.read(65536))["name"]
                        if isinstance(name, str) and name:
                            cached["location"] = name[:200]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            self.store.set("identity", cached)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return {"device_id": self.config["device_id"], "role": self.config["role"],
                "project": cached.get("project", "unknown"), "location": cached.get("location", "unknown")}

    def current(self):
        link = self.root / "current"
        if not link.is_symlink():
            raise RuntimeError("First adopt a health-enabled, tested baseline with init")
        path = link.resolve(strict=True)
        info = read_json(path / "BUILD_INFO.json")
        version_key(info["version"])
        return path, info

    def check(self, info):
        value = health(self.port)
        pid = int(systemctl("show", self.service, "--property=MainPID", "--value"))
        if (pid <= 0 or not isinstance(value.get("boot_id"), str) or not value["boot_id"]
                or value.get("pid") != pid or value.get("version") != info["version"]
                or value.get("build_id") != info["build_id"]):
            raise RuntimeError("Health response is not from the expected running build")
        return value

    def wait_healthy(self, info):
        deadline = time.monotonic() + float(self.config.get("startup_timeout", 180))
        stable = float(self.config.get("stable_seconds", 60))
        started, boot, initial = None, None, None
        problem = "health endpoint unavailable"
        while time.monotonic() < deadline + stable:
            try:
                value = self.check(info)
                good = update_ready(value)
                problem = ",".join(key for section in ("checks", "live") for key, ok in value.get(section, {}).items() if not ok)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                problem = error_summary(error)
                value, good = {}, False
            now = time.monotonic()
            if not good or value.get("boot_id") != boot:
                started, boot, initial = None, value.get("boot_id"), None
            if good and started is None:
                started, initial = now, value.get("counters", {})
            if good and started is not None and now - started >= stable:
                if not initial or not all(value.get("counters", {}).get(k, 0) > v for k, v in initial.items()):
                    raise RuntimeError("Health counters did not advance")
                return value
            if now >= deadline and started is None:
                break
            time.sleep(2)
        raise RuntimeError("Startup/health validation timed out: " + problem)

    def point(self, path):
        temporary = self.root / (".current-" + str(uuid.uuid4()))
        temporary.symlink_to(path)
        os.replace(temporary, self.root / "current")
        sync_directory(self.root)

    def releases(self):
        base = "https://api.github.com/repos/" + self.config["repo"] + "/releases"
        url, result, pages = base + "?per_page=100", [], 0
        while url:
            if not url.startswith(base + "?"):
                raise ValueError("Unexpected GitHub pagination URL")
            cached = self.store.get("page:" + url, {})
            headers = {"Accept": "application/vnd.github+json"}
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            try:
                with request(url, self.config.get("github_token", ""), headers=headers) as response:
                    body = json.loads(response.read(4 * 1024**2))
                    links = response.headers.get("Link", "")
                    next_url = None
                    for part in links.split(","):
                        match = re.search(r'<([^>]+)>;\s*rel="next"', part)
                        if match:
                            next_url = match[1]
                    cached = {"etag": response.headers.get("ETag"), "body": body, "next": next_url}
                    self.store.set("page:" + url, cached)
            except urllib.error.HTTPError as error:
                if error.code == 304 and cached.get("body") is not None:
                    pass
                else:
                    delay = 60
                    try:
                        delay = max(delay, float(error.headers.get("Retry-After", 0)),
                                    float(error.headers.get("X-RateLimit-Reset", 0)) - time.time())
                    except (TypeError, ValueError):
                        pass
                    self.store.set("poll_after", time.time() + delay)
                    raise
            if not isinstance(cached.get("body"), list):
                raise ValueError("Unexpected GitHub releases response")
            result.extend(cached["body"])
            url = cached.get("next")
            pages += 1
            if pages >= 100 and url:
                raise ValueError("Too many release pages; refusing partial discovery")
        return result

    def asset(self, release, name):
        found = [asset for asset in release.get("assets", []) if asset.get("name") == name and asset.get("state") == "uploaded"]
        if len(found) != 1:
            raise ValueError("Missing or ambiguous Release asset")
        asset = found[0]
        prefix = "https://api.github.com/repos/" + self.config["repo"] + "/releases/assets/"
        if not re.fullmatch(re.escape(prefix) + r"\d+", asset.get("url", "")):
            raise ValueError("Unexpected asset API origin")
        return asset

    def manifest(self, release):
        asset = self.asset(release, "release.json")
        with request(asset["url"], self.config.get("github_token", ""),
                     headers={"Accept": "application/octet-stream"}) as response:
            value = json.loads(response.read(65536))
        if not isinstance(value, dict):
            raise ValueError("Release manifest must be an object")
        if value.get("version") != release["tag_name"] or type(value.get("config_schema")) is not int or value["config_schema"] != 1:
            raise ValueError("Version/config schema mismatch")
        if type(value.get("data_schema")) is not int or value["data_schema"] != 1:
            raise ValueError("Unsupported business-data migration; manual migration required")
        if not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", "")):
            raise ValueError("Missing archive checksum")
        name = value.get("archive", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.tar\.gz", name):
            raise ValueError("Invalid archive filename")
        projects = value.get("projects")
        if not isinstance(projects, list) or not projects or not all(isinstance(p, str) for p in projects):
            raise ValueError("Explicit project applicability required")
        return value

    def stage(self, release, manifest, attempt, compare_baseline=True):
        self.target_python(manifest)  # Fail before downloading a large incompatible package.
        target = self.root / "releases" / (release["tag_name"] + "-" + attempt)
        target.mkdir(parents=True, mode=0o700)
        archive = target / "package.tar.gz"
        asset = self.asset(release, manifest["archive"])
        size = int(manifest["size"])
        if size <= 0 or size != asset.get("size") or size > int(self.config.get("max_archive_bytes", 5 * 1024**3)):
            raise ValueError("Archive size mismatch/limit")
        if shutil.disk_usage(target).free < size * 5 + int(self.config.get("min_free_bytes", 1024**3)):
            raise RuntimeError("Insufficient space for package and isolated environment")
        total = 0
        with request(asset["url"], self.config.get("github_token", ""),
                     headers={"Accept": "application/octet-stream"}, timeout=60) as response, open(archive, "xb") as output:
            while chunk := response.read(1024**2):
                total += len(chunk)
                if total > size:
                    raise ValueError("Archive larger than manifest")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != size or digest(archive) != manifest["sha256"]:
            raise ValueError("Archive integrity check failed")
        return self.prepare(target, manifest, compare_baseline=compare_baseline)

    def target_python(self, manifest):
        minor = manifest.get("python", "")
        if not isinstance(minor, str) or not re.fullmatch(r"3\.[0-9]{1,2}", minor) or int(minor.split(".")[1]) < 10:
            raise ValueError("Invalid Release Python version")
        candidates = [self.config.get("python", "python3"), "python" + minor]
        for candidate in dict.fromkeys(candidates):
            try:
                executable = independent_python(candidate, self.shared, self.root)
            except ValueError:
                continue
            value = subprocess.check_output([executable, "-c",
                "import sys,platform;print(str(sys.version_info.major)+'.'+str(sys.version_info.minor));print(platform.machine())"],
                text=True, timeout=30).splitlines()
            if value == [minor, manifest.get("architecture")]:
                return executable
        raise ValueError("No installed system Python/CPU matches Release wheelhouse; original service is unchanged")

    def prepare(self, target, manifest, compare_baseline=True):
        archive = target / "package.tar.gz"
        safe_extract(archive, target, int(self.config.get("max_unpacked_bytes", 20 * 1024**3)))
        app = target / "app"
        for name in ("main.py", "requirements.txt", "ota/core.py", "ota/health.py", "config.defaults.json"):
            if not (app / name).is_file():
                raise ValueError("Missing application prerequisite")
        info = read_json(app / "BUILD_INFO.json")
        if (info.get("version") != manifest["version"] or info.get("build_id") != manifest.get("build_id")
                or info.get("commit") != manifest.get("commit")):
            raise ValueError("Build identity mismatch")
        python_cmd = self.target_python(manifest)
        baseline = self.current()[1] if compare_baseline else info
        if (info.get("data_schema") != 1 or info.get("feature_model_sha256") != baseline.get("feature_model_sha256")
                or info.get("feature_model_sha256") != digest(app / "models/data/20180402-114759-vggface2.pt")):
            raise ValueError("Feature model/schema change requires an explicit data migration")
        for name in (*SHARED, "config.json", "venv"):
            if (app / name).exists():
                raise ValueError("Package contains mutable host data/environment")
        for model in app.rglob("*"):
            if model.is_file() and model.suffix in (".pt", ".onnx", ".bin", ".pkl"):
                with open(model, "rb") as source:
                    if source.read(80).startswith(b"version https://git-lfs.github.com/spec/"):
                        raise ValueError("Unresolved Git LFS model")
        # Dependencies are prepared offline; the old environment is never modified.
        subprocess.run([python_cmd, "-m", "venv", str(app / "venv")], check=True, timeout=120)
        python = str(app / "venv/bin/python")
        subprocess.run([python, "-m", "pip", "install", "--no-index", "--find-links", str(target / "wheelhouse"),
                        "--only-binary=:all:", "-r", "requirements.txt"], cwd=app, check=True, timeout=1800, stdout=subprocess.DEVNULL)
        subprocess.run([python, "-m", "pip", "check"], cwd=app, check=True, timeout=120, stdout=subprocess.DEVNULL)
        for name in SHARED:
            shared = self.shared / name
            shared.mkdir(parents=True, exist_ok=True)
            (app / name).symlink_to(shared, target_is_directory=True)
        (app / "config.json").symlink_to(self.shared / "config.json")
        sync_tree(target)
        return app, info

    def update(self, release, manifest):
        old, old_info = self.current()
        old_status = self.check(old_info)
        if not update_ready(old_status):
            return
        attempt = str(uuid.uuid4())
        identity = self.identity()
        data = {"from_version": old_info["version"], "target_version": release["tag_name"],
                "old_path": str(old), "old_info": old_info, "sha256": manifest["sha256"],
                "running_version": old_info["version"], "service_healthy": old_status.get("healthy") is True}
        self.store.record(attempt, "preparing", data, identity)
        try:
            new, info = self.stage(release, manifest, attempt)
            data.update(new_path=str(new), new_info=info)
            self.store.record(attempt, "prepared", data, identity)
            config_path = (self.shared / "config.json").resolve()
            with file_lock(str(config_path) + ".lock"):
                migration_file = new / "config.migrations.json"
                merged = migrate_config(read_json(new / "config.defaults.json"), read_json(config_path),
                                        read_json(migration_file) if migration_file.exists() else [])
                if not update_ready(self.check(old_info)):
                    raise RuntimeError("Baseline became unhealthy; switch cancelled")
                backup = self.root / "backups" / (attempt + ".json")
                atomic_json(backup, read_json(config_path))
                data["backup"] = str(backup)
                data.update(service_healthy=False, running_version="unknown")
                self.store.record(attempt, "stopping", data, identity)
                try:
                    systemctl("stop", self.service)
                    atomic_json(config_path, merged)
                    self.store.record(attempt, "switching", data, identity)
                    self.point(new)
                    systemctl("start", self.service)
                    self.store.record(attempt, "validating", data, identity)
                    verified = self.wait_healthy(info)
                    data.update(running_version=info["version"], service_healthy=verified.get("healthy") is True,
                                rollback="not_needed")
                    self.store.record(attempt, "success", data, identity)
                except Exception as error:
                    data["error"] = error_summary(error)
                    data.setdefault("failed_at", timestamp())
                    self.rollback(attempt, data, identity)
        except Exception as error:
            # Failures before a switch leave the old release untouched.
            row = next((row for row in self.store.unfinished() if row["id"] == attempt), None)
            if row is None:
                return
            if row["phase"] not in ("preparing", "prepared"):
                # Persistence/rollback errors must never be reported as a preparation failure.
                raise
            data.update(error=error_summary(error), rollback="not_needed")
            try:
                data["service_healthy"] = self.check(old_info).get("healthy") is True
            except Exception:
                data["service_healthy"] = False
            self.store.record(attempt, "failed", data, identity)

    def cleanup(self):
        """Only UUID-named directories created by recorded attempts; no glob delete."""
        directory = self.root / "releases"
        if directory.is_symlink():
            raise ValueError("Managed releases directory must not be a symlink")
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY updated DESC")]
        protected = {self.current()[0]}
        successes = [row for row in rows if row["phase"] == "success"][:int(self.config.get("keep_releases", 2))]
        for row in rows:
            data = json.loads(row["data"])
            if row in successes or row["phase"] not in ("success", "failed", "rollback_failed"):
                keys = ("old_path", "new_path") if row["phase"] != "success" or row == successes[0] else ("new_path",)
                for key in keys:
                    if key in data:
                        protected.add(Path(data[key]).resolve())
        removed = []
        for row in rows:
            if row["phase"] == "rollback_failed":
                continue
            try:
                uuid.UUID(row["id"])
                version_key(row["tag"])
            except (ValueError, AttributeError):
                continue
            candidate = directory / (row["tag"] + "-" + row["id"])
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            if candidate.resolve().parent != directory.resolve():
                raise ValueError("Release cleanup escaped managed directory")
            if any(path == candidate.resolve() or path.is_relative_to(candidate.resolve()) for path in protected):
                continue
            shutil.rmtree(candidate)  # Does not follow the app's shared-data symlinks.
            removed.append(candidate.name)
        if removed:
            sync_directory(directory)
            self.store.set("last_cleanup", {"at": time.time(), "removed": removed})

    def rollback(self, attempt, data, identity):
        data.update(service_healthy=False, running_version="unknown")
        try:
            self.store.record(attempt, "rolling_back", data, identity)
        except Exception:
            # Journal/disk failure must not prevent an emergency rollback.
            print("OTA rollback journal unavailable; continuing restoration", flush=True)
        try:
            systemctl("stop", self.service)
            atomic_json((self.shared / "config.json").resolve(), read_json(data["backup"]))
            self.point(Path(data["old_path"]))
            systemctl("reset-failed", self.service)
            systemctl("start", self.service)
            verified = self.wait_healthy(data["old_info"])
            data.update(running_version=data["old_info"]["version"], rollback="success",
                        service_healthy=verified.get("healthy") is True)
        except Exception as error:
            data.update(rollback="failed", rollback_error=error_summary(error), service_healthy=False,
                        running_version="unknown", rollback_failed_at=timestamp())
            self.store.set("blocked", True)
            self.store.record(attempt, "rollback_failed", data, identity)
        else:
            # If recording fails, recovery sees the old checkpoint and retries
            # recording; it must not stop an already restored healthy baseline.
            self.store.record(attempt, "failed", data, identity)

    def recover(self):
        for row in self.store.unfinished():
            data = json.loads(row["data"])
            identity = self.identity()
            data.setdefault("error", "InterruptedUpdate")
            if row["phase"] in ("preparing", "prepared"):
                data.update(rollback="not_needed", service_healthy=False)
                try:
                    data["service_healthy"] = self.check(data["old_info"]).get("healthy") is True
                except Exception:
                    pass
                self.store.record(row["id"], "failed", data, identity)
            else:
                with file_lock(str((self.shared / "config.json").resolve()) + ".lock"):
                    try:
                        restored_status = self.check(data["old_info"])
                        restored = (self.current()[0] == Path(data["old_path"])
                                    and update_ready(restored_status)
                                    and read_json(self.shared / "config.json") == read_json(data["backup"]))
                    except Exception:
                        restored = False
                    if restored:
                        data.update(running_version=data["old_info"]["version"], rollback="success",
                                    service_healthy=restored_status.get("healthy") is True)
                        self.store.record(row["id"], "failed", data, identity)
                        continue
                    # Conservative after interruption: restore the recorded baseline.
                    self.rollback(row["id"], data, identity)

    def once(self):
        with file_lock(self.root / "update.lock", blocking=False):
            if self.setup_pending():
                return
            self.recover()
            if self.store.get("blocked") or self.config["role"] == "development":
                return
            try:
                self.cleanup()
            except Exception as error:
                self.store.set("cleanup_error", error_summary(error))
            if time.time() < self.store.get("poll_after", 0):
                return
            _, info = self.current()
            releases = self.releases()
            retry = self.store.get("retry", "")
            failed = self.store.failed() - {retry}
            if retry:
                releases = [value for value in releases if value.get("tag_name") == retry]
            identity = self.identity()
            while candidate := select_release(releases, self.config["role"], info["version"], failed):
                try:
                    manifest = self.manifest(candidate)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    try:
                        baseline_healthy = self.check(info).get("healthy") is True
                    except Exception:
                        baseline_healthy = False
                    self.store.record(str(uuid.uuid4()), "failed", {
                        "from_version": info["version"], "target_version": candidate["tag_name"],
                        "running_version": info["version"], "error": error_summary(error),
                        "rollback": "not_needed", "service_healthy": baseline_healthy}, identity)
                    break
                if "*" in manifest["projects"] or identity["project"] in manifest["projects"]:
                    if candidate["tag_name"] == retry:
                        self.store.set("retry", "")
                    self.update(candidate, manifest)
                    break
                releases.remove(candidate)

    def notify_once(self):
        if self.config["role"] == "development":
            return
        with file_lock(self.root / "notify.lock", blocking=False):
            try:
                with file_lock(self.root / "update.lock", blocking=False):
                    self.monitor()
            except BlockingIOError:
                # Intentional update downtime is not a separate service incident.
                pass
            mail_once(self.config, self.store)

    def monitor(self):
        if self.setup_pending() or self.store.unfinished() or self.store.get("blocked"):
            return
        failures = {}
        try:
            _, info = self.current()
            status = self.check(info)
            if status.get("core_ok") is not True:
                failures["core"] = "Core health validation failed"
            if status.get("camera_ok") is False:
                offline = [camera for camera, ok in status.get("cameras", {}).items() if not ok]
                failures["camera"] = "No recent frames: " + ",".join(offline)
            if status.get("external_ok") is not True:
                failures["mqtt"] = "MQTT connection/subscription or face synchronization unavailable"
            summary = {"running_version": info["version"], "service_healthy": status.get("healthy") is True}
        except Exception as error:
            failures["core"] = error_summary(error)
            status = {"healthy": False, "core_ok": False}
            summary = {"running_version": "unknown", "service_healthy": False}
        self.store.set("last_health", status)
        self.store.monitor(failures, self.identity(), summary, float(self.config.get("alert_grace_seconds", 300)))

    def setup_pending(self):
        path = self.root / "setup.json"
        return path.is_file() and read_json(path).get("phase") not in ("complete", "failed", "rollback_failed")

    def status(self):
        try:
            selected = self.current()[1]
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            selected = None
        path = self.root / "setup.json"
        setup = read_json(path) if path.is_file() else {}
        return {"selected_build": selected, "setup": {key: setup[key] for key in
                ("phase", "target_version", "error", "rollback", "rollback_error", "backup_dir") if key in setup},
                "pending": self.store.outstanding(), "latest_update": self.store.latest_summary(),
                "last_health": self.store.get("last_health"), "cleanup_error": self.store.get("cleanup_error"),
                "blocked": self.store.get("blocked", False)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=("prepare-baseline", "verify-baseline", "init", "once", "poll", "notify", "notify-once", "retry", "resume", "status"))
    parser.add_argument("--baseline")
    parser.add_argument("--version")
    parser.add_argument("--archive")
    parser.add_argument("--manifest")
    args = parser.parse_args()
    required = {"prepare-baseline": ("archive", "manifest"), "verify-baseline": ("baseline",),
                "init": ("baseline",), "retry": ("version",)}
    for field in required.get(args.action, ()):
        if not getattr(args, field):
            parser.error("--" + field + " is required for " + args.action)
    agent = Agent(args.config)
    if args.action == "prepare-baseline":
        with file_lock(agent.root / "update.lock", blocking=False):
            manifest = read_json(args.manifest)
            version_key(manifest["version"])
            if manifest.get("config_schema") != 1 or manifest.get("data_schema") != 1:
                raise ValueError("Unsupported baseline schema")
            if Path(args.archive).stat().st_size != manifest["size"] or digest(args.archive) != manifest["sha256"]:
                raise ValueError("Baseline package integrity check failed")
            if shutil.disk_usage(agent.root).free < manifest["size"] * 5 + 1024**3:
                raise ValueError("Insufficient baseline preparation space")
            destination = agent.root / "releases" / (manifest["version"] + "-baseline-" + str(uuid.uuid4()))
            destination.mkdir(parents=True)
            shutil.copyfile(args.archive, destination / "package.tar.gz")
            app, _ = agent.prepare(destination, manifest, compare_baseline=False)
            print("Prepared baseline (not started):", app)
    elif args.action == "verify-baseline":
        print(json.dumps(agent.wait_healthy(read_json(Path(args.baseline) / "BUILD_INFO.json")), indent=2))
    elif args.action == "init":
        with file_lock(agent.root / "update.lock", blocking=False):
            if (agent.root / "current").exists():
                raise RuntimeError("Baseline already enrolled")
            baseline = Path(args.baseline).resolve(strict=True)
            info = read_json(baseline / "BUILD_INFO.json")
            version_key(info["version"])
            if info.get("config_schema") != 1 or info.get("data_schema") != 1 or not info.get("feature_model_sha256"):
                raise RuntimeError("Baseline must use the supported config/data schema and feature-model identity")
            if not (baseline / "venv/bin/python").exists() or not update_ready(agent.check(info)):
                raise RuntimeError("Baseline must already be running and pass full health checks")
            agent.point(baseline)
            agent.store.set("device_id", agent.config["device_id"])
    elif args.action == "status":
        print(json.dumps(agent.status(), ensure_ascii=False, indent=2))
    elif args.action == "retry":
        with file_lock(agent.root / "update.lock", blocking=False):
            key = version_key(args.version)
            if (not args.version.startswith("v") or args.version not in agent.store.failed()
                    or key <= version_key(agent.current()[1]["version"])
                    or agent.config["role"] == "production" and not key[3]):
                raise ValueError("Retry requires a failed, newer, role-eligible published version")
            agent.store.set("retry", args.version)
    elif args.action == "resume":
        with file_lock(agent.root / "update.lock", blocking=False):
            if not update_ready(agent.check(agent.current()[1])):
                raise RuntimeError("Repair and validate service before resuming")
            agent.store.set("blocked", False)
    elif args.action in ("once", "notify-once"):
        (agent.once if args.action == "once" else agent.notify_once)()
    else:
        while True:
            try:
                agent = Agent(args.config)  # Apply atomic settings on the next iteration.
                (agent.once if args.action == "poll" else agent.notify_once)()
            except Exception as error:
                # No URLs, credentials, config or potentially sensitive stderr.
                print("OTA operation deferred:", type(error).__name__, flush=True)
            interval = float(agent.config.get("poll_seconds", 300)) if args.action == "poll" else 15
            if args.action == "poll" and not agent.config.get("github_token"):
                interval = max(interval, 1200)
            time.sleep(max(10, interval) + random.uniform(0, 10))


if __name__ == "__main__":
    main()
