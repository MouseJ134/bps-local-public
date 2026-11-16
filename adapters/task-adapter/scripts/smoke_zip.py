"""Verify the complete source ZIP and run tests from a fresh directory."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile

from build_zip import PACKAGE_FILES, require_disabled_default_policy


def verify_package(package):
    """Check trusted allowlist and defaults, not just a self-declared manifest."""
    entries = package.infolist()
    if len(entries) > 100 or sum(item.file_size for item in entries) > 5 * 1024 * 1024:
        raise ValueError("Archive exceeds source package bounds")
    names = [item.filename for item in entries]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate archive member")
    roots = set()
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if (path.is_absolute() or ".." in path.parts or len(path.parts) < 2
                or "\\" in entry.filename or entry.is_dir() or str(path) != entry.filename):
            raise ValueError("Unsafe archive path")
        if (entry.external_attr >> 16) & 0o170000 not in (0, 0o100000):
            raise ValueError("Nonregular archive member")
        roots.add(path.parts[0])
    if len(roots) != 1:
        raise ValueError("Expected one package root")
    package_root = next(iter(roots))
    prefix = package_root + "/"
    expected_files = set(PACKAGE_FILES) | {"LICENSE"}
    if set(names) != {prefix + name for name in expected_files | {"MANIFEST.json"}}:
        raise ValueError("Archive does not match the required source allowlist")
    manifest = json.loads(package.read(prefix + "MANIFEST.json"))
    if (not isinstance(manifest, dict) or manifest.get("format") != "source-only"
            or manifest.get("line_endings") != "LF" or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != expected_files):
        raise ValueError("Invalid source package manifest")
    version = manifest.get("version")
    if (not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*", version)
            or package_root != "task-adapter-" + version
            or package.read(prefix + "VERSION").decode("utf-8").strip() != version):
        raise ValueError("Package version does not match its root and manifest")
    for name, digest in manifest["files"].items():
        raw = package.read(prefix + name)
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or hashlib.sha256(raw).hexdigest() != digest):
            raise ValueError("Manifest hash mismatch")
        raw.decode("utf-8")
        if b"\r\n" in raw:
            raise ValueError("Source package line endings must be LF")
    require_disabled_default_policy(package.read(prefix + "src/tool_contract_policy.json"))
    return package_root, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    parser.add_argument("archive", type=Path, nargs="?", default=root / "dist" / f"task-adapter-{version}.zip")
    args = parser.parse_args()
    with zipfile.ZipFile(args.archive) as package, tempfile.TemporaryDirectory() as temp:
        package_root, manifest = verify_package(package)
        package.extractall(temp)
        root = Path(temp) / package_root
        report_path = Path(temp) / "test-results.json"
        subprocess.run([sys.executable, str(root / "run_tests.py"), "--report", str(report_path)],
                       cwd=root, check=True, timeout=120)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        module = "src/schema_contract_compat.py"
        if (report.get("success") is not True or report.get("source_unchanged_during_run") is not True
                or report.get("source_sha256", {}).get(module) != manifest["files"][module]):
            raise ValueError("Extracted parameter guard source was not verified by the test run")
        print(json.dumps({"archive_tests_run": report["tests_run"], "success": report["success"],
                          "manifest_verified": True, "default_policy_disabled": True,
                          "parameter_guard_module_verified": True}))


if __name__ == "__main__":
    main()
