"""Validate public ZIP boundaries, file integrity and repeatable builds."""
from pathlib import Path
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("adapter_zip_builder", ROOT / "scripts/build_zip.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class PackagingTests(unittest.TestCase):
    def source_fixture(self, root):
        for name in builder.PACKAGE_FILES:
            p=root/name; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text("synthetic source\r\n",encoding="utf-8",newline="")
        (root/"VERSION").write_text("0.6.0-dev.1\n",encoding="utf-8")
        (root/"LICENSE").write_text("Synthetic fixture license\n",encoding="utf-8")

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
                prefix="task-adapter-0.6.0-dev.1/"
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
            (root/"VERSION").write_text("0.6.0-dev.1",encoding="utf-8")
            (root/"src/auth_bridge.py").unlink()
            with self.assertRaises(ValueError):builder.build(root)
            self.assertFalse((root/"dist").exists())
