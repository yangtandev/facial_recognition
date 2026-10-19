"""Durable storage, release selection and safe filesystem/network primitives."""
import contextlib
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import sqlite3
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def version_key(tag):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?", tag)
    if not match:
        raise ValueError("Unsupported SemVer tag")
    major, minor, patch, rc = match.groups()
    return (int(major), int(minor), int(patch), rc is None, int(rc or 0))


def select_release(releases, role, current, failed=()):
    if role == "development":
        return None
    if role not in ("test", "production"):
        raise ValueError("Unknown host role")
    baseline = version_key(current)
    candidates = []
    for release in releases:
        try:
            key = version_key(release["tag_name"])
        except (KeyError, TypeError, ValueError):
            continue
        if not release["tag_name"].startswith("v"):
            continue
        if release.get("draft") or release["tag_name"] in failed:
            continue
        if release.get("prerelease") is not (not key[3]):
            continue
        if role == "production" and not key[3]:
            continue
        if key > baseline:
            candidates.append((key, release))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".ota-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def save_config(path, data):
    # Resolve the shared target; replacing a release's symlink would split configs.
    target = Path(path).resolve()
    with file_lock(str(target) + ".lock", blocking=False):
        # Preserve unknown/site-specific fields even when a stale web form omits them.
        atomic_json(target, overlay_config(read_json(target), data) if target.exists() else data)


def overlay_config(current, updates):
    """Explicit GUI/web edits may change types; omitted site fields are retained."""
    if not isinstance(current, dict) or not isinstance(updates, dict):
        raise ValueError("Configuration must be an object")
    result = copy.deepcopy(current)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = overlay_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_defaults(defaults, current):
    if not isinstance(defaults, dict) or not isinstance(current, dict):
        raise ValueError("Configuration must be an object")
    result = copy.deepcopy(current)
    for key, value in defaults.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif isinstance(value, dict):
            result[key] = merge_defaults(value, result[key])
        elif value is not None and type(result[key]) is not type(value) and not (
                type(value) in (int, float) and type(result[key]) in (int, float)):
            raise ValueError("Configuration type mismatch: " + key)
    return result


def migrate_config(defaults, current, rules):
    """Explicit, idempotent key renames; never silently overwrite site values."""
    if not isinstance(current, dict) or not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Invalid configuration migration list")
    result = copy.deepcopy(current)
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"op", "from", "to"} or rule["op"] != "rename":
            raise ValueError("Unsupported configuration migration")
        source, target = rule["from"], rule["to"]
        for path in (source, target):
            if (not isinstance(path, list) or not 0 < len(path) <= 16
                    or not all(isinstance(key, str) and 0 < len(key) <= 128 for key in path)):
                raise ValueError("Invalid migration path")
        if source == target or source[:len(target)] == target or target[:len(source)] == source:
            raise ValueError("Overlapping migration paths")
        parent = result
        for key in source[:-1]:
            if not isinstance(parent, dict):
                raise ValueError("Invalid source configuration object")
            parent = parent.get(key, {})
        if not isinstance(parent, dict):
            raise ValueError("Invalid source configuration object")
        if source[-1] not in parent:
            continue  # Already migrated, or the old field was never configured.
        destination = result
        for key in target[:-1]:
            destination = destination.setdefault(key, {})
            if not isinstance(destination, dict):
                raise ValueError("Invalid destination configuration object")
        value = parent[source[-1]]
        if target[-1] in destination and destination[target[-1]] != value:
            raise ValueError("Conflicting configuration rename")
        destination[target[-1]] = value
        del parent[source[-1]]
    return merge_defaults(defaults, result)


def digest(path):
    checksum = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def sync_tree(root):
    for directory, _, filenames in os.walk(root):
        for name in filenames:
            path = Path(directory) / name
            if not path.is_symlink():
                with open(path, "rb") as stream:
                    os.fsync(stream.fileno())
        sync_directory(directory)


def error_summary(error):
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP " + str(error.code)
    if isinstance(error, (ValueError, RuntimeError)):
        return (type(error).__name__ + ": " + " ".join(str(error).split()))[:240]
    return type(error).__name__


def safe_extract(archive, destination, max_bytes=20 * 1024**3):
    """No links/devices/traversal, including on Python 3.10 (no filter API)."""
    destination = Path(destination)
    seen, size = set(), 0
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            parts = PurePosixPath(member.name)
            if parts.is_absolute() or ".." in parts.parts or "\\" in member.name:
                raise ValueError("Unsafe archive path")
            if not parts.parts or parts.parts[0] not in ("app", "wheelhouse"):
                raise ValueError("Unexpected archive root")
            if member.name in seen or not (member.isdir() or member.isfile()):
                raise ValueError("Duplicate or special archive member")
            seen.add(member.name)
            size += member.size
            if size > max_bytes:
                raise ValueError("Archive exceeds unpacked size limit")
            target = destination.joinpath(*parts.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as source, open(target, "xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
    sync_directory(destination)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ValueError("Refusing non-HTTPS redirect")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlparse(request.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def request(url, token="", data=None, headers=None, timeout=30):
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("Remote requests require HTTPS")
    values = {"User-Agent": "facial-recognition-ota", **(headers or {})}
    if token:
        values["Authorization"] = "Bearer " + token
    body = None if data is None else json.dumps(data, allow_nan=False).encode()
    if body is not None:
        values["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=values)
    return urllib.request.build_opener(SafeRedirect()).open(req, timeout=timeout)


def backoff(attempt):
    return min(300, 10 * 2 ** min(attempt, 5)) + random.uniform(0, 5)


def timestamp(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, tag TEXT NOT NULL, phase TEXT NOT NULL,
                    data TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    body TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                    tries INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS mail_recipients (
                    event TEXT NOT NULL, recipient TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(event,recipient));
            """)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return _connection(db)

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def record(self, attempt, phase, data, identity):
        now = timestamp()
        if phase in ("rolling_back", "failed", "rollback_failed"):
            data.setdefault("failed_at", now)
        if phase == "rolling_back":
            data.setdefault("rollback_started_at", now)
        if phase == "rollback_failed":
            data.setdefault("rollback_failed_at", now)
        if phase == "failed" and data.get("rollback") == "success":
            data.setdefault("rollback_completed_at", now)
        public = {key: data[key] for key in ("from_version", "target_version", "sha256", "running_version",
                  "service_healthy", "rollback", "error", "rollback_error", "failed_at",
                  "rollback_started_at", "rollback_failed_at", "rollback_completed_at", "operation") if key in data}
        event = {"event_id": str(uuid.uuid4()), "device_id": identity["device_id"],
                 "project": identity.get("project", "unknown"),
                 "location": identity.get("location", "unknown"),
                 "role": identity["role"], "attempt_id": attempt,
                 "kind": "update", "phase": phase, "occurred_at": time.time(), **public}
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?)",
                       (attempt, data["target_version"], phase, json.dumps(data), 0))
            sequence = self._enqueue(db, event)
            db.execute("UPDATE attempts SET updated=? WHERE id=?", (sequence, attempt))

    @staticmethod
    def _enqueue(db, event):
        cursor = db.execute("INSERT INTO outbox(id,body) VALUES (?,?)", (event["event_id"], "{}"))
        event["seq"] = cursor.lastrowid
        db.execute("UPDATE outbox SET body=? WHERE id=?", (json.dumps(event, allow_nan=False), event["event_id"]))
        if not Store.is_notification(event):
            db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (event["event_id"],))
        return cursor.lastrowid

    @staticmethod
    def is_notification(event):
        return event.get("kind") == "alert" or (event.get("kind") == "update" and event.get("phase") in ("failed", "rollback_failed"))

    def monitor(self, failures, identity, summary, grace):
        """Persist incident state and its one notification in the same transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM state WHERE key='incidents'").fetchone()
            incidents = json.loads(row[0]) if row else {}
            now = time.time()
            for key in list(incidents):
                if key not in failures:
                    del incidents[key]
            for key, reason in failures.items():
                incident = incidents.setdefault(key, {"since": now, "notified": False})
                if not incident["notified"] and now - incident["since"] >= grace:
                    self._enqueue(db, {**identity, **summary, "event_id": str(uuid.uuid4()),
                        "kind": "alert", "alert": key, "error": reason,
                        "occurred_at": incident["since"], "failed_at": timestamp(incident["since"])})
                    incident["notified"] = True
            db.execute("INSERT OR REPLACE INTO state VALUES ('incidents',?)", (json.dumps(incidents),))

    def migrate_notifications(self):
        # Preserve legacy audit/failures, but stop treating routine phases as pending mail.
        with self.connect() as db:
            db.execute("""UPDATE outbox SET delivered=1 WHERE delivered=0
                AND json_extract(body,'$.kind')!='alert'
                AND NOT (json_extract(body,'$.kind')='update'
                    AND json_extract(body,'$.phase') IN ('failed','rollback_failed'))""")

    def recipients(self, event_id, addresses):
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO mail_recipients(event,recipient) VALUES (?,?)",
                           ((event_id, address) for address in addresses))
            return [row[0] for row in db.execute("SELECT recipient FROM mail_recipients WHERE event=? AND delivered=0", (event_id,))]

    def recipient_sent(self, event_id, recipient):
        with self.connect() as db:
            db.execute("UPDATE mail_recipients SET delivered=1 WHERE event=? AND recipient=?", (event_id, recipient))

    def pending(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM outbox WHERE delivered=0 AND due<=? ORDER BY seq LIMIT 100", (time.time(),))]

    def outstanding(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0]

    def acknowledge(self, event_id):
        with self.connect() as db:
            db.execute("UPDATE outbox SET delivered=1,error='' WHERE id=?", (event_id,))

    def defer(self, event_id, tries, error):
        with self.connect() as db:
            db.execute("UPDATE outbox SET tries=?,due=?,error=? WHERE id=?",
                       (tries, time.time() + backoff(tries), error, event_id))

    def unfinished(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM attempts WHERE phase NOT IN ('success','failed','rollback_failed') ORDER BY updated")]

    def failed(self):
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT tag FROM attempts WHERE phase IN ('failed','rollback_failed')")}

    def latest_summary(self):
        with self.connect() as db:
            row = db.execute("SELECT id,phase,data FROM attempts ORDER BY updated DESC LIMIT 1").fetchone()
            if not row:
                return {}
            data = json.loads(row["data"])
            return {"attempt_id": row["id"], "phase": row["phase"], **{key: data[key] for key in
                ("target_version", "from_version", "rollback", "error", "rollback_error", "failed_at",
                 "rollback_started_at", "rollback_failed_at", "rollback_completed_at") if key in data}}


@contextlib.contextmanager
def _connection(db):
    try:
        with db:
            yield db
    finally:
        db.close()
