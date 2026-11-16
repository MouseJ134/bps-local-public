"""Validate public ZIP boundaries, file integrity and repeatable builds."""
from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import sys
import warnings
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("adapter_zip_builder", ROOT / "scripts/build_zip.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
sys.modules["build_zip"] = builder
smoke_spec = importlib.util.spec_from_file_location("adapter_zip_smoke", ROOT / "scripts/smoke_zip.py")
smoke = importlib.util.module_from_spec(smoke_spec)
smoke_spec.loader.exec_module(smoke)

class PackagingTests(unittest.TestCase):
    def source_fixture(self, root):
        for name in builder.PACKAGE_FILES:
            p=root/name; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text("synthetic source\r\n",encoding="utf-8",newline="")
        (root/"VERSION").write_text("0.6.0-dev.2\n",encoding="utf-8")
        (root/"LICENSE").write_text("Synthetic fixture license\n",encoding="utf-8")
        (root/"src/tool_contract_policy.json").write_text(
            json.dumps({"schema_version": 3, "enabled": False}), encoding="utf-8")

    def test_allowlist_excludes_credentials_logs_and_unknown_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.source_fixture(root)
            for name in ("src/private_patch.py","src/auth.json","diagnostics/raw.log","backup/history.json","test-results.json"):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("SYNTHETIC-EXCLUDED",encoding="utf-8")
            archive,_=builder.build(root,root/"dist")
            with zipfile.ZipFile(archive) as package:
                names={name.split("/",1)[1] for name in package.namelist()}
                self.assertEqual(names,set(builder.PACKAGE_FILES)|{"LICENSE","MANIFEST.json"})
                for name in package.namelist():self.assertNotIn(b"SYNTHETIC-EXCLUDED",package.read(name))

    def test_manifest_hashes_and_repeat_builds_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.source_fixture(root)
            a,first=builder.build(root,root/"one")
            b,second=builder.build(root,root/"two")
            self.assertEqual(first,second);self.assertEqual(a.read_bytes(),b.read_bytes())
            with zipfile.ZipFile(a) as package:
                prefix="task-adapter-0.6.0-dev.2/"
                manifest=json.loads(package.read(prefix+"MANIFEST.json"))
                for name,digest in manifest["files"].items():
                    raw=package.read(prefix+name)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),digest)
                    self.assertNotIn(b"\r\n",raw)
                self.assertEqual(package.namelist(),sorted(package.namelist()))
            self.assertEqual(a.with_suffix(".zip.sha256").read_text().split()[0],first)

    def test_missing_source_and_invalid_version_do_not_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.source_fixture(root)
            (root/"VERSION").write_text("../../unsafe",encoding="utf-8")
            with self.assertRaises(ValueError):builder.build(root)
            (root/"VERSION").write_text("0.6.0-dev.2",encoding="utf-8")
            (root/"src/auth_bridge.py").unlink()
            with self.assertRaises(ValueError):builder.build(root)
            self.assertFalse((root/"dist").exists())

    def rewrite_archive(self, archive, change):
        with zipfile.ZipFile(archive) as package:
            contents = {name: package.read(name) for name in package.namelist()}
        change(contents)
        with zipfile.ZipFile(archive, "w") as package:
            for name, raw in contents.items():
                package.writestr(name, raw)

    def test_parameter_guard_and_changelog_are_required(self):
        required = {"CHANGELOG.md", "src/schema_contract_compat.py",
                    "tests/test_schema_contract_compat.py", "tests/test_schema_guard_bridge.py"}
        self.assertTrue(required.issubset(builder.PACKAGE_FILES))
        for missing in required:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); self.source_fixture(root); (root / missing).unlink()
                with self.assertRaises(ValueError):
                    builder.build(root)
                self.assertFalse((root / "dist").exists())

    def test_enabled_or_invalid_default_policy_is_not_packaged(self):
        for raw in ["{}", "[]", "invalid", '{"enabled":true}', '{"enabled":"false"}', '{"enabled":0}']:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); self.source_fixture(root)
                (root / "src/tool_contract_policy.json").write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    builder.build(root)
                self.assertFalse((root / "dist").exists())

    def test_smoke_verifies_complete_allowlist_and_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.source_fixture(root)
            archive, _ = builder.build(root)
            with zipfile.ZipFile(archive) as package:
                package_root, manifest = smoke.verify_package(package)
                self.assertEqual(package_root, "task-adapter-0.6.0-dev.2")
                self.assertIn("src/schema_contract_compat.py", manifest["files"])

    def test_smoke_rejects_omitted_guard_even_with_updated_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.source_fixture(root); archive, _ = builder.build(root)
            def remove(contents):
                prefix = "task-adapter-0.6.0-dev.2/"
                del contents[prefix + "src/schema_contract_compat.py"]
                manifest = json.loads(contents[prefix + "MANIFEST.json"])
                del manifest["files"]["src/schema_contract_compat.py"]
                contents[prefix + "MANIFEST.json"] = json.dumps(manifest).encode()
            self.rewrite_archive(archive, remove)
            with zipfile.ZipFile(archive) as package, self.assertRaises(ValueError):
                smoke.verify_package(package)

    def test_smoke_rejects_extra_member_even_with_updated_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.source_fixture(root); archive, _ = builder.build(root)
            def add(contents):
                prefix = "task-adapter-0.6.0-dev.2/"; raw = b"synthetic extra source"
                contents[prefix + "src/unknown_module.py"] = raw
                manifest = json.loads(contents[prefix + "MANIFEST.json"])
                manifest["files"]["src/unknown_module.py"] = hashlib.sha256(raw).hexdigest()
                contents[prefix + "MANIFEST.json"] = json.dumps(manifest).encode()
            self.rewrite_archive(archive, add)
            with zipfile.ZipFile(archive) as package, self.assertRaises(ValueError):
                smoke.verify_package(package)

    def test_smoke_rejects_enabled_policy_with_valid_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.source_fixture(root); archive, _ = builder.build(root)
            def enable(contents):
                prefix = "task-adapter-0.6.0-dev.2/"; name = "src/tool_contract_policy.json"
                raw = b'{"enabled":true}'
                contents[prefix + name] = raw
                manifest = json.loads(contents[prefix + "MANIFEST.json"])
                manifest["files"][name] = hashlib.sha256(raw).hexdigest()
                contents[prefix + "MANIFEST.json"] = json.dumps(manifest).encode()
            self.rewrite_archive(archive, enable)
            with zipfile.ZipFile(archive) as package, self.assertRaises(ValueError):
                smoke.verify_package(package)

    def test_smoke_rejects_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.source_fixture(root); archive, _ = builder.build(root)
            self.rewrite_archive(archive, lambda contents: contents.__setitem__(
                "task-adapter-0.6.0-dev.2/src/schema_contract_compat.py", b"changed"))
            with zipfile.ZipFile(archive) as package, self.assertRaises(ValueError):
                smoke.verify_package(package)

    def test_smoke_rejects_duplicate_and_unsafe_members(self):
        for member in ["task-adapter-0.6.0-dev.2/README.md", "../unsafe.py", "task-adapter-0.6.0-dev.2/../unsafe.py"]:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); self.source_fixture(root); archive, _ = builder.build(root)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(archive, "a") as package:
                        package.writestr(member, b"synthetic")
                with zipfile.ZipFile(archive) as package, self.assertRaises(ValueError):
                    smoke.verify_package(package)

    def test_smoke_rejects_manifest_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.source_fixture(root); archive, _ = builder.build(root)
            def change(contents):
                name = "task-adapter-0.6.0-dev.2/MANIFEST.json"
                manifest = json.loads(contents[name]); manifest["version"] = "9.9.9"
                contents[name] = json.dumps(manifest).encode()
            self.rewrite_archive(archive, change)
            with zipfile.ZipFile(archive) as package, self.assertRaises(ValueError):
                smoke.verify_package(package)
