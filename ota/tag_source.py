"""Pinned Git sources, content-verified models and fully locked environments."""
import argparse
import base64
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import subprocess
import urllib.parse
import zipfile

from ota.core import atomic_json, digest, read_json, request, sync_tree
from ota.package_release import INCLUDE, REQUIRED

CONTRACT = "ota/deploy.json"
FEATURE_MODEL = "models/data/20180402-114759-vggface2.pt"
MODEL_SUFFIXES = {".pt", ".onnx", ".bin", ".pkl"}
BOOTSTRAP = {"pip", "setuptools", "wheel"}


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements_digest(path):
    # Git's text normalization may differ from a developer's CRLF working tree.
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def relative(name):
    if not isinstance(name, str):
        raise ValueError("Unsafe deployment path")
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute()
            or ".." in path.parts or "\\" in name or str(path) != name):
        raise ValueError("Unsafe deployment path")
    return path


def validate(value):
    if not isinstance(value, dict) or type(value.get("format")) is not int or value["format"] != 1:
        raise ValueError("Unsupported tag deployment contract")
    if any(type(value.get(key)) is not int or value[key] != 1 for key in ("config_schema", "data_schema")):
        raise ValueError("Unsupported config/data migration; manual migration required")
    if (not re.fullmatch(r"3\.\d{1,2}", value.get("python", ""))
            or value.get("architecture") not in ("x86_64", "aarch64")):
        raise ValueError("Explicit Python/CPU target required")
    projects = value.get("projects")
    if not isinstance(projects, list) or not projects or not all(isinstance(p, str) and p for p in projects):
        raise ValueError("Explicit project applicability required")
    if not isinstance(value.get("defaults"), dict):
        raise ValueError("Configuration defaults required")
    if not re.fullmatch(r"[0-9a-f]{64}", value.get("requirements_sha256", "")):
        raise ValueError("Requirements identity required")
    models = value.get("models")
    if not isinstance(models, dict) or FEATURE_MODEL not in models:
        raise ValueError("Model identities required")
    wheels = value.get("wheels")
    if not isinstance(wheels, list) or not wheels or len(wheels) > 300:
        raise ValueError("Complete wheel lock required")
    names, filenames = set(), set()
    for path, item in models.items():
        parts = relative(path)
        if parts.parts[0] != "models" or parts.suffix not in MODEL_SUFFIXES:
            raise ValueError("Invalid model path")
        validate_content(item)
    if value.get("feature_model_sha256") != models[FEATURE_MODEL]["sha256"]:
        raise ValueError("Feature model identity mismatch")
    for item in wheels:
        validate_content(item)
        filename, name, version = item.get("filename", ""), item.get("name", ""), item.get("version", "")
        if (not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", filename)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or not re.fullmatch(r"[A-Za-z0-9_.+!]+", version)
                or name in names or filename in filenames):
            raise ValueError("Invalid or duplicate wheel identity")
        names.add(name)
        filenames.add(filename)
        if "path" in item:
            if str(relative(item["path"])) != "other/" + filename:
                raise ValueError("Invalid vendored wheel path")
        if "url" in item:
            parsed = urllib.parse.urlparse(item["url"])
            if (parsed.scheme != "https" or parsed.netloc != "download.pytorch.org"
                    or not parsed.path.startswith("/whl/cpu/") or parsed.query or parsed.fragment
                    or urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) != filename):
                raise ValueError("Invalid CPU wheel URL")
    return value


def validate_content(item):
    if (not isinstance(item, dict) or type(item.get("size")) is not int or item["size"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))):
        raise ValueError("Content size/SHA256 required")


def matches(path, item):
    return (path.is_file() and not path.is_symlink() and path.stat().st_size == item["size"]
            and digest(path) == item["sha256"])


def git_environment(config):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    # Credentials stay out of command lines, files and exception output.
    if config.get("github_token"):
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        credential = base64.b64encode(("x-access-token:" + config["github_token"]).encode()).decode()
        env.update({"GIT_CONFIG_COUNT": str(count + 1),
                    "GIT_CONFIG_KEY_" + str(count): "http.https://github.com/.extraHeader",
                    "GIT_CONFIG_VALUE_" + str(count): "Authorization: Basic " + credential})
    return env


def git(directory, config, *args, input=None):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "transfer.fsckObjects=true",
                           "-C", str(directory), *args], env=git_environment(config), input=input,
                          check=True, capture_output=True, text=True, timeout=1800).stdout.strip()


def checkout(target, config, commit):
    source = target / "source"
    source.mkdir()
    git(source, config, "init", "--quiet")
    git(source, config, "remote", "add", "origin", "https://github.com/" + config["repo"] + ".git")
    git(source, config, "config", "remote.origin.promisor", "true")
    git(source, config, "config", "remote.origin.partialclonefilter", "blob:none")
    patterns = "\n".join("/" + name for name in sorted(INCLUDE | {".gitattributes"})) + "\n"
    git(source, config, "sparse-checkout", "init", "--no-cone")
    git(source, config, "sparse-checkout", "set", "--no-cone", "--stdin", input=patterns)
    git(source, config, "fetch", "--quiet", "--depth=1", "--filter=blob:none", "--no-tags", "origin", commit)
    git(source, config, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    if git(source, config, "rev-parse", "HEAD") != commit:
        raise ValueError("Fetched source commit mismatch")
    return source


def source_files(source, config):
    entries = git(source, config, "ls-files", "--stage", "-z").split("\0")
    files = []
    for entry in filter(None, entries):
        identity, name = entry.split("\t", 1)
        path = relative(name)
        if path.parts[0] not in INCLUDE or name.endswith(".local.json") or path.name.startswith(".env"):
            continue
        if path.parts[0] == "py_ssh" and path.suffix != ".py":
            continue
        if identity.split()[0] not in ("100644", "100755") or (source / name).is_symlink():
            raise ValueError("Non-regular deployment source")
        files.append(name)
    if not set(REQUIRED).issubset(files) or CONTRACT not in files:
        raise ValueError("Missing tracked application prerequisite")
    return files


def materialize_models(source, manifest, agent, files):
    expected = {name for name in files if Path(name).suffix in MODEL_SUFFIXES}
    if expected != set(manifest["models"]):
        raise ValueError("Source models do not match deployment contract")
    try:
        baseline = agent.current()[0]
    except RuntimeError:
        baseline = agent.shared
    for name, item in manifest["models"].items():
        path = source / name
        if matches(path, item):
            continue
        # A pointer is part of the pinned Git content; its object must agree too.
        with open(path, "rb") as stream:
            pointer = stream.read(1024)
        match = re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n?", pointer)
        if not match or match[1].decode() != item["sha256"] or int(match[2]) != item["size"]:
            raise ValueError("Invalid/mismatched Git LFS model pointer")
        cached = baseline / name
        if matches(cached, item):
            shutil.copyfile(cached, path)
        else:
            git(source, agent.config, "-c", "lfs.url=https://github.com/" + agent.config["repo"] + ".git/info/lfs",
                "lfs", "fetch", "--include=" + name, "--exclude=", "origin", manifest["commit"])
            git(source, agent.config, "lfs", "checkout", name)
        if not matches(path, item):
            raise ValueError("Model integrity check failed")


def inventory(python):
    code = ("import importlib.metadata as m,json,platform,sys,sysconfig;"
            "print(json.dumps({'python':str(sys.version_info.major)+'.'+str(sys.version_info.minor),"
            "'architecture':platform.machine(),'abi':sysconfig.get_config_var('SOABI'),"
            "'packages':[(d.metadata['Name'],d.version) for d in m.distributions()]}))")
    return json.loads(subprocess.check_output([str(python), "-I", "-c", code], text=True, timeout=60))


def verify_installed_files(python, names):
    # RECORD catches missing/changed installed files before an existing venv is reused.
    code = '''import base64,hashlib,importlib.metadata as m,json,re,sys
names = set(json.loads(sys.argv[1]))
expected = {}
for distribution in m.distributions():
    name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
    if name not in names:
        continue
    if distribution.files is None:
        raise ValueError("Installed distribution has no RECORD")
    for file in distribution.files:
        if file.hash is None:
            if not str(file).endswith((".pyc", ".dist-info/RECORD")):
                raise ValueError("Unverifiable installed file")
            continue
        path = distribution.locate_file(file)
        expected.setdefault(path.resolve(), set()).add((file.hash.mode, file.hash.value))
for path, hashes in expected.items():
    checksums = {mode: hashlib.new(mode) for mode, value in hashes}
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            for checksum in checksums.values():
                checksum.update(chunk)
    actual = {(mode, base64.urlsafe_b64encode(checksum.digest()).rstrip(b"=").decode())
              for mode, checksum in checksums.items()}
    if not actual.intersection(hashes):
        raise ValueError("Installed file integrity mismatch")
'''
    subprocess.run([str(python), "-I", "-c", code, json.dumps(sorted(names))], check=True,
                   capture_output=True, timeout=300)


def verify_wheel_records(python, paths, wheels):
    """Bind installed RECORDs to the verified target wheels, including relocations."""
    if len(paths) != len(wheels):
        raise ValueError("Incomplete target wheel evidence")
    for path, item in zip(paths, wheels):
        if path is None or not matches(path, item):
            raise ValueError("Target wheel evidence unavailable")
    code = '''import csv,importlib.metadata as m,io,json,zipfile,sys
for name, path in json.loads(sys.argv[1]):
    distribution = m.distribution(name)
    installed = {str(file): file for file in distribution.files or ()}
    with zipfile.ZipFile(path) as bundle:
        records = [name for name in bundle.namelist() if name.endswith(".dist-info/RECORD")]
        if len(records) != 1:
            raise ValueError("Missing/ambiguous target wheel RECORD")
        for name, checksum, size in csv.reader(io.StringIO(bundle.read(records[0]).decode())):
            if not checksum:
                if name != records[0]:
                    raise ValueError("Unverifiable target wheel file")
                continue
            mode, value = checksum.split("=", 1)
            script = False
            if ".data/" in name:
                category, suffix = name.split(".data/", 1)[1].split("/", 1)
                if category in ("purelib", "platlib"):
                    candidates = [installed[suffix]] if suffix in installed else []
                else:
                    candidates = [file for key, file in installed.items()
                                  if key.startswith("../") and key.endswith("/" + suffix)]
                if len(candidates) > 1:
                    candidates = [file for file in candidates if file.hash is not None
                                  and (file.hash.mode, file.hash.value) == (mode, value)]
                script = category == "scripts"
            else:
                candidates = [installed[name]] if name in installed else []
            if len(candidates) != 1 or candidates[0].hash is None:
                raise ValueError("Target wheel file absent/ambiguous: " + name)
            file = candidates[0]
            if (file.hash.mode, file.hash.value) == (mode, value):
                continue
            raw = bundle.read(name) if script else b""
            if raw.startswith((b"#!python\\n", b"#!pythonw\\n")):
                actual = distribution.locate_file(file).read_bytes()
                if actual.startswith(b"#!") and actual.partition(b"\\n")[2] == raw.partition(b"\\n")[2]:
                    continue  # Wheel installer rewrites only the interpreter line.
            raise ValueError("Installed RECORD differs from locked target wheel")
'''
    pairs = [(item["name"], str(path)) for path, item in zip(paths, wheels)]
    subprocess.run([str(python), "-I", "-c", code, json.dumps(pairs)], check=True,
                   capture_output=True, timeout=120)


def validate_requirements(app, wheels):
    vendored = {item["path"] for item in wheels if "path" in item}
    for item in wheels:
        if "path" in item and not matches(app / item["path"], item):
            raise ValueError("Vendored wheel integrity check failed")
    for line in (app / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!]+", line)
                and line.removeprefix("./") not in vendored):
            raise ValueError("Contract supports exact pins and locked vendored wheels only")


def verify_environment(python, app, manifest, target, wheel_paths=None):
    validate_requirements(app, manifest["wheels"])
    value = inventory(python)
    if any(value.get(key) != target.get(key) for key in ("python", "architecture", "abi")):
        raise ValueError("Environment Python ABI/CPU mismatch")
    packages = [(normalized(name), version) for name, version in value["packages"]]
    if len(dict(packages)) != len(packages):
        raise ValueError("Duplicate installed distributions")
    expected = {item["name"]: item["version"] for item in manifest["wheels"]}
    actual = {name: version for name, version in packages if name not in BOOTSTRAP or name in expected}
    if actual != expected:
        raise ValueError("Installed environment differs from complete wheel lock")
    verify_installed_files(python, expected)
    if wheel_paths is None:
        wheel_paths = [app / item["path"] if "path" in item else app.parent / "wheelhouse" / item["filename"]
                       for item in manifest["wheels"]]
    verify_wheel_records(python, wheel_paths, manifest["wheels"])
    for args in (("check",), ("install", "--dry-run", "--no-index", "--no-deps", "-r", "requirements.txt")):
        subprocess.run([str(python), "-I", "-m", "pip", "--isolated", *args], cwd=app, check=True,
                       capture_output=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def wheel_url(item):
    if item.get("url"):
        return item["url"]
    url = "https://pypi.org/pypi/" + item["name"] + "/" + item["version"] + "/json"
    with request(url) as response:
        metadata = json.loads(response.read(4 * 1024**2))
    matches = [entry["url"] for entry in metadata.get("urls", [])
               if entry.get("filename") == item["filename"] and entry.get("size") == item["size"]
               and entry.get("digests", {}).get("sha256") == item["sha256"]]
    if len(matches) != 1 or urllib.parse.urlparse(matches[0]).netloc != "files.pythonhosted.org" or not matches[0].startswith("https://"):
        raise ValueError("Locked wheel unavailable from PyPI")
    return matches[0]


def obtain_wheel(agent, app, item):
    # ponytail: retain verified wheels; add pruning if cache growth causes pressure.
    cache = agent.root / "wheelhouse" / item["sha256"]
    cache.mkdir(parents=True, exist_ok=True)
    wheel = cache / item["filename"]
    candidate = cached_wheel(agent, app, item)
    if candidate is not None:
        if candidate != wheel:
            shutil.copyfile(candidate, wheel)
        return wheel
    if "path" in item:
        raise ValueError("Vendored wheel integrity check failed")
    temporary = wheel.with_suffix(".part")
    total = 0
    with request(wheel_url(item), timeout=60) as response, open(temporary, "wb") as output:
        while chunk := response.read(1024**2):
            total += len(chunk)
            if total > item["size"]:
                raise ValueError("Wheel larger than lock")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if not matches(temporary, item):
        raise ValueError("Wheel integrity check failed")
    os.replace(temporary, wheel)
    return wheel


def cached_wheel(agent, app, item):
    if "path" in item:
        path = app / item["path"]
        if not matches(path, item):
            raise ValueError("Vendored wheel integrity check failed")
        return path
    paths = [agent.root / "wheelhouse" / item["sha256"] / item["filename"],
             app.parent / "wheelhouse" / item["filename"]]
    try:
        paths.append(agent.current()[0].parent / "wheelhouse" / item["filename"])
    except RuntimeError:
        pass
    paths.extend((agent.root / "releases").glob("*/wheelhouse/" + item["filename"]))
    return next((path for path in paths if matches(path, item)), None)


def environment(agent, app, manifest, python_cmd):
    validate_requirements(app, manifest["wheels"])
    target = inventory(python_cmd)
    if target["python"] != manifest["python"] or target["architecture"] != manifest["architecture"]:
        raise ValueError("Target interpreter mismatch")
    try:
        baseline = agent.current()[0]
        existing = (baseline / "venv").resolve(strict=True)
        wheels = [cached_wheel(agent, app, item) for item in manifest["wheels"]]
        verify_environment(existing / "bin/python", app, manifest, target, wheels)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        if shutil.disk_usage(app).free < sum(item["size"] for item in manifest["wheels"]) * 4 + int(agent.config.get("min_free_bytes", 1024**3)):
            raise RuntimeError("Insufficient isolated environment space")
        wheels = [obtain_wheel(agent, app, item) for item in manifest["wheels"]]
        subprocess.run([python_cmd, "-m", "venv", str(app / "venv")], check=True,
                       capture_output=True, timeout=120)
        python = app / "venv/bin/python"
        subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "install", "--no-index",
                        "--no-deps", "--only-binary=:all:", *map(str, wheels)], cwd=app, check=True,
                       capture_output=True, timeout=1800)
        verify_environment(python, app, manifest, target, wheels)
        return "created"
    (app / "venv").symlink_to(existing, target_is_directory=True)
    return "reused"


def stage(agent, release, manifest, attempt, compare_baseline=True):
    from ota.updater import SHARED
    validate(manifest)
    python_cmd = agent.target_python(manifest)
    if (manifest["version"] != release["tag_name"] or manifest["commit"] != release["commit"]
            or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])):
        raise ValueError("Tag/source identity mismatch")
    if compare_baseline and agent.current()[1].get("feature_model_sha256") != manifest["feature_model_sha256"]:
        raise ValueError("Feature model change requires explicit data migration")
    target = agent.root / "releases" / (release["tag_name"] + "-" + attempt)
    target.mkdir(parents=True, mode=0o700)
    required_space = sum(item["size"] for item in manifest["models"].values()) * 2
    if shutil.disk_usage(target).free < required_space + int(agent.config.get("min_free_bytes", 1024**3)):
        raise RuntimeError("Insufficient staging/environment space")
    source = checkout(target, agent.config, manifest["commit"])
    if digest(source / CONTRACT) != manifest["sha256"]:
        raise ValueError("Pinned deployment contract mismatch")
    files = source_files(source, agent.config)
    materialize_models(source, manifest, agent, files)
    app = target / "app"
    app.mkdir()
    for name in files:
        destination = app / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    if requirements_digest(app / "requirements.txt") != manifest["requirements_sha256"]:
        raise ValueError("Requirements do not match deployment contract")
    info = {key: manifest[key] for key in ("version", "commit", "build_id", "config_schema", "data_schema",
                                         "python", "architecture", "feature_model_sha256")}
    atomic_json(app / "BUILD_INFO.json", info)
    atomic_json(app / "config.defaults.json", manifest["defaults"])
    migrations = read_json(app / "setting/ota_migrations.json")
    atomic_json(app / "config.migrations.json", migrations)
    mode = environment(agent, app, manifest, python_cmd)
    atomic_json(target / "preparation.json", {"source_commit": manifest["commit"], "environment": mode})
    for name in SHARED:
        shared = agent.shared / name
        shared.mkdir(parents=True, exist_ok=True)
        (app / name).symlink_to(shared, target_is_directory=True)
    (app / "config.json").symlink_to(agent.shared / "config.json")
    shutil.rmtree(source)
    sync_tree(target)
    return app, info


def contract(root, wheelhouse, projects, python, architecture):
    """Offline authoring: use the complete, already tested wheelhouse."""
    root, wheelhouse = Path(root), Path(wheelhouse)
    files = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"]).decode().split("\0")
    models = {}
    for name in files:
        if name and Path(name).parts[0] == "models" and Path(name).suffix in MODEL_SUFFIXES:
            path = root / name
            with open(path, "rb") as stream:
                pointer = stream.read(80).startswith(b"version https://git-lfs.github.com/spec/")
            if path.is_symlink() or pointer:
                raise ValueError("Resolve regular LFS models before authoring contract")
            models[name] = {"size": path.stat().st_size, "sha256": digest(path)}
    wheels = []
    for path in sorted(wheelhouse.glob("*.whl")):
        if path.is_symlink():
            raise ValueError("Non-regular wheel")
        with zipfile.ZipFile(path) as bundle:
            entries = [name for name in bundle.namelist() if name.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise ValueError("Ambiguous wheel metadata")
            metadata = BytesParser().parsebytes(bundle.read(entries[0]))
        item = {"filename": path.name, "name": normalized(metadata["Name"]), "version": metadata["Version"],
                "size": path.stat().st_size, "sha256": digest(path)}
        vendored = root / "other" / path.name
        if matches(vendored, item):
            item["path"] = "other/" + path.name
        elif item["name"] in ("torch", "torchvision", "torchaudio") and "+cpu" in item["version"]:
            item["url"] = "https://download.pytorch.org/whl/cpu/" + urllib.parse.quote(path.name)
        wheels.append(item)
    defaults = runpy.run_path(str(root / "setting/build_config.py"))["default_config"]
    validate_requirements(root, wheels)
    return validate({"format": 1, "projects": projects, "python": python, "architecture": architecture,
                     "config_schema": 1, "data_schema": 1, "defaults": defaults, "models": models,
                     "feature_model_sha256": models[FEATURE_MODEL]["sha256"], "wheels": wheels,
                     "requirements_sha256": requirements_digest(root / "requirements.txt")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--wheelhouse", required=True)
    parser.add_argument("--projects", nargs="+", required=True)
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--architecture", default="x86_64", choices=("x86_64", "aarch64"))
    args = parser.parse_args()
    value = contract(args.root, args.wheelhouse, args.projects, args.python, args.architecture)
    atomic_json(Path(args.root) / CONTRACT, value)
    print("Prepared tag contract:", len(value["wheels"]), "locked wheels; commit with the tested source")


if __name__ == "__main__":
    main()
