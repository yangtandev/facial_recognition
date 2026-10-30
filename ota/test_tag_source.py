"""Tag-only regression tests; no cloud writes, field services or real email."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.error
import uuid
import zipfile

from ota.core import atomic_json, digest, read_json, select_release
from ota.package_release import REQUIRED
from ota import tag_source as source
from ota.updater import Agent


class Response(io.BytesIO):
    def __init__(self, value, headers=None):
        super().__init__(json.dumps(value).encode())
        self.headers = headers or {}


def wheel(path, version="1.0", distribution="ota_fixture", value=1, headers=False):
    """A real installable wheel, with real RECORD hashes."""
    info = distribution + "-" + version + ".dist-info/"
    files = {"ota_fixture/__init__.py": ("VALUE = " + str(value) + "\n").encode(),
             info + "METADATA": ("Metadata-Version: 2.1\nName: " + source.normalized(distribution) + "\nVersion: " + version + "\n").encode(),
             info + "WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    if headers:
        prefix = distribution + "-" + version + ".data/headers/"
        files.update({prefix + "fixture.h": b"first header\n", prefix + "include/fixture.h": b"second header\n"})
    record = []
    for name, content in files.items():
        checksum = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        record.append(name + ",sha256=" + checksum + "," + str(len(content)))
    files[info + "RECORD"] = ("\n".join(record + [info + "RECORD,,"]) + "\n").encode()
    with zipfile.ZipFile(path, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        atomic_json(self.shared / "config.json", {"Server": {"API_url": "https://project.example/api", "location_ID": 1}})
        self.config = {"device_id": str(uuid.uuid4()), "role": "test", "repo": "owner/repo",
                       "smtp": {"host": "smtp.example", "tls": "starttls", "port": 587,
                                "from": "from@example.com", "to": ["to@example.com"]},
                       "state_dir": str(self.root / "state"), "shared_dir": str(self.shared)}
        atomic_json(self.root / "agent.json", self.config)
        self.agent = Agent(self.root / "agent.json")
        self.old = self.root / "old"
        self.old.mkdir()
        self.commit = "a" * 40
        atomic_json(self.old / "BUILD_INFO.json", {"version": "v1.0.0", "commit": "b" * 40,
                                                   "feature_model_sha256": hashlib.sha256(b"model").hexdigest()})
        self.agent.point(self.old)

    def fixture(self):
        app = self.root / "fixture"
        app.mkdir()
        files = list(REQUIRED) + [source.CONTRACT, "setting/build_config.py"]
        for name in files:
            target = app / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"# fixture\n")
        (app / source.FEATURE_MODEL).write_bytes(b"model")
        (app / "requirements.txt").write_text("ota-fixture==1.0\n")
        (app / "setting/build_config.py").write_text("default_config = {'new': True}\n")
        atomic_json(app / "setting/ota_migrations.json", [])
        vendor = app / "other/ota_fixture-1.0-py3-none-any.whl"
        wheel(vendor)
        files.append(str(vendor.relative_to(app)))
        wheels = self.root / "wheels"
        wheels.mkdir()
        (wheels / vendor.name).write_bytes(vendor.read_bytes())
        with patch("ota.tag_source.subprocess.check_output", return_value="\0".join(files).encode()):
            contract = source.contract(app, wheels, ["project"], "3.12", "x86_64")
        atomic_json(app / source.CONTRACT, contract)
        manifest = {**contract, "version": "v1.0.1-rc.1", "commit": self.commit,
                    "build_id": "v1.0.1-rc.1:" + self.commit, "sha256": digest(app / source.CONTRACT)}
        tag = {"tag_name": manifest["version"], "commit": self.commit}
        return app, files, manifest, tag

    def test_tags_without_release_and_pagination_etag_and_moved_identity(self):
        base = "https://api.github.com/repos/owner/repo/tags?per_page=100"
        next_url = base + "&page=2"
        pages = [Response([{"name": "v1.1.0-rc.2", "commit": {"sha": self.commit}}],
                          {"Link": '<' + next_url + '>; rel="next"', "ETag": "one"}),
                 Response([{"name": "v1.0.1", "commit": {"sha": "c" * 40}}])]
        with patch("ota.updater.request", side_effect=pages) as request:
            tags = self.agent.releases()
        self.assertEqual([base, next_url], [call.args[0] for call in request.call_args_list])
        self.assertEqual("v1.1.0-rc.2", select_release(tags, "test", "v1.0.0")["tag_name"])
        self.assertEqual("v1.0.1", select_release(tags, "production", "v1.0.0")["tag_name"])
        errors = [urllib.error.HTTPError(base, 304, "", {}, None), urllib.error.HTTPError(next_url, 304, "", {}, None)]
        with patch("ota.updater.request", side_effect=errors) as request:
            self.assertEqual(tags, self.agent.releases())
        self.assertEqual("one", request.call_args_list[0].kwargs["headers"]["If-None-Match"])
        with patch("ota.updater.request", return_value=Response([{"name": "v1.1.0-rc.2", "commit": {"sha": "d" * 40}}])):
            moved = self.agent.releases()[0]
        with patch("ota.updater.request") as request, self.assertRaisesRegex(ValueError, "moved"):
            self.agent.manifest(moved)
        request.assert_not_called()

    def test_contract_is_read_at_commit_and_historical_absence_cached(self):
        app, files, manifest, tag = self.fixture()
        with patch("ota.updater.request", return_value=io.BytesIO((app / source.CONTRACT).read_bytes())) as request:
            self.assertEqual(manifest, self.agent.manifest(tag))
        self.assertIn("/contents/ota/deploy.json?ref=" + self.commit, request.call_args.args[0])
        self.assertNotIn("/releases", request.call_args.args[0])
        with patch("ota.updater.request") as request:
            self.assertEqual(manifest, Agent(self.root / "agent.json").manifest(tag))
        request.assert_not_called()
        old_tag = {"tag_name": "v1.4.1-rc.1", "commit": "e" * 40}
        with patch("ota.updater.request", side_effect=urllib.error.HTTPError("https://api.github.com", 404, "", {}, None)):
            self.assertIsNone(self.agent.manifest(old_tag))
        with patch("ota.updater.request") as request:
            self.assertIsNone(self.agent.manifest(old_tag))
        request.assert_not_called()

    def test_metadata_missing_skips_to_applicable_tag_without_attempt(self):
        self.agent.identity = lambda: {"device_id": self.config["device_id"], "role": "test", "project": "project"}
        self.agent.check = lambda info: {"healthy": True}
        tags = [{"tag_name": "v1.2.0"}, {"tag_name": "v1.1.0"}]
        with patch.object(self.agent, "releases", return_value=tags), \
             patch.object(self.agent, "manifest", side_effect=[None, {"projects": ["project"]}]), \
             patch.object(self.agent, "update") as update:
            self.agent.once()
        self.assertEqual("v1.1.0", update.call_args.args[0]["tag_name"])
        self.assertEqual(set(), self.agent.store.failed())

    def test_contract_rejects_bad_schema_duplicate_wheels_and_unsafe_paths(self):
        _, _, manifest, _ = self.fixture()
        for change in ({"data_schema": 2}, {"projects": []}, {"wheels": manifest["wheels"] * 2},
                       {"models": {"../escape.pt": manifest["models"][source.FEATURE_MODEL]}},
                       {"feature_model_sha256": "0" * 64}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                source.validate({**manifest, **change})

    def test_source_rejects_links_submodules_and_missing_prerequisites(self):
        app, files, _, _ = self.fixture()
        for mode in ("120000", "160000"):
            entries = "\0".join(mode + " " + self.commit + " 0\t" + name for name in files) + "\0"
            with patch("ota.tag_source.git", return_value=entries), self.assertRaises(ValueError):
                source.source_files(app, self.config)
        with patch("ota.tag_source.git", return_value="100644 " + self.commit + " 0\tmain.py\0"), self.assertRaises(ValueError):
            source.source_files(app, self.config)

    def test_git_credentials_stay_in_environment_and_requirements_normalize_crlf(self):
        config = {**self.config, "github_token": "private-token-fixture"}
        with patch("ota.tag_source.subprocess.run", return_value=types.SimpleNamespace(stdout="ok")) as run:
            source.git(self.root, config, "fetch", "origin", self.commit)
        self.assertNotIn(config["github_token"], str(run.call_args.args))
        self.assertEqual("0", run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"])
        first, second = self.root / "crlf.txt", self.root / "lf.txt"
        first.write_bytes(b"package==1.0\r\n")
        second.write_bytes(b"package==1.0\n")
        self.assertEqual(source.requirements_digest(first), source.requirements_digest(second))

    def test_staging_source_identity_and_shared_data_without_service_changes(self):
        app, files, manifest, tag = self.fixture()
        with patch("ota.tag_source.checkout", return_value=app), \
             patch("ota.tag_source.source_files", return_value=files), \
             patch("ota.tag_source.environment", return_value="reused"), \
             patch.object(self.agent, "target_python", return_value=sys.executable):
            new, info = self.agent.stage(tag, manifest, str(uuid.uuid4()))
        self.assertEqual(self.old, self.agent.current()[0])
        self.assertEqual(tag["commit"], info["commit"])
        self.assertEqual(self.shared / "config.json", (new / "config.json").resolve())
        self.assertEqual("reused", read_json(new.parent / "preparation.json")["environment"])

    def test_staging_rejects_changed_contract_before_environment(self):
        app, _, manifest, tag = self.fixture()
        (app / source.CONTRACT).write_text("{}")
        with patch("ota.tag_source.checkout", return_value=app), \
             patch("ota.tag_source.environment") as environment, \
             patch.object(self.agent, "target_python", return_value=sys.executable), self.assertRaises(ValueError):
            self.agent.stage(tag, manifest, str(uuid.uuid4()))
        environment.assert_not_called()

    def test_models_reused_only_if_pointer_and_local_hash_match(self):
        app, files, manifest, _ = self.fixture()
        item = manifest["models"][source.FEATURE_MODEL]
        old = self.old / source.FEATURE_MODEL
        old.parent.mkdir(parents=True)
        old.write_bytes(b"model")
        pointer = ("version https://git-lfs.github.com/spec/v1\noid sha256:" + item["sha256"] + "\nsize 5\n").encode()
        (app / source.FEATURE_MODEL).write_bytes(pointer)
        with patch("ota.tag_source.git") as git:
            source.materialize_models(app, manifest, self.agent, files)
        git.assert_not_called()
        self.assertEqual(b"model", (app / source.FEATURE_MODEL).read_bytes())
        (app / source.FEATURE_MODEL).write_bytes(pointer.replace(item["sha256"].encode(), b"0" * 64))
        with self.assertRaises(ValueError):
            source.materialize_models(app, manifest, self.agent, files)

    def test_corrupt_model_download_and_incomplete_models_refused(self):
        app, files, manifest, _ = self.fixture()
        item = manifest["models"][source.FEATURE_MODEL]
        (app / source.FEATURE_MODEL).write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + item["sha256"] + "\nsize 5\n")
        with patch("ota.tag_source.git") as git, self.assertRaisesRegex(ValueError, "integrity"):
            source.materialize_models(app, manifest, self.agent, files)
        self.assertEqual(2, git.call_count)
        with self.assertRaises(ValueError):
            source.materialize_models(app, manifest, self.agent, files + ["models/missing.pt"])

    def test_wheel_cache_checks_bytes_and_reuses_legacy_wheelhouse(self):
        app, _, manifest, _ = self.fixture()
        item = {k: v for k, v in manifest["wheels"][0].items() if k != "path"}
        legacy = self.agent.root / "releases/legacy/wheelhouse" / item["filename"]
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes((app / "other" / item["filename"]).read_bytes())
        with patch("ota.tag_source.request") as request:
            cached = source.obtain_wheel(self.agent, app, item)
            self.assertEqual(item["sha256"], digest(cached))
            self.assertEqual(cached, source.obtain_wheel(self.agent, app, item))
        request.assert_not_called()
        legacy.unlink()
        cached.write_bytes(b"corrupt")
        with patch("ota.tag_source.wheel_url", return_value="https://files.pythonhosted.org/example"), \
             patch("ota.tag_source.request", return_value=io.BytesIO(b"truncated")), self.assertRaisesRegex(ValueError, "integrity"):
            source.obtain_wheel(self.agent, app, item)

    def test_real_environment_creation_reuse_and_changed_lock_keep_old_untouched(self):
        app, _, manifest, _ = self.fixture()
        manifest["python"] = str(sys.version_info.major) + "." + str(sys.version_info.minor)
        manifest["architecture"] = source.inventory(sys.executable)["architecture"]
        self.assertEqual("created", source.environment(self.agent, app, manifest, sys.executable))
        atomic_json(app / "BUILD_INFO.json", {"version": "v1.0.1", "commit": self.commit})
        self.agent.point(app)
        second = self.root / "second"
        second.mkdir()
        (second / "other").mkdir()
        item = manifest["wheels"][0]
        (second / item["path"]).write_bytes((app / item["path"]).read_bytes())
        (second / "requirements.txt").write_text("ota-fixture==1.0\n")
        with patch("ota.tag_source.obtain_wheel") as download:
            self.assertEqual("reused", source.environment(self.agent, second, manifest, sys.executable))
        download.assert_not_called()
        self.assertEqual((app / "venv").resolve(), (second / "venv").resolve())
        third = self.root / "third"
        (third / "other").mkdir(parents=True)
        (third / "requirements.txt").write_text("ota-fixture==2.0\n")
        changed = copy.deepcopy(manifest)
        path = third / "other/ota_fixture-2.0-py3-none-any.whl"
        wheel(path, "2.0")
        changed["wheels"] = [{"name": "ota-fixture", "version": "2.0", "filename": path.name,
                              "path": "other/" + path.name, "sha256": digest(path), "size": path.stat().st_size}]
        self.assertEqual("created", source.environment(self.agent, third, changed, sys.executable))
        self.assertEqual([("ota-fixture", "1.0")], [(source.normalized(n), v) for n, v in source.inventory(app / "venv/bin/python")["packages"] if source.normalized(n) == "ota-fixture"])
        (app / "venv/lib" / ("python" + manifest["python"]) / "site-packages/ota_fixture/__init__.py").write_text("tampered")
        with self.assertRaises(subprocess.CalledProcessError):
            source.verify_environment(app / "venv/bin/python", second, manifest, source.inventory(sys.executable))

    def test_unsupported_requirements_and_wrong_vendored_bytes_fail_before_download(self):
        app, _, manifest, _ = self.fixture()
        for line in ("ota-fixture @ https://example.com/source.tar.gz", "--index-url https://example.com", "ota-fixture>=1.0"):
            (app / "requirements.txt").write_text(line + "\n")
            with patch("ota.tag_source.obtain_wheel") as obtain, self.assertRaises(ValueError):
                source.environment(self.agent, app, manifest, sys.executable)
            obtain.assert_not_called()
        (app / "requirements.txt").write_text("ota-fixture==1.0\n")
        (app / manifest["wheels"][0]["path"]).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            source.environment(self.agent, app, manifest, sys.executable)

    def test_cleanup_preserves_environment_owner_until_no_longer_referenced(self):
        apps = []
        for index in range(4):
            tag, attempt = "v1.0." + str(index), str(uuid.uuid4())
            app = self.agent.root / "releases" / (tag + "-" + attempt) / "app"
            app.mkdir(parents=True)
            atomic_json(app / "BUILD_INFO.json", {"version": tag})
            self.agent.store.record(attempt, "success", {"target_version": tag, "old_path": str(apps[-1] if apps else self.old), "new_path": str(app)}, self.config)
            apps.append(app)
        (apps[0] / "venv").mkdir()
        (apps[-1] / "venv").symlink_to(apps[0] / "venv")
        self.agent.point(apps[-1])
        self.agent.cleanup()
        self.assertTrue(apps[0].is_dir())
        self.assertTrue((apps[-1] / "venv").is_dir())
        self.assertFalse(apps[1].exists())
        (apps[-1] / "venv").unlink()
        self.agent.cleanup()
        self.assertFalse(apps[0].exists())

    def test_overlapping_locked_distributions_accept_known_files_but_reject_tampering(self):
        app, _, manifest, _ = self.fixture()
        manifest["python"] = str(sys.version_info.major) + "." + str(sys.version_info.minor)
        target = source.inventory(sys.executable)
        manifest["architecture"] = target["architecture"]
        overlap = app / "other/ota_overlap-1.0-py3-none-any.whl"
        wheel(overlap, distribution="ota_overlap", value=2)
        manifest["wheels"].append({"name": "ota-overlap", "version": "1.0", "filename": overlap.name,
                                   "size": overlap.stat().st_size, "sha256": digest(overlap),
                                   "path": "other/" + overlap.name})
        self.assertEqual("created", source.environment(self.agent, app, manifest, sys.executable))
        source.verify_environment(app / "venv/bin/python", app, manifest, target)
        installed = app / "venv/lib" / ("python" + manifest["python"]) / "site-packages/ota_fixture/__init__.py"
        installed.write_text("UNKNOWN = True\n")
        with self.assertRaises(subprocess.CalledProcessError):
            source.verify_environment(app / "venv/bin/python", app, manifest, target)

    def test_changed_wheel_content_with_same_name_and_version_rebuilds_environment(self):
        app, _, manifest, _ = self.fixture()
        manifest["python"] = str(sys.version_info.major) + "." + str(sys.version_info.minor)
        target = source.inventory(sys.executable)
        manifest["architecture"] = target["architecture"]
        self.assertEqual("created", source.environment(self.agent, app, manifest, sys.executable))
        atomic_json(app / "BUILD_INFO.json", {"version": "v1.0.1"})
        self.agent.point(app)
        second = self.root / "changed-content"
        (second / "other").mkdir(parents=True)
        (second / "requirements.txt").write_bytes((app / "requirements.txt").read_bytes())
        changed = copy.deepcopy(manifest)
        item = changed["wheels"][0]
        path = second / item["path"]
        wheel(path, value=99)
        item.update(sha256=digest(path), size=path.stat().st_size)
        self.assertEqual("created", source.environment(self.agent, second, changed, sys.executable))
        self.assertFalse((second / "venv").is_symlink())
        check = "import ota_fixture;print(ota_fixture.VALUE)"
        self.assertEqual("1", subprocess.check_output([str(app / "venv/bin/python"), "-I", "-c", check], text=True).strip())
        self.assertEqual("99", subprocess.check_output([str(second / "venv/bin/python"), "-I", "-c", check], text=True).strip())

    def test_relocated_duplicate_header_names_use_locked_content_to_disambiguate(self):
        app, _, manifest, _ = self.fixture()
        manifest["python"] = str(sys.version_info.major) + "." + str(sys.version_info.minor)
        manifest["architecture"] = source.inventory(sys.executable)["architecture"]
        item = manifest["wheels"][0]
        path = app / item["path"]
        wheel(path, headers=True)
        item.update(sha256=digest(path), size=path.stat().st_size)
        self.assertEqual("created", source.environment(self.agent, app, manifest, sys.executable))


if __name__ == "__main__":
    unittest.main()
