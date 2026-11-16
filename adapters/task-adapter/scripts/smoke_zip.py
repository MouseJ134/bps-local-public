"""Verify a generated source ZIP and run its tests from a fresh directory."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    root=Path(__file__).resolve().parents[1]
    version=(root/"VERSION").read_text(encoding="utf-8").strip()
    parser.add_argument("archive",type=Path,nargs="?",default=root/"dist"/f"task-adapter-{version}.zip")
    args=parser.parse_args()
    with zipfile.ZipFile(args.archive) as package, tempfile.TemporaryDirectory() as temp:
        entries=package.infolist()
        if len(entries)>100 or sum(x.file_size for x in entries)>5*1024*1024:
            raise ValueError("Archive exceeds source package bounds")
        names=[x.filename for x in entries]
        if len(set(names))!=len(names):raise ValueError("Duplicate archive member")
        roots=set()
        for entry in entries:
            path=PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or len(path.parts)<2 or "\\" in entry.filename or entry.is_dir():
                raise ValueError("Unsafe archive path")
            if (entry.external_attr>>16)&0o170000 not in (0,0o100000):raise ValueError("Nonregular archive member")
            roots.add(path.parts[0])
        if len(roots)!=1:raise ValueError("Expected one package root")
        prefix=next(iter(roots))+"/"
        manifest=json.loads(package.read(prefix+"MANIFEST.json"))
        expected={prefix+n for n in manifest["files"]}|{prefix+"MANIFEST.json"}
        if set(names)!=expected:raise ValueError("Manifest does not cover every archive member")
        for name,digest in manifest["files"].items():
            if hashlib.sha256(package.read(prefix+name)).hexdigest()!=digest:raise ValueError("Manifest hash mismatch")
        package.extractall(temp)
        root=Path(temp)/next(iter(roots))
        subprocess.run([sys.executable,str(root/"run_tests.py"),"--report",str(Path(temp)/"test-results.json")],cwd=root,check=True,timeout=120)
        report=json.loads((Path(temp)/"test-results.json").read_text(encoding="utf-8"))
        print(json.dumps({"archive_tests_run":report["tests_run"],"success":report["success"],"manifest_verified":True}))

if __name__=="__main__":main()
