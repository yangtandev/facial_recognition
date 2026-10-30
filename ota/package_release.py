"""Build immutable application + resolved models + offline wheelhouse assets."""
import argparse
import io
import json
import platform
from pathlib import Path
import runpy
import subprocess
import tarfile
import sys

from ota.core import atomic_json, digest, migrate_config, read_json, version_key

INCLUDE = {"main.py", "analytics_reporter.py", "install.sh", "requirements.txt", "README.md",
           "RELEASE_NOTES.md", "init", "models", "other", "ui", "web_ui", "setting", "py_ssh", "ota"}
REQUIRED = ("main.py", "requirements.txt", "ota/__init__.py", "ota/core.py", "ota/health.py", "py_ssh/ssh.py",
            "ota/updater.py", "ota/tag_source.py", "ota/notifications.py", "ota/configure.py", "ota/install_agent.py", "ota/setup.py", "ota/diagnostics.py", "ota/agent.example.json",
            "models/data/20180402-114759-vggface2.pt", "other/pvms_Library-0.0.2b0-py3-none-any.whl",
            "setting/ota_migrations.json")


def package(root, wheelhouse, output, version, projects, target_python, architecture):
    version_key(version)
    if not version.startswith("v"):
        raise ValueError("Release tags must use the v prefix")
    root, wheelhouse, output = Path(root).resolve(), Path(wheelhouse).resolve(), Path(output).resolve()
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tag = subprocess.run(["git", "rev-parse", "--verify", "refs/tags/" + version + "^{commit}"],
                         cwd=root, capture_output=True, text=True)
    if tag.returncode == 0 and tag.stdout.strip() != commit:
        raise ValueError("Existing release tag does not point to this source commit")
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    files = [path for path in files if path and Path(path).parts[0] in INCLUDE
             and not Path(path).name.startswith(".env") and not path.endswith(".local.json")
             and (Path(path).parts[0] != "py_ssh" or path.endswith(".py"))]
    for name in REQUIRED:
        if name not in files or not (root / name).is_file():
            raise ValueError("Missing tracked release prerequisite: " + name)
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels or any(path.is_symlink() for path in wheels):
        raise ValueError("A populated, regular-file wheelhouse is required")
    if len({path.name.split("-")[0].lower() for path in wheels}) != len(wheels):
        raise ValueError("Wheelhouse must contain one tested version per dependency")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ("facial-recognition-" + version + ".tar.gz")
    if archive.exists() or (output / "release.json").exists():
        raise FileExistsError("Do not overwrite release assets; use a new output directory")
    build = {"version": version, "commit": commit, "build_id": version + ":" + commit,
             "config_schema": 1, "data_schema": 1, "python": target_python, "architecture": architecture,
             "feature_model_sha256": digest(root / "models/data/20180402-114759-vggface2.pt")}
    defaults = runpy.run_path(str(root / "setting/build_config.py"))["default_config"]
    migrations = read_json(root / "setting/ota_migrations.json")
    migrate_config({}, {}, migrations)  # Validate the declarative policy before publishing.
    with tarfile.open(archive, "x:gz") as bundle:
        for name in files:
            source = root / name
            if not source.is_file() or source.is_symlink():
                raise ValueError("Source contains a missing/non-regular file: " + name)
            with open(source, "rb") as stream:
                if stream.read(80).startswith(b"version https://git-lfs.github.com/spec/"):
                    raise ValueError("Run git lfs pull before packaging: " + name)
            bundle.add(source, arcname="app/" + name, recursive=False)
        for name, value in (("BUILD_INFO.json", build), ("config.defaults.json", defaults), ("config.migrations.json", migrations)):
            content = json.dumps(value, ensure_ascii=False).encode()
            entry = tarfile.TarInfo("app/" + name)
            entry.size, entry.mode = len(content), 0o600
            bundle.addfile(entry, io.BytesIO(content))
        for wheel in wheels:
            bundle.add(wheel, arcname="wheelhouse/" + wheel.name, recursive=False)
    manifest = {**build, "archive": archive.name, "sha256": digest(archive),
                "size": archive.stat().st_size, "projects": projects}
    atomic_json(output / "release.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--wheelhouse", required=True)
    parser.add_argument("--output", help="Default: ota/dist/<version> under root")
    parser.add_argument("--version", required=True)
    parser.add_argument("--projects", nargs="+", required=True)
    parser.add_argument("--target-python", default=f"{sys.version_info.major}.{sys.version_info.minor}",
                        help="Default: current Python minor; wheelhouse must match")
    parser.add_argument("--architecture", default=platform.machine(), choices=("x86_64", "aarch64"))
    args = parser.parse_args()
    output = args.output or str(Path(args.root) / "ota/dist" / args.version)
    result = package(args.root, args.wheelhouse, output, args.version,
                     args.projects, args.target_python, args.architecture)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
