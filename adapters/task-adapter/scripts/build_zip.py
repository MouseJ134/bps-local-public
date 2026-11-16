"""Build a deterministic, text-only source ZIP from an explicit allowlist."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = (
    "README.md", "VERSION", "requirements.txt", "run_tests.py",
    "docs/INTERFACE_CONTRACT.md", "docs/SECURITY.md",
    "src/auth_bridge.py", "src/catalog_compat.py", "src/error_observer.py",
    "src/request_codec.py", "src/task_profiles.py", "src/tool_contracts.py",
    "src/tool_contract_policy.json", "src/tool_contract_policy.example.json",
    "tests/test_bridge_integration.py", "tests/test_error_observer.py",
    "tests/test_task_profiles.py", "tests/test_tool_contracts.py", "tests/test_packaging.py",
    "scripts/build_zip.py", "scripts/smoke_zip.py",
)


def normalized_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Only regular source files can be packaged")
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")

def build(root=ROOT, output=None):
    root = Path(root).resolve()
    output = Path(output) if output is not None else root / "dist"
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*", version):
        raise ValueError("Invalid adapter version")
    files = {name: normalized_bytes(root / name) for name in PACKAGE_FILES}
    license_file = root / "LICENSE"
    if not license_file.exists(): license_file = root.parents[1] / "LICENSE"
    files["LICENSE"] = normalized_bytes(license_file)
    manifest = {"version": version, "format": "source-only", "line_endings": "LF", "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    files["MANIFEST.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"task-adapter-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(f"task-adapter-{version}/{name}", (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(entry, data, compresslevel=9)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, digest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    archive, digest = build(output=args.output)
    print(json.dumps({"archive": archive.name, "sha256": digest, "format": "source-only"}))

if __name__ == "__main__": main()
